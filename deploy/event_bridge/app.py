import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta

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

LEFT_EXIT_WINDOW_SECONDS = int(os.getenv("LEFT_EXIT_WINDOW_SECONDS", "30"))
LEFT_EXIT_MAX_EXTRA_PEOPLE = int(os.getenv("LEFT_EXIT_MAX_EXTRA_PEOPLE", "2"))
MAX_ACTIVE_VEHICLE_EXIT_SESSIONS = int(os.getenv("MAX_ACTIVE_VEHICLE_EXIT_SESSIONS", "2"))
VIRTUAL_GATE_LINE_X = int(os.getenv("VIRTUAL_GATE_LINE_X", "320"))
INSIDE_SIDE = os.getenv("INSIDE_SIDE", "right").lower()
GATE_DEBOUNCE_UPDATES = int(os.getenv("GATE_DEBOUNCE_UPDATES", "2"))
TRACK_TTL_SECONDS = int(os.getenv("TRACK_TTL_SECONDS", "300"))

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "10"))
ALERT_COOLDOWN_SECONDS = int(os.getenv("ALERT_COOLDOWN_SECONDS", "900"))
FRIGATE_BASE_URL = os.getenv("FRIGATE_BASE_URL", "http://frigate:5000")
FRIGATE_CAMERA = os.getenv("FRIGATE_CAMERA", "cam1")

ALERT_KEY_NO_ONE_GATE_OPEN = "no_one_gate_open"

app = FastAPI()

side_streaks = {}


