import os
import cv2
from core.config import FACES_DIR, FACE_RECOGNITION_AVAILABLE

if FACE_RECOGNITION_AVAILABLE:
    import face_recognition

# --- Dữ liệu khuôn mặt ---
authorized_face_encodings = []
authorized_face_names = []


def load_faces():
    """Load/Reload danh sách khuôn mặt từ thư mục config/faces."""
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
    print(f"✅ Loaded {len(authorized_face_names)} authorized faces: {authorized_face_names}")


def check_face(frame):
    """Nhận diện khuôn mặt và kiểm tra trong danh sách ủy quyền."""
    if not FACE_RECOGNITION_AVAILABLE or not authorized_face_encodings:
        return None, None

    rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    face_locations = face_recognition.face_locations(rgb_frame)
    face_encs = face_recognition.face_encodings(rgb_frame, face_locations)

    for face_enc, loc in zip(face_encs, face_locations):
        matches = face_recognition.compare_faces(authorized_face_encodings, face_enc, tolerance=0.6)
        if True in matches:
            name = authorized_face_names[matches.index(True)]
            return name, loc
        else:
            return "STRANGER", loc
    return None, None


def check_plate(plate_text, authorized_plates):
    """Kiểm tra biển số xe có trong danh sách ủy quyền không."""
    normalized = plate_text.upper().replace(" ", "").replace("-", "")
    for auth_plate in authorized_plates:
        if auth_plate.replace("-", "") in normalized or normalized in auth_plate.replace("-", ""):
            return True, auth_plate
    return False, None


# Load danh sách khuôn mặt lúc khởi tạo
load_faces()
