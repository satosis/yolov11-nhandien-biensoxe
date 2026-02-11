import numpy as np
import cv2
from ultralytics import YOLO
from core.config import DOOR_MODEL_PATH, USE_AI_DOOR_DETECTION, DOOR_ROI, BRIGHTNESS_THRESHOLD

# --- Load door model ---
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
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)

    if x2 > x1 and y2 > y1:
        roi = frame[y1:y2, x1:x2]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        brightness = np.mean(gray)

        if brightness > BRIGHTNESS_THRESHOLD:
            return 'open'
        else:
            return 'closed'

    return 'unknown'
