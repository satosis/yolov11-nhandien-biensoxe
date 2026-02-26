# Hệ Thống Nhận Diện Biển Số & Camera AI (Orange Pi)

Chạy toàn bộ hệ thống bằng một lệnh:

```bash
chmod +x install.sh
./install.sh
```

`install.sh` hiện sẽ tự copy HACS và Frigate Home Assistant integration vào `data/homeassistant/custom_components/` (nếu tải được), để bạn restart Home Assistant rồi Add Integration trong UI.

⚠️ `install.sh` sẽ dọn Docker cũ **của dự án này** trước khi chạy mới (compose down + remove orphan + volumes của project), không xoá toàn bộ Docker của máy.

Sau khi cài đặt xong, chạy ứng dụng:
```bash
source venv/bin/activate
python main.py
```

## Mô hình AI
Hệ thống sử dụng model `models/bien_so_xe.pt` cho cả nhận diện vật thể và biển số, tối ưu hóa cho tốc độ trên thiết bị biên.

## Cấu hình Bảo mật
Sửa file `.env` (được tạo từ `.env.example` sau khi chạy install):

```
TELEGRAM_BOT_TOKEN=
CHAT_ID_IMPORTANT=
CHAT_ID_REGULAR=
RTSP_URL=
OCR_SOURCE=
```

## Cấu hình Camera RTSP
Sửa trong `.env`:
- `CAMERA_MAC`: MAC cố định của camera (khuyến nghị dùng để tự động tìm IP).
- `CAMERA_IP_SUBNET`: subnet nội bộ để quét khi cần (ví dụ `10.115.215.0/24`).
- `CAMERA_IP`: không cần khai báo thủ công trong `.env` (sẽ tự tạo runtime từ `CAMERA_MAC`).
- `RTSP_URL`: Đường dẫn luồng hình ảnh chính từ Camera.
- `OCR_SOURCE`: Nguồn nhận diện (vd: `rtsp` hoặc `webcam`).

Trong `deploy/frigate/config.yml`, địa chỉ stream dùng biến `{CAMERA_IP}`.
Khi chạy `./cmd up`, script `deploy/scripts/resolve_camera_ip.py` sẽ tự dò `CAMERA_IP` theo `CAMERA_MAC` và ghi vào `.camera.env` trước khi khởi động Docker.


## Phát hiện camera bị lệch góc (so với ban đầu)
- Hệ thống tự chụp một **baseline frame** khi camera ổn định.
- Mỗi vài frame sẽ so sánh frame hiện tại với baseline bằng **ORB feature matching + RANSAC affine**.
- Nếu vượt ngưỡng liên tiếp (`rotation`, `translation`, `inlier ratio`, `scale`) thì tạo sự kiện `CAMERA_SHIFT` và gửi cảnh báo Telegram.
- Khi camera quay về gần góc cũ, hệ thống ghi `CAMERA_SHIFT_RECOVERED`.

Các biến tinh chỉnh trong `.env`:
- `CAMERA_SHIFT_CHECK_EVERY_FRAMES`
- `CAMERA_SHIFT_MIN_INLIER_RATIO`
- `CAMERA_SHIFT_MAX_ROTATION_DEG`
- `CAMERA_SHIFT_MAX_TRANSLATION_PX`
- `CAMERA_SHIFT_MAX_SCALE_DELTA`
- `CAMERA_SHIFT_ALERT_CONSECUTIVE`

## Tính năng Đếm Người & Xe
- Hệ thống tự động đếm số lượng người và xe tải ra/vào.
- Logic "Gate Logic": Tự động trừ số người khi có xe đi ra (tài xế).
- Các tham số tinh chỉnh trong `.env`:
  - `LEFT_EXIT_WINDOW_SECONDS`: Thời gian chờ cửa sổ thoát.
  - `MAX_ACTIVE_VEHICLE_EXIT_SESSIONS`: Số phiên xe hoạt động tối đa.

## Lệnh Telegram (Quản lý)
Sử dụng trong nhóm chat:
- Bot sẽ tự đăng ký danh sách lệnh Telegram (menu `/`) khi `event_bridge` khởi động.
- Cảnh báo quan trọng (không có người nhưng cửa chưa đóng) chỉ gửi khi đính kèm ảnh chụp camera.
- `/gate_closed`: Đặt trạng thái cửa là ĐÓNG.
- `/gate_open`: Đặt trạng thái cửa là MỞ.
- `/gate_status`: Xem trạng thái cửa + số người/xe.
- `/report`: Xem báo cáo nhanh.
- `/start` hoặc `/help`: Hiển thị menu lệnh nhanh.

