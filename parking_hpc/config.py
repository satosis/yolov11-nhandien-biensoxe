"""
parking_hpc/config.py
Central configuration for the high-performance parking monitor.
All tuneable constants live here — no magic numbers in other modules.
"""
import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"), override=False)
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".camera.env"), override=True)

# ── RTSP Sources ──────────────────────────────────────────────────────────────
# Force TCP transport to avoid UDP packet loss on LAN
RTSP_CAM1 = os.getenv("RTSP_URL", "rtsp://admin:password@192.168.1.55:554/cam/realmonitor?channel=1&subtype=0")
RTSP_CAM2 = os.getenv("CAMERA_2_URL", "")

# ── ROI Polygon (entrance zone) ───────────────────────────────────────────────
# Normalised [0..1] (x, y) pairs — scaled to actual frame size at runtime.
# Default: lower-centre trapezoid covering a typical gate entrance.
ROI_POLYGON_NORM = [
    (0.25, 0.40),
    (0.75, 0.40),
    (0.90, 1.00),
    (0.10, 1.00),
]

# ── Model Paths ───────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLATE_MODEL_PATH  = os.path.join(BASE_DIR, "models", "bien_so_xe.pt")
FACE_MODEL_DIR    = os.path.join(BASE_DIR, "models", "insightface")  # buffalo_sc or w600k_r50
KNOWN_FACES_DIR   = os.path.join(BASE_DIR, "config", "faces")

# ── Inference Tuning ──────────────────────────────────────────────────────────
PLATE_DETECT_IMGSZ   = 640
PLATE_DETECT_CONF    = 0.45
PLATE_DETECT_EVERY_N = 3        # run plate model every N frames (CPU budget)
FRAME_BUFFER_SIZE    = 5        # frames to buffer for voting
VOTE_MIN_CONF        = 0.70     # minimum OCR confidence to count a vote
FACE_RECOG_EVERY_N   = 10       # run face recognition every N frames

# ── Motion Detection ──────────────────────────────────────────────────────────
MOTION_THRESHOLD     = 1500     # contour area px² to trigger AI
MOTION_BLUR_KSIZE    = 21       # Gaussian blur kernel for background subtraction
MOTION_DILATE_ITER   = 2

# ── Frame Grabber ─────────────────────────────────────────────────────────────
GRAB_WIDTH           = 1280     # decode resolution (hardware scales down)
GRAB_HEIGHT          = 720
GRAB_FPS_CAP         = 15       # cap capture FPS to save CPU
RTSP_BUFFER_SIZE     = 1        # cv2 internal buffer — keep at 1 for low latency
RTSP_RECONNECT_DELAY = 3        # seconds before reconnect attempt

# ── Shared Memory ─────────────────────────────────────────────────────────────
# Each frame: GRAB_WIDTH * GRAB_HEIGHT * 3 bytes (BGR uint8)
SHM_FRAME_BYTES = GRAB_WIDTH * GRAB_HEIGHT * 3
SHM_NAME_CAM1   = "hpc_cam1_frame"
SHM_NAME_CAM2   = "hpc_cam2_frame"

# ── Queue Sizes ───────────────────────────────────────────────────────────────
INFER_QUEUE_MAXSIZE  = 4   # frames waiting for inference
RESULT_QUEUE_MAXSIZE = 32  # inference results waiting for UI

# ── Storage ───────────────────────────────────────────────────────────────────
SNAPSHOT_DIR = os.getenv("SNAPSHOT_DIR", "/mnt/storage/snapshots")
DB_PATH      = os.getenv("DB_PATH", os.path.join(BASE_DIR, "db", "door_events.db"))

# ── Web UI ────────────────────────────────────────────────────────────────────
UI_HOST      = "0.0.0.0"   # bind all interfaces (Tailscale + LAN)
UI_PORT      = 5050
UI_STREAM_FPS = 8           # JPEG frames pushed via SocketIO per second
UI_JPEG_QUALITY = 70

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN         = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_IMPORTANT = os.getenv("TELEGRAM_CHAT_IMPORTANT", "")

# ── CPU Governor ─────────────────────────────────────────────────────────────
# Applied once at startup by main.py
CPU_GOVERNOR = "performance"