def normalize_plate(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


def utc_now() -> str:
    return datetime.utcnow().isoformat()


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
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS counters_state (
            id INTEGER PRIMARY KEY,
            people_count INTEGER NOT NULL,
            vehicle_count INTEGER NOT NULL,
            updated_at_utc TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS object_tracks (
            track_key TEXT PRIMARY KEY,
            label TEXT,
            last_seen_utc TEXT,
            last_side TEXT,
            counted_in INTEGER NOT NULL,
            counted_out INTEGER NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS counter_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            label TEXT,
            direction TEXT,
            delta INTEGER,
            new_count INTEGER,
            track_key TEXT,
            source TEXT,
            note TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS vehicle_exit_sessions (
            session_id TEXT PRIMARY KEY,
            started_at_utc TEXT NOT NULL,
            camera TEXT,
            vehicle_track_key TEXT,
            active INTEGER NOT NULL,
            left_person_decrements INTEGER NOT NULL,
            max_left_person_decrements INTEGER NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS gate_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            gate_closed INTEGER NOT NULL,
            updated_at_utc TEXT NOT NULL,
            updated_by TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS alerts (
            alert_key TEXT PRIMARY KEY,
            last_sent_utc TEXT
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
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_counter_events_ts_utc ON counter_events (ts_utc)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_vehicle_exit_sessions_active ON vehicle_exit_sessions (active)"
    )
    conn.commit()
    conn.close()

    ensure_counters_state()
    ensure_gate_state()


def ensure_counters_state() -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM counters_state WHERE id = 1")
        row = cursor.fetchone()
        if not row:
            cursor.execute(
                "INSERT INTO counters_state (id, people_count, vehicle_count, updated_at_utc) VALUES (1, 0, 0, ?)",
                (utc_now(),),
            )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("Counters state init failed: %s", exc)


def ensure_gate_state() -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM gate_state WHERE id = 1")
        row = cursor.fetchone()
        if not row:
            cursor.execute(
                "INSERT INTO gate_state (id, gate_closed, updated_at_utc, updated_by) VALUES (1, 0, ?, ?)",
                (utc_now(), "system"),
            )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("Gate state init failed: %s", exc)


def get_counters() -> tuple[int, int]:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT people_count, vehicle_count FROM counters_state WHERE id = 1")
        row = cursor.fetchone()
        conn.close()
        if row:
            return int(row[0]), int(row[1])
    except sqlite3.Error as exc:
        logger.warning("Counters read failed: %s", exc)
    return 0, 0


def update_counters(people_count: int, vehicle_count: int) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE counters_state SET people_count = ?, vehicle_count = ?, updated_at_utc = ? WHERE id = 1",
            (people_count, vehicle_count, utc_now()),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("Counters update failed: %s", exc)


def get_gate_state() -> tuple[int, str | None, str | None]:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT gate_closed, updated_at_utc, updated_by FROM gate_state WHERE id = 1")
        row = cursor.fetchone()
        conn.close()
        if row:
            return int(row[0]), row[1], row[2]
    except sqlite3.Error as exc:
        logger.warning("Gate state read failed: %s", exc)
    return 0, None, None


def set_gate_state(gate_closed: int, updated_by: str) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE gate_state SET gate_closed = ?, updated_at_utc = ?, updated_by = ? WHERE id = 1",
            (gate_closed, utc_now(), updated_by),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("Gate state update failed: %s", exc)


def get_alert_last(alert_key: str) -> str | None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT last_sent_utc FROM alerts WHERE alert_key = ?", (alert_key,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[0]
    except sqlite3.Error as exc:
        logger.warning("Alert read failed: %s", exc)
    return None


def update_alert_last(alert_key: str, timestamp: str) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO alerts (alert_key, last_sent_utc) VALUES (?, ?) ON CONFLICT(alert_key) DO UPDATE SET last_sent_utc = excluded.last_sent_utc",
            (alert_key, timestamp),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("Alert update failed: %s", exc)


def log_counter_event(label: str, direction: str, delta: int, new_count: int, track_key: str, source: str, note: str) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO counter_events (ts_utc, label, direction, delta, new_count, track_key, source, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (utc_now(), label, direction, delta, new_count, track_key, source, note),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("Counter event log failed: %s", exc)


def get_track(track_key: str) -> dict | None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT track_key, label, last_seen_utc, last_side, counted_in, counted_out FROM object_tracks WHERE track_key = ?",
            (track_key,),
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            return {
                "track_key": row[0],
                "label": row[1],
                "last_seen_utc": row[2],
                "last_side": row[3],
                "counted_in": int(row[4]),
                "counted_out": int(row[5]),
            }
    except sqlite3.Error as exc:
        logger.warning("Track read failed: %s", exc)
    return None


def upsert_track(track_key: str, label: str, last_side: str | None) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO object_tracks (track_key, label, last_seen_utc, last_side, counted_in, counted_out)
            VALUES (?, ?, ?, ?, 0, 0)
            ON CONFLICT(track_key) DO UPDATE SET
                label=excluded.label,
                last_seen_utc=excluded.last_seen_utc,
                last_side=excluded.last_side
            """,
            (track_key, label, utc_now(), last_side),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("Track upsert failed: %s", exc)


def update_track_side(track_key: str, last_side: str | None) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE object_tracks SET last_seen_utc = ?, last_side = ? WHERE track_key = ?",
            (utc_now(), last_side, track_key),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("Track side update failed: %s", exc)


def mark_track_counted(track_key: str, direction: str) -> None:
    if direction not in {"in", "out"}:
        return
    field = "counted_in" if direction == "in" else "counted_out"
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE object_tracks SET {field} = 1, last_seen_utc = ? WHERE track_key = ?",
            (utc_now(), track_key),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("Track mark counted failed: %s", exc)


def cleanup_tracks() -> None:
    cutoff = datetime.utcnow() - timedelta(seconds=TRACK_TTL_SECONDS)
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM object_tracks WHERE last_seen_utc < ?", (cutoff.isoformat(),))
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("Track cleanup failed: %s", exc)


def close_expired_sessions() -> None:
    cutoff = datetime.utcnow() - timedelta(seconds=LEFT_EXIT_WINDOW_SECONDS)
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE vehicle_exit_sessions SET active = 0 WHERE active = 1 AND started_at_utc < ?",
            (cutoff.isoformat(),),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("Close expired sessions failed: %s", exc)


def enforce_session_limit() -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT session_id FROM vehicle_exit_sessions WHERE active = 1 ORDER BY started_at_utc ASC"
        )
        rows = cursor.fetchall()
        if rows and len(rows) > MAX_ACTIVE_VEHICLE_EXIT_SESSIONS:
            to_close = rows[: len(rows) - MAX_ACTIVE_VEHICLE_EXIT_SESSIONS]
            for row in to_close:
                cursor.execute(
                    "UPDATE vehicle_exit_sessions SET active = 0 WHERE session_id = ?",
                    (row[0],),
                )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("Session limit enforcement failed: %s", exc)


def create_vehicle_exit_session(camera: str, track_key: str) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO vehicle_exit_sessions (
                session_id, started_at_utc, camera, vehicle_track_key, active,
                left_person_decrements, max_left_person_decrements
            ) VALUES (?, ?, ?, ?, 1, 0, ?)
            """,
            (str(uuid.uuid4()), utc_now(), camera, track_key, LEFT_EXIT_MAX_EXTRA_PEOPLE),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("Create vehicle exit session failed: %s", exc)


def apply_left_exit_decrement() -> bool:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT session_id, left_person_decrements, max_left_person_decrements, started_at_utc
            FROM vehicle_exit_sessions
            WHERE active = 1
            ORDER BY started_at_utc DESC
            """
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return False
        session_id, left_dec, max_dec, started_at = row
        started_dt = datetime.fromisoformat(started_at)
        if datetime.utcnow() - started_dt > timedelta(seconds=LEFT_EXIT_WINDOW_SECONDS):
            cursor.execute(
                "UPDATE vehicle_exit_sessions SET active = 0 WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()
            conn.close()
            return False
        if left_dec >= max_dec:
            cursor.execute(
                "UPDATE vehicle_exit_sessions SET active = 0 WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()
            conn.close()
            return False
        cursor.execute(
            "UPDATE vehicle_exit_sessions SET left_person_decrements = left_person_decrements + 1 WHERE session_id = ?",
            (session_id,),
        )
        conn.commit()
        conn.close()
        return True
    except sqlite3.Error as exc:
        logger.warning("Apply left exit decrement failed: %s", exc)
        return False


def active_session_count() -> int:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM vehicle_exit_sessions WHERE active = 1")
        count = cursor.fetchone()[0]
        conn.close()
        return int(count)
    except sqlite3.Error as exc:
        logger.warning("Active session count failed: %s", exc)
        return 0


def send_telegram_message(chat_id: str, text: str) -> None:
    if not TELEGRAM_TOKEN or not chat_id:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=10)
    except requests.RequestException as exc:
        logger.warning("Telegram send failed: %s", exc)


def send_telegram_photo(chat_id: str, caption: str, image_bytes: bytes) -> bool:
    if not TELEGRAM_TOKEN or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        response = requests.post(
            url,
            data={"chat_id": chat_id, "caption": caption},
            files={"photo": ("snapshot.jpg", image_bytes)},
            timeout=15,
        )
        return response.ok
    except requests.RequestException as exc:
        logger.warning("Telegram sendPhoto failed: %s", exc)
        return False


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
            (plate_norm, label, utc_now(), added_by, None),
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
            (status, utc_now(), confirmed_by, plate_norm),
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
                utc_now(),
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
    ts_utc = utc_now()
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


def get_track_key(payload: dict) -> str | None:
    camera = payload.get("camera") or "cam"
    label = payload.get("label") or "unknown"
    track_id = payload.get("id") or payload.get("event_id")
    after = payload.get("after") or {}
    track_id = track_id or after.get("id") or after.get("event_id")
    if not track_id:
        return None
    return f"{camera}:{label}:{track_id}"


def infer_direction(payload: dict, track_key: str) -> tuple[str | None, str, str | None]:
    direction = payload.get("direction")
    if direction in {"in", "out"}:
        return direction, "frigate", None
    after = payload.get("after") or {}
    direction = after.get("direction")
    if direction in {"in", "out"}:
        return direction, "frigate", None

    box = payload.get("box") or after.get("box")
    if not isinstance(box, (list, tuple)) or len(box) < 4:
        return None, "none", None

    try:
        center_x = (float(box[0]) + float(box[2])) / 2.0
    except (TypeError, ValueError):
        return None, "none", None

    side = "left" if center_x < VIRTUAL_GATE_LINE_X else "right"
    track = get_track(track_key)
    last_side = track.get("last_side") if track else None

    if last_side == side:
        side_streaks[track_key] = side_streaks.get(track_key, 0) + 1
    else:
        side_streaks[track_key] = 1

    if side_streaks[track_key] < GATE_DEBOUNCE_UPDATES:
        update_track_side(track_key, side)
        return None, "virtual", side

    update_track_side(track_key, side)
    if last_side and last_side != side:
        if last_side == INSIDE_SIDE and side != INSIDE_SIDE:
            return "out", "virtual", side
        if last_side != INSIDE_SIDE and side == INSIDE_SIDE:
            return "in", "virtual", side

    return None, "virtual", side


def handle_counting(payload: dict) -> None:
    label = payload.get("label")
    if label not in {"person", "car", "truck"}:
        return

    track_key = get_track_key(payload)
    if not track_key:
        return

    cleanup_tracks()
    close_expired_sessions()
    enforce_session_limit()

    direction, source, side = infer_direction(payload, track_key)
    if side:
        upsert_track(track_key, label, side)
    else:
        upsert_track(track_key, label, None)

    track = get_track(track_key)
    if not track:
        return

    people_count, vehicle_count = get_counters()

    if direction == "in" and not track["counted_in"]:
        if label == "person":
            people_count += 1
            log_counter_event(label, "in", 1, people_count, track_key, source, "person_in")
        else:
            vehicle_count += 1
            log_counter_event(label, "in", 1, vehicle_count, track_key, source, "vehicle_in")
        mark_track_counted(track_key, "in")

    if direction == "out" and not track["counted_out"]:
        if label == "person":
            people_count = max(0, people_count - 1)
            applied_left = apply_left_exit_decrement()
            note = "left_side_exit_after_vehicle" if applied_left else "person_out"
            log_counter_event(label, "out", -1, people_count, track_key, source, note)
        else:
            vehicle_count = max(0, vehicle_count - 1)
            log_counter_event(label, "out", -1, vehicle_count, track_key, source, "vehicle_out")
            people_count = max(0, people_count - 1)
            log_counter_event("person", "out", -1, people_count, track_key, source, "driver_exit_assumed_right")
            create_vehicle_exit_session(payload.get("camera"), track_key)
        mark_track_counted(track_key, "out")

    update_counters(people_count, vehicle_count)


def fetch_snapshot() -> bytes | None:
    endpoints = [
        f"{FRIGATE_BASE_URL}/api/{FRIGATE_CAMERA}/latest.jpg",
        f"{FRIGATE_BASE_URL}/api/{FRIGATE_CAMERA}/snapshot.jpg",
    ]
    for url in endpoints:
        try:
            response = requests.get(url, timeout=10)
            if response.ok and response.content:
                return response.content
        except requests.RequestException as exc:
            logger.warning("Snapshot fetch failed from %s: %s", url, exc)
    return None


def alert_loop() -> None:
    while True:
        try:
            people_count, _ = get_counters()
            gate_closed, _, _ = get_gate_state()
            if people_count == 0 and gate_closed == 0:
                last_sent = get_alert_last(ALERT_KEY_NO_ONE_GATE_OPEN)
                now = datetime.utcnow()
                should_send = True
                if last_sent:
                    try:
                        last_dt = datetime.fromisoformat(last_sent)
                        should_send = (now - last_dt).total_seconds() >= ALERT_COOLDOWN_SECONDS
                    except ValueError:
                        should_send = True
                if should_send:
                    caption = (
                        "CẢNH BÁO QUAN TRỌNG: Không có ai trong lán nhưng cửa cuốn chưa đóng\n"
                        f"Thời gian: {now.isoformat()}\n"
                        f"people_count={people_count}"
                    )
                    snapshot = fetch_snapshot()
                    sent = False
                    if snapshot:
                        sent = send_telegram_photo(CHAT_ID_IMPORTANT, caption, snapshot)
                    if not sent:
                        send_telegram_message(CHAT_ID_IMPORTANT, caption)
                    update_alert_last(ALERT_KEY_NO_ONE_GATE_OPEN, now.isoformat())
        except Exception as exc:
            logger.warning("Alert loop error: %s", exc)
        finally:
            threading.Event().wait(CHECK_INTERVAL_SECONDS)


def on_mqtt_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except json.JSONDecodeError:
        logger.warning("Invalid JSON payload")
        return

    event_id = insert_event(payload)
    handle_plate_workflow(payload, event_id)
    handle_counting(payload)
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

    if cmd in {"/gate_closed", "/gate_open", "/gate_status"}:
        if cmd == "/gate_closed":
            set_gate_state(1, user_label)
            send_telegram_message(chat_id, "✅ Đã đặt trạng thái cửa: ĐÓNG")
        elif cmd == "/gate_open":
            set_gate_state(0, user_label)
            send_telegram_message(chat_id, "✅ Đã đặt trạng thái cửa: MỞ")
        else:
            gate_closed, updated_at, updated_by = get_gate_state()
            people_count, _ = get_counters()
            status = "ĐÓNG" if gate_closed == 1 else "MỞ"
            send_telegram_message(
                chat_id,
                f"Trạng thái cửa: {status}\nCập nhật: {updated_at} bởi {updated_by}\npeople_count={people_count}",
            )
        return {"ok": True}

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
    people_count, vehicle_count = get_counters()
    gate_closed, _, _ = get_gate_state()
    return {
        "status": "ok",
        "people_count": people_count,
        "vehicle_count": vehicle_count,
        "gate_closed": gate_closed,
        "last_alert_time": get_alert_last(ALERT_KEY_NO_ONE_GATE_OPEN),
        "active_exit_sessions": active_session_count(),
    }


def main() -> None:
    init_db()
    mqtt_thread = threading.Thread(target=start_mqtt_loop, daemon=True)
    mqtt_thread.start()
    alert_thread = threading.Thread(target=alert_loop, daemon=True)
    alert_thread.start()
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
