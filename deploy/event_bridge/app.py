import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime

import paho.mqtt.client as mqtt
import requests
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("event_bridge")

MQTT_HOST = os.getenv("MQTT_HOST", "mosquitto")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "frigate/events")
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")

DB_PATH = os.getenv("DB_PATH", "/data/events.db")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID_IMPORTANT = os.getenv("TELEGRAM_CHAT_IMPORTANT")
CHAT_ID_NONIMPORTANT = os.getenv("TELEGRAM_CHAT_NONIMPORTANT")

TELEGRAM_WEBHOOK_SECRET_PATH = os.getenv("TELEGRAM_WEBHOOK_SECRET_PATH", "")
TELEGRAM_SECRET_TOKEN = os.getenv("TELEGRAM_SECRET_TOKEN", "")

IMPORTANT_LABELS = {"person", "truck"}
NONIMPORTANT_LABELS = {"car"}
ALLOWED_EVENT_TYPES = {"new", "end"}

app = FastAPI()


def normalize_plate(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            camera TEXT,
            event_type TEXT,
            label TEXT,
            sub_label TEXT,
            score REAL,
            zone TEXT,
            payload_json TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS vehicle_whitelist (
            plate_norm TEXT PRIMARY KEY,
            label TEXT,
            added_at_utc TEXT,
            added_by TEXT,
            note TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_plates (
            pending_id TEXT PRIMARY KEY,
            event_id INTEGER,
            plate_raw TEXT,
            plate_norm TEXT,
            first_seen_utc TEXT,
            status TEXT,
            confirmed_at_utc TEXT,
            confirmed_by TEXT
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_ts_utc ON events (ts_utc)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_label ON events (label)")
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_vehicle_whitelist_plate_norm ON vehicle_whitelist (plate_norm)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_plates_plate_norm ON pending_plates (plate_norm)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_pending_plates_status ON pending_plates (status)"
    )
    conn.commit()
    conn.close()


def send_telegram_message(chat_id: str, text: str) -> None:
    if not TELEGRAM_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except requests.RequestException as exc:
        logger.warning("Telegram send failed: %s", exc)


def is_plate_whitelisted(plate_norm: str) -> bool:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM vehicle_whitelist WHERE plate_norm = ? LIMIT 1",
            (plate_norm,),
        )
        row = cursor.fetchone()
        conn.close()
        return row is not None
    except sqlite3.Error as exc:
        logger.warning("Whitelist lookup failed: %s", exc)
        return False


def upsert_vehicle_whitelist(plate_norm: str, label: str, added_by: str) -> bool:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO vehicle_whitelist (plate_norm, label, added_at_utc, added_by, note)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(plate_norm) DO UPDATE SET
                label=excluded.label,
                added_at_utc=excluded.added_at_utc,
                added_by=excluded.added_by,
                note=excluded.note
            """,
            (plate_norm, label, datetime.utcnow().isoformat(), added_by, None),
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.Error as exc:
        logger.warning("Whitelist upsert failed: %s", exc)
        return False


def update_pending_status(plate_norm: str, status: str, confirmed_by: str) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE pending_plates
            SET status = ?, confirmed_at_utc = ?, confirmed_by = ?
            WHERE plate_norm = ? AND status = 'pending'
            """,
            (status, datetime.utcnow().isoformat(), confirmed_by, plate_norm),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("Pending status update failed: %s", exc)


def insert_pending_plate(event_id: int, plate_raw: str, plate_norm: str) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT OR IGNORE INTO pending_plates (
                pending_id, event_id, plate_raw, plate_norm, first_seen_utc, status
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                event_id,
                plate_raw,
                plate_norm,
                datetime.utcnow().isoformat(),
                "pending",
            ),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("Insert pending plate failed: %s", exc)


def extract_plate(payload: dict) -> str:
    for key in ("plate", "plate_text", "plate_number", "ocr_plate", "license_plate"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def insert_event(payload: dict) -> int:
    ts_utc = datetime.utcnow().isoformat()
    camera = payload.get("camera")
    event_type = payload.get("type")
    label = payload.get("label")
    sub_label = payload.get("sub_label")
    score = payload.get("top_score")
    zones = payload.get("zones") or []
    zone = zones[0] if isinstance(zones, list) and zones else None
    payload_json = json.dumps(payload, ensure_ascii=False)

    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO events (ts_utc, camera, event_type, label, sub_label, score, zone, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts_utc, camera, event_type, label, sub_label, score, zone, payload_json),
        )
        event_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return event_id
    except sqlite3.Error as exc:
        logger.warning("Insert event failed: %s", exc)
        return 0


def handle_plate_workflow(payload: dict, event_id: int) -> None:
    plate_raw = extract_plate(payload)
    plate_norm = normalize_plate(plate_raw)
    if not plate_norm:
        return
    if is_plate_whitelisted(plate_norm):
        return
    insert_pending_plate(event_id, plate_raw, plate_norm)
    if CHAT_ID_NONIMPORTANT:
        send_telegram_message(
            CHAT_ID_NONIMPORTANT,
            f"Xe lạ phát hiện: {plate_norm}\n"
            f"Xác nhận:\n/mine {plate_norm}\n/staff {plate_norm}\n/reject {plate_norm}",
        )


def maybe_notify_telegram(payload: dict) -> None:
    event_type = payload.get("type")
    if event_type not in ALLOWED_EVENT_TYPES:
        return
    label = payload.get("label") or "unknown"
    message = f"Frigate {event_type}: {label}"
    if label in IMPORTANT_LABELS:
        send_telegram_message(CHAT_ID_IMPORTANT, message)
    elif label in NONIMPORTANT_LABELS:
        send_telegram_message(CHAT_ID_NONIMPORTANT, message)
    else:
        send_telegram_message(CHAT_ID_NONIMPORTANT, message)


def on_mqtt_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except json.JSONDecodeError:
        logger.warning("Invalid JSON payload")
        return

    event_id = insert_event(payload)
    handle_plate_workflow(payload, event_id)
    maybe_notify_telegram(payload)


def start_mqtt_loop() -> None:
    client = mqtt.Client()
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    client.on_message = on_mqtt_message

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            logger.info("MQTT connected")
            client.subscribe(MQTT_TOPIC)
        else:
            logger.warning("MQTT connect failed: %s", rc)

    def on_disconnect(client, userdata, rc):
        logger.warning("MQTT disconnected: %s", rc)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
            client.loop_forever()
        except Exception as exc:
            logger.warning("MQTT loop error: %s", exc)
            try:
                client.disconnect()
            except Exception:
                pass


@app.post("/telegram/webhook/{secret_path}")
async def telegram_webhook(
    secret_path: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(
        default=None, alias="X-Telegram-Bot-Api-Secret-Token"
    ),
):
    if TELEGRAM_WEBHOOK_SECRET_PATH and secret_path != TELEGRAM_WEBHOOK_SECRET_PATH:
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    if TELEGRAM_SECRET_TOKEN and x_telegram_bot_api_secret_token != TELEGRAM_SECRET_TOKEN:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})

    try:
        update = await request.json()
    except Exception:
        return {"ok": True}

    message = update.get("message") or update.get("edited_message") or {}
    text = message.get("text") or ""
    chat_id = (message.get("chat") or {}).get("id")
    user = message.get("from") or {}
    user_label = user.get("username") or str(user.get("id") or "unknown")

    if not text.startswith("/") or not chat_id:
        return {"ok": True}

    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower()
    plate_raw = parts[1] if len(parts) > 1 else ""
    plate_norm = normalize_plate(plate_raw)

    if cmd in {"/mine", "/staff", "/reject"} and not plate_norm:
        send_telegram_message(chat_id, "Thiếu biển số. Ví dụ: /mine 51A12345")
        return {"ok": True}

    if cmd == "/mine":
        if upsert_vehicle_whitelist(plate_norm, "mine", user_label):
            update_pending_status(plate_norm, "approved_mine", user_label)
            send_telegram_message(chat_id, f"✅ Đã thêm {plate_norm} vào whitelist (mine).")
        else:
            send_telegram_message(chat_id, f"⚠️ Không thể cập nhật whitelist cho {plate_norm}.")
    elif cmd == "/staff":
        if upsert_vehicle_whitelist(plate_norm, "staff", user_label):
            update_pending_status(plate_norm, "approved_staff", user_label)
            send_telegram_message(chat_id, f"✅ Đã thêm {plate_norm} vào whitelist (staff).")
        else:
            send_telegram_message(chat_id, f"⚠️ Không thể cập nhật whitelist cho {plate_norm}.")
    elif cmd == "/reject":
        update_pending_status(plate_norm, "rejected", user_label)
        send_telegram_message(chat_id, f"✅ Đã từ chối {plate_norm}.")

    return {"ok": True}


@app.get("/health")
async def health():
    return {"status": "ok"}


def main() -> None:
    init_db()
    mqtt_thread = threading.Thread(target=start_mqtt_loop, daemon=True)
    mqtt_thread.start()
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
