import sys
import os
from ultralytics import YOLO

def export_model(model_path, format="onnx"):
    """
    Xuất model YOLO sang các định dạng tối ưu.
    Supported formats: onnx, ncnn, openvino, engine, coreml, torchscript
    """
    if not os.path.exists(model_path):
        print(f"❌ Không tìm thấy file: {model_path}")
        return

    print(f"🚀 Đang xuất {model_path} sang định dạng {format}...")
    try:
        model = YOLO(model_path)
        # Tối ưu hóa: imgsz=640, half=True (cho GPU), simplify=True
        path = model.export(format=format, imgsz=640, simplify=True)
        print(f"✅ Thành công! File đã lưu tại: {path}")
    except Exception as e:
        print(f"❌ Lỗi khi xuất model: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Sử dụng: python export_model.py <path_to_model.pt> [format]")
        print("Ví dụ: python export_model.py models/bien_so_xe.pt onnx")
        sys.exit(1)
    
    m_path = sys.argv[1]
    fmt = sys.argv[2] if len(sys.argv) > 2 else "onnx"
    export_model(m_path, fmt)
