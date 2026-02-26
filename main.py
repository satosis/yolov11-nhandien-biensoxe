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
    authorized_plates, normalize_plate, DB_PATH,
    CAMERA_SHIFT_CHECK_EVERY_FRAMES,
    CAMERA_SHIFT_MIN_INLIER_RATIO,
    CAMERA_SHIFT_MAX_ROTATION_DEG,
    CAMERA_SHIFT_MAX_TRANSLATION_PX,
    CAMERA_SHIFT_MAX_SCALE_DELTA,
    CAMERA_SHIFT_ALERT_CONSECUTIVE,
    PROCESS_WIDTH, STREAM_WIDTH, STREAM_FPS, STREAM_JPEG_QUALITY,
    GENERAL_DETECT_IMGSZ, GENERAL_DETECT_CONF, PLATE_DETECT_EVERY_N_FRAMES,
)
from core.database import DatabaseManager
from core.door_controller import DoorController
from core.mqtt_manager import MQTTManager
from core.mjpeg_streamer import MJPEGStreamer
from core.camera_orientation_monitor import CameraOrientationMonitor

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

streamer = MJPEGStreamer(stream_width=STREAM_WIDTH, fps=STREAM_FPS, jpeg_quality=STREAM_JPEG_QUALITY)

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


def _resolve_class_ids(model):
    """Tìm class id cho person/xe từ model names để tránh hard-code sai model."""
    names = getattr(model, "names", {}) or {}
    person_ids = set()
    vehicle_ids = set()

    person_aliases = {"person", "nguoi", "người"}
    vehicle_aliases = {
        "truck",
        "car",
        "vehicle",
        "van",
        "bus",
        "motorcycle",
        "motorbike",
        "bike",
        "bicycle",
        "xe",
        "xe_tai",
        "xe tai",
        "oto",
        "ô tô",
    }

    for idx, raw_name in names.items():
        label = str(raw_name).strip().lower()
        if label in person_aliases:
            person_ids.add(int(idx))
        if label in vehicle_aliases or label.startswith("xe"):
            vehicle_ids.add(int(idx))

    # fallback cho model COCO nếu names không có/khớp như kỳ vọng
    if not person_ids:
        person_ids.add(0)
    if not vehicle_ids:
        coco_vehicle_ids = {1, 2, 3, 5, 7}
        vehicle_ids = {idx for idx in coco_vehicle_ids if idx < len(names)} or {7}

    return person_ids, vehicle_ids


PERSON_CLASS_IDS, VEHICLE_CLASS_IDS = _resolve_class_ids(general_model)
print(f"ℹ️ person class ids: {sorted(PERSON_CLASS_IDS)} | vehicle class ids: {sorted(VEHICLE_CLASS_IDS)}")

# --- PaddleOCR ---
from util.ocr_utils import VNPlateOCR
plate_ocr = VNPlateOCR()
print("✅ PaddleOCR initialized for Vietnamese plates")


def ocr_plate(image):
    text, prob = plate_ocr.read_plate_with_prob(image)
    return text, prob


# --- Parse OCR source ---


def resize_for_process(frame, target_width):
    if target_width <= 0 or frame.shape[1] <= target_width:
        return frame
    ratio = target_width / float(frame.shape[1])
    new_h = max(1, int(frame.shape[0] * ratio))
    return cv2.resize(frame, (target_width, new_h), interpolation=cv2.INTER_AREA)


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

# Màu hiển thị vùng nhận diện theo yêu cầu vận hành
PERSON_BOX_COLOR = (0, 255, 255)  # vàng
VEHICLE_BOX_COLOR = (255, 0, 0)   # xanh dương

