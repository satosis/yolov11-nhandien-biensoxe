-- deploy/postgres/init.sql
-- Khởi tạo schema PostgreSQL cho hệ thống Camera AI
-- Chạy tự động khi container postgres khởi động lần đầu

-- ============================================================
-- Bảng 1: plate_events — Sự kiện nhận diện biển số
-- ============================================================
CREATE TABLE IF NOT EXISTS plate_events (
    id              BIGSERIAL PRIMARY KEY,
    camera_id       VARCHAR(50) NOT NULL,
    plate_number    VARCHAR(20),
    vehicle_type    VARCHAR(20) CHECK (vehicle_type IN ('motorbike','car','truck','unknown')),
    confidence      FLOAT,
    direction       VARCHAR(10) CHECK (direction IN ('in','out','unknown')),
    image_path      TEXT,
    crossed_line    BOOLEAN DEFAULT FALSE,
    event_time      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_plate_events_event_time ON plate_events (event_time DESC);
CREATE INDEX IF NOT EXISTS idx_plate_events_camera_id ON plate_events (camera_id);
CREATE INDEX IF NOT EXISTS idx_plate_events_plate_number ON plate_events (plate_number);

-- ============================================================
-- Bảng 2: counting_stats — Đếm người/xe qua vạch theo giờ
-- ============================================================
CREATE TABLE IF NOT EXISTS counting_stats (
    id              BIGSERIAL PRIMARY KEY,
    camera_id       VARCHAR(50) NOT NULL,
    stat_date       DATE NOT NULL,
    stat_hour       SMALLINT NOT NULL CHECK (stat_hour BETWEEN 0 AND 23),
    people_in       INTEGER DEFAULT 0,
    people_out      INTEGER DEFAULT 0,
    vehicle_in      INTEGER DEFAULT 0,
    vehicle_out     INTEGER DEFAULT 0,
    motorbike_count INTEGER DEFAULT 0,
    car_count       INTEGER DEFAULT 0,
    truck_count     INTEGER DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (camera_id, stat_date, stat_hour)
);

CREATE INDEX IF NOT EXISTS idx_counting_stats_date ON counting_stats (stat_date DESC);
CREATE INDEX IF NOT EXISTS idx_counting_stats_camera ON counting_stats (camera_id, stat_date);

-- ============================================================
-- Bảng 3: plate_whitelist — Whitelist/blacklist biển số
-- ============================================================
CREATE TABLE IF NOT EXISTS plate_whitelist (
    id              BIGSERIAL PRIMARY KEY,
    plate_number    VARCHAR(20) NOT NULL UNIQUE,
    list_type       VARCHAR(10) NOT NULL CHECK (list_type IN ('white','black')),
    owner_name      VARCHAR(100),
    note            TEXT,
    added_by        VARCHAR(50),
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_whitelist_plate ON plate_whitelist (plate_number) WHERE is_active = TRUE;

-- ============================================================
-- Bảng 4: alert_history — Lịch sử cảnh báo Telegram
-- ============================================================
CREATE TABLE IF NOT EXISTS alert_history (
    id                  BIGSERIAL PRIMARY KEY,
    alert_type          VARCHAR(50) NOT NULL,
    camera_id           VARCHAR(50),
    plate_number        VARCHAR(20),
    message_text        TEXT,
    image_path          TEXT,
    telegram_chat_id    VARCHAR(50),
    telegram_message_id BIGINT,
    sent_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    acknowledged_at     TIMESTAMPTZ,
    acknowledged_by     VARCHAR(50)
);

CREATE INDEX IF NOT EXISTS idx_alert_history_sent_at ON alert_history (sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_history_type ON alert_history (alert_type);
CREATE INDEX IF NOT EXISTS idx_alert_history_plate ON alert_history (plate_number);

-- ============================================================
-- Bảng 5: camera_status_log — Trạng thái camera theo thời gian
-- ============================================================
CREATE TABLE IF NOT EXISTS camera_status_log (
    id                  BIGSERIAL PRIMARY KEY,
    camera_id           VARCHAR(50) NOT NULL,
    camera_name         VARCHAR(100),
    status              VARCHAR(20) NOT NULL CHECK (status IN ('online','offline','shift','error')),
    shift_score         FLOAT,
    shift_type          VARCHAR(30),
    baseline_updated_at TIMESTAMPTZ,
    logged_at           TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_camera_status_camera_id ON camera_status_log (camera_id, logged_at DESC);
CREATE INDEX IF NOT EXISTS idx_camera_status_logged_at ON camera_status_log (logged_at DESC);

-- ============================================================
-- Bảng 6: monthly_reports — Báo cáo tháng/ngày đã tổng hợp
-- ============================================================
CREATE TABLE IF NOT EXISTS monthly_reports (
    id              BIGSERIAL PRIMARY KEY,
    report_type     VARCHAR(10) NOT NULL CHECK (report_type IN ('daily','monthly')),
    report_date     DATE NOT NULL,
    camera_id       VARCHAR(50),
    total_vehicles  INTEGER DEFAULT 0,
    total_people    INTEGER DEFAULT 0,
    unknown_plates  INTEGER DEFAULT 0,
    alert_count     INTEGER DEFAULT 0,
    peak_hour       SMALLINT,
    chart_path      TEXT,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (report_type, report_date, camera_id)
);

CREATE INDEX IF NOT EXISTS idx_monthly_reports_date ON monthly_reports (report_date DESC);

-- ============================================================
-- Seed data mẫu
-- ============================================================

-- ============================================================
-- Bảng 7: app_settings — Cấu hình ứng dụng (thay thế .env)
-- ============================================================
CREATE TABLE IF NOT EXISTS app_settings (
    key        VARCHAR(100) PRIMARY KEY,
    value      TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO app_settings (key, value) VALUES
    ('TELEGRAM_TOKEN', ''),
    ('TELEGRAM_CHAT_IMPORTANT', ''),
    ('TELEGRAM_CHAT_NONIMPORTANT', ''),
    ('RTSP_URL', ''),
    ('CAMERA_MAC', ''),
    ('CAMERA_IP_SUBNET', ''),
    ('OCR_SOURCE', 'rtsp'),
    ('LINE_Y_RATIO', '0.62'),
    ('CAMERA_2_URL', ''),
    ('CAMERA_3_URL', ''),
    ('CAMERA_4_URL', ''),
    ('CAMERA_UI_USER', 'admin'),
    ('CAMERA_UI_PASS', ''),
    ('IMOU_OPEN_APP_ID', ''),
    ('IMOU_OPEN_APP_SECRET', ''),
    ('IMOU_OPEN_DEVICE_ID', '')
ON CONFLICT (key) DO NOTHING;

-- Whitelist biển số mẫu (chỉ insert nếu chưa có)
INSERT INTO plate_whitelist (plate_number, list_type, owner_name, note, added_by)
VALUES
    ('29A12345', 'white', 'Chủ xe', 'Xe chủ nhà kho', 'admin'),
    ('51G99999', 'white', 'Nhân viên bảo vệ', 'Ca sáng', 'admin')
ON CONFLICT (plate_number) DO NOTHING;
