# Được sử dụng cho đọc hình ảnh
from ultralytics import YOLO
import cv2
import easyocr
import logging
logging.getLogger("ultralytics").setLevel(logging.WARNING)

# Load EasyOCR
# Tùy chọn: Nếu bạn không có GPU, hãy giữ gpu=False.
# Nếu có GPU và muốn tăng tốc, hãy thử gpu=True (yêu cầu cài đặt CUDA và cuDNN).
ocrReader = easyocr.Reader(['en','vi'], gpu=False)

# Load model YOLO
license_plate_recognition = YOLO("./models/license_plate_recognition.pt")

# --- CẤU HÌNH ĐỂ ĐỌC ẢNH ---
img_path = "./bien-so-xe.jpg"  # Đường dẫn đến image của bạn
frame = cv2.imread(img_path)   # Đọc ảnh trực tiếp vào biến 'frame'

# Kiểm tra xem ảnh có được đọc thành công không
if frame is None:
    print(f"Lỗi: Không thể đọc ảnh từ đường dẫn '{img_path}'. Vui lòng kiểm tra lại đường dẫn và file.")
    exit() # Thoát chương trình nếu không đọc được ảnh

print("Chương trình đang chạy, bấm P để in biển số hoặc Space để kết thúc chương trình !")
current_plates = []  # Biến lưu biển số nhận diện được trong frame hiện tại

# --- Phần xử lý ảnh tĩnh (chỉ chạy một lần) ---
current_plates = []  # Xóa danh sách cũ

# Thực hiện nhận diện biển số bằng YOLO
# Lưu ý: .track() thường dùng cho video để theo dõi ID của vật thể.
# Với ảnh tĩnh, bạn có thể dùng .predict() nếu không cần theo dõi ID.
# Tuy nhiên, .track() vẫn sẽ hoạt động và trả về các box.
license_plates = license_plate_recognition.track(frame, persist=False) # persist=False cho ảnh tĩnh

for plates in license_plates: # Duyệt qua các kết quả từ YOLO
    for bbox in plates.boxes: # Duyệt qua từng bounding box (biển số)
        # Lấy tọa độ bounding box
        # bbox.xyxy[0] trả về [x1, y1, x2, y2] cho một đối tượng
        x1, y1, x2, y2 = map(int, bbox.xyxy[0])

        # Cắt phần biển số từ khung hình gốc
        # Đảm bảo tọa độ cắt không vượt ra ngoài kích thước ảnh
        plate_img = frame[max(0, y1):min(frame.shape[0], y2), max(0, x1):min(frame.shape[1], x2)]

        # Đảm bảo plate_img không rỗng trước khi truyền vào EasyOCR
        if plate_img.shape[0] > 0 and plate_img.shape[1] > 0:
            # Thực hiện nhận diện ký tự (OCR) trên ảnh biển số đã cắt
            text = ocrReader.readtext(plate_img, detail=0) # detail=0 chỉ lấy chuỗi ký tự
            text_str = " ".join(text).strip() # Ghép các phần text lại thành một chuỗi
            current_plates.append(text_str) # Thêm biển số vào danh sách

            # Vẽ hình chữ nhật xung quanh biển số
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 255, 255), 2) # Màu vàng, độ dày 2

            # Ghi text biển số lên ảnh
            # Đảm bảo text không bị ghi ra ngoài ảnh và font size phù hợp
            cv2.putText(frame, text_str, (x1, y1 - 10 if y1 - 10 > 10 else y1 + 20), cv2.FONT_HERSHEY_SIMPLEX,
                        0.9, (255, 255, 255), 2) # Màu trắng, độ dày 2

# Hiển thị kết quả
cv2.imshow("License Plates Recognition", frame)

# --- Phần điều khiển tương tác (Chỉ chờ phím bấm một lần cho ảnh tĩnh) ---
while True: # Vòng lặp để chờ người dùng nhấn P hoặc Space
    key = cv2.waitKey(0) & 0xFF # Chờ vô thời hạn cho đến khi có phím nhấn

    if key == ord(" "):  # Nhấn Space để thoát
        break
    elif key == ord("p"):  # Nhấn phím P để in biển số ra Terminal
        print("\nBiển số xe nhận diện được trong khung hình hiện tại:")
        if current_plates:
            for i, plate in enumerate(current_plates, 1):
                print(f"{i}. {plate}")
        else:
            print("Không nhận diện được biển số nào.")
    # Nếu muốn thoát khi nhấn bất kỳ phím nào khác 'p' hoặc ' ', bỏ dòng break dưới đây
    # break # Thoát khỏi vòng lặp nếu không phải 'p' hoặc ' ' (ví dụ: nhấn Enter)

# Không cần cap.release() vì không dùng VideoCapture
cv2.destroyAllWindows()
print("Đã đóng chương trình.")
