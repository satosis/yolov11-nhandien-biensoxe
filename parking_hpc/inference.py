"""
parking_hpc/inference.py
Process 2 — AI Inference

Pipeline per motion token:
  1. Read frame from SharedMemory (zero-copy)
  2. YOLOv10-small → detect license plate bounding boxes
  3. Frame Buffer: accumulate FRAME_BUFFER_SIZE detections, vote on best plate crop
  4. Plate enhancement: Gaussian Blur → Adaptive Threshold → PaddleOCR
  5. InsightFace (ONNX/OpenCL) → face recognition every FACE_RECOG_EVERY_N frames
  6. Auto-snapshot: save high-res JPEG to SNAPSHOT_DIR on new plate
  7. Push InferenceResult to result_queue for UI/logging

Designed for RK3399: uses onnxruntime with OpenCLExecutionProvider where available.
"""
import os
import time
import signal
import logging
import collections
from dataclasses import dataclass, field
from multiprocessing import Process, Queue, Event, shared_memory
from typing import Optional

import cv2
import numpy as np

from parking_hpc import config as cfg

logger = logging.getLogger("inference")


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class InferenceResult:
    cam_id: str
    ts: float
    plate_text: str = ""
    plate_conf: float = 0.0
    plate_bbox: tuple = ()          # (x1, y1, x2, y2) in original frame coords
    face_name: str = ""
    face_conf: float = 0.0
    snapshot_path: str = ""
    annotated_frame: Optional[np.ndarray] = field(default=None, repr=False)


# ── Plate enhancement ─────────────────────────────────────────────────────────

def enhance_plate(crop: np.ndarray) -> np.ndarray:
    """
    Improve OCR accuracy on a plate crop:
      1. Upscale 2× (bilinear) — PaddleOCR prefers ≥32px height
      2. Gaussian blur to reduce JPEG noise
      3. Adaptive threshold → clean binary image
    Returns a 3-channel BGR image (PaddleOCR expects BGR or RGB).
    """
    h, w = crop.shape[:2]
    upscaled = cv2.resize(crop, (w * 2, h * 2), interpolation=cv2.INTER_LINEAR)
    gray = cv2.cvtColor(upscaled, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    binary = cv2.adaptiveThreshold(
        blurred, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 11, 2,
    )
    return cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)


# ── Frame buffer / voting ─────────────────────────────────────────────────────

class PlateVoter:
    """
    Accumulate (plate_text, confidence) pairs over FRAME_BUFFER_SIZE frames.
    Return the text with the highest total confidence score (weighted vote).
    """

    def __init__(self, buffer_size: int = cfg.FRAME_BUFFER_SIZE):
        self._buffer: collections.deque = collections.deque(maxlen=buffer_size)
        self._buffer_size = buffer_size

    def add(self, text: str, conf: float):
        if text and conf >= cfg.VOTE_MIN_CONF:
            self._buffer.append((text, conf))

    def is_ready(self) -> bool:
        return len(self._buffer) >= self._buffer_size

    def best(self) -> tuple[str, float]:
        """Return (best_text, avg_conf) or ('', 0) if buffer not ready."""
        if not self._buffer:
            return "", 0.0
        scores: dict[str, float] = {}
        counts: dict[str, int] = {}
        for text, conf in self._buffer:
            scores[text] = scores.get(text, 0.0) + conf
            counts[text] = counts.get(text, 0) + 1
        best_text = max(scores, key=lambda t: scores[t])
        avg_conf = scores[best_text] / counts[best_text]
        return best_text, avg_conf

    def reset(self):
        self._buffer.clear()


# ── Model wrappers ────────────────────────────────────────────────────────────

class PlateDetector:
    """YOLOv10/YOLOv11 plate detector via Ultralytics."""

    def __init__(self):
        from ultralytics import YOLO
        self._model = YOLO(cfg.PLATE_MODEL_PATH)
        logger.info("PlateDetector loaded: %s", cfg.PLATE_MODEL_PATH)

    def detect(self, frame: np.ndarray) -> list[tuple[int, int, int, int, float]]:
        """Return list of (x1, y1, x2, y2, conf) for class=1 (license_plate)."""
        results = self._model(frame, imgsz=cfg.PLATE_DETECT_IMGSZ, conf=cfg.PLATE_DETECT_CONF, verbose=False)
        boxes = []
        for r in results:
            for b in r.boxes:
                if int(b.cls[0]) == 1:
                    x1, y1, x2, y2 = map(int, b.xyxy[0])
                    boxes.append((x1, y1, x2, y2, float(b.conf[0])))
        return boxes


