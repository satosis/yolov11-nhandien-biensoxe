"""
streamlit_qa.py — Parking Camera Management Dashboard
Tabs: Nhận diện | DORI Calculator | Asset Registry | SLA Report | Camera Health
Run: streamlit run streamlit_qa.py
"""
import os
import sys
import time
import json
import sqlite3
from datetime import datetime, date, timedelta
from pathlib import Path

import cv2
import numpy as np
import streamlit as st
from ultralytics import YOLO

# Add project root to path so core/ imports work
sys.path.insert(0, os.path.dirname(__file__))

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Parking Camera Management",
    page_icon="🅿",
    layout="wide",
)

MODEL_PATH = os.path.join(os.path.dirname(__file__), "models", "license_plate_recognition.pt")
SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), "data", "snapshots")
os.makedirs(SNAPSHOT_DIR, exist_ok=True)

# ── Model cache ───────────────────────────────────────────────────────────────
@st.cache_resource(show_spinner="Đang tải model YOLO…")
def load_model():
    return YOLO(MODEL_PATH)


@st.cache_resource(show_spinner="Đang tải PaddleOCR…")
def load_ocr():
    try:
        from paddleocr import PaddleOCR
        return PaddleOCR(use_angle_cls=True, lang="en", show_log=False, use_gpu=False)
    except ImportError:
        return None


@st.cache_resource(show_spinner="Đang tải InsightFace…")
def load_face():
    try:
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(name="buffalo_sc", providers=["CPUExecutionProvider"])
        app.prepare(ctx_id=0, det_size=(320, 320))
        return app
    except ImportError:
        return None


# ── Plate enhancement ─────────────────────────────────────────────────────────
def enhance_plate(crop: np.ndarray) -> np.ndarray:
    h, w = crop.shape[:2]
    up = cv2.resize(crop, (w * 3, h * 3), interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(up, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    binary = cv2.adaptiveThreshold(
        blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
    )
    return cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)


def read_plate_ocr(ocr, crop: np.ndarray) -> tuple[str, float]:
    if ocr is None:
        return "", 0.0
    result = ocr.ocr(crop, cls=True)
    if not result or not result[0]:
        return "", 0.0
    texts, confs = [], []
    for line in result[0]:
        if line and len(line) >= 2:
            texts.append(line[1][0])
            confs.append(float(line[1][1]))
    if not texts:
        return "", 0.0
    return "".join(texts).upper().replace(" ", ""), sum(confs) / len(confs)


# ── Core detection ────────────────────────────────────────────────────────────
def detect_plates(model, ocr, img: np.ndarray, conf_thresh: float, iou_thresh: float):
    t0 = time.perf_counter()
    results = model(img, imgsz=640, conf=conf_thresh, iou=iou_thresh, verbose=False)
    det_ms = (time.perf_counter() - t0) * 1000

    detections = []
    annotated = img.copy()

    for r in results:
        for b in r.boxes:
            x1, y1, x2, y2 = map(int, b.xyxy[0])
            det_conf = float(b.conf[0])
            crop = img[y1:y2, x1:x2]
            if crop.size == 0:
                continue

            enhanced = enhance_plate(crop)
            plate_text, ocr_conf = read_plate_ocr(ocr, enhanced)
            final_conf = det_conf * ocr_conf if ocr_conf > 0 else det_conf

            color = (0, 200, 0) if final_conf > 0.6 else (0, 140, 255)
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)

            label = plate_text if plate_text else f"plate {det_conf:.2f}"
            if ocr_conf > 0:
                label += f"  ({ocr_conf:.2f})"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.65, 2)
            cv2.rectangle(annotated, (x1, max(y1 - th - 8, 0)), (x1 + tw + 6, y1), color, -1)
            cv2.putText(annotated, label, (x1 + 3, max(y1 - 4, th)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 0, 0), 2)

            detections.append({
                "bbox": (x1, y1, x2, y2),
                "det_conf": det_conf,
                "plate_text": plate_text,
                "ocr_conf": ocr_conf,
                "final_conf": final_conf,
                "crop": crop,
                "enhanced": enhanced,
            })

    return annotated, detections, det_ms


