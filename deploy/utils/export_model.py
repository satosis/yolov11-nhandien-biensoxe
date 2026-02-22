import importlib.util
import os
import sys

DEFAULT_ONNX_OPSET = 18


def ensure_onnx_requirements() -> bool:
    missing = []
    for module_name in ("onnx", "onnxscript"):
        if importlib.util.find_spec(module_name) is None:
            missing.append(module_name)

    if missing:
        print(
            "❌ Thiếu package cho export ONNX: "
            + ", ".join(missing)
            + f" (Python {sys.version.split()[0]})"
        )
        print("👉 Chạy: source venv/bin/activate && pip install -r requirements.txt")
        return False
    return True


def use_onnx_simplify() -> bool:
    """Default OFF for Orange Pi/CPU-only to avoid noisy onnxruntime GPU discovery warnings."""
    return os.getenv("ONNX_SIMPLIFY", "0").strip().lower() in {"1", "true", "yes", "on"}


def export_model(model_path, format="onnx", onnx_opset: int = DEFAULT_ONNX_OPSET):
    """
    Xuất model YOLO sang các định dạng tối ưu.
    Supported formats: onnx, ncnn, openvino, engine, coreml, torchscript
    """
    if not os.path.exists(model_path):
        print(f"❌ Không tìm thấy file: {model_path}")
        return

    if format == "onnx" and not ensure_onnx_requirements():
        return

    print(f"🚀 Đang xuất {model_path} sang định dạng {format}...")
    try:
        from ultralytics import YOLO

        model = YOLO(model_path)
        export_kwargs = {"format": format, "imgsz": 640, "simplify": True}
        if format == "onnx":
            export_kwargs["opset"] = onnx_opset
            export_kwargs["simplify"] = use_onnx_simplify()
            print(f"ℹ️ ONNX export opset={onnx_opset}, simplify={export_kwargs['simplify']}")

        path = model.export(**export_kwargs)
        print(f"✅ Thành công! File đã lưu tại: {path}")
    except Exception as e:
        print(f"❌ Lỗi khi xuất model: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Sử dụng: python export_model.py <path_to_model.pt> [format] [onnx_opset]")
        print("Ví dụ: python export_model.py models/bien_so_xe.pt onnx 18")
        print("Tuỳ chọn: ONNX_SIMPLIFY=1 để bật optimize/simplify sau export ONNX")
        sys.exit(1)

    m_path = sys.argv[1]
    fmt = sys.argv[2] if len(sys.argv) > 2 else "onnx"
    opset = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_ONNX_OPSET
    export_model(m_path, fmt, opset)
