from ultralytics import YOLO
import cv2
import logging
import os
import re
import time
import requests
import sqlite3
import json
import threading
import numpy as np
import uuid
from datetime import datetime
from dotenv import load_dotenv

# Import face_recognition (cần cài dlib)
try:
    import face_recognition
    FACE_RECOGNITION_AVAILABLE = True
except ImportError:
    FACE_RECOGNITION_AVAILABLE = False

# Load môi trường
load_dotenv()
logging.getLogger("ultralytics").setLevel(logging.WARNING)

import shutil
import psutil
import subprocess

# --- 1. CONFIGURATION ---
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_IMPORTANT = os.getenv("CHAT_ID_IMPORTANT")
CHAT_REGULAR = os.getenv("CHAT_ID_REGULAR")
DB_PATH = os.getenv("DATABASE_PATH", "./db/door_events.db")
USE_N8N = os.getenv("USE_N8N", "false").lower() == "true"
N8N_WEBHOOK_URL = os.getenv("N8N_WEBHOOK_URL")

PLATE_MODEL_PATH = "./models/bien_so_xe.pt"
GENERAL_MODEL_PATH = "./models/bien_so_xe.pt"
DOOR_MODEL_PATH = "./models/door_model.pt"  # Custom trained model (optional)
LINE_Y = 300
RTSP_URL = os.getenv("RTSP_URL", "rtsp://<USER>:<PASS>@<CAMERA_IP>:554/cam/realmonitor?channel=1&subtype=0")
OCR_SOURCE = os.getenv("OCR_SOURCE", "rtsp").strip()
SIGNAL_LOSS_TIMEOUT = 30

# --- CẤU HÌNH PHÁT HIỆN CỬA (Brightness-based fallback) ---
# ROI: (x1, y1, x2, y2) - Vùng cửa cuốn trong frame
# Bạn cần điều chỉnh theo vị trí camera thực tế
DOOR_ROI = (100, 50, 540, 400)  # Điều chỉnh theo frame của bạn
BRIGHTNESS_THRESHOLD = 80  # Ngưỡng sáng: > threshold = cửa mở
USE_AI_DOOR_DETECTION = os.path.exists(DOOR_MODEL_PATH)

# --- LOAD AUTHORIZED LIST ---
CONFIG_PATH = "./config/authorized.json"
FACES_DIR = "./config/faces"
authorized_plates = []
authorized_face_encodings = []
authorized_face_names = []

if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
        authorized_plates = [p.upper().replace(" ", "") for p in config.get("plates", [])]
        print(f"✅ Loaded {len(authorized_plates)} authorized plates: {authorized_plates}")

if FACE_RECOGNITION_AVAILABLE and os.path.exists(FACES_DIR):
    for filename in os.listdir(FACES_DIR):
        if filename.lower().endswith(('.jpg', '.jpeg', '.png')):
            filepath = os.path.join(FACES_DIR, filename)
            img = face_recognition.load_image_file(filepath)
            encodings = face_recognition.face_encodings(img)
            if encodings:
                authorized_face_encodings.append(encodings[0])
                name = os.path.splitext(filename)[0].replace("_", " ")
                authorized_face_names.append(name)
    print(f"✅ Loaded {len(authorized_face_names)} authorized faces: {authorized_face_names}")

