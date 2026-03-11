"""
CameraManager — quản lý N camera RTSP streams, mỗi camera 1 thread đọc.
Tối ưu cho Orange Pi: buffer nhỏ, resize sớm, sleep để tránh spin CPU.
Recording continuity: phát hiện gap > GAP_ALERT_SECONDS, log vào DB.
"""
import cv2
import threading
import time
import os
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from core.mjpeg_streamer import MJPEGStreamer

logger = logging.getLogger("camera_manager")

OFFLINE_TIMEOUT    = 10   # giây không có frame → đánh dấu offline
RECONNECT_DELAY    = 5    # giây chờ trước khi reconnect
STREAM_WIDTH       = 640  # resize ngay khi đọc để tiết kiệm CPU
GAP_ALERT_SECONDS  = 300  # gap > 5 phút → log health event


@dataclass
class CameraStream:
    cam_id: str
    name: str
    rtsp_url: str
    streamer: MJPEGStreamer = field(default_factory=lambda: MJPEGStreamer(stream_width=STREAM_WIDTH, fps=8, jpeg_quality=68))
    thread: Optional[threading.Thread] = None
    last_frame_time: float = 0.0
    online: bool = False
    _stop: threading.Event = field(default_factory=threading.Event)
    # Gap tracking
    _gap_start: Optional[float] = field(default=None)
    gap_count_today: int = 0
    offline_count_today: int = 0


class CameraManager:
    def __init__(self, db=None):
        """
        db: DatabaseManager instance (optional). Nếu có, ghi health events vào DB.
        """
        self._cameras: dict[str, CameraStream] = {}
        self._lock = threading.Lock()
        self._db = db

    def add_camera(self, cam_id: str, rtsp_url: str, name: str = ""):
        """Thêm camera và bắt đầu thread đọc RTSP."""
        if not name:
            name = cam_id
        cam = CameraStream(cam_id=cam_id, name=name, rtsp_url=rtsp_url)
        with self._lock:
            self._cameras[cam_id] = cam
        t = threading.Thread(target=self._read_loop, args=(cam_id,), daemon=True, name=f"cam-{cam_id}")
        cam.thread = t
        t.start()

    def get_streamer(self, cam_id: str) -> Optional[MJPEGStreamer]:
        with self._lock:
            cam = self._cameras.get(cam_id)
        return cam.streamer if cam else None

    def get_all_status(self) -> list[dict]:
        """Trả về list trạng thái tất cả cameras."""
        result = []
        with self._lock:
            cams = list(self._cameras.values())
        for cam in cams:
            age = time.time() - cam.last_frame_time if cam.last_frame_time > 0 else None
            gap_active = cam._gap_start is not None
            gap_duration = (time.time() - cam._gap_start) if gap_active else 0.0
            result.append({
                "id": cam.cam_id,
                "name": cam.name,
                "online": cam.online,
                "last_frame_age": round(age, 1) if age is not None else None,
                "gap_active": gap_active,
                "gap_duration_s": round(gap_duration, 1),
                "gap_count_today": cam.gap_count_today,
                "offline_count_today": cam.offline_count_today,
            })
        return result

    def snapshot(self, cam_id: str) -> Optional[bytes]:
        """Trả về JPEG bytes của frame hiện tại, hoặc None nếu offline."""
        streamer = self.get_streamer(cam_id)
        if streamer is None:
            return None
        return streamer.get_snapshot()

    def _read_loop(self, cam_id: str):
        """Thread đọc RTSP liên tục, tự reconnect khi mất kết nối."""
        while True:
            with self._lock:
                cam = self._cameras.get(cam_id)
            if cam is None or cam._stop.is_set():
                break

            cap = cv2.VideoCapture(cam.rtsp_url)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # tránh buffer lag

            if not cap.isOpened():
                self._mark_offline(cam)
                time.sleep(RECONNECT_DELAY)
                continue

            fps = cap.get(cv2.CAP_PROP_FPS) or 15
            frame_sleep = 1.0 / max(1, min(fps, 30))

            while not cam._stop.is_set():
                ret, frame = cap.read()
                if not ret or frame is None:
                    break

                # Resize ngay khi đọc
                if frame.shape[1] > STREAM_WIDTH:
                    ratio = STREAM_WIDTH / float(frame.shape[1])
                    new_h = max(1, int(frame.shape[0] * ratio))
                    frame = cv2.resize(frame, (STREAM_WIDTH, new_h), interpolation=cv2.INTER_AREA)

                cam.streamer.update_frame(frame)
                now = time.time()

                # Close any open gap when frames resume
                if cam._gap_start is not None:
                    gap_dur = now - cam._gap_start
                    if gap_dur >= GAP_ALERT_SECONDS:
                        self._log_health(cam_id, "GAP",
                                         datetime.utcfromtimestamp(cam._gap_start).isoformat(),
                                         datetime.utcnow().isoformat(),
                                         gap_dur,
                                         f"Recording gap {gap_dur:.0f}s")
                        cam.gap_count_today += 1
                    cam._gap_start = None

                cam.last_frame_time = now
                cam.online = True
                time.sleep(frame_sleep)

            cap.release()
            self._mark_offline(cam)

            if not cam._stop.is_set():
                time.sleep(RECONNECT_DELAY)

    def _mark_offline(self, cam: CameraStream):
        """Đánh dấu camera offline và bắt đầu đếm gap."""
        was_online = cam.online
        cam.online = False
        if was_online:
            cam.offline_count_today += 1
            cam._gap_start = time.time()
            self._log_health(cam.cam_id, "OFFLINE",
                             datetime.utcnow().isoformat(),
                             notes="Camera went offline")

    def _log_health(self, cam_id: str, event_type: str, started_at: str,
                    ended_at: str = None, duration_seconds: float = None,
                    notes: str = None):
        """Ghi health event vào DB nếu có DB instance."""
        if self._db is None:
            logger.warning("[%s] %s — %s (no DB)", cam_id, event_type, notes or "")
            return
        try:
            self._db.log_camera_event(cam_id, event_type, started_at,
                                      ended_at, duration_seconds, notes)
        except Exception as e:
            logger.error("Health log error: %s", e)

    def _check_offline(self):
        """Cập nhật trạng thái online/offline dựa trên last_frame_time."""
        now = time.time()
        with self._lock:
            for cam in self._cameras.values():
                if cam.last_frame_time > 0 and (now - cam.last_frame_time) > OFFLINE_TIMEOUT:
                    cam.online = False