class OCRReader:
    """PaddleOCR lightweight wrapper."""

    def __init__(self):
        from paddleocr import PaddleOCR
        # use_angle_cls=False speeds up inference; lang='en' for plate chars
        self._ocr = PaddleOCR(use_angle_cls=False, lang="en", show_log=False, use_gpu=False)
        logger.info("PaddleOCR initialised")

    def read(self, crop: np.ndarray) -> tuple[str, float]:
        """Return (text, confidence) from a plate crop image."""
        result = self._ocr.ocr(crop, cls=False)
        if not result or not result[0]:
            return "", 0.0
        texts, confs = [], []
        for line in result[0]:
            if line and len(line) >= 2:
                txt = line[1][0]
                conf = float(line[1][1])
                texts.append(txt)
                confs.append(conf)
        if not texts:
            return "", 0.0
        combined = "".join(texts).upper().replace(" ", "")
        avg_conf = sum(confs) / len(confs)
        return combined, avg_conf


class FaceRecognizer:
    """
    InsightFace with ONNX Runtime.
    Tries OpenCLExecutionProvider (RK3399 Mali GPU) then falls back to CPU.
    """

    def __init__(self):
        import onnxruntime as ort
        providers = ort.get_available_providers()
        preferred = []
        if "OpenCLExecutionProvider" in providers:
            preferred.append("OpenCLExecutionProvider")
        preferred.append("CPUExecutionProvider")

        from insightface.app import FaceAnalysis
        self._app = FaceAnalysis(
            name="buffalo_sc",
            root=cfg.FACE_MODEL_DIR,
            providers=preferred,
        )
        self._app.prepare(ctx_id=0, det_size=(320, 320))
        self._known: dict[str, np.ndarray] = {}  # name → embedding
        self._load_known_faces()
        logger.info("FaceRecognizer ready. Known faces: %d", len(self._known))

    def _load_known_faces(self):
        """Load face embeddings from KNOWN_FACES_DIR/{name}/*.jpg"""
        if not os.path.isdir(cfg.KNOWN_FACES_DIR):
            return
        for person in os.listdir(cfg.KNOWN_FACES_DIR):
            person_dir = os.path.join(cfg.KNOWN_FACES_DIR, person)
            if not os.path.isdir(person_dir):
                continue
            embeddings = []
            for fname in os.listdir(person_dir):
                if not fname.lower().endswith((".jpg", ".jpeg", ".png")):
                    continue
                img = cv2.imread(os.path.join(person_dir, fname))
                if img is None:
                    continue
                faces = self._app.get(img)
                if faces:
                    embeddings.append(faces[0].normed_embedding)
            if embeddings:
                self._known[person] = np.mean(embeddings, axis=0)

    def identify(self, frame: np.ndarray) -> tuple[str, float]:
        """Return (name, similarity) for the most prominent face, or ('', 0)."""
        faces = self._app.get(frame)
        if not faces:
            return "", 0.0
        best_name, best_sim = "STRANGER", 0.0
        for face in faces:
            emb = face.normed_embedding
            for name, ref_emb in self._known.items():
                sim = float(np.dot(emb, ref_emb))
                if sim > best_sim:
                    best_sim = sim
                    best_name = name if sim > 0.35 else "STRANGER"
        return best_name, best_sim


# ── Snapshot helper ───────────────────────────────────────────────────────────

