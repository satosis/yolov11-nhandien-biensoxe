# Hướng Dẫn Train Model Nhận Diện Biển Số & Người (YOLOv8)

Tài liệu này hướng dẫn bạn cách tự train (huấn luyện) lại model `bien_so_xe.pt` để nó hiểu và nhận diện được **Biển số xe Việt Nam** và **Người**.

## 1. Chuẩn Bị Dữ Liệu (Quan trọng nhất)
Model AI chỉ thông minh khi được học dữ liệu tốt.

### Bước 1: Thu thập hình ảnh
Bạn cần **100-500 ảnh** chụp thực tế từ góc camera của bạn:
- Ảnh xe ô tô/xe máy có biển số rõ nét.
- Ảnh người đi bộ.
- Chụp ở nhiều điều kiện sáng: Sáng, Tối, Mưa.

### Bước 2: Gán nhãn (Labeling)
Dùng công cụ **Roboflow** (trên web) hoặc **LabelImg** (cài trên máy tính) để vẽ khung chữ nhật quanh đối tượng.
- Class model cần học:
  - `license_plate`: Vẽ hình chữ nhật ôm sát biển số xe.
  - `person`: Vẽ quanh người (nếu dùng model gốc thì không cần train lại cái này, nhưng nếu train chung thì nên gán nhãn lại).

### Bước 3: Xuất dữ liệu (Export Dataset)
Chọn định dạng **YOLOv8** khi export. Cấu trúc thư mục sẽ như sau:
```
dataset/
├── data.yaml  (File cấu hình class)
├── train/
│   ├── images/ (Chứa ảnh train)
│   └── labels/ (Chứa file .txt tọa độ)
└── val/
    ├── images/ (Chứa ảnh test)
    └── labels/
```

## 2. Cấu Hình Train
Tạo file `data.yaml` với nội dung (Roboflow thường tự tạo file này):

```yaml
train: ../train/images
val: ../val/images

nc: 2
names: ['person', 'license_plate']
```

## 3. Thực Hiện Train (Trên máy tính mạnh hoặc Google Colab)
**KHÔNG NÊN** train trên Orange Pi vì rất chậm. Hãy dùng Laptop có GPU rời hoặc Google Colab miễn phí.

### Cách 1: Train bằng Python Script
Tạo file `train.py`:
```python
from ultralytics import YOLO

# Load model gốc (nano)
model = YOLO('yolov8n.pt') 

# Bắt đầu train
results = model.train(
    data='path/to/dataset/data.yaml',
    epochs=100,      # Số vòng lặp (càng nhiều càng kĩ nhưng lâu)
    imgsz=640,       # Kích thước ảnh
    batch=16,
    name='bien_so_xe_custom' # Tên folder kết quả
)
```

### Cách 2: Train bằng Dòng lệnh (Terminal)
```bash
yolo task=detect mode=train model=yolov8n.pt data=dataset/data.yaml epochs=100 imgsz=640
```

## 4. Kết Quả & Sử Dụng
Sau khi chạy xong (khoảng 1-2 tiếng), file model tốt nhất sẽ nằm ở:
`runs/detect/bien_so_xe_custom/weights/best.pt`

### 5. Cập nhật vào hệ thống
1. Đổi tên file `best.pt` thành `bien_so_xe.pt`.
2. Copy đè vào thư mục `models/` trên Orange Pi.
3. Khởi động lại hệ thống (`python main.py`).

---
**Mẹo:** Để tiết kiệm thời gian, bạn có thể tải dataset biển số có sẵn trên Roboflow Universe (từ khóa "Vietnam License Plate") về train lại (Fine-tune).
