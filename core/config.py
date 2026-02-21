import os
import json
import logging
import re
from urllib.parse import urlsplit, urlunsplit
from dotenv import load_dotenv



def resolve_rtsp_url(rtsp_url: str, camera_ip: str) -> str:
    if not rtsp_url or not camera_ip:
        return rtsp_url
    if "{CAMERA_IP}" in rtsp_url:
        return rtsp_url.replace("{CAMERA_IP}", camera_ip)

    parsed = urlsplit(rtsp_url)
    if not parsed.scheme.startswith("rtsp"):
        return rtsp_url

    hostname = parsed.hostname
    if not hostname:
        return rtsp_url

    auth = ""
    if parsed.username:
        auth = parsed.username
        if parsed.password:
            auth += f":{parsed.password}"
        auth += "@"

    port = f":{parsed.port}" if parsed.port else ""
    new_netloc = f"{auth}{camera_ip}{port}"
    return urlunsplit((parsed.scheme, new_netloc, parsed.path, parsed.query, parsed.fragment))


RTSP_URL = resolve_rtsp_url(_RTSP_URL_RAW, CAMERA_IP)
load_dotenv()
logging.getLogger("ultralytics").setLevel(logging.WARNING)

# --- Telegram ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_IMPORTANT = os.getenv("TELEGRAM_CHAT_IMPORTANT")
CHAT_REGULAR = os.getenv("TELEGRAM_CHAT_NONIMPORTANT")

# --- Database ---
DB_PATH = "./db/door_events.db"

# --- Model Paths ---
PLATE_MODEL_PATH = "./models/bien_so_xe.pt"
GENERAL_MODEL_PATH = "./models/bien_so_xe.pt"
DOOR_MODEL_PATH = "./models/door_model.pt"

# --- Detection ---
LINE_Y = 300
RTSP_URL = os.getenv("RTSP_URL", "")
OCR_SOURCE = "rtsp"
SIGNAL_LOSS_TIMEOUT = 30

# --- Cửa cuốn (Brightness-based fallback) ---
DOOR_ROI = (100, 50, 540, 400)
BRIGHTNESS_THRESHOLD = 80
USE_AI_DOOR_DETECTION = os.path.exists(DOOR_MODEL_PATH)

# --- Authorized list ---
CONFIG_PATH = "./config/authorized.json"
FACES_DIR = "./config/faces"

authorized_plates = []
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
        authorized_plates = [p.upper().replace(" ", "") for p in config.get("plates", [])]
        print(f"✅ Loaded {len(authorized_plates)} authorized plates: {authorized_plates}")

# --- Face recognition (optional) ---
try:
    import face_recognition
    FACE_RECOGNITION_AVAILABLE = True
except ImportError:
    FACE_RECOGNITION_AVAILABLE = False

# --- Utility ---
def normalize_plate(plate_text):
    return re.sub(r'[^A-Z0-9]', '', plate_text.upper())