def detect_faces(face_app, img: np.ndarray, known_dir: str):
    if face_app is None:
        return img.copy(), []

    # Load known embeddings
    known: dict[str, np.ndarray] = {}
    if os.path.isdir(known_dir):
        for person in os.listdir(known_dir):
            pdir = os.path.join(known_dir, person)
            if not os.path.isdir(pdir):
                continue
            embs = []
            for f in os.listdir(pdir):
                if not f.lower().endswith((".jpg", ".jpeg", ".png")):
                    continue
                pimg = cv2.imread(os.path.join(pdir, f))
                if pimg is None:
                    continue
                faces = face_app.get(pimg)
                if faces:
                    embs.append(faces[0].normed_embedding)
            if embs:
                known[person] = np.mean(embs, axis=0)

    t0 = time.perf_counter()
    faces = face_app.get(img)
    det_ms = (time.perf_counter() - t0) * 1000

    annotated = img.copy()
    detections = []

    for face in faces:
        bbox = face.bbox.astype(int)
        x1, y1, x2, y2 = bbox[0], bbox[1], bbox[2], bbox[3]
        emb = face.normed_embedding

        name, sim = "STRANGER", 0.0
        for pname, ref in known.items():
            s = float(np.dot(emb, ref))
            if s > sim:
                sim = s
                name = pname if s > 0.35 else "STRANGER"

        color = (0, 200, 0) if name != "STRANGER" else (0, 0, 220)
        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
        label = f"{name} ({sim:.2f})"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.65, 2)
        cv2.rectangle(annotated, (x1, max(y1 - th - 8, 0)), (x1 + tw + 6, y1), color, -1)
        cv2.putText(annotated, label, (x1 + 3, max(y1 - 4, th)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 0, 0), 2)

        detections.append({"name": name, "similarity": sim, "bbox": (x1, y1, x2, y2)})

    return annotated, detections, det_ms


# ── DB helper ─────────────────────────────────────────────────────────────────
@st.cache_resource
def get_db():
    try:
        from core.database import DatabaseManager
        from core.config import DB_PATH
        return DatabaseManager(DB_PATH)
    except Exception:
        return None


# ── Asset registry helper ─────────────────────────────────────────────────────
@st.cache_resource
def get_asset_registry():
    try:
        from core.asset_registry import AssetRegistry
        from core.config import DB_PATH
        return AssetRegistry(DB_PATH)
    except Exception:
        return None


# ── UI ────────────────────────────────────────────────────────────────────────
st.title("🅿 Parking Camera Management Dashboard")

tab_qa, tab_dori, tab_asset, tab_sla, tab_health = st.tabs([
    "🔍 Nhận diện",
    "📐 DORI Calculator",
    "🗂 Asset Registry",
    "📊 SLA Report",
    "❤️ Camera Health",
])

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1 — NHẬN DIỆN
# ═══════════════════════════════════════════════════════════════════════════════
# ── Sidebar (global) ─────────────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Cài đặt")
    mode = st.radio("Chế độ", ["🔢 Biển số xe", "👤 Khuôn mặt"], index=0)
    st.divider()
    conf_thresh = st.slider("Confidence threshold", 0.1, 0.95, 0.25, 0.05)
    iou_thresh  = st.slider("IoU threshold (NMS)",  0.1, 0.9,  0.45, 0.05)
    st.divider()
    show_enhanced = st.checkbox("Hiển thị ảnh enhanced", value=True)
    auto_save     = st.checkbox("Tự động lưu snapshot",  value=False)
    st.divider()
    known_dir = st.text_input(
        "Thư mục khuôn mặt đã biết",
        value=os.path.join(os.path.dirname(__file__), "config", "faces"),
    )
    st.caption(f"Model: `{Path(MODEL_PATH).name}`")