## Báo cáo Tháng
- Báo cáo dạng văn bản:
  ```bash
  ./cmd report-month YYYY-MM
  ```
- Biểu đồ (PNG lưu tại `./data/event_bridge/reports/`):
  ```bash
  ./cmd chart-month YYYY-MM
  ```

## Home Assistant (Tích hợp Nhà thông minh)
Truy cập: `http://192.168.1.131:8123`

Tích hợp sẵn:
- **Tuya Integration**: Điều khiển cửa cuốn tự động.
- **MQTT Discovery**: Tự động nhận diện cảm biến.
  - `sensor.shed_people_count`: Đếm người.
  - `sensor.shed_vehicle_count`: Đếm xe.
  - `cover.garage_door`: Điều khiển cửa cuốn.
- Trên HA chỉ giữ các nút điều khiển camera + xem lịch sử (Frigate).
  - `button.shed_ptz_panorama`: xoay camera sang toàn cảnh.
  - `button.shed_ptz_gate`: đưa camera về vị trí mặc định.
  - `switch.shed_ptz_mode`: công tắc điều khiển nhanh (Bật = panorama, Tắt = gate) để dễ thao tác ngay trên tile.
  - Cụm 4 nút PTZ: `button.shed_ptz_up`, `button.shed_ptz_down`, `button.shed_ptz_left`, `button.shed_ptz_right` để chỉnh góc camera theo từng nấc.
  - Nút lịch sử: mở `Frigate NVR` và `Frigate Events`.

### Tự động hóa (Automation)
- Tự động mở cửa Tuya khi nhận diện biển số xe quen (`whitelist`).
- Tự động đóng cửa sau 5 phút nếu không có người.
- Tự động đưa camera về vị trí mặc định khi không có người 5 phút.
- HA chỉ hiển thị text số lượng **người/xe qua vạch đỏ** (`sensor.shed_people_count`, `sensor.shed_vehicle_count`).
- Màu nhãn vùng nhận diện trên video: **Người = vàng**, **Xe = xanh** khi qua vạch đỏ.


## Truy cập Home Assistant từ mọi mạng (không phụ thuộc LAN)
Để không bị mất kết nối khi đổi Wi-Fi/4G, dự án đã hỗ trợ tách URL nội bộ/ngoại mạng bằng biến `.env`:
- `HA_INTERNAL_URL`: URL dùng khi ở cùng mạng nội bộ (VD `http://192.168.1.131:8123`).
- `HA_EXTERNAL_URL`: URL public để truy cập từ mạng khác (VD domain qua Cloudflare Tunnel/DuckDNS/Nabu Casa).

### Cấu hình nhanh
1. Sửa `.env`:
   - `HA_INTERNAL_URL=...`
   - `HA_EXTERNAL_URL=...`
2. Khởi động lại Home Assistant: `docker compose up -d homeassistant`
3. Trong app Home Assistant Companion, chọn **Connection = Auto** và kiểm tra cả Internal/External URL.

### Tuỳ chọn bật Tailscale để truy cập HA qua mạng khác
- Điền `TS_AUTHKEY` trong `.env` (Auth key từ Tailscale admin, chỉ cần khi bật profile `remote_ha_tailscale`).
- Tuỳ chọn đặt `TS_HOSTNAME` (mặc định `ha-gateway`).
- Chạy profile tailscale:
  - `docker compose --profile remote_ha_tailscale up -d tailscale`
- Trên thiết bị đã đăng nhập cùng tailnet, truy cập HA bằng MagicDNS:
  - `http://<TS_HOSTNAME>.<ten-tailnet>.ts.net:8123`
- Khi dùng Tailscale, nên đặt `HA_EXTERNAL_URL` theo URL MagicDNS ở trên để app HA tự kết nối đúng khi ra khỏi LAN.


## Kiến trúc khuyến nghị: HA để điều khiển, NVR để xem lịch sử
- **Home Assistant**: hiển thị trạng thái + nút điều khiển nhanh (PTZ, reset baseline, cửa cuốn).
- **NVR (khuyến nghị Frigate)**: lưu record/timeline/events và phát lại lịch sử.
- Có thể thay Frigate bằng **MotionEye** hoặc **Scrypted**, nhưng Frigate phù hợp nhất khi cần event/object timeline.

### Quy trình tích hợp camera Imou
1. **Bật ONVIF/RTSP trên Imou**
   - Trong app Imou Life, bật mục ONVIF/RTSP/Local Protocol (tên có thể khác tùy model).
   - Tạo user/pass ONVIF/RTSP nếu camera hỗ trợ tách tài khoản.
2. **Thêm vào HA bằng ONVIF**
   - HA → Settings → Devices & services → Add integration → ONVIF.
   - Lợi ích: live stream, PTZ control (nếu hỗ trợ), snapshot/profile stream.
