from ultralytics import YOLO
import cv2
import easyocr
import logging
logging.getLogger("ultralytics").setLevel(logging.WARNING)

# Load EasyOCR
ocrReader = easyocr.Reader(['en','vi'], gpu=False)

# Load model YOLO
license_plate_recognition = YOLO("./models/license_plate_recognition.pt")

# Dùng webcam (0 là webcam mặc định)
cap = cv2.VideoCapture(0)

ret = True
print("Chương trình đang chạy, bấm P để in biển số hoặc Space để kết thúc chương trình !")
current_plates = []  # Biến lưu biển số nhận diện được trong frame hiện tại
while ret:
    ret, frame = cap.read()
    if not ret:
        break

    current_plates = []  # Xóa danh sách cũ mỗi khung hình

    license_plates = license_plate_recognition.track(frame, persist=True)

    for plates in license_plates:
        for bbox in plates.boxes:
            x1, y1, x2, y2 = map(int, bbox.xyxy[0])
            plate_img = frame[y1:y2, x1:x2]

            text = ocrReader.readtext(plate_img, detail=0)
            text_str = " ".join(text).strip()
            current_plates.append(text_str)

            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 255), 2)
            cv2.putText(frame, text_str, (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX,
                        0.9, (255, 255, 255), 2)

    cv2.imshow("License Plates Recognition", frame)

    key = cv2.waitKey(1) & 0xFF

    if key == ord(" "):  # Nhấn Space để thoát
        break
    elif key == ord("p"):  # Nhấn phím P để in biển số ra Terminal
        print("\nBiển số xe nhận diện được trong khung hình hiện tại:")
        if current_plates:
            for i, plate in enumerate(current_plates, 1):
                print(f"{i}. {plate}")
        else:
            print("Không nhận diện được biển số nào.")

cap.release()
cv2.destroyAllWindows()