# Load models once
model    = load_model()
ocr      = load_ocr()
face_app = load_face()
db       = get_db()
registry = get_asset_registry()

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 1 — NHẬN DIỆN
# ═══════════════════════════════════════════════════════════════════════════════
with tab_qa:
    sub_upload, sub_cam = st.tabs(["📁 Upload ảnh", "📷 Webcam / URL"])
    with sub_upload:
        uploaded = st.file_uploader(
            "Chọn ảnh (JPG/PNG)",
            type=["jpg", "jpeg", "png"],
            accept_multiple_files=True,
        )
    with sub_cam:
        c1, c2 = st.columns([3, 1])
        with c1:
            img_url = st.text_input("URL ảnh hoặc đường dẫn file")
        with c2:
            use_webcam = st.checkbox("Dùng webcam")

    images_to_process: list[tuple[str, np.ndarray]] = []
    if uploaded:
        for f in uploaded:
            arr = np.frombuffer(f.read(), np.uint8)
            im = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if im is not None:
                images_to_process.append((f.name, im))
    if img_url:
        try:
            import urllib.request
            if img_url.startswith("http"):
                with urllib.request.urlopen(img_url, timeout=5) as resp:
                    arr = np.frombuffer(resp.read(), np.uint8)
                    im = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            else:
                im = cv2.imread(img_url)
            if im is not None:
                images_to_process.append((os.path.basename(img_url), im))
            else:
                st.error("Không đọc được ảnh.")
        except Exception as e:
            st.error(f"Lỗi tải ảnh: {e}")
    if use_webcam:
        cam_img = st.camera_input("Chụp ảnh từ webcam")
        if cam_img:
            arr = np.frombuffer(cam_img.read(), np.uint8)
            im = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if im is not None:
                images_to_process.append(("webcam.jpg", im))

    if not images_to_process:
        st.info("Upload ảnh hoặc nhập URL để bắt đầu nhận diện.")
    else:
        for fname, img in images_to_process:
            st.subheader(f"📄 {fname}  —  {img.shape[1]}×{img.shape[0]}px")
            is_plate = "Biển số" in mode
            if is_plate:
                annotated, detections, det_ms = detect_plates(model, ocr, img, conf_thresh, iou_thresh)
            else:
                annotated, detections, det_ms = detect_faces(face_app, img, known_dir)

            col_orig, col_ann = st.columns(2)
            with col_orig:
                st.caption("Ảnh gốc")
                st.image(cv2.cvtColor(img, cv2.COLOR_BGR2RGB), use_container_width=True)
            with col_ann:
                st.caption(f"Kết quả  ({det_ms:.0f} ms)")
                st.image(cv2.cvtColor(annotated, cv2.COLOR_BGR2RGB), use_container_width=True)

            if is_plate:
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Phát hiện", len(detections))
                if detections:
                    best = max(detections, key=lambda d: d["final_conf"])
                    m2.metric("Biển số tốt nhất", best["plate_text"] or "—")
                    m3.metric("OCR conf", f"{best['ocr_conf']:.2%}" if best["ocr_conf"] > 0 else "N/A")
                    m4.metric("Det conf", f"{best['det_conf']:.2%}")
                if detections:
                    st.markdown("**Chi tiết:**")
                    cols = st.columns(min(len(detections), 4))
                    for i, det in enumerate(detections):
                        with cols[i % len(cols)]:
                            st.image(cv2.cvtColor(det["crop"], cv2.COLOR_BGR2RGB),
                                     caption=f"Crop #{i+1}", use_container_width=True)
                            if show_enhanced:
                                st.image(cv2.cvtColor(det["enhanced"], cv2.COLOR_BGR2RGB),
                                         caption="Enhanced", use_container_width=True)
                            st.markdown(f"**`{det['plate_text'] or '—'}`**")
                            st.caption(f"det={det['det_conf']:.3f} ocr={det['ocr_conf']:.3f}")
                            if auto_save and det["plate_text"]:
                                ts = int(time.time())
                                sp = os.path.join(SNAPSHOT_DIR, f"{det['plate_text']}_{ts}.jpg")
                                cv2.imwrite(sp, img, [cv2.IMWRITE_JPEG_QUALITY, 95])
                                st.success(f"Saved: {sp}")
            else:
                m1, m2 = st.columns(2)
                m1.metric("Khuôn mặt", len(detections))
                if detections:
                    best = max(detections, key=lambda d: d["similarity"])
                    m2.metric("Người nhận diện", best["name"])
                if detections:
                    cols = st.columns(min(len(detections), 4))
                    for i, det in enumerate(detections):
                        with cols[i % len(cols)]:
                            x1, y1, x2, y2 = det["bbox"]
                            fc = img[max(0,y1):y2, max(0,x1):x2]
                            if fc.size > 0:
                                st.image(cv2.cvtColor(fc, cv2.COLOR_BGR2RGB),
                                         caption=f"{det['name']} ({det['similarity']:.2f})",
                                         use_container_width=True)

            sc, _ = st.columns([1, 3])
            with sc:
                ok, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 95])
                if ok:
                    st.download_button("💾 Tải ảnh kết quả", data=buf.tobytes(),
                                       file_name=f"qa_{fname}", mime="image/jpeg",
                                       key=f"dl_{fname}")
            st.divider()

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 2 — DORI CALCULATOR
# ═══════════════════════════════════════════════════════════════════════════════
with tab_dori:
    st.subheader("📐 DORI Distance Calculator (IEC 62676-4)")
    st.caption("Tính khoảng cách tối đa cho từng mức nhận diện theo tiêu chuẩn công nghiệp.")

    DORI_PPM = {"Detection": 25, "Observation": 62, "Recognition": 125, "Identification": 250}

    dc1, dc2, dc3 = st.columns(3)
    with dc1:
        res_w = st.number_input("Độ phân giải ngang (px)", min_value=320, max_value=7680,
                                value=1920, step=1)
        res_h = st.number_input("Độ phân giải dọc (px)", min_value=240, max_value=4320,
                                value=1080, step=1)
    with dc2:
        scene_w = st.number_input("Chiều rộng vùng giám sát (m)", min_value=0.5,
                                  max_value=50.0, value=3.0, step=0.5)
        hfov = st.number_input("Góc nhìn ngang — HFoV (°)", min_value=10.0,
                               max_value=180.0, value=90.0, step=1.0)
    with dc3:
        mount_h = st.number_input("Chiều cao gắn camera (m)", min_value=1.0,
                                  max_value=10.0, value=3.0, step=0.5)
        target_h = st.number_input("Chiều cao đối tượng (m)", min_value=0.3,
                                   max_value=3.0, value=1.8, step=0.1)

    st.divider()
    st.markdown("**Kết quả khoảng cách tối đa:**")

    import math
    rows = []
    for level, ppm in DORI_PPM.items():
        # Max distance based on horizontal pixel density
        max_dist_h = res_w / (ppm * scene_w)
        # Max distance based on vertical pixel density (target height)
        max_dist_v = res_h / (ppm * target_h)
        max_dist = min(max_dist_h, max_dist_v)
        # Recommended lens focal length (35mm equiv approx)
        # f = sensor_width / (2 * tan(HFoV/2)) — simplified
        focal_mm = round(max_dist * 1000 / (2 * math.tan(math.radians(hfov / 2)) * 1000), 1)
        rows.append({
            "Mức DORI": level,
            "PPM yêu cầu": ppm,
            "Khoảng cách tối đa (m)": round(max_dist, 2),
            "Gợi ý focal (mm)": focal_mm,
            "Đạt tiêu chuẩn": "✅" if max_dist >= 1.0 else "⚠️ Quá gần",
        })

    import pandas as pd
    df_dori = pd.DataFrame(rows)
    st.dataframe(df_dori, use_container_width=True, hide_index=True)

    # Visual bar chart
    st.bar_chart(df_dori.set_index("Mức DORI")["Khoảng cách tối đa (m)"])

    st.info(
        "**Hướng dẫn:** Identification (250 PPM) là mức tối thiểu để nhận dạng pháp lý. "
        "Nếu khoảng cách tối đa < 2m, cần camera độ phân giải cao hơn hoặc lens tele hơn."
    )

    # Camera naming preview
    st.divider()
    st.subheader("🏷 Đặt tên camera theo chuẩn công nghiệp")
    st.caption("Schema: `[REGION]-[SITE]-[BUILDING]-[ZONE]-[ID]-[MÔ_TẢ]`")
    nc1, nc2, nc3, nc4, nc5, nc6 = st.columns(6)
    region   = nc1.text_input("Region", "VN",      max_chars=4).upper()
    site     = nc2.text_input("Site",   "HCM01",   max_chars=6).upper()
    building = nc3.text_input("Building","PARKING", max_chars=10).upper()
    zone     = nc4.text_input("Zone",   "EXT",     max_chars=6).upper()
    cam_num  = nc5.number_input("Số thứ tự", 1, 999, 1)
    desc     = nc6.text_input("Mô tả", "ENTRANCE_GATE").upper().replace(" ", "_")
    cam_name = f"{region}-{site}-{building}-{zone}-C{cam_num:03d}-{desc}"
    st.code(cam_name, language=None)
    st.caption("Chỉ dùng chữ HOA, số, dấu `-` và `_`. Không dấu cách, không ký tự đặc biệt.")

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 3 — ASSET REGISTRY
# ═══════════════════════════════════════════════════════════════════════════════
with tab_asset:
    st.subheader("🗂 Asset Registry — CMDB Camera")
    if registry is None:
        st.warning("Asset Registry chưa khởi tạo. Kiểm tra core/asset_registry.py và DB_PATH.")
    else:
        assets = registry.get_all()
        if assets:
            import pandas as pd
            df_assets = pd.DataFrame(assets)
            st.dataframe(df_assets, use_container_width=True, hide_index=True)
            # Export
            csv = df_assets.to_csv(index=False).encode("utf-8")
            st.download_button("📥 Export CSV", data=csv,
                               file_name="camera_assets.csv", mime="text/csv")
        else:
            st.info("Chưa có camera nào trong registry.")

        st.divider()
        st.subheader("➕ Thêm / Cập nhật camera")
        with st.form("asset_form"):
            af1, af2 = st.columns(2)
            a_id      = af1.text_input("Camera ID *", placeholder="VN-HCM01-PARKING-EXT-C001")
            a_name    = af2.text_input("Tên hiển thị *", placeholder="Cổng vào chính")
            af3, af4  = st.columns(2)
            a_ip      = af3.text_input("IP Address", placeholder="192.168.1.55")
            a_mac     = af4.text_input("MAC Address", placeholder="30:24:50:67:cb:1b")
            af5, af6  = st.columns(2)
            a_model   = af5.text_input("Model", placeholder="Imou IPC-F42FEP")
            a_fw      = af6.text_input("Firmware", placeholder="2.800.0000.1.R")
            af7, af8  = st.columns(2)
            a_serial  = af7.text_input("Serial Number")
            a_rtsp    = af8.text_input("RTSP URL", placeholder="rtsp://admin:pass@ip/stream")
            af9, af10, af11 = st.columns(3)
            a_lat     = af9.number_input("Latitude", value=0.0, format="%.6f")
            a_lon     = af10.number_input("Longitude", value=0.0, format="%.6f")
            a_height  = af11.number_input("Chiều cao gắn (m)", value=3.0, step=0.5)
            af12, af13 = st.columns(2)
            a_fov     = af12.number_input("FoV ngang (°)", value=90.0, step=1.0)
            a_dori    = af13.selectbox("DORI class", ["Detection","Observation","Recognition","Identification"])
            a_notes   = st.text_area("Ghi chú")
            submitted = st.form_submit_button("💾 Lưu")
            if submitted:
                if not a_id or not a_name:
                    st.error("Camera ID và Tên là bắt buộc.")
                else:
                    registry.upsert({
                        "cam_id": a_id.strip(), "name": a_name.strip(),
                        "ip": a_ip, "mac": a_mac, "model": a_model,
                        "firmware": a_fw, "serial": a_serial, "rtsp_url": a_rtsp,
                        "location_lat": a_lat, "location_lon": a_lon,
                        "mount_height_m": a_height, "fov_deg": a_fov,
                        "dori_class": a_dori, "notes": a_notes,
                        "install_date": datetime.utcnow().date().isoformat(),
                    })
                    st.success(f"Đã lưu camera: {a_id}")
                    st.rerun()

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 4 — SLA REPORT
# ═══════════════════════════════════════════════════════════════════════════════
with tab_sla:
    st.subheader("📊 SLA Report")
    if db is None:
        st.warning("Không kết nối được DB.")
    else:
        import pandas as pd
        sla_days = st.slider("Số ngày hiển thị", 7, 90, 30)
        rows = db.get_sla_daily(days=sla_days)
        if rows:
            df_sla = pd.DataFrame(rows)
            # Summary metrics
            avg_uptime = df_sla["uptime_pct"].mean()
            total_gaps = df_sla["gap_count"].sum()
            total_offline = df_sla["offline_count"].sum()
            s1, s2, s3, s4 = st.columns(4)
            s1.metric("Uptime trung bình", f"{avg_uptime:.2f}%",
                      delta="✅ OK" if avg_uptime >= 99 else "⚠️ Dưới SLO")
            s2.metric("Tổng gaps", int(total_gaps))
            s3.metric("Tổng offline events", int(total_offline))
            s4.metric("Số ngày báo cáo", len(df_sla["report_date"].unique()))

            st.divider()
            # Uptime trend chart
            st.markdown("**Uptime % theo ngày:**")
            pivot = df_sla.pivot_table(index="report_date", columns="cam_id",
                                       values="uptime_pct", aggfunc="mean")
            st.line_chart(pivot)

            st.markdown("**Dữ liệu chi tiết:**")
            st.dataframe(df_sla, use_container_width=True, hide_index=True)

            csv = df_sla.to_csv(index=False).encode("utf-8")
            st.download_button("📥 Export SLA CSV", data=csv,
                               file_name="sla_report.csv", mime="text/csv")
        else:
            st.info("Chưa có dữ liệu SLA. Dữ liệu được tính tự động hàng ngày bởi services/sla_reporter.py.")

        st.divider()
        st.subheader("📋 SLA Targets (tiêu chuẩn công nghiệp)")
        import pandas as pd
        st.table(pd.DataFrame([
            {"Priority": "P1", "Sự cố": "Toàn hệ thống sập",    "Acknowledge": "≤ 15 phút", "Resolve": "≤ 4 giờ"},
            {"Priority": "P2", "Sự cố": "Recording server lỗi", "Acknowledge": "≤ 1 giờ",   "Resolve": "≤ 8 giờ"},
            {"Priority": "P3", "Sự cố": "Camera đơn lẻ lỗi",   "Acknowledge": "≤ 4 giờ",   "Resolve": "≤ 24 giờ"},
            {"Priority": "P4", "Sự cố": "Vấn đề không quan trọng","Acknowledge":"≤ 24 giờ", "Resolve": "≤ 72 giờ"},
        ]))