3. **Giao phần lịch sử cho Frigate**
   - Dùng RTSP feed cho Frigate.
   - Frigate cung cấp Timeline / Events / Clips / Retention.
   - Nếu chỉ dùng HA + ONVIF mà không có NVR, HA chủ yếu là xem live chứ không thay thế đầy đủ chức năng playback lịch sử.

## Điều khiển PTZ (Camera xoay 360)
Cấu hình trong `.env` để Home Assistant điều khiển xoay camera:
- `ONVIF_HOST`, `ONVIF_PORT`, `ONVIF_USER`, `ONVIF_PASS`
- `ONVIF_PRESET_GATE`: Vị trí soi cổng.
- `ONVIF_PRESET_PANORAMA`: Vị trí toàn cảnh.
- (Tuỳ chọn) `ONVIF_PRESET_UP`, `ONVIF_PRESET_DOWN`, `ONVIF_PRESET_LEFT`, `ONVIF_PRESET_RIGHT`: preset cho 4 nút điều hướng.
- (Fallback nếu không có preset hướng) `PTZ_MOVE_SPEED`, `PTZ_MOVE_DURATION`: mức nudge bằng ONVIF move.

Hành vi:
- Chỉ đổi state PTZ/OCR khi lệnh ONVIF thành công (tránh hiển thị “đã quay” nhưng camera không đổi góc).
- Khi chuyển sang toàn cảnh, OCR tạm dừng.
- Tự động quay về vị trí cổng sau `PTZ_AUTO_RETURN_SECONDS` giây.

## Các lệnh vận hành (trong thư mục dự án)
```bash
./cmd stats         # Xem thống kê
./cmd today         # Xem sự kiện hôm nay
./cmd last 50       # 50 sự kiện gần nhất
./cmd pending       # Danh sách biển số lạ chờ duyệt
./cmd whitelist     # Danh sách biển số quen
./cmd gate          # Trạng thái cổng
./cmd test-ptz      # Test tính năng xoay camera
./cmd remote-check  # Kiểm tra nhanh cấu hình truy cập HA từ mạng khác
./cmd webcam-people --camera 0 --model models/yolo26n.pt  # Test nhận diện người trực tiếp từ webcam máy tính
```

