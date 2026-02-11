"""
Smart Door System - Main Entry Point
Chỉ chứa main loop nhận diện. Tất cả logic đã tách vào core/ và services/.
"""
import cv2
import os
import time
import threading
import uuid
from datetime import datetime
from ultralytics import YOLO

# --- Core ---
from core.config import (
    GENERAL_MODEL_PATH, PLATE_MODEL_PATH, LINE_Y, RTSP_URL, OCR_SOURCE,
    SIGNAL_LOSS_TIMEOUT, DOOR_ROI, FACE_RECOGNITION_AVAILABLE,
    authorized_plates, normalize_plate, DB_PATH
)
from core.database import DatabaseManager
from core.door_controller import DoorController
from core.mqtt_manager import MQTTManager
from core.mjpeg_streamer import MJPEGStreamer

# --- Services ---
from services.telegram_service import notify_telegram, start_telegram_threads
from services.face_service import load_faces, check_face, check_plate
from services.door_service import check_door_state
from services.system_monitor import get_cpu_temp, system_monitor_loop
from services.api_server import start_api_server

# ========== KHỞI TẠO ==========
db = DatabaseManager(DB_PATH)
door_controller = DoorController()
mqtt_manager = MQTTManager(door_controller)
mqtt_manager.start()
print("✅ MQTT Manager started")

streamer = MJPEGStreamer()

# --- Trạng thái toàn cục ---
truck_count = 0
person_count = 0
door_open = True


def get_state():
    """Trả về trạng thái hiện tại cho API và Telegram."""
    return person_count, truck_count, door_open


def get_counts():
    """Trả về số lượng cho Telegram."""
    return truck_count, person_count


# --- Khởi chạy threads ---
start_telegram_threads(db, load_faces, mqtt_manager, get_cpu_temp, get_counts)
threading.Thread(target=start_api_server, args=(streamer, get_state, mqtt_manager), daemon=True).start()
threading.Thread(target=system_monitor_loop, daemon=True).start()

print("🚀 Smart Door System STARTED.")
print("✅ API Server started at http://0.0.0.0:8000/video_feed")

# --- Khởi tạo mô hình YOLO ---
general_model = YOLO(GENERAL_MODEL_PATH)
plate_model = YOLO(PLATE_MODEL_PATH)

# --- PaddleOCR ---
from util.ocr_utils import VNPlateOCR
plate_ocr = VNPlateOCR()
print("✅ PaddleOCR initialized for Vietnamese plates")


def ocr_plate(image):
    text, prob = plate_ocr.read_plate_with_prob(image)
    return text, prob


# --- Parse OCR source ---
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

# --- Biến trạng thái main loop ---
last_frame_time = time.time()
last_person_seen_time = time.time()
notification_sent = False
signal_loss_alerted = False
tracked_ids = {}

# ========== MAIN LOOP ==========
while True:
    if ocr_mode == "image":
        ret = True
        frame = image_frame.copy()
    else:
        ret, frame = cap.read()

    # Kiểm tra mất tín hiệu
    if not ret and ocr_mode != "image":
        if not signal_loss_alerted and (time.time() - last_frame_time) > SIGNAL_LOSS_TIMEOUT:
            msg = "CẢNH BÁO: Mất tín hiệu camera!"
            db.log_event("SIGNAL_LOSS", msg, truck_count, person_count)
            notify_telegram(msg, important=True)
            signal_loss_alerted = True
        time.sleep(1)
        continue

    signal_loss_alerted = False
    last_frame_time = time.time()

    # 1. Nhận diện người/xe tải (YOLO tracking)
    results = general_model.track(frame, persist=True, verbose=False)

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

    # 2. Nhận diện khuôn mặt (mỗi 2 giây)
    if FACE_RECOGNITION_AVAILABLE and int(time.time()) % 2 == 0:
        name, loc = check_face(frame)
        if name == "STRANGER":
            face_id = str(int(time.time()))
            temp_dir = "./config/faces/temp"
            os.makedirs(temp_dir, exist_ok=True)
            temp_path = os.path.join(temp_dir, f"{face_id}.jpg")

            top, right, bottom, left = loc
            face_img = frame[top:bottom, left:right]
            if face_img.size > 0:
                cv2.imwrite(temp_path, face_img)

                msg = f"Người lạ phát hiện! ID: `{face_id}`\nDuyệt: `/staff_face {face_id} Ten_Nhan_Vien`"
                db.log_event("STRANGER", msg, truck_count, person_count)

                try:
                    from core.config import TOKEN, CHAT_REGULAR
                    url = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
                    with open(temp_path, "rb") as f:
                        import requests
                        requests.post(url, data={"chat_id": CHAT_REGULAR, "caption": msg}, files={"photo": f})
                except Exception as e:
                    print(f"Lỗi gửi ảnh Telegram: {e}")
                    notify_telegram(msg, important=True)

        elif name:
            cv2.putText(frame, name, (loc[3], loc[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

    # 3. Nhận diện biển số (chỉ chạy nếu OCR được bật)
    if mqtt_manager.ocr_enabled:
        plate_results = plate_model(frame, verbose=False)
        for pr in plate_results:
            for pbox in pr.boxes:
                px1, py1, px2, py2 = map(int, pbox.xyxy[0])
                cls = int(pbox.cls[0])
                if cls == 1:  # license_plate
                    plate_crop = frame[py1:py2, px1:px2]
                    if plate_crop.size > 0:
                        plate_text, prob = ocr_plate(plate_crop)

                        if prob < 0.7 and plate_text:
                            save_path = f"./data/active_learning/plate_{int(time.time())}.jpg"
                            os.makedirs("./data/active_learning", exist_ok=True)
                            cv2.imwrite(save_path, plate_crop)
                            print(f"📀 Saved Active Learning sample: {plate_text} ({prob:.2f})")

                    if plate_text:
                        plate_norm = normalize_plate(plate_text)
                        if plate_norm:
                            is_auth, matched = check_plate(plate_text, authorized_plates)
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
                                print(f"✅ Xe quen: {plate_norm} -> MỞ CỬA")
                                mqtt_manager.publish_trigger_open()
                                cv2.putText(frame, "OPENING DOOR...", (px1, py1 - 30),
                                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                        cv2.putText(frame, plate_text, (px1, py1 - 10),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 200, 255), 2)
                    cv2.rectangle(frame, (px1, py1), (px2, py2), (255, 0, 255), 2)

    # 4. Kiểm tra trạng thái cửa cuốn
    current_door_state = check_door_state(frame)
    if current_door_state != 'unknown':
        new_door_open = (current_door_state == 'open')

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