# ═══════════════════════════════════════════════════════════════════════════════
# TAB 5 — CAMERA HEALTH
# ═══════════════════════════════════════════════════════════════════════════════
with tab_health:
    st.subheader("❤️ Camera Health Monitor")
    if db is None:
        st.warning("Không kết nối được DB.")
    else:
        import pandas as pd
        h_hours = st.slider("Hiển thị sự kiện trong N giờ gần nhất", 1, 168, 24)
        events = db.get_camera_health(hours=h_hours)
        if events:
            df_h = pd.DataFrame(events)
            # Color-code by event type
            def _color(row):
                if row["event_type"] == "OFFLINE":
                    return ["background-color: #3a1a1a"] * len(row)
                if row["event_type"] == "GAP":
                    return ["background-color: #3a2a1a"] * len(row)
                return [""] * len(row)
            st.dataframe(df_h.style.apply(_color, axis=1),
                         use_container_width=True, hide_index=True)

            # Summary per camera
            st.divider()
            st.markdown("**Tóm tắt theo camera:**")
            summary = df_h.groupby(["cam_id", "event_type"]).size().reset_index(name="count")
            st.dataframe(summary, use_container_width=True, hide_index=True)
        else:
            st.success(f"Không có sự kiện health trong {h_hours} giờ qua. ✅")

        # Legal hold management
        st.divider()
        st.subheader("🔒 Legal Hold")
        st.caption("Đánh dấu footage không được tự động xóa (dùng cho điều tra, pháp lý).")
        lh1, lh2 = st.columns([3, 1])
        with lh1:
            hold_path = st.text_input("Đường dẫn file cần giữ lại",
                                      placeholder="/mnt/storage/snapshots/cam1_51A12345_20260311.jpg")
            hold_reason = st.text_input("Lý do", placeholder="Điều tra sự cố ngày 11/03/2026")
            hold_by = st.text_input("Người yêu cầu", placeholder="Nguyễn Văn A")
        with lh2:
            st.write("")
            st.write("")
            if st.button("🔒 Đặt Legal Hold"):
                if hold_path and hold_reason and hold_by:
                    ok = db.add_legal_hold(hold_path, hold_reason, hold_by)
                    st.success("Đã đặt legal hold.") if ok else st.error("Lỗi.")
                else:
                    st.warning("Điền đầy đủ thông tin.")
            if st.button("🔓 Giải phóng Hold"):
                if hold_path:
                    db.release_legal_hold(hold_path)
                    st.success("Đã giải phóng.")
