# =========================================================
# 🚪 TRAIN DOOR STATE DETECTION MODEL (YOLOv26)
# Chạy trên Google Colab để sử dụng GPU miễn phí
# =========================================================

# --- BƯỚC 1: CÀI ĐẶT THƯ VIỆN ---
# !pip install ultralytics roboflow

# --- BƯỚC 2: TẢI DATASET TỪ ROBOFLOW ---
# Truy cập https://app.roboflow.com để tạo dataset
# Chọn YOLOv8 format khi export

from roboflow import Roboflow

# Thay thế bằng API key và project của bạn
# rf = Roboflow(api_key="YOUR_API_KEY")
# project = rf.workspace("YOUR_WORKSPACE").project("door-detection")
# dataset = project.version(1).download("yolov8")

# --- BƯỚC 3: TẠO FILE CẤU HÌNH (nếu tự annotate) ---
# Tạo file door_dataset.yaml với nội dung:
"""
path: /content/door_dataset
train: images/train
val: images/val

names:
  0: door_open
  1: door_closed
"""

# --- BƯỚC 4: TRAIN MODEL ---
from ultralytics import YOLO

# Tải model pretrained
model = YOLO("yolov8n.pt")  # hoặc yolov26n.pt nếu có sẵn

# Train với dataset của bạn
# model.train(
#     data="door_dataset.yaml",  # Hoặc đường dẫn từ Roboflow
#     epochs=100,
#     imgsz=640,
#     batch=16,
#     name="door_detector"
# )

# --- BƯỚC 5: EXPORT MODEL ---
# model.export(format="onnx")  # Cho Orange Pi (nhẹ hơn)
# Hoặc lưu file .pt về máy:
# !cp runs/detect/door_detector/weights/best.pt /content/drive/MyDrive/door_model.pt

# =========================================================
# HƯỚNG DẪN SỬ DỤNG NHANH:
# =========================================================
# 1. Thu thập ảnh cửa cuốn trong 2 trạng thái (mở/đóng)
#    - Ít nhất 100 ảnh mỗi trạng thái
#    - Chụp ở nhiều góc độ và điều kiện ánh sáng
#
# 2. Upload lên Roboflow (roboflow.com) -> Tạo project mới
#    - Chọn "Object Detection"
#    - Annotate các ảnh với labels: "door_open", "door_closed"
#    - Export với format "YOLOv8"
#
# 3. Chạy script này trên Google Colab:
#    - Uncomment các dòng code ở trên
#    - Thay thế API key của bạn
#    - Chạy training (khoảng 30-60 phút)
#
# 4. Tải model về và đặt vào thư mục models/
#    - Đổi tên thành: door_model.pt
#
# 5. Cập nhật main.py để sử dụng door_model.pt
# =========================================================

print("Script training cho Door Detection đã sẵn sàng!")
print("Xem hướng dẫn chi tiết ở phần comment bên trên.")
