# CLAUDE.md — Bộ nhớ chung cho Camera AI Project

## Tổng quan hệ thống

Hệ thống giám sát bãi giữ xe thông minh chạy trên **Orange Pi 4 Pro (ARM)**, tích hợp:
- Nhận diện biển số xe Việt Nam (YOLOv11 + PaddleOCR)
- Đếm người/xe qua vạch ảo (TripwireTracker)
- Phát hiện camera lệch góc (ORB feature matching + RANSAC)
- Điều khiển cửa cuốn tự động (Tuya + MQTT)
- Cảnh báo Telegram (2 kênh: quan trọng / thường)
- Tích hợp Home Assistant + Frigate NVR
- PTZ camera control qua ONVIF + Imou Open API
- Multi-camera grid UI (FastAPI + MJPEG)


## Stack công nghệ

| Thành phần | Công nghệ |
|---|---|
| AI inference | YOLOv11 (Ultralytics), PaddleOCR, face_recognition |
| Backend | FastAPI + uvicorn, paho-mqtt |
| Camera | OpenCV RTSP, ONVIF, Imou Open API |
| NVR | Frigate (Docker) |
| Smart home | Home Assistant (Docker) |
| Messaging | Telegram Bot API |
| Database | SQLite (core/database.py) |
| Container | Docker Compose |
| Network | Tailscale (remote access) |


## Kiến trúc tổng thể

```
RTSP Camera (Imou 2K)
    │
    ├─► main.py (AI core: YOLO track + OCR + TripwireTracker)
    │       └─► core/database.py (SQLite: events, whitelist, pending_plates)
    │       └─► services/telegram_service.py (cảnh báo)
    │       └─► core/mqtt_manager.py (MQTT publish state)
    │       └─► services/api_server.py (MJPEG stream + dashboard)
    │
    ├─► deploy/event_bridge/app.py (Docker: Frigate events → Telegram + ONVIF PTZ)
    │
    ├─► Frigate NVR (Docker: record + detect + zones)
    │       └─► MQTT → mosquitto → event_bridge
    │
    ├─► Home Assistant (Docker: automation + dashboard)
    │       └─► MQTT sensors: shed_people_count, shed_vehicle_count
    │       └─► cover.garage_door (Tuya)
    │

```

## Cấu trúc folder

```
yolov11-nhandien-biensoxe/
├── main.py                    # Entry point: AI loop chính
├── CLAUDE.md                  # File này
├── .env                       # Credentials (KHÔNG commit)
├── .env.example               # Template env
├── requirements.txt           # Python deps
├── docker-compose.yml         # Stack: mosquitto, frigate, homeassistant, event_bridge, ai_core
├── install.sh                 # One-command setup
├── cmd                        # CLI shortcuts (stats, today, whitelist...)
│
├── core/                      # Business logic thuần Python
│   ├── config.py              # Tất cả constants + env loading
│   ├── database.py            # SQLite: events, whitelist, pending_plates, camera_health, SLA
│   ├── asset_registry.py      # CMDB camera assets
│   ├── camera_orientation_monitor.py  # ORB shift detection
│   ├── door_controller.py     # Tuya door control
│   ├── mjpeg_streamer.py      # MJPEG frame buffer
│   ├── mqtt_manager.py        # MQTT publish/subscribe
│   └── tripwire.py            # Vạch ảo đếm người/xe
│
├── services/                  # Application services
│   ├── api_server.py          # FastAPI: /dashboard, /video_feed, /snapshot, /login
│   ├── camera_manager.py      # Multi-camera RTSP manager + gap detection
│   ├── telegram_service.py    # Telegram bot commands + alerts
│   ├── face_service.py        # face_recognition wrapper
│   ├── door_service.py        # Door state detection
│   ├── system_monitor.py      # CPU temp monitoring
│   ├── retention_manager.py   # Auto-delete snapshots (30 ngày)
│   └── sla_reporter.py        # SLA daily metrics
│
├── util/
│   └── ocr_utils.py           # VNPlateOCR (PaddleOCR wrapper)
│
├── config/
│   ├── authorized.json        # Whitelist biển số (JSON)
│   └── faces/                 # Face embeddings
│
├── models/
│   ├── bien_so_xe.pt          # YOLOv11: biển số + người + xe
│   └── door_model.pt          # Door state model (optional)
│
├── deploy/
│   ├── event_bridge/app.py    # Frigate event handler + PTZ + Telegram
│   ├── frigate/config.yml     # Frigate NVR config (zones, detect, record)
│   ├── mosquitto/             # MQTT broker config
│   ├── scripts/               # resolve_camera_ip.py, check_remote_ha.py
│   └── reporting/             # monthly_chart.py
│

├── parking_hpc/               # High-performance multiprocessing pipeline
├── tests/
│   └── test_security.py       # Security self-tests (20 tests, tất cả PASS)
└── streamlit_qa.py            # Streamlit QA dashboard (5 tabs)
```

## Biến môi trường quan trọng

