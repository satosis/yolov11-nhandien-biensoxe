FROM python:3.10-slim

WORKDIR /app

# Cài đặt thư viện hệ thống cần thiết cho OpenCV, dlib và glib
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    build-essential \
    cmake \
    libopenblas-dev \
    liblapack-dev \
    libx11-dev \
    libgtk-3-dev \
    && rm -rf /var/lib/apt/lists/*

# Cài đặt các thư viện AI cốt lõi (Bỏ qua OPi.GPIO và dlib vì build trên Docker ARM rất lỗi)
RUN pip install --no-cache-dir \
    ultralytics==8.3.160 \
    opencv-python-headless \
    paddlepaddle>=2.6.0 \
    paddleocr>=2.7.0 \
    python-dotenv \
    requests \
    fastapi==0.115.0 \
    uvicorn[standard]==0.30.6 \
    paho-mqtt==2.1.0 \
    pandas>=2.0 \
    onnx==1.17.0 \
    onnxscript>=0.1.0

# (Tùy chọn) Cài face_recognition nếu cần, nhưng tạm thời bỏ qua để build nhanh không lỗi dlib
# RUN pip install face_recognition

# Lệnh mặc định, chạy main.py
CMD ["python", "-u", "main.py"]