## Xử lý sự cố (Troubleshooting)
- **Không nhận diện được người / `Person count = 0`**: kiểm tra camera `imou_2k` trong Frigate có đang track `person` (`deploy/frigate/config.yml`), và lưu ý sensor `Shed People Count` chỉ tăng/giảm khi object **đi qua vạch ảo trái/phải** (không phải cứ xuất hiện trong khung hình là tăng).
- **Cần test độc lập bằng webcam máy tính**: chạy `./cmd webcam-people --camera 0 --model models/yolo26n.pt` để kiểm tra model có nhận diện `person` ngoài pipeline RTSP/Frigate hay không.
- **Dùng Tailscale nhưng không truy cập được HA**: kiểm tra `tailscale status`, xác nhận node online trong tailnet, và đặt lại `HA_EXTERNAL_URL` theo MagicDNS `http://<TS_HOSTNAME>.<tailnet>.ts.net:8123`.
- **Không truy cập được HA từ mạng khác**: chạy `./cmd remote-check` để kiểm tra nhanh `.env` (`HA_INTERNAL_URL`, `HA_EXTERNAL_URL`, `TS_AUTHKEY`) và trạng thái cổng local 8123 trước khi debug tiếp.
- **Muốn xem lịch sử camera ngay trong HA**: dùng nút "Mở Frigate NVR"/"Mở Frigate Events" trên dashboard, hoặc mở trực tiếp `http://<host>:5000`; HA nên dùng cho điều khiển, NVR dùng cho timeline/record.
- **Lỗi RTSP**: Kiểm tra đường dẫn, user/pass camera trong `.env`.
- **Frigate báo lỗi đăng nhập camera / container `frigate` thoát code 1**: đảm bảo đã điền `RTSP_USER` và `RTSP_PASS` trong `.env`; `./cmd up` hiện cũng tự fallback lấy user/pass từ `RTSP_URL` và ghi vào `.camera.env` để tránh thiếu biến `{RTSP_USER}`/`{RTSP_PASS}` trong `deploy/frigate/config.yml`.
- **Lỗi MQTT**: Kiểm tra container `mosquitto` hoặc Log.
- **Vẫn thấy log cũ `/app/app.py ... client = mqtt.Client()`**: image `event_bridge` chưa rebuild theo code mới. Chạy lại `./cmd up` (lệnh này tự `docker compose build event_bridge`, rồi chờ health của Frigate trước khi kết thúc).
- **Cảnh báo `Snapshot fetch failed ... connection refused` lúc mới `./cmd up`**: thường do Frigate đang khởi động (`health: starting`). Đợi Frigate `healthy` rồi kiểm tra lại log.
- **Lỗi export ONNX (`No module named onnxscript`)**: chạy lại `source venv/bin/activate && pip install -r requirements.txt` để cài `onnx` + `onnxscript`, rồi chạy lại export model.
- **Cảnh báo ONNX opset/version converter**: script export mặc định dùng `opset=18` để tránh lỗi convert từ opset thấp (ví dụ lỗi `No Adapter To Version ... for Resize`). Có thể chạy tay: `python3 deploy/utils/export_model.py models/bien_so_xe.pt onnx 18`.
- **Cảnh báo `onnxruntime ... GPU device discovery failed` trên Orange Pi CPU-only**: đây là cảnh báo phụ khi optimize/simplify ONNX, không làm export fail. Script hiện mặc định `simplify=False` cho ONNX để giảm cảnh báo này; nếu cần tối ưu thêm thì bật `ONNX_SIMPLIFY=1` trước khi export.
- **Python version**: dự án đang chạy tốt với Python 3.10.x (ví dụ `Python 3.10.12`).
- **Lỗi `IndentationError` trong `core/config.py`**: chạy `python3 -m py_compile core/config.py`; installer sẽ tự thử `git checkout -- core/config.py` và fallback template. Nếu vẫn lỗi, chạy `git pull` rồi thử lại.
- **Lỗi `Cannot resolve CAMERA_IP from CAMERA_MAC`**: script hiện sẽ tự quét nhiều dải mạng LAN (bao gồm interface nội bộ và fallback), nhưng bạn vẫn nên đặt `CAMERA_IP_SUBNET` đúng dải mạng (vd `10.115.215.0/24`) để dò nhanh/chính xác hơn, rồi chạy lại `./cmd up`.
- **Lỗi `env file .camera.env not found` khi `./cmd up`**: đã được xử lý trong lệnh `./cmd up` mới (tự tạo `.camera.env` rỗng trước khi chạy Docker). Nếu đang dùng bản cũ, cập nhật mã mới hoặc tự tạo tạm bằng `touch .camera.env`.
- **Lỗi `Permission denied` khi cài HACS (`data/homeassistant/custom_components`)**: sửa quyền rồi chạy lại install: `sudo chown -R $USER:$USER data/homeassistant && ./install.sh`.
- **Lỗi `HACS package is invalid (missing custom_components/hacs)`**: installer đã tự thử fallback `git clone hacs/integration` khi archive không đúng layout; nếu vẫn lỗi, kiểm tra mạng GitHub rồi chạy lại `./install.sh`.
- **Frigate không xuất hiện trong Add Integration**: chạy lại `./install.sh`, sau đó `docker compose ps` để chắc `homeassistant` đang `Up`, đợi 30-60 giây và refresh trình duyệt HA.
- **Lỗi `Could not download Frigate HA integration` / `curl 404`**: installer mới đã tự thử nhiều URL fallback (main/master/release/codeload). Chạy lại `./install.sh` rồi kiểm tra lại.
- **Khi cài integration từ GitHub**: installer đã ẩn lỗi 404 của từng URL fallback để tránh gây hiểu nhầm; chỉ báo lỗi khi mọi URL đều thất bại.
- **Lỗi `This site can't be reached` (HA 8123 refused)**: chạy `docker compose ps` và `docker compose logs --tail=200 homeassistant` để kiểm tra container Home Assistant có đang chạy/crash không; sau đó chạy lại `./install.sh` để stack được `up -d --build` tự động.
- **`install.sh` báo hoàn tất nhưng không có container chạy**: bản mới sẽ fail-fast nếu `docker compose up` lỗi, running = 0, hoặc stack chỉ lên một phần (thiếu service đang running). Kiểm tra `docker compose ps -a` và `docker compose logs --tail=200 frigate`.
- **Lỗi Docker shim segfault (`unexpected fault address`, `failed to start shim`)**: `install.sh` sẽ chạy preflight `docker run --rm hello-world`, tự restart Docker daemon và retry. Nếu preflight vẫn fail, installer sẽ dừng sớm để tránh tạo stack nửa vời; khi đó reboot host rồi nâng cấp Docker/containerd (`sudo apt-get install --only-upgrade docker-ce docker-ce-cli containerd.io`).
- **Lỗi Cửa cuốn**: Kiểm tra kết nối Tuya trong Home Assistant hoặc file `core/door_controller.py`.