def save_snapshot(frame: np.ndarray, cam_id: str, plate_text: str) -> str:
    """Save high-res JPEG to SNAPSHOT_DIR. Returns saved path."""
    os.makedirs(cfg.SNAPSHOT_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_plate = plate_text.replace("/", "_").replace("\\", "_") or "unknown"
    filename = f"{cam_id}_{safe_plate}_{ts}.jpg"
    path = os.path.join(cfg.SNAPSHOT_DIR, filename)
    cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
    return path


# ── Inference worker ──────────────────────────────────────────────────────────

class InferenceWorker:
    def __init__(self, infer_queue: Queue, result_queue: Queue, stop_event: Event):
        self.infer_queue = infer_queue
        self.result_queue = result_queue
        self.stop_event = stop_event

        self._plate_detector = PlateDetector()
        self._ocr = OCRReader()
        self._face_recog = FaceRecognizer()

        # Per-camera voters
        self._voters: dict[str, PlateVoter] = {}
        self._seen_plates: set[str] = set()
        self._frame_counter = 0

    def _get_voter(self, cam_id: str) -> PlateVoter:
        if cam_id not in self._voters:
            self._voters[cam_id] = PlateVoter()
        return self._voters[cam_id]

    def _read_shm_frame(self, shm_name: str) -> Optional[np.ndarray]:
        try:
            shm = shared_memory.SharedMemory(name=shm_name, create=False)
            arr = np.ndarray(
                (cfg.GRAB_HEIGHT, cfg.GRAB_WIDTH, 3),
                dtype=np.uint8,
                buffer=shm.buf[4:],  # skip 4-byte counter header
            ).copy()  # copy before closing shm
            shm.close()
            return arr
        except Exception as e:
            logger.warning("SHM read error (%s): %s", shm_name, e)
            return None

    def run(self):
        logger.info("Inference worker started")
        while not self.stop_event.is_set():
            try:
                token = self.infer_queue.get(timeout=0.5)
            except Exception:
                continue

            cam_id: str = token["cam_id"]
            shm_name: str = token["shm_name"]
            ts: float = token["ts"]

            frame = self._read_shm_frame(shm_name)
            if frame is None:
                continue

            result = InferenceResult(cam_id=cam_id, ts=ts)
            result.annotated_frame = frame.copy()

            # ── Plate detection ───────────────────────────────────────────────
            boxes = self._plate_detector.detect(frame)
            voter = self._get_voter(cam_id)

            for x1, y1, x2, y2, det_conf in boxes:
                crop = frame[y1:y2, x1:x2]
                if crop.size == 0:
                    continue
                enhanced = enhance_plate(crop)
                text, ocr_conf = self._ocr.read(enhanced)
                voter.add(text, ocr_conf * det_conf)

                # Draw box on annotated frame
                cv2.rectangle(result.annotated_frame, (x1, y1), (x2, y2), (255, 0, 255), 2)
                cv2.putText(
                    result.annotated_frame, f"{text} {ocr_conf:.2f}",
                    (x1, max(y1 - 8, 10)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 0, 255), 2,
                )

            if voter.is_ready():
                best_text, best_conf = voter.best()
                voter.reset()
                if best_text:
                    result.plate_text = best_text
                    result.plate_conf = best_conf
                    # Auto-snapshot on new plate
                    if best_text not in self._seen_plates:
                        self._seen_plates.add(best_text)
                        result.snapshot_path = save_snapshot(frame, cam_id, best_text)
                        logger.info("[%s] New plate: %s (%.2f) → %s",
                                    cam_id, best_text, best_conf, result.snapshot_path)

            # ── Face recognition (every N frames) ────────────────────────────
            self._frame_counter += 1
            if self._frame_counter % cfg.FACE_RECOG_EVERY_N == 0:
                name, sim = self._face_recog.identify(frame)
                result.face_name = name
                result.face_conf = sim
                if name and name != "STRANGER":
                    cv2.putText(
                        result.annotated_frame, f"FACE: {name} ({sim:.2f})",
                        (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2,
                    )

            # Push to UI/logging
            if not self.result_queue.full():
                result_queue_safe = result
                result_queue_safe.annotated_frame = result.annotated_frame  # keep ndarray
                self.result_queue.put_nowait(result)

        logger.info("Inference worker stopped")


# ── Process entry point ───────────────────────────────────────────────────────

def inference_process(infer_queue: Queue, result_queue: Queue, stop_event: Event):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
    worker = InferenceWorker(infer_queue, result_queue, stop_event)
    worker.run()