frame_count = 0
camera_shift_alerted = False
camera_monitor = CameraOrientationMonitor(
    check_every_n_frames=CAMERA_SHIFT_CHECK_EVERY_FRAMES,
    min_inlier_ratio=CAMERA_SHIFT_MIN_INLIER_RATIO,
    max_rotation_deg=CAMERA_SHIFT_MAX_ROTATION_DEG,
    max_translation_px=CAMERA_SHIFT_MAX_TRANSLATION_PX,
    max_scale_delta=CAMERA_SHIFT_MAX_SCALE_DELTA,
    required_consecutive_alerts=CAMERA_SHIFT_ALERT_CONSECUTIVE,
)
camera_baseline_ready = False

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
    frame_count += 1

    frame = resize_for_process(frame, PROCESS_WIDTH)

    # 0. Giám sát camera có lệch khỏi góc ban đầu hay không
    if not camera_baseline_ready:
        camera_baseline_ready = camera_monitor.set_baseline(frame)
        if camera_baseline_ready:
            print("✅ Camera baseline đã được chụp để theo dõi lệch góc.")
    else:
        shift_result = camera_monitor.evaluate(frame)
        if shift_result is not None:
            if shift_result.is_shifted and not camera_shift_alerted:
                camera_shift_alerted = True
                msg = (
                    "🚨 CẢNH BÁO: Camera có dấu hiệu lệch góc khỏi vị trí ban đầu "
                    f"(rot={shift_result.rotation_deg:.2f}°, "
                    f"trans={shift_result.translation_px:.1f}px, "
                    f"inlier={shift_result.inlier_ratio:.2f})."
                )
                print(msg)
                db.log_event("CAMERA_SHIFT", msg, truck_count, person_count)
                notify_telegram(msg, important=True)
            elif not shift_result.is_shifted and camera_shift_alerted:
                camera_shift_alerted = False
                msg = "✅ Camera đã quay lại gần góc ban đầu."
                print(msg)
                db.log_event("CAMERA_SHIFT_RECOVERED", msg, truck_count, person_count)
                notify_telegram(msg)

    # 1. Nhận diện người/xe (YOLO tracking)
    results = general_model.track(frame, persist=True, verbose=False, imgsz=GENERAL_DETECT_IMGSZ, conf=GENERAL_DETECT_CONF)

    save_active_learning = False

    for r in results:
        for bbox in r.boxes:
            x1, y1, x2, y2 = map(int, bbox.xyxy[0])
            obj_id = int(bbox.id[0]) if bbox.id is not None else None
            cls = int(bbox.cls[0])
            center_y = (y1 + y2) // 2
            is_person = cls in PERSON_CLASS_IDS
            is_vehicle = cls in VEHICLE_CLASS_IDS

            crossed_red_line = False
            if obj_id is not None and obj_id in tracked_ids:
                prev_y = tracked_ids[obj_id]

                if prev_y < LINE_Y and center_y >= LINE_Y:
                    event_msg = ""
                    crossed_red_line = True
                    if is_vehicle:
                        truck_count += 1
                        event_msg = f"Xe {obj_id} đi vào kho."
                    elif is_person:
                        person_count += 1
                        event_msg = f"Người {obj_id} đi vào kho."

                    if event_msg:
                        db.log_event("IN", event_msg, truck_count, person_count)
                        notify_telegram(event_msg)

                elif prev_y >= LINE_Y and center_y < LINE_Y:
                    event_msg = ""
                    crossed_red_line = True
                    if is_vehicle:
                        truck_count = max(0, truck_count - 1)
                        person_count = max(0, person_count - 1)
                        event_msg = f"Xe {obj_id} đi ra. Tự động trừ 1 người."
                    elif is_person:
                        person_count = max(0, person_count - 1)
                        event_msg = f"Người {obj_id} đi ra."

                    if event_msg:
                        db.log_event("OUT", event_msg, truck_count, person_count)
                        notify_telegram(event_msg)

            if obj_id is not None:
                tracked_ids[obj_id] = center_y

            if is_person:
                last_person_seen_time = time.time()
                notification_sent = False

            # Hiển thị label vùng nhận diện: người vàng, xe xanh
            if is_person or is_vehicle:
                box_color = PERSON_BOX_COLOR if is_person else VEHICLE_BOX_COLOR
                label_name = "NGUOI" if is_person else "XE"
                if crossed_red_line:
                    label_name += " QUA VACH DO"
                display_id = obj_id if obj_id is not None else "NA"
                cv2.rectangle(frame, (x1, y1), (x2, y2), box_color, 2)
                cv2.putText(
                    frame,
                    f"{label_name} #{display_id}",
                    (x1, max(20, y1 - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.55,
                    box_color,
                    2,
                )

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
    if mqtt_manager.ocr_enabled and frame_count % max(1, PLATE_DETECT_EVERY_N_FRAMES) == 0:
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
                                cv2.putText(frame, "BIEN SO HOP LE - MO CUA!", (px1, py1 - 30),
                                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                        cv2.putText(frame, f"BS: {plate_text}", (px1, py1 - 10),
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
    cv2.line(frame, (0, LINE_Y), (frame.shape[1], LINE_Y), (0, 0, 255), 5)
    cv2.putText(frame, f"Qua vach do - Xe: {truck_count} | Nguoi: {person_count}", (10, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.85, (0, 0, 255), 2)
    cv2.putText(frame, f"Cua: {door_status}", (10, 80), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 3)
    
    # Cập nhật thông tin thời gian thực
    now_str = datetime.now().strftime("%H:%M:%S - %d/%m/%Y")
    cv2.putText(frame, now_str, (frame.shape[1] - 380, 40), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)

    # Cập nhật Streamer
    streamer.update_frame(frame)

    # Disable GUI for headless Linux servers
    # cv2.imshow("Smart Door System", frame)
    # if (cv2.waitKey(1) & 0xFF) == ord(" "):
    #     break
    if ocr_mode == "image":
        break

if cap is not None:
    cap.release()
cv2.destroyAllWindows()