# --- DATABASE MANAGER ---
class DatabaseManager:
    def __init__(self, path):
        self.path = path
        db_dir = os.path.dirname(self.path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        self.init_db()

    def init_db(self):
        conn = sqlite3.connect(self.path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME, event_type TEXT, description TEXT,
                truck_count INTEGER, person_count INTEGER
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS vehicle_whitelist (
                plate_norm TEXT PRIMARY KEY,
                label TEXT,
                added_at_utc TEXT,
                added_by TEXT,
                note TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_plates (
                pending_id TEXT PRIMARY KEY,
                event_id INTEGER,
                plate_raw TEXT,
                plate_norm TEXT,
                first_seen_utc TEXT,
                status TEXT,
                confirmed_at_utc TEXT,
                confirmed_by TEXT
            )
        ''')
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_vehicle_whitelist_plate_norm ON vehicle_whitelist (plate_norm)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_pending_plates_plate_norm ON pending_plates (plate_norm)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_pending_plates_status ON pending_plates (status)'
        )
        conn.commit()
        conn.close()

    def is_plate_whitelisted(self, plate_norm):
        try:
            conn = sqlite3.connect(self.path)
            cursor = conn.cursor()
            cursor.execute('SELECT 1 FROM vehicle_whitelist WHERE plate_norm = ? LIMIT 1', (plate_norm,))
            row = cursor.fetchone()
            conn.close()
            return row is not None
        except sqlite3.Error:
            return False

    def add_pending_plate(self, pending_id, event_id, plate_raw, plate_norm, first_seen_utc):
        try:
            conn = sqlite3.connect(self.path)
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT OR IGNORE INTO pending_plates (
                    pending_id, event_id, plate_raw, plate_norm, first_seen_utc, status
                ) VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (pending_id, event_id, plate_raw, plate_norm, first_seen_utc, "pending")
            )
            conn.commit()
            conn.close()
        except sqlite3.Error:
            pass

    def upsert_vehicle_whitelist(self, plate_norm, label, added_by, note=None):
        try:
            conn = sqlite3.connect(self.path)
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO vehicle_whitelist (plate_norm, label, added_at_utc, added_by, note)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(plate_norm) DO UPDATE SET
                    label=excluded.label,
                    added_at_utc=excluded.added_at_utc,
                    added_by=excluded.added_by,
                    note=excluded.note
                ''',
                (plate_norm, label, datetime.utcnow().isoformat(), added_by, note)
            )
            conn.commit()
            conn.close()
            return True
        except sqlite3.Error:
            return False

    def update_pending_status(self, plate_norm, status, confirmed_by):
        try:
            conn = sqlite3.connect(self.path)
            cursor = conn.cursor()
            cursor.execute(
                '''
                UPDATE pending_plates
                SET status = ?, confirmed_at_utc = ?, confirmed_by = ?
                WHERE plate_norm = ? AND status = 'pending'
                ''',
                (status, datetime.utcnow().isoformat(), confirmed_by, plate_norm)
            )
            conn.commit()
            conn.close()
            return True
        except sqlite3.Error:
            return False

    def log_event(self, event_type, description, trucks, people):
        conn = sqlite3.connect(self.path)
        cursor = conn.cursor()
        cursor.execute('INSERT INTO events (timestamp, event_type, description, truck_count, person_count) VALUES (?, ?, ?, ?, ?)',
                       (datetime.now(), event_type, description, trucks, people))
        event_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return event_id

    def get_stats(self):
        conn = sqlite3.connect(self.path)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*), event_type FROM events GROUP BY event_type')
        stats = cursor.fetchall()
        conn.close()
        return stats
    
    def get_pending_plates(self):
        conn = sqlite3.connect(self.path)
        cursor = conn.cursor()
        cursor.execute("SELECT plate_norm, plate_raw, first_seen_utc FROM pending_plates WHERE status = 'pending'")
        pending = cursor.fetchall()
        conn.close()
        return pending

db = DatabaseManager(DB_PATH)

# --- 2. HÀM THÔNG BÁO ---
def notify_telegram(message, important=False):
    if USE_N8N and N8N_WEBHOOK_URL:
        try:
            requests.post(N8N_WEBHOOK_URL, json={"message": message, "important": important})
        except Exception as e:
            print(f"Lỗi gửi n8n: {e}")

    chat_id = CHAT_IMPORTANT if important else CHAT_REGULAR
    prefix = "🚨 [QUAN TRỌNG] " if important else "ℹ️ [THÔNG BÁO] "
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": prefix + message})
    except Exception as e:
        print(f"Lỗi gửi Telegram: {e}")

def handle_telegram_command(text, chat_id, user_id):
    """Xử lý lệnh từ Telegram"""
    parts = text.strip().split()
    if not parts:
        return
    
    cmd = parts[0].lower()
    
    # Lệnh mở/đóng cửa
    if cmd == "/open":
        print(f"Telegram CMD: OPEN from {user_id}")
        mqtt_manager.publish_trigger_open()
        notify_telegram(f"Đã gửi lệnh MỞ cửa theo yêu cầu của {user_id}")
        return

    # Lệnh duyệt biển số
    if cmd in ["/staff", "/reject", "/mine"]:
        if len(parts) < 2:
            notify_telegram(f"Lỗi: Thiếu biển số. VD: {cmd} 29A12345")
            return
        
        plate_raw = parts[1]
        plate_norm = normalize_plate(plate_raw)
        
        if cmd == "/mine":
            if db.upsert_vehicle_whitelist(plate_norm, "mine", str(user_id)):
                db.update_pending_status(plate_norm, "approved_mine", str(user_id))
                notify_telegram(f"✅ Đã thêm {plate_norm} vào danh sách CỦA TÔI.")
            else:
                notify_telegram(f"⚠️ Lỗi khi thêm {plate_norm}.")
        elif cmd == "/staff":
            if db.upsert_vehicle_whitelist(plate_norm, "staff", str(user_id)):
                db.update_pending_status(plate_norm, "approved_staff", str(user_id))
                notify_telegram(f"✅ Đã thêm {plate_norm} vào danh sách NHÂN VIÊN.")
            else:
                notify_telegram(f"⚠️ Lỗi khi thêm {plate_norm}.")
                
        elif cmd == "/reject":
            db.update_pending_status(plate_norm, "rejected", str(user_id))
            notify_telegram(f"🚫 Đã từ chối biển số {plate_norm}.")
    
    # Lệnh xem các biển số đang chờ duyệt
    if cmd == "/pending":
        pending_plates = db.get_pending_plates()
        if pending_plates:
            msg = "Các biển số đang chờ duyệt:\n"
            for plate_norm, plate_raw, first_seen_utc in pending_plates:
                msg += f"- `{plate_norm}` (raw: {plate_raw}, từ: {first_seen_utc})\n"
            notify_telegram(msg)
        else:
            notify_telegram("Không có biển số nào đang chờ duyệt.")

    # Lệnh duyệt khuôn mặt
    if cmd == "/staff_face":
        if len(parts) < 3:
            notify_telegram("Lỗi cú pháp: /staff_face [ID_TAM] [TEN_NHAN_VIEN]")
            return
        
        face_id = parts[1]
        staff_name = parts[2].replace(" ", "_")
        
        temp_path = f"./config/faces/temp/{face_id}.jpg"
        target_path = f"./config/faces/{staff_name}.jpg"
        
        if os.path.exists(temp_path):
            try:
                os.rename(temp_path, target_path)
                notify_telegram(f"✅ Đã thêm nhân viên: {staff_name}")
                # Reload faces
                load_faces()
            except Exception as e:
                notify_telegram(f"⚠️ Lỗi khi lưu ảnh: {e}")
        else:
            notify_telegram(f"⚠️ Không tìm thấy ảnh tạm: {face_id}")
    
    # Lệnh dọn dẹp
    if cmd == "/cleanup":
        if len(parts) < 2:
            notify_telegram("Lỗi cú pháp: /cleanup [faces|active_learning|db]")
            return
        
        target = parts[1].lower()
        if target == "faces":
            try:
                if os.path.exists(FACES_DIR):
                    shutil.rmtree(FACES_DIR)
                    os.makedirs(FACES_DIR)
                    notify_telegram("✅ Đã dọn dẹp thư mục khuôn mặt.")
                    load_faces() # Reload empty list
                else:
                    notify_telegram("Thư mục khuôn mặt không tồn tại.")
            except Exception as e:
                notify_telegram(f"⚠️ Lỗi khi dọn dẹp khuôn mặt: {e}")
        elif target == "active_learning":
            try:
                al_dir = "./data/active_learning"
                if os.path.exists(al_dir):
                    shutil.rmtree(al_dir)
                    os.makedirs(al_dir)
                    notify_telegram("✅ Đã dọn dẹp thư mục active learning.")
                else:
                    notify_telegram("Thư mục active learning không tồn tại.")
            except Exception as e:
                notify_telegram(f"⚠️ Lỗi khi dọn dẹp active learning: {e}")
        elif target == "db":
            try:
                if os.path.exists(DB_PATH):
                    os.remove(DB_PATH)
                    db.init_db() # Re-initialize empty DB
                    notify_telegram("✅ Đã dọn dẹp cơ sở dữ liệu.")
                else:
                    notify_telegram("Tệp cơ sở dữ liệu không tồn tại.")
            except Exception as e:
                notify_telegram(f"⚠️ Lỗi khi dọn dẹp cơ sở dữ liệu: {e}")
        else:
            notify_telegram("⚠️ Mục tiêu dọn dẹp không hợp lệ. Chọn: faces, active_learning, db.")


def load_faces():
    global authorized_face_encodings, authorized_face_names
    authorized_face_encodings = []
    authorized_face_names = []
    if FACE_RECOGNITION_AVAILABLE and os.path.exists(FACES_DIR):
        for filename in os.listdir(FACES_DIR):
            if filename.lower().endswith(('.jpg', '.jpeg', '.png')):
                filepath = os.path.join(FACES_DIR, filename)
                try:
                    img = face_recognition.load_image_file(filepath)
                    encodings = face_recognition.face_encodings(img)
                    if encodings:
                        authorized_face_encodings.append(encodings[0])
                        name = os.path.splitext(filename)[0].replace("_", " ")
                        authorized_face_names.append(name)
                except Exception as e:
                    print(f"Lỗi load face {filename}: {e}")
    print(f"✅ Reloaded {len(authorized_face_names)} authorized faces")

# --- MJPEG STREAMER INIT ---
from core.mjpeg_streamer import MJPEGStreamer
def start_api_server():
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")

@app.get("/")
def dashboard():
    """Trang chủ Dashboard đơn giản"""
    from fastapi.responses import HTMLResponse
    html_content = """
    <html>
        <head>
            <title>Smart Door Monitoring Dashboard</title>
            <style>
                body { font-family: sans-serif; background: #121212; color: white; text-align: center; }
                .container { display: flex; flex-direction: column; align-items: center; margin-top: 20px; }
                img { border: 5px solid #333; border-radius: 10px; max-width: 90%; }
                .stats { margin-top: 20px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; width: 60%; }
                .card { background: #1e1e1e; padding: 20px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.5); }
                h1 { color: #00e676; }
                .logs { width: 80%; background: #222; margin-top: 30px; text-align: left; padding: 20px; border-radius: 10px; }
            </style>
        </head>
        <body>
            <h1>🚪 Smart Door AI Dashboard</h1>
            <div class="container">
                <img src="/video_feed" alt="Live View">
                <div class="stats" id="stats-container">
                    <div class="card"><h3>Người</h3><p id="p-count">0</p></div>
                    <div class="card"><h3>Xe Tải</h3><p id="t-count">0</p></div>
                    <div class="card"><h3>Cửa</h3><p id="door-status">Đang tải...</p></div>
                </div>
                <div class="logs">
                    <h3>Hoạt động gần đây:</h3>
                    <ul id="log-list"></ul>
                </div>
            </div>
            <script>
                async function update() {
                    const res = await fetch('/api/status');
                    const data = await res.json();
                    document.getElementById('p-count').innerText = data.people;
                    document.getElementById('t-count').innerText = data.trucks;
                    document.getElementById('door-status').innerText = data.door ? "🔓 MỞ" : "🔒 ĐÓNG";
                    
                    const logList = document.getElementById('log-list');
                    logList.innerHTML = data.recent_logs.map(l => `<li>[${l[0]}] <b>${l[1]}</b>: ${l[2]}</li>`).join('');
                }
                setInterval(update, 2000);
            </script>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content)

@app.get("/api/status")
def get_api_status():
    """API lấy trạng thái hệ thống cho Dashboard"""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT timestamp, event_type, description FROM events ORDER BY id DESC LIMIT 5")
    logs = cursor.fetchall()
    conn.close()
    
    return {
        "people": person_count,
        "trucks": truck_count,
        "door": door_open,
        "recent_logs": logs
    }

def telegram_polling_loop():
    """Vòng lặp nhận tin nhắn từ Telegram"""
    if not TOKEN:
        return
        
    last_update_id = 0
    print("🤖 Telegram Bot listening...")
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 30}
            resp = requests.get(url, params=params, timeout=40)
            
            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    for update in data.get("result", []):
                        last_update_id = update["update_id"]
                        
                        # Chỉ xử lý tin nhắn văn bản
                        if "message" in update and "text" in update["message"]:
                            msg = update["message"]
                            text = msg["text"]
                            chat_id = msg["chat"]["id"]
                            user_id = msg["from"]["id"]
                            
                            # Log tin nhắn đến
                            # print(f"Tele msg: {text} from {user_id}")
                            
                            if str(chat_id) in [CHAT_IMPORTANT, CHAT_REGULAR]:
                                handle_telegram_command(text, chat_id, user_id)
            
            time.sleep(1)
        except Exception as e:
            print(f"Telegram polling error: {e}")
            time.sleep(5)

# --- SYSTEM MONITORING ---
def get_cpu_temp():
    try:
        # For Raspberry Pi / Linux
        result = subprocess.run(['vcgencmd', 'measure_temp'], capture_output=True, text=True)
        if result.returncode == 0:
            return float(result.stdout.split('=')[1].split('\'')[0])
        # For other Linux systems
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            return int(f.read()) / 1000.0
    except (FileNotFoundError, IndexError, ValueError):
        return None

def system_monitor_loop():
    while True:
        cpu_percent = psutil.cpu_percent(interval=1)
        mem_info = psutil.virtual_memory()
        disk_info = psutil.disk_usage('/')
        
        temp = get_cpu_temp()
        temp_str = f"{temp:.1f}°C" if temp else "N/A"

        # print(f"CPU: {cpu_percent:.1f}% | RAM: {mem_info.percent:.1f}% | Disk: {disk_info.percent:.1f}% | Temp: {temp_str}")
        
        # Gửi cảnh báo nếu CPU/RAM/Disk quá cao
        if cpu_percent > 90:
            notify_telegram(f"CẢNH BÁO: CPU đang ở mức cao: {cpu_percent:.1f}%", important=True)
        if mem_info.percent > 90:
            notify_telegram(f"CẢNH BÁO: RAM đang ở mức cao: {mem_info.percent:.1f}%", important=True)
        if disk_info.percent > 90:
            notify_telegram(f"CẢNH BÁO: Đĩa cứng đầy: {disk_info.percent:.1f}%", important=True)

        time.sleep(60) # Kiểm tra mỗi 1 phút

# --- FACE/PLATE MATCHING ---
def check_face(frame):
    """Nhận diện khuôn mặt và kiểm tra trong danh sách ủy quyền."""
    if not FACE_RECOGNITION_AVAILABLE or not authorized_face_encodings:
        return None, None  # Bỏ qua nếu không có thư viện hoặc danh sách trống
    
    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    face_locations = face_recognition.face_locations(rgb_frame)
    face_encs = face_recognition.face_encodings(rgb_frame, face_locations)
    
    for face_enc, loc in zip(face_encs, face_locations):
        matches = face_recognition.compare_faces(authorized_face_encodings, face_enc, tolerance=0.6)
        if True in matches:
            name = authorized_face_names[matches.index(True)]
            return name, loc  # Nhân viên hợp lệ
        else:
            return "STRANGER", loc  # Người lạ
    return None, None

def check_plate(plate_text):
    """Kiểm tra biển số xe có trong danh sách ủy quyền không."""
    normalized = plate_text.upper().replace(" ", "").replace("-", "")
    for auth_plate in authorized_plates:
        if auth_plate.replace("-", "") in normalized or normalized in auth_plate.replace("-", ""):
            return True, auth_plate
    return False, None

def normalize_plate(plate_text):
    return re.sub(r'[^A-Z0-9]', '', plate_text.upper())

# --- DOOR STATE DETECTION ---
door_model = None
if USE_AI_DOOR_DETECTION:
    try:
        door_model = YOLO(DOOR_MODEL_PATH)
        print(f"✅ Loaded door detection model: {DOOR_MODEL_PATH}")
    except Exception as e:
        print(f"⚠️ Không thể load door model: {e}. Dùng phương pháp độ sáng.")

def check_door_state(frame):
    """
    Kiểm tra trạng thái cửa cuốn.
    Returns: 'open', 'closed', hoặc 'unknown'
    """
    # Phương pháp 1: AI Model (nếu có)
    if door_model is not None:
        results = door_model(frame, verbose=False)
        for r in results:
            for box in r.boxes:
                cls_id = int(box.cls[0])
                cls_name = door_model.names[cls_id]
                if 'open' in cls_name.lower():
                    return 'open'
                elif 'close' in cls_name.lower():
                    return 'closed'
    
    # Phương pháp 2: Brightness-based (fallback)
    x1, y1, x2, y2 = DOOR_ROI
    h, w = frame.shape[:2]
    # Đảm bảo ROI nằm trong frame
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)
    
    if x2 > x1 and y2 > y1:
        roi = frame[y1:y2, x1:x2]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        brightness = np.mean(gray)
        
        if brightness > BRIGHTNESS_THRESHOLD:
            return 'open'  # Sáng = thấy ánh sáng bên ngoài = cửa mở
        else:
            return 'closed'  # Tối = cửa đóng
    
    return 'unknown'

# --- TELEGRAM BOT HANDLER ---
truck_count = 0
person_count = 0

def telegram_bot_handler():
    last_update_id = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=10"
            r = requests.get(url, timeout=15).json()
            if r.get("ok"):
                for update in r["result"]:
                    last_update_id = update["update_id"]
                    msg = update.get("message", {})
                    text = msg.get("text", "")
                    chat_id = msg.get("chat", {}).get("id")
                    user = msg.get("from", {})
                    user_label = user.get("username") or str(user.get("id") or "unknown")

                    if cmd == "/stats":
                        rows = db.get_stats()
                        stat_text = "📊 Thống kê hôm nay:\n"
                        for row in rows:
                            stat_text += f"- {row[1]}: {row[0]} lần\n"
                        stat_text += f"\nHiện tại: {truck_count} xe, {person_count} người."
                        
                        # Thêm thông tin hệ thống
                        temp = get_cpu_temp()
                        temp_str = f"{temp:.1f}°C" if temp else "N/A"
                        disk = psutil.disk_usage('/')
                        stat_text += f"\n\n🖥 Hệ thống:\n- Temp: {temp_str}\n- Disk: {disk.percent}%"
                        
                        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", 
                                      json={"chat_id": chat_id, "text": stat_text})
                        continue

                    if cmd == "/sys":
                        # Alias for /stats system part
                        notify_telegram(f"🖥 Hệ thống: {get_cpu_temp():.1f}°C | Disk: {psutil.disk_usage('/').percent}%")
                        continue

                    if cmd == "/cleanup":
                        try:
                            al_dir = "./data/active_learning"
                            if os.path.exists(al_dir):
                                shutil.rmtree(al_dir)
                                os.makedirs(al_dir)
                            notify_telegram("✅ Đã dọn dẹp bộ nhớ đệm (Active Learning).")
                        except Exception as e:
                            notify_telegram(f"⚠️ Lỗi: {e}")
                        continue

                    if not text or not text.startswith("/"):
                        continue

                    parts = text.strip().split(maxsplit=1)
                    cmd = parts[0].split("@")[0].lower()
                    plate_raw = parts[1] if len(parts) > 1 else ""
                    plate_norm = normalize_plate(plate_raw)
                    if cmd in {"/mine", "/staff", "/reject"} and not plate_norm:
                        requests.post(
                            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                            json={"chat_id": chat_id, "text": "Thiếu biển số. Ví dụ: /mine 51A12345"}
                        )
                        continue

                    if cmd == "/mine":
                        if db.upsert_vehicle_whitelist(plate_norm, "mine", user_label):
                            db.update_pending_status(plate_norm, "approved_mine", user_label)
                            reply = f"✅ Đã thêm {plate_norm} vào whitelist (mine)."
                        else:
                            reply = f"⚠️ Không thể cập nhật whitelist cho {plate_norm}."
                        requests.post(
                            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                            json={"chat_id": chat_id, "text": reply}
                        )
                    elif cmd == "/staff":
                        if db.upsert_vehicle_whitelist(plate_norm, "staff", user_label):
                            db.update_pending_status(plate_norm, "approved_staff", user_label)
                            reply = f"✅ Đã thêm {plate_norm} vào whitelist (staff)."
                        else:
                            reply = f"⚠️ Không thể cập nhật whitelist cho {plate_norm}."
                        requests.post(
                            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                            json={"chat_id": chat_id, "text": reply}
                        )
                    elif cmd == "/reject":
                        db.update_pending_status(plate_norm, "rejected", user_label)
                        requests.post(
                            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                            json={"chat_id": chat_id, "text": f"✅ Đã từ chối {plate_norm}."}
                        )
        except: 
            pass
        time.sleep(2)

threading.Thread(target=telegram_bot_handler, daemon=True).start()

# --- KHỞI TẠO MÔ HÌNH ---
general_model = YOLO(GENERAL_MODEL_PATH)
plate_model = YOLO(PLATE_MODEL_PATH)

# PaddleOCR cho biển số VN 2 dòng
from util.ocr_utils import VNPlateOCR
plate_ocr = VNPlateOCR()
print("✅ PaddleOCR initialized for Vietnamese plates")

# Modify ocr_plate to return text and probability
def ocr_plate(image):
    text, prob = plate_ocr.read_plate_with_prob(image) # Assuming read_plate_with_prob exists or needs to be added
    return text, prob

# --- BIẾN TRẠNG THÁI ---
from core.door_controller import DoorController
from core.mqtt_manager import MQTTManager

door_controller = DoorController()
mqtt_manager = MQTTManager(door_controller)
mqtt_manager.start()
print("✅ MQTT Manager started")

# Start Telegram Bot Polling
tele_thread = threading.Thread(target=telegram_polling_loop, daemon=True)
tele_thread.start()

    # Bắt đầu API Server
threading.Thread(target=start_api_server, daemon=True).start()
    
    # Bắt đầu Monitoring Thread
threading.Thread(target=system_monitor_loop, daemon=True).start()

print("🚀 Smart Door System STARTED.")
# api_thread.start() # This line is redundant after the above change
print("✅ API Server started at http://0.0.0.0:8000/video_feed")

door_open = True
last_frame_time = time.time()
last_person_seen_time = time.time()
notification_sent = False
signal_loss_alerted = False
tracked_ids = {}

def parse_ocr_source(source):
    normalized = source.lower()
    if normalized.startswith("image:") or normalized.startswith("image="):
        image_path = source.split(":", 1)[1] if ":" in source else source.split("=", 1)[1]
        return "image", image_path.strip()
    if normalized in ("webcam", "camera", "local"):
        return "webcam", 0
    if normalized in ("rtsp", "ip", "network"):
        return "rtsp", RTSP_URL
    print(f"⚠️ OCR_SOURCE không hợp lệ: {source}. Dùng RTSP_URL mặc định.")
    return "rtsp", RTSP_URL

ocr_mode, ocr_payload = parse_ocr_source(OCR_SOURCE)
cap = None
image_frame = None
if ocr_mode == "image":
    image_frame = cv2.imread(ocr_payload)
    if image_frame is None:
        print(f"Lỗi đọc ảnh OCR: {ocr_payload}")
        exit()
else:
    cap = cv2.VideoCapture(ocr_payload)
    if not cap.isOpened():
        print("Lỗi kết nối Video.")
        exit()

notify_telegram("Hệ thống cửa cuốn thông minh đã khởi động.", important=True)

# --- MAIN LOOP ---
while True:
    if ocr_mode == "image":
        ret = True
        frame = image_frame.copy()
    else:
        ret, frame = cap.read()
    
    # Kiểm tra mất tín hiệu
    if not ret and ocr_mode != "image":
        if not signal_loss_alerted and (time.time() - last_frame_time) > SIGNAL_LOSS_TIMEOUT:
            msg = "CẢNH BÁO: Mất tín hiệu camera quá 30 giây!"
            db.log_event("SIGNAL_LOSS", msg, truck_count, person_count)
            notify_telegram(msg, important=True)
            signal_loss_alerted = True
        time.sleep(1)
        continue
    
    last_frame_time = time.time()
    signal_loss_alerted = False
    
    # 1. Nhận diện người/xe tải (YOLOv26n)
    results = general_model.track(frame, persist=True, verbose=False)
    
    # Active Learning: Lưu ảnh nếu độ tin cậy thấp
    save_active_learning = False

    for r in results:
        for bbox in r.boxes:
            if bbox.id is None:
                continue
            
            x1, y1, x2, y2 = map(int, bbox.xyxy[0])
            obj_id = int(bbox.id[0])
            cls = int(bbox.cls[0])
            center_y = (y1 + y2) // 2
            
            if obj_id in tracked_ids:
                prev_y = tracked_ids[obj_id]
                
                if prev_y < LINE_Y and center_y >= LINE_Y:
                    event_msg = ""
                    if cls == 7:  # Truck
                        truck_count += 1
                        event_msg = f"Xe tải {obj_id} đi vào kho."
                    elif cls == 0:  # Person
                        person_count += 1
                        event_msg = f"Người {obj_id} đi vào kho."
                    
                    if event_msg:
                        db.log_event("IN", event_msg, truck_count, person_count)
                        notify_telegram(event_msg)

                elif prev_y >= LINE_Y and center_y < LINE_Y:
                    event_msg = ""
                    if cls == 7:
                        truck_count = max(0, truck_count - 1)
                        person_count = max(0, person_count - 1)
                        event_msg = f"Xe tải {obj_id} đi ra. Tự động trừ 1 người."
                    elif cls == 0:
                        person_count = max(0, person_count - 1)
                        event_msg = f"Người {obj_id} đi ra."
                    
                    if event_msg:
                        db.log_event("OUT", event_msg, truck_count, person_count)
                        notify_telegram(event_msg)
            
            tracked_ids[obj_id] = center_y
            
            if cls == 0:
                last_person_seen_time = time.time()
                notification_sent = False

    # 2. Nhận diện khuôn mặt (mỗi 30 frame để tiết kiệm CPU)
    if FACE_RECOGNITION_AVAILABLE and int(time.time()) % 2 == 0:
        name, loc = check_face(frame)
        if name == "STRANGER":
            # Lưu ảnh tạm để duyệt
            face_id = str(int(time.time()))
            temp_dir = "./config/faces/temp"
            os.makedirs(temp_dir, exist_ok=True)
            temp_path = os.path.join(temp_dir, f"{face_id}.jpg")
            
            # Crop khuôn mặt
            top, right, bottom, left = loc
            face_img = frame[top:bottom, left:right]
            if face_img.size > 0:
                cv2.imwrite(temp_path, face_img)
                
                msg = f"Người lạ phát hiện! ID: `{face_id}`\nDuyệt: `/staff_face {face_id} Ten_Nhan_Vien`"
                db.log_event("STRANGER", msg, truck_count, person_count)
                
                # Gửi ảnh qua API sendPhoto
                try:
                    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
                    with open(temp_path, "rb") as f:
                        requests.post(url, data={"chat_id": CHAT_REGULAR, "caption": msg}, files={"photo": f})
                except Exception as e:
                    print(f"Lỗi gửi ảnh Telegram: {e}")
                    notify_telegram(msg, important=True)
                    
        elif name:
            cv2.putText(frame, name, (loc[3], loc[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

    # 3. Nhận diện biển số (plate_model + PaddleOCR)
    plate_results = plate_model(frame, verbose=False)
    for pr in plate_results:
        for pbox in pr.boxes:
            px1, py1, px2, py2 = map(int, pbox.xyxy[0])
            cls = int(pbox.cls[0]) # Extract class for plate detection
            # OCR Biển số
            if cls == 1: # Giả sử class 1 là license_plate
                plate_crop = frame[py1:py2, px1:px2]
                if plate_crop.size > 0:
                    plate_text, prob = ocr_plate(plate_crop)
                    
                    # Nếu nhận diện biển số với độ tin cậy thấp (< 0.7) -> Lưu Active Learning
                    if prob < 0.7 and plate_text:
                        save_path = f"./data/active_learning/plate_{int(time.time())}.jpg"
                        os.makedirs("./data/active_learning", exist_ok=True)
                        cv2.imwrite(save_path, plate_crop)
                        print(f"📀 Saved Active Learning sample: {plate_text} ({prob:.2f})")
                if plate_text:
                    plate_norm = normalize_plate(plate_text)
                    if plate_norm:
                        is_auth, matched = check_plate(plate_text)
                        is_whitelisted = is_auth or db.is_plate_whitelisted(plate_norm)
                        if not is_whitelisted:
                            msg = f"Xe lạ phát hiện: {plate_norm}"
                            event_id = db.log_event("UNKNOWN_PLATE", msg, truck_count, person_count)
                            pending_id = str(uuid.uuid4())
                            db.add_pending_plate(
                                pending_id=pending_id,
                                event_id=event_id,
                                plate_raw=plate_text,
                                plate_norm=plate_norm,
                                first_seen_utc=datetime.utcnow().isoformat()
                            )
                            notify_telegram(
                                f"{msg}\nXác nhận:\n/mine {plate_norm}\n/staff {plate_norm}\n/reject {plate_norm}",
                                important=False
                            )
                        else:
                            # KNOWN PLATE -> TRIGGER TUYA OPEN
                            print(f"✅ Xe quen: {plate_norm} -> MỞ CỬA")
                            mqtt_manager.publish_trigger_open()
                            cv2.putText(frame, "OPENING DOOR...", (px1, py1 - 30), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                    cv2.putText(frame, plate_text, (px1, py1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 255), 2)
                cv2.rectangle(frame, (px1, py1), (px2, py2), (255, 0, 255), 2)

    # 4. Kiểm tra trạng thái cửa cuốn (mỗi giây)
    current_door_state = check_door_state(frame)
    if current_door_state != 'unknown':
        new_door_open = (current_door_state == 'open')
        
        # Phát hiện thay đổi trạng thái
        if new_door_open != door_open:
            door_open = new_door_open
            state_msg = "Cửa cuốn đã MỞ." if door_open else "Cửa cuốn đã ĐÓNG."
            db.log_event("DOOR_STATE", state_msg, truck_count, person_count)
            notify_telegram(state_msg)
    
    # 5. Cảnh báo cửa mở quá 5 phút không có người
    if door_open and person_count == 0:
        if (time.time() - last_person_seen_time) / 60 > 5 and not notification_sent:
            msg = "CẢNH BÁO: Cửa mở nhưng không có người quá 5 phút!"
            db.log_event("ALERT", msg, truck_count, person_count)
            notify_telegram(msg, important=True)
            notification_sent = True

    # MQTT Update
    mqtt_manager.publish_state(person_count, truck_count, door_open)

    # GUI
    door_status = "🔓 MỞ" if door_open else "🔒 ĐÓNG"
    cv2.line(frame, (0, LINE_Y), (frame.shape[1], LINE_Y), (0, 0, 255), 2)
    cv2.putText(frame, f"T:{truck_count} P:{person_count} | {door_status}", (10, 40), 1, 2, (0, 0, 255), 2)
    # Vẽ ROI cửa cuốn
    x1, y1, x2, y2 = DOOR_ROI
    cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 255), 2)
    cv2.putText(frame, "DOOR ROI", (x1, y1 - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1)
    # Cập nhật Streamer
    streamer.update_frame(frame)
    
    cv2.imshow("Smart Door System", frame)
    if (cv2.waitKey(1) & 0xFF) == ord(" "):
        break
    if ocr_mode == "image":
        break

if cap is not None:
    cap.release()
cv2.destroyAllWindows()
