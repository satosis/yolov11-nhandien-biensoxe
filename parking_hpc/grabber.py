"""
parking_hpc/grabber.py
Process 1 — Frame Grabber

Responsibilities:
  - Open RTSP stream(s) with hardware-accelerated decode (FFmpeg/V4L2 backend)
  - Detect motion inside the ROI polygon
  - Write latest frame into SharedMemory (zero-copy IPC)
  - Push (cam_id, shm_offset, timestamp) tokens into infer_queue when motion fires
  - Reconnect automatically on stream loss

Runs as a standalone multiprocessing.Process — no imports from inference.py or ui_server.py.
"""
import time
import logging
import signal
import numpy as np
import cv2
from multiprocessing import Process, Queue, Event, shared_memory
from typing import Optional

from parking_hpc import config as cfg

logger = logging.getLogger("grabber")


# ── ROI helpers ───────────────────────────────────────────────────────────────

def _build_roi_mask(h: int, w: int) -> np.ndarray:
    """Return a uint8 mask (255 inside ROI, 0 outside) for the given frame size."""
    pts = np.array(
        [(int(x * w), int(y * h)) for x, y in cfg.ROI_POLYGON_NORM],
        dtype=np.int32,
    )
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.fillPoly(mask, [pts], 255)
    return mask


def _motion_in_roi(prev_gray: np.ndarray, curr_gray: np.ndarray, mask: np.ndarray) -> bool:
    """Return True if significant motion is detected inside the ROI."""
    diff = cv2.absdiff(prev_gray, curr_gray)
    blurred = cv2.GaussianBlur(diff, (cfg.MOTION_BLUR_KSIZE, cfg.MOTION_BLUR_KSIZE), 0)
    _, thresh = cv2.threshold(blurred, 25, 255, cv2.THRESH_BINARY)
    roi_thresh = cv2.bitwise_and(thresh, thresh, mask=mask)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    dilated = cv2.dilate(roi_thresh, kernel, iterations=cfg.MOTION_DILATE_ITER)
    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    return any(cv2.contourArea(c) > cfg.MOTION_THRESHOLD for c in contours)


# ── Camera reader ─────────────────────────────────────────────────────────────

class CameraReader:
    """
    Reads one RTSP stream, writes frames to SharedMemory, signals motion.

    SharedMemory layout (SHM_FRAME_BYTES bytes):
        [0..3]   : uint32 frame counter (little-endian) — reader checks for new frame
        [4..]    : BGR uint8 flat array (GRAB_HEIGHT × GRAB_WIDTH × 3)
    """

    HEADER_BYTES = 4  # frame counter prefix

    def __init__(
        self,
        cam_id: str,
        rtsp_url: str,
        shm_name: str,
        infer_queue: Queue,
        stop_event: Event,
    ):
        self.cam_id = cam_id
        self.rtsp_url = rtsp_url
        self.shm_name = shm_name
        self.infer_queue = infer_queue
        self.stop_event = stop_event

        total = self.HEADER_BYTES + cfg.SHM_FRAME_BYTES
        try:
            self._shm = shared_memory.SharedMemory(name=shm_name, create=True, size=total)
        except FileExistsError:
            # Previous run didn't clean up — reuse
            self._shm = shared_memory.SharedMemory(name=shm_name, create=False, size=total)
        self._buf = np.ndarray(
            (cfg.GRAB_HEIGHT, cfg.GRAB_WIDTH, 3),
            dtype=np.uint8,
            buffer=self._shm.buf[self.HEADER_BYTES:],
        )
        self._counter = np.ndarray((1,), dtype=np.uint32, buffer=self._shm.buf[:self.HEADER_BYTES])
        self._counter[0] = 0

    def run(self):
        logger.info("[%s] Grabber started → %s", self.cam_id, self.rtsp_url)
        frame_interval = 1.0 / cfg.GRAB_FPS_CAP
        roi_mask: Optional[np.ndarray] = None
        prev_gray: Optional[np.ndarray] = None
        frame_idx = 0

        while not self.stop_event.is_set():
            cap = self._open_capture()
            if cap is None:
                time.sleep(cfg.RTSP_RECONNECT_DELAY)
                continue

            logger.info("[%s] Stream opened", self.cam_id)
            t_last = time.monotonic()

            while not self.stop_event.is_set():
                ret, frame = cap.read()
                if not ret or frame is None:
                    logger.warning("[%s] Frame read failed — reconnecting", self.cam_id)
                    break

                # Throttle to GRAB_FPS_CAP
                now = time.monotonic()
                elapsed = now - t_last
                if elapsed < frame_interval:
                    time.sleep(frame_interval - elapsed)
                t_last = time.monotonic()

                # Resize to target resolution
                if frame.shape[1] != cfg.GRAB_WIDTH or frame.shape[0] != cfg.GRAB_HEIGHT:
                    frame = cv2.resize(
                        frame, (cfg.GRAB_WIDTH, cfg.GRAB_HEIGHT), interpolation=cv2.INTER_LINEAR
                    )

                # Build ROI mask once (depends on frame size)
                if roi_mask is None:
                    roi_mask = _build_roi_mask(cfg.GRAB_HEIGHT, cfg.GRAB_WIDTH)

                # Write frame to shared memory (zero-copy for inference process)
                np.copyto(self._buf, frame)
                self._counter[0] = (int(self._counter[0]) + 1) & 0xFFFFFFFF

                # Motion detection
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                if prev_gray is not None and frame_idx % cfg.PLATE_DETECT_EVERY_N == 0:
                    if _motion_in_roi(prev_gray, gray, roi_mask):
                        token = {
                            "cam_id": self.cam_id,
                            "shm_name": self.shm_name,
                            "frame_idx": frame_idx,
                            "ts": time.time(),
                        }
                        if not self.infer_queue.full():
                            self.infer_queue.put_nowait(token)
                prev_gray = gray
                frame_idx += 1

            cap.release()
            if not self.stop_event.is_set():
                logger.info("[%s] Reconnecting in %ds…", self.cam_id, cfg.RTSP_RECONNECT_DELAY)
                time.sleep(cfg.RTSP_RECONNECT_DELAY)

        self._shm.close()
        self._shm.unlink()
        logger.info("[%s] Grabber stopped", self.cam_id)

    def _open_capture(self) -> Optional[cv2.VideoCapture]:
        """Open RTSP with FFmpeg backend + hardware-friendly flags."""
        # Prefer FFmpeg backend; fall back to default
        cap = cv2.VideoCapture(self.rtsp_url, cv2.CAP_FFMPEG)
        if not cap.isOpened():
            cap = cv2.VideoCapture(self.rtsp_url)
        if not cap.isOpened():
            logger.error("[%s] Cannot open stream: %s", self.cam_id, self.rtsp_url)
            return None

        cap.set(cv2.CAP_PROP_BUFFERSIZE, cfg.RTSP_BUFFER_SIZE)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, cfg.GRAB_WIDTH)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg.GRAB_HEIGHT)
        cap.set(cv2.CAP_PROP_FPS, cfg.GRAB_FPS_CAP)
        return cap


# ── Process entry point ───────────────────────────────────────────────────────

def grabber_process(
    cam_id: str,
    rtsp_url: str,
    shm_name: str,
    infer_queue: Queue,
    stop_event: Event,
):
    """Entry point for multiprocessing.Process(target=grabber_process, ...)."""
    # Ignore SIGINT in child — parent handles shutdown via stop_event
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    reader = CameraReader(cam_id, rtsp_url, shm_name, infer_queue, stop_event)
    reader.run()