```bash
# Telegram
TELEGRAM_TOKEN=                    # Bot token
TELEGRAM_CHAT_IMPORTANT=           # Chat ID cảnh báo quan trọng
TELEGRAM_CHAT_NONIMPORTANT=        # Chat ID thông báo thường

# Camera
RTSP_URL=rtsp://user:pass@ip:554/... # Stream chính
CAMERA_MAC=30:24:50:67:cb:1b        # MAC để tự tìm IP
CAMERA_IP_SUBNET=                   # Subnet quét ARP (vd: 192.168.1.0/24)
OCR_SOURCE=rtsp                     # rtsp | webcam | image:/path

# Vạch ảo
LINE_Y_RATIO=0.62                   # Vị trí vạch theo % chiều cao (0.0-1.0)
LINE_Y_PIXELS=0                     # Override tuyệt đối (0 = dùng ratio)

# Multi-camera UI
CAMERA_UI_USER=admin
CAMERA_UI_PASS=changeme
CAMERA_2_URL=                       # Camera phụ 2 (optional)
CAMERA_3_URL=                       # Camera phụ 3 (optional)
CAMERA_4_URL=                       # Camera phụ 4 (optional)
SNAPSHOT_DIR=./data/snapshots

# Home Assistant
HA_INTERNAL_URL=http://192.168.1.131:8123
HA_EXTERNAL_URL=https://ha-gateway.ts.net:8123

# Tailscale (remote access)
TS_AUTHKEY=                         # Auth key (chỉ cần lần đầu)
TS_HOSTNAME=ha-gateway

# Imou Open API (PTZ cloud)
IMOU_OPEN_APP_ID=
IMOU_OPEN_APP_SECRET=
IMOU_OPEN_DEVICE_ID=

# PostgreSQL (nếu dùng)
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=camera_ai
POSTGRES_USER=camera_user
POSTGRES_PASSWORD=
DATABASE_URL=postgresql+asyncpg://camera_user:password@localhost:5432/camera_ai
```

## Lệnh thường dùng

### Khởi động / tắt hệ thống
```bash
./install.sh                    # Cài đặt lần đầu (Docker stack + venv)
./cmd up                        # Khởi động Docker stack (resolve CAMERA_IP trước)
./cmd down                      # Tắt Docker stack
source venv/bin/activate && python main.py   # Chạy AI core trực tiếp
```

### Xem log và trạng thái
```bash
./cmd stats                     # Thống kê sự kiện
./cmd today                     # Sự kiện hôm nay
./cmd last 50                   # 50 sự kiện gần nhất
./cmd gate                      # Trạng thái cửa + số người/xe
./cmd pending                   # Biển số lạ chờ duyệt
./cmd whitelist                 # Danh sách biển số quen
./cmd remote-check              # Kiểm tra cấu hình HA remote
docker compose logs -f event_bridge   # Log event bridge
docker compose logs -f frigate        # Log Frigate NVR
```

### Báo cáo
```bash
./cmd report-month 2026-03      # Báo cáo tháng dạng text
./cmd chart-month 2026-03       # Biểu đồ PNG (lưu ./data/event_bridge/reports/)
python -m services.sla_reporter --run-now   # SLA report hôm qua
```

### Quản lý whitelist biển số
```bash
# Qua Telegram bot:
/mine 29A12345                  # Thêm xe của tôi
/staff 51G99999                 # Thêm xe nhân viên
/reject 30A00000                # Từ chối biển số lạ
/whitelist                      # Xem danh sách
```

### Test và debug
```bash
python3.11 -m pytest tests/test_security.py -v   # Security tests (20 tests)
./cmd webcam-people --camera 0 --model models/bien_so_xe.pt   # Test webcam
./cmd test-ptz                  # Test PTZ camera
python -m services.retention_manager --run-now   # Chạy retention ngay
streamlit run streamlit_qa.py   # QA dashboard (5 tabs)
```

### Restream với vạch đỏ
```bash
scripts/restream_tripwire.sh "rtsp://..." "rtsp://0.0.0.0:8554/cam_doorline" 0.62 6
```

## Quy tắc KHÔNG được vi phạm

1. **KHÔNG hardcode credentials** — tất cả secrets phải qua `.env`
2. **KHÔNG commit `.env`** — chỉ commit `.env.example`
3. **KHÔNG xóa whitelist biển số** mà không backup `config/authorized.json` và bảng `vehicle_whitelist`
4. **KHÔNG thay đổi `LINE_Y_RATIO`** mà không test lại TripwireTracker với video thực
5. **KHÔNG sửa `deploy/frigate/config.yml`** mà không rebuild: `docker compose up -d frigate`
6. **KHÔNG sửa Dockerfile** mà không chạy `./cmd up` để rebuild image
7. **KHÔNG dùng f-string SQL** — luôn dùng parameterized query `cursor.execute(sql, (param,))`
8. **KHÔNG xóa file trong `data/snapshots/`** thủ công — dùng RetentionManager hoặc legal hold
9. **KHÔNG thay đổi MQTT topic prefix** `frigate/` mà không cập nhật cả `event_bridge/app.py` và `frigate/config.yml`
10. **KHÔNG sửa `core/config.py`** mà không chạy `python3 -m py_compile core/config.py` để kiểm tra syntax

## Workflow chuẩn khi thêm tính năng mới



## Thông tin kỹ thuật quan trọng

- **Python version**: 3.10.x (production), 3.11.x (dev/test)
- **Model chính**: `models/bien_so_xe.pt` — dùng cho cả general detection và plate detection
- **DB path**: `./db/door_events.db` (SQLite)
- **API port**: 8000 (ai_core Docker: 8080→8000), event_bridge: 8000
- **Frigate UI**: http://host:5000
- **HA**: http://host:8123
- **MQTT broker**: mosquitto:1883
- **Camera shift**: ORB + RANSAC, ngưỡng rotation=3.5°, translation=18px
- **Vạch ảo**: TripwireTracker, buffer=3 frames, cooldown=3s
- **Retention**: 30 ngày mặc định, legal hold không bao giờ xóa
- **Security tests**: 20 tests, tất cả PASS (chạy: `python3.11 -m pytest tests/test_security.py -v`)
