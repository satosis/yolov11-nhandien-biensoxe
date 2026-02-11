import os
import json
import logging
import re
from dotenv import load_dotenv

# Load môi trường
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
