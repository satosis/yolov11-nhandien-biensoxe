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
from onvif import ONVIFCamera
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("event_bridge")

MQTT_HOST = "mosquitto"
MQTT_PORT = 1883
MQTT_TOPIC = "frigate/events"
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")

DB_PATH = "/data/events.db"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID_IMPORTANT = os.getenv("TELEGRAM_CHAT_IMPORTANT")
CHAT_ID_NONIMPORTANT = os.getenv("TELEGRAM_CHAT_NONIMPORTANT")

IMPORTANT_LABELS = {"person", "truck"}
NONIMPORTANT_LABELS = {"car"}
ALLOWED_EVENT_TYPES = {"new", "end"}

LEFT_EXIT_WINDOW_SECONDS = 30  # (30 giây)
LEFT_EXIT_MAX_EXTRA_PEOPLE = 4  # Cố định tối đa 4 người đi kèm xe
MAX_ACTIVE_VEHICLE_EXIT_SESSIONS = 2  # Cố định tối đa 2 phiên xe thoát cùng lúc
VIRTUAL_GATE_LINE_X = 320
INSIDE_SIDE = "right"
GATE_DEBOUNCE_UPDATES = 2
TRACK_TTL_SECONDS = 300

CHECK_INTERVAL_SECONDS = 10
ALERT_COOLDOWN_SECONDS = 900
FRIGATE_BASE_URL = "http://frigate:5000"
FRIGATE_CAMERA = "cam1"

DRIVER_LINK_WINDOW_SECONDS = 60
DEDUPE_SECONDS = 15
MATCH_VEHICLE_REENTRY_SECONDS = 86400

PTZ_AUTO_RETURN_SECONDS = 300

ONVIF_HOST = os.getenv("ONVIF_HOST", "")
ONVIF_PORT = 80
ONVIF_USER = os.getenv("ONVIF_USER", "")
ONVIF_PASS = os.getenv("ONVIF_PASS", "")
ONVIF_PROFILE_TOKEN = os.getenv("ONVIF_PROFILE_TOKEN", "")
ONVIF_PRESET_GATE = os.getenv("ONVIF_PRESET_GATE", "")
ONVIF_PRESET_PANORAMA = os.getenv("ONVIF_PRESET_PANORAMA", "")
EVENT_BRIDGE_TEST_MODE = False
ONVIF_SIMULATE_FAIL = False

# Relay control for garage door
# Relay control for garage door
RELAY_TYPE = "gpio"
RELAY_HTTP_URL = ""
RELAY_GPIO_PIN = 11  # Orange Pi 4 Pro (Physical Pin 11)

# Door state
door_state_lock = threading.Lock()
door_state = "closed"  # open, closed, opening, closing

ALERT_KEY_NO_ONE_GATE_OPEN = "no_one_gate_open"

STATE_TOPICS = {
    "people_count": "shed/state/people_count",
    "vehicle_count": "shed/state/vehicle_count",
    "gate_closed": "shed/state/gate_closed",
    "ptz_mode": "shed/state/ptz_mode",
    "ocr_enabled": "shed/state/ocr_enabled",
    "last_view_utc": "shed/state/last_view_utc",
    "door": "shed/state/door",
}

COMMAND_TOPICS = {
    "shed/cmd/gate_open",
    "shed/cmd/gate_closed",
    "shed/cmd/ptz_panorama",
    "shed/cmd/ptz_gate",
    "shed/cmd/view_heartbeat",
    "shed/cmd/door",
}

app = FastAPI()

side_streaks: dict[str, int] = {}
ptz_state_lock = threading.Lock()
ptz_state_cache = {
    "mode": "gate",
    "ocr_enabled": 1,
    "last_view_utc": None,
    "updated_at_utc": None,
    "updated_by": None,
}

mqtt_client: mqtt.Client | None = None


TELEGRAM_BOT_COMMANDS = [
    {"command": "gate_open", "description": "Mở trạng thái cổng"},
    {"command": "gate_closed", "description": "Đóng trạng thái cổng"},
    {"command": "gate_status", "description": "Xem trạng thái cổng"},
    {"command": "mine", "description": "Thêm biển số whitelist mine"},
    {"command": "staff", "description": "Thêm biển số whitelist staff"},
    {"command": "reject", "description": "Từ chối biển số pending"},
    {"command": "person_add", "description": "Thêm person_identity"},
    {"command": "person_list", "description": "Xem danh sách person_identity"},
]


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
        CREATE TABLE IF NOT EXISTS ptz_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            mode TEXT NOT NULL,
            ocr_enabled INTEGER NOT NULL,
            last_view_utc TEXT,
            updated_at_utc TEXT NOT NULL,
            updated_by TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ptz_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            action TEXT NOT NULL,
            reason TEXT,
            prev_mode TEXT,
            new_mode TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS ptz_test_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            preset TEXT NOT NULL,
            success INTEGER NOT NULL
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
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS person_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_key TEXT,
            camera TEXT,
            entered_at_utc TEXT NOT NULL,
            exited_at_utc TEXT,
            source TEXT,
            confidence REAL
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS vehicle_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_key TEXT,
            plate_norm TEXT,
            vehicle_type TEXT,
            camera TEXT,
            entered_at_utc TEXT NOT NULL,
            exited_at_utc TEXT,
            time_outside_seconds INTEGER,
            source TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS driver_attribution (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            direction TEXT NOT NULL,
            person_identity TEXT,
            vehicle_identity TEXT,
            vehicle_session_id INTEGER,
            person_session_id INTEGER,
            evidence_json TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS gate_alert_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            gate_closed INTEGER NOT NULL,
            people_count INTEGER NOT NULL,
            note TEXT,
            snapshot_path TEXT
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_aggregates (
            day_utc TEXT NOT NULL,
            person_identity TEXT NOT NULL,
            vehicle_identity TEXT NOT NULL,
            direction TEXT NOT NULL,
            trips_count INTEGER NOT NULL,
            PRIMARY KEY(day_utc, person_identity, vehicle_identity, direction)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS people_whitelist (
            person_identity TEXT PRIMARY KEY,
            note TEXT,
            added_at_utc TEXT,
            added_by TEXT
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
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_person_sessions_entered ON person_sessions (entered_at_utc)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_vehicle_sessions_entered ON vehicle_sessions (entered_at_utc)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_driver_attribution_ts_utc ON driver_attribution (ts_utc)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_driver_attribution_person ON driver_attribution (person_identity)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_driver_attribution_vehicle ON driver_attribution (vehicle_identity)"
    )
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_gate_alert_events_ts_utc ON gate_alert_events (ts_utc)"
    )
    conn.commit()
    conn.close()

    ensure_counters_state()
    ensure_gate_state()
    ensure_ptz_state()


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


def ensure_ptz_state() -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM ptz_state WHERE id = 1")
        row = cursor.fetchone()
        if not row:
            cursor.execute(
                "INSERT INTO ptz_state (id, mode, ocr_enabled, last_view_utc, updated_at_utc, updated_by) VALUES (1, ?, 1, NULL, ?, ?)",
                ("gate", utc_now(), "system"),
            )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("PTZ state init failed: %s", exc)


def mqtt_publish(topic: str, payload: str, retain: bool = True) -> None:
    client = mqtt_client
    if not client:
        return
    try:
        client.publish(topic, payload=payload, qos=0, retain=retain)
    except Exception as exc:
        logger.warning("MQTT publish failed for %s: %s", topic, exc)


def publish_discovery() -> None:
    device = {
        "identifiers": ["shed_controller"],
        "name": "Shed Controller",
        "manufacturer": "custom",
    }

    discovery_payloads = {
        "homeassistant/sensor/shed_people_count/config": {
            "name": "Shed People Count",
            "state_topic": STATE_TOPICS["people_count"],
            "unique_id": "shed_people_count",
            "device": device,
        },
        "homeassistant/sensor/shed_vehicle_count/config": {
            "name": "Shed Vehicle Count",
            "state_topic": STATE_TOPICS["vehicle_count"],
            "unique_id": "shed_vehicle_count",
            "device": device,
        },
        "homeassistant/binary_sensor/shed_gate_closed/config": {
            "name": "Shed Gate Closed",
            "state_topic": STATE_TOPICS["gate_closed"],
            "payload_on": "1",
            "payload_off": "0",
            "unique_id": "shed_gate_closed",
            "device": device,
        },
        "homeassistant/button/shed_gate_open/config": {
            "name": "Shed Gate Open",
            "command_topic": "shed/cmd/gate_open",
            "unique_id": "shed_gate_open",
            "device": device,
        },
        "homeassistant/button/shed_gate_closed/config": {
            "name": "Shed Gate Closed",
            "command_topic": "shed/cmd/gate_closed",
            "unique_id": "shed_gate_closed",
            "device": device,
        },
        "homeassistant/button/shed_ptz_panorama/config": {
            "name": "PTZ Panorama",
            "command_topic": "shed/cmd/ptz_panorama",
            "unique_id": "shed_ptz_panorama",
            "device": device,
        },
        "homeassistant/button/shed_ptz_gate/config": {
            "name": "PTZ Gate",
            "command_topic": "shed/cmd/ptz_gate",
            "unique_id": "shed_ptz_gate",
            "device": device,
        },
        "homeassistant/sensor/shed_ptz_mode/config": {
            "name": "PTZ Mode",
            "state_topic": STATE_TOPICS["ptz_mode"],
            "unique_id": "shed_ptz_mode",
            "device": device,
        },
        "homeassistant/binary_sensor/shed_ocr_enabled/config": {
            "name": "OCR Enabled",
            "state_topic": STATE_TOPICS["ocr_enabled"],
            "payload_on": "1",
            "payload_off": "0",
            "unique_id": "shed_ocr_enabled",
            "device": device,
        },
    }

    for topic, payload in discovery_payloads.items():
        mqtt_publish(topic, json.dumps(payload, ensure_ascii=False), retain=True)


def publish_state() -> None:
    people_count, vehicle_count = get_counters()
    gate_closed, _, _ = get_gate_state()
    ptz_state = get_ptz_state()

    mqtt_publish(STATE_TOPICS["people_count"], str(people_count))
    mqtt_publish(STATE_TOPICS["vehicle_count"], str(vehicle_count))
    mqtt_publish(STATE_TOPICS["gate_closed"], str(gate_closed))
    mqtt_publish(STATE_TOPICS["ptz_mode"], ptz_state["mode"])
    mqtt_publish(STATE_TOPICS["ocr_enabled"], str(ptz_state["ocr_enabled"]))
    mqtt_publish(STATE_TOPICS["last_view_utc"], ptz_state.get("last_view_utc") or "")
    
    with door_state_lock:
        current_door_state = door_state
    mqtt_publish(STATE_TOPICS["door"], current_door_state)


def get_ptz_state() -> dict:
    with ptz_state_lock:
        cached = ptz_state_cache.copy()
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT mode, ocr_enabled, last_view_utc, updated_at_utc, updated_by FROM ptz_state WHERE id = 1"
        )
        row = cursor.fetchone()
        conn.close()
        if row:
            state = {
                "mode": row[0],
                "ocr_enabled": int(row[1]),
                "last_view_utc": row[2],
                "updated_at_utc": row[3],
                "updated_by": row[4],
            }
            with ptz_state_lock:
                ptz_state_cache.update(state)
            return state
    except sqlite3.Error as exc:
        logger.warning("PTZ state read failed: %s", exc)
    return cached


def set_ptz_state(mode: str, ocr_enabled: int, updated_by: str, last_view_utc: str | None = None) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE ptz_state SET mode = ?, ocr_enabled = ?, last_view_utc = ?, updated_at_utc = ?, updated_by = ? WHERE id = 1",
            (mode, ocr_enabled, last_view_utc, utc_now(), updated_by),
        )
        conn.commit()
        conn.close()
        with ptz_state_lock:
            ptz_state_cache.update(
                {
                    "mode": mode,
                    "ocr_enabled": ocr_enabled,
                    "last_view_utc": last_view_utc,
                    "updated_at_utc": utc_now(),
                    "updated_by": updated_by,
                }
            )
    except sqlite3.Error as exc:
        logger.warning("PTZ state update failed: %s", exc)
    publish_state()


def insert_ptz_event(action: str, reason: str, prev_mode: str | None, new_mode: str | None) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO ptz_events (ts_utc, action, reason, prev_mode, new_mode)
            VALUES (?, ?, ?, ?, ?)
            """,
            (utc_now(), action, reason, prev_mode, new_mode),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("PTZ event insert failed: %s", exc)


def update_ptz_last_view(updated_by: str) -> None:
    state = get_ptz_state()
    if state["mode"] != "panorama":
        return
    last_view = utc_now()
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE ptz_state SET last_view_utc = ?, updated_at_utc = ?, updated_by = ? WHERE id = 1",
            (last_view, utc_now(), updated_by),
        )
        conn.commit()
        conn.close()
        with ptz_state_lock:
            ptz_state_cache.update({"last_view_utc": last_view, "updated_at_utc": utc_now(), "updated_by": updated_by})
    except sqlite3.Error as exc:
        logger.warning("PTZ last_view update failed: %s", exc)
    publish_state()


def is_ocr_enabled() -> bool:
    return get_ptz_state().get("ocr_enabled", 1) == 1


def ptz_goto_preset(preset_token: str) -> bool:
    if not (ONVIF_HOST and ONVIF_USER and ONVIF_PASS and preset_token):
        logger.warning("ONVIF preset not configured; skipping PTZ move")
        return False
    if EVENT_BRIDGE_TEST_MODE:
        success = 0 if ONVIF_SIMULATE_FAIL else 1
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO ptz_test_calls (ts_utc, preset, success)
                VALUES (?, ?, ?)
                """,
                (utc_now(), preset_token, success),
            )
            conn.commit()
            conn.close()
        except sqlite3.Error as exc:
            logger.warning("PTZ test call insert failed: %s", exc)
        if not success:
            logger.warning("ONVIF simulate failure enabled; skipping PTZ move")
            return False
        return True
    try:
        camera = ONVIFCamera(ONVIF_HOST, ONVIF_PORT, ONVIF_USER, ONVIF_PASS)
        media = camera.create_media_service()
        ptz = camera.create_ptz_service()
        if ONVIF_PROFILE_TOKEN:
            profile = {"token": ONVIF_PROFILE_TOKEN}
        else:
            profiles = media.GetProfiles()
            if not profiles:
                logger.warning("No ONVIF profiles found")
                return False
            profile = profiles[0]
        ptz.GotoPreset({"ProfileToken": profile["token"], "PresetToken": preset_token})
        return True
    except Exception as exc:
        logger.warning("ONVIF goto preset failed: %s", exc)
        return False


def ensure_state_publish_loop() -> None:
    while True:
        try:
            publish_state()
        except Exception as exc:
            logger.warning("State publish loop error: %s", exc)
        finally:
            threading.Event().wait(30)


def auto_return_loop() -> None:
    while True:
        try:
            state = get_ptz_state()
            if state["mode"] == "panorama":
                last_view = state.get("last_view_utc")
                if last_view:
                    try:
                        last_dt = datetime.fromisoformat(last_view)
                        idle_seconds = (datetime.utcnow() - last_dt).total_seconds()
                    except ValueError:
                        idle_seconds = PTZ_AUTO_RETURN_SECONDS
                else:
                    idle_seconds = PTZ_AUTO_RETURN_SECONDS

                if idle_seconds >= PTZ_AUTO_RETURN_SECONDS:
                    if ptz_goto_preset(ONVIF_PRESET_GATE):
                        prev_mode = state["mode"]
                        set_ptz_state("gate", 1, "auto", None)
                        insert_ptz_event("auto_return", "no_heartbeat_5m", prev_mode, "gate")
                        log_counter_event(
                            "ptz",
                            "auto",
                            0,
                            0,
                            "ptz",
                            "auto",
                            "auto_return_no_viewers",
                        )
        except Exception as exc:
            logger.warning("Auto return loop error: %s", exc)
        finally:
            threading.Event().wait(10)


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
    publish_state()


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
    publish_state()


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


def log_counter_event(
    label: str, direction: str, delta: int, new_count: int, track_key: str, source: str, note: str
) -> None:
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


def configure_telegram_commands() -> None:
    if not TELEGRAM_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setMyCommands"
    payloads = [
        {"commands": TELEGRAM_BOT_COMMANDS},
        {"scope": {"type": "all_group_chats"}, "commands": TELEGRAM_BOT_COMMANDS},
    ]
    for payload in payloads:
        try:
            response = requests.post(url, json=payload, timeout=10)
            if not response.ok:
                logger.warning("setMyCommands failed: %s", response.text)
        except requests.RequestException as exc:
            logger.warning("Telegram setMyCommands failed: %s", exc)


def telegram_help_text() -> str:
    return (
        "📌 Lệnh điều khiển:\n"
        "/gate_open - Mở trạng thái cổng\n"
        "/gate_closed - Đóng trạng thái cổng\n"
        "/gate_status - Xem trạng thái cổng\n"
        "/mine <bienso> - Duyệt whitelist mine\n"
        "/staff <bienso> - Duyệt whitelist staff\n"
        "/reject <bienso> - Từ chối pending\n"
        "/person_add <ten> - Thêm person_identity\n"
        "/person_list - Xem danh sách person_identity"
    )


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
    if not is_ocr_enabled():
        return
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


def resolve_vehicle_identity(plate_norm: str | None, session_id: int | None) -> str:
    if plate_norm:
        return plate_norm
    if session_id:
        return f"unknown_vehicle_{session_id}"
    return "unknown_vehicle"


def open_person_session(person_key: str | None, camera: str | None, source: str) -> int | None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO person_sessions (person_key, camera, entered_at_utc, source)
            VALUES (?, ?, ?, ?)
            """,
            (person_key, camera, utc_now(), source),
        )
        session_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return session_id
    except sqlite3.Error as exc:
        logger.warning("Open person session failed: %s", exc)
        return None


def close_person_session(person_key: str | None) -> int | None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        if person_key:
            cursor.execute(
                """
                SELECT id FROM person_sessions
                WHERE person_key = ? AND exited_at_utc IS NULL
                ORDER BY entered_at_utc DESC LIMIT 1
                """,
                (person_key,),
            )
        else:
            cursor.execute(
                """
                SELECT id FROM person_sessions
                WHERE exited_at_utc IS NULL
                ORDER BY entered_at_utc DESC LIMIT 1
                """
            )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return None
        session_id = row[0]
        cursor.execute(
            "UPDATE person_sessions SET exited_at_utc = ? WHERE id = ?",
            (utc_now(), session_id),
        )
        conn.commit()
        conn.close()
        return session_id
    except sqlite3.Error as exc:
        logger.warning("Close person session failed: %s", exc)
        return None


def find_recent_person_session(direction: str, event_time: datetime) -> int | None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        if direction == "out":
            cursor.execute(
                """
                SELECT id, exited_at_utc FROM person_sessions
                WHERE exited_at_utc IS NOT NULL
                ORDER BY exited_at_utc DESC LIMIT 1
                """
            )
        else:
            cursor.execute(
                """
                SELECT id, entered_at_utc FROM person_sessions
                ORDER BY entered_at_utc DESC LIMIT 1
                """
            )
        row = cursor.fetchone()
        conn.close()
        if not row:
            return None
        ts = row[1]
        if not ts:
            return None
        ts_dt = datetime.fromisoformat(ts)
        if abs((event_time - ts_dt).total_seconds()) <= DRIVER_LINK_WINDOW_SECONDS:
            return row[0]
    except sqlite3.Error as exc:
        logger.warning("Find recent person session failed: %s", exc)
    return None


def open_vehicle_session(
    vehicle_key: str | None,
    plate_norm: str | None,
    vehicle_type: str,
    camera: str | None,
    source: str,
) -> int | None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO vehicle_sessions (vehicle_key, plate_norm, vehicle_type, camera, entered_at_utc, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (vehicle_key, plate_norm, vehicle_type, camera, utc_now(), source),
        )
        session_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return session_id
    except sqlite3.Error as exc:
        logger.warning("Open vehicle session failed: %s", exc)
        return None


def close_vehicle_session(vehicle_key: str | None, plate_norm: str | None, vehicle_type: str) -> int | None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        if plate_norm:
            cursor.execute(
                """
                SELECT id FROM vehicle_sessions
                WHERE plate_norm = ? AND exited_at_utc IS NULL
                ORDER BY entered_at_utc DESC LIMIT 1
                """,
                (plate_norm,),
            )
        elif vehicle_key:
            cursor.execute(
                """
                SELECT id FROM vehicle_sessions
                WHERE vehicle_key = ? AND exited_at_utc IS NULL
                ORDER BY entered_at_utc DESC LIMIT 1
                """,
                (vehicle_key,),
            )
        else:
            cursor.execute(
                """
                SELECT id FROM vehicle_sessions
                WHERE vehicle_type = ? AND exited_at_utc IS NULL
                ORDER BY entered_at_utc DESC LIMIT 1
                """,
                (vehicle_type,),
            )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return None
        session_id = row[0]
        cursor.execute(
            "UPDATE vehicle_sessions SET exited_at_utc = ? WHERE id = ?",
            (utc_now(), session_id),
        )
        conn.commit()
        conn.close()
        return session_id
    except sqlite3.Error as exc:
        logger.warning("Close vehicle session failed: %s", exc)
        return None


def update_time_outside(
    plate_norm: str | None, vehicle_key: str | None, vehicle_type: str, entered_at: str
) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cutoff = datetime.utcnow() - timedelta(seconds=MATCH_VEHICLE_REENTRY_SECONDS)
        if plate_norm:
            cursor.execute(
                """
                SELECT id, exited_at_utc FROM vehicle_sessions
                WHERE plate_norm = ? AND exited_at_utc IS NOT NULL AND time_outside_seconds IS NULL
                ORDER BY exited_at_utc DESC LIMIT 1
                """,
                (plate_norm,),
            )
        elif vehicle_key:
            cursor.execute(
                """
                SELECT id, exited_at_utc FROM vehicle_sessions
                WHERE vehicle_key = ? AND exited_at_utc IS NOT NULL AND time_outside_seconds IS NULL
                ORDER BY exited_at_utc DESC LIMIT 1
                """,
                (vehicle_key,),
            )
        else:
            cursor.execute(
                """
                SELECT id, exited_at_utc FROM vehicle_sessions
                WHERE vehicle_type = ? AND exited_at_utc IS NOT NULL AND time_outside_seconds IS NULL
                ORDER BY exited_at_utc DESC LIMIT 1
                """,
                (vehicle_type,),
            )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return
        session_id, exited_at = row
        exited_dt = datetime.fromisoformat(exited_at)
        entered_dt = datetime.fromisoformat(entered_at)
        if exited_dt < cutoff:
            conn.close()
            return
        delta = int((entered_dt - exited_dt).total_seconds())
        if delta < 0:
            conn.close()
            return
        cursor.execute(
            "UPDATE vehicle_sessions SET time_outside_seconds = ? WHERE id = ?",
            (delta, session_id),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("Update time outside failed: %s", exc)


def insert_driver_attribution(
    direction: str,
    person_identity: str,
    vehicle_identity: str,
    vehicle_session_id: int | None,
    person_session_id: int | None,
    evidence: dict,
) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cutoff = datetime.utcnow() - timedelta(seconds=DEDUPE_SECONDS)
        cursor.execute(
            """
            SELECT id FROM driver_attribution
            WHERE person_identity = ? AND vehicle_identity = ? AND direction = ? AND ts_utc >= ?
            ORDER BY ts_utc DESC LIMIT 1
            """,
            (person_identity, vehicle_identity, direction, cutoff.isoformat()),
        )
        if cursor.fetchone():
            conn.close()
            return
        cursor.execute(
            """
            INSERT INTO driver_attribution (ts_utc, direction, person_identity, vehicle_identity, vehicle_session_id, person_session_id, evidence_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                direction,
                person_identity,
                vehicle_identity,
                vehicle_session_id,
                person_session_id,
                json.dumps(evidence),
            ),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("Insert driver attribution failed: %s", exc)


def get_person_identity_for_session(session_id: int | None) -> str:
    return "unknown_person"


def save_snapshot(snapshot_bytes: bytes) -> str | None:
    try:
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        dir_path = os.path.join(os.path.dirname(DB_PATH), "snapshots")
        os.makedirs(dir_path, exist_ok=True)
        path = os.path.join(dir_path, f"gate_alert_{ts}.jpg")
        with open(path, "wb") as f:
            f.write(snapshot_bytes)
        return path
    except OSError as exc:
        logger.warning("Snapshot save failed: %s", exc)
        return None


def insert_gate_alert_event(
    gate_closed: int, people_count: int, note: str, snapshot_path: str | None
) -> None:
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO gate_alert_events (ts_utc, gate_closed, people_count, note, snapshot_path)
            VALUES (?, ?, ?, ?, ?)
            """,
            (utc_now(), gate_closed, people_count, note, snapshot_path),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        logger.warning("Gate alert event insert failed: %s", exc)


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
    ocr_enabled = is_ocr_enabled()

    if direction == "in" and not track["counted_in"]:
        if label == "person":
            people_count += 1
            log_counter_event(label, "in", 1, people_count, track_key, source, "person_in")
            open_person_session(track_key, payload.get("camera"), source)
            person_session_id = find_recent_person_session("in", datetime.utcnow())
            person_identity = get_person_identity_for_session(person_session_id)
            insert_driver_attribution(
                "in",
                person_identity,
                "unknown_vehicle",
                None,
                person_session_id,
                {"reason": "person_in_only"},
            )
        else:
            vehicle_count += 1
            log_counter_event(label, "in", 1, vehicle_count, track_key, source, "vehicle_in")
            plate_norm = normalize_plate(extract_plate(payload)) if ocr_enabled else None
            if plate_norm == "":
                plate_norm = None
            vehicle_session_id = open_vehicle_session(
                track_key, plate_norm, label, payload.get("camera"), source
            )
            if vehicle_session_id:
                update_time_outside(plate_norm, track_key, label, utc_now())
                vehicle_identity = resolve_vehicle_identity(plate_norm, vehicle_session_id)
                person_session_id = find_recent_person_session("in", datetime.utcnow())
                person_identity = get_person_identity_for_session(person_session_id)
                evidence = {"reason": "vehicle_in_link", "vehicle_session_id": vehicle_session_id}
                insert_driver_attribution(
                    "in",
                    person_identity,
                    vehicle_identity,
                    vehicle_session_id,
                    person_session_id,
                    evidence,
                )
        mark_track_counted(track_key, "in")

    if direction == "out" and not track["counted_out"]:
        if label == "person":
            people_count = max(0, people_count - 1)
            applied_left = apply_left_exit_decrement()
            note = "left_side_exit_after_vehicle" if applied_left else "person_out"
            log_counter_event(label, "out", -1, people_count, track_key, source, note)
            person_session_id = close_person_session(track_key)
            person_identity = get_person_identity_for_session(person_session_id)
            insert_driver_attribution(
                "out",
                person_identity,
                "unknown_vehicle",
                None,
                person_session_id,
                {"reason": "person_out_only"},
            )
        else:
            vehicle_count = max(0, vehicle_count - 1)
            log_counter_event(label, "out", -1, vehicle_count, track_key, source, "vehicle_out")
            people_count = max(0, people_count - 1)
            log_counter_event(
                "person",
                "out",
                -1,
                people_count,
                track_key,
                source,
                "driver_exit_assumed_right",
            )
            create_vehicle_exit_session(payload.get("camera"), track_key)
            plate_norm = normalize_plate(extract_plate(payload)) if ocr_enabled else None
            if plate_norm == "":
                plate_norm = None
            vehicle_session_id = close_vehicle_session(track_key, plate_norm, label)
            vehicle_identity = resolve_vehicle_identity(plate_norm, vehicle_session_id)
            person_session_id = find_recent_person_session("out", datetime.utcnow())
            person_identity = get_person_identity_for_session(person_session_id)
            evidence = {"reason": "vehicle_out_link", "vehicle_session_id": vehicle_session_id}
            insert_driver_attribution(
                "out",
                person_identity,
                vehicle_identity,
                vehicle_session_id,
                person_session_id,
                evidence,
            )
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
                    snapshot_path = None
                    if snapshot:
                        snapshot_path = save_snapshot(snapshot)
                        sent = send_telegram_photo(CHAT_ID_IMPORTANT, caption, snapshot)
                    if not sent:
                        send_telegram_message(CHAT_ID_IMPORTANT, caption)
                    insert_gate_alert_event(gate_closed, people_count, "no_one_gate_open", snapshot_path)
                    update_alert_last(ALERT_KEY_NO_ONE_GATE_OPEN, now.isoformat())
        except Exception as exc:
            logger.warning("Alert loop error: %s", exc)
        finally:
            threading.Event().wait(CHECK_INTERVAL_SECONDS)


def control_door(action: str) -> None:
    """Control garage door relay: OPEN, CLOSE, STOP"""
    global door_state
    logger.info("Controlling door: %s (Type: %s)", action, RELAY_TYPE)

    if RELAY_TYPE == "gpio":
        try:
            import OPi.GPIO as GPIO
            # Setup GPIO (BOARD or BCM - Orange Pi usually BOARD or SUNXI)
            GPIO.setmode(GPIO.BOARD) 
            GPIO.setup(RELAY_GPIO_PIN, GPIO.OUT)
            
            # Pulse logic for garage door (Toggle)
            GPIO.output(RELAY_GPIO_PIN, GPIO.HIGH)
            threading.Event().wait(0.5)  # 0.5s pulse
            GPIO.output(RELAY_GPIO_PIN, GPIO.LOW)
            
            GPIO.cleanup()
        except ImportError:
            logger.error("OPi.GPIO not installed. Run: pip install OPi.GPIO")
        except Exception as exc:
            logger.error("GPIO control failed: %s", exc)

    elif RELAY_TYPE == "tasmota":
        if RELAY_HTTP_URL:
            try:
                # Assumes simple toggle for garage door
                requests.get(f"{RELAY_HTTP_URL}/cm?cmnd=Power%20TOGGLE", timeout=2)
            except Exception as exc:
                logger.error("Tasmota control failed: %s", exc)

    # Simulating state change for UI feedback
    with door_state_lock:
        if action == "OPEN":
            door_state = "open"
        elif action == "CLOSE":
            door_state = "closed"
        
        mqtt_publish(STATE_TOPICS["door"], door_state)


def handle_mqtt_command(topic: str, payload: str) -> None:
    if topic == "shed/cmd/gate_open":
        set_gate_state(0, "ha")
        return
    if topic == "shed/cmd/gate_closed":
        set_gate_state(1, "ha")
        return
    if topic == "shed/cmd/door":
        control_door(payload)
        return
    if topic == "shed/cmd/ptz_panorama":
        if ptz_goto_preset(ONVIF_PRESET_PANORAMA):
            prev_mode = get_ptz_state()["mode"]
            set_ptz_state("panorama", 0, "ha", utc_now())
            insert_ptz_event("set_panorama", "manual", prev_mode, "panorama")
        return
    if topic == "shed/cmd/ptz_gate":
        if ptz_goto_preset(ONVIF_PRESET_GATE):
            prev_mode = get_ptz_state()["mode"]
            set_ptz_state("gate", 1, "ha", None)
            insert_ptz_event("set_gate", "manual", prev_mode, "gate")
        return
    if topic == "shed/cmd/view_heartbeat":
        update_ptz_last_view("ha")
        return


def on_mqtt_message(client, userdata, msg):
    if msg.topic in COMMAND_TOPICS:
        payload = msg.payload.decode("utf-8", errors="ignore")
        handle_mqtt_command(msg.topic, payload)
        return

    if msg.topic != MQTT_TOPIC:
        return

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
    global mqtt_client
    client = mqtt.Client()
    mqtt_client = client
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    client.on_message = on_mqtt_message

    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            logger.info("MQTT connected")
            client.subscribe(MQTT_TOPIC)
            for topic in COMMAND_TOPICS:
                client.subscribe(topic)
            publish_discovery()
            publish_state()
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


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
):

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

    if cmd in {"/start", "/help"}:
        send_telegram_message(chat_id, telegram_help_text())
        return {"ok": True}

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

    if cmd == "/person_add":
        if not plate_raw:
            send_telegram_message(chat_id, "Thiếu tên. Ví dụ: /person_add nhanvien_A")
            return {"ok": True}
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO people_whitelist (person_identity, note, added_at_utc, added_by) VALUES (?, ?, ?, ?) ON CONFLICT(person_identity) DO UPDATE SET note=excluded.note, added_at_utc=excluded.added_at_utc, added_by=excluded.added_by",
                (plate_raw, "", utc_now(), user_label),
            )
            conn.commit()
            conn.close()
            send_telegram_message(chat_id, f"✅ Đã thêm person_identity: {plate_raw}")
        except sqlite3.Error as exc:
            logger.warning("Person add failed: %s", exc)
            send_telegram_message(chat_id, "⚠️ Không thể thêm person_identity.")
        return {"ok": True}

    if cmd == "/person_list":
        try:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT person_identity FROM people_whitelist ORDER BY person_identity ASC")
            rows = cursor.fetchall()
            conn.close()
            if rows:
                names = "\n".join([r[0] for r in rows])
                send_telegram_message(chat_id, f"Danh sách person_identity:\n{names}")
            else:
                send_telegram_message(chat_id, "Chưa có person_identity.")
        except sqlite3.Error as exc:
            logger.warning("Person list failed: %s", exc)
            send_telegram_message(chat_id, "⚠️ Không thể lấy danh sách.")
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
    ptz_state = get_ptz_state()
    seconds_since_last_view = None
    if ptz_state["mode"] == "panorama":
        last_view = ptz_state.get("last_view_utc")
        if last_view:
            try:
                seconds_since_last_view = int(
                    (datetime.utcnow() - datetime.fromisoformat(last_view)).total_seconds()
                )
            except ValueError:
                seconds_since_last_view = None

    return {
        "status": "ok",
        "people_count": people_count,
        "vehicle_count": vehicle_count,
        "gate_closed": gate_closed,
        "last_alert_time": get_alert_last(ALERT_KEY_NO_ONE_GATE_OPEN),
        "active_exit_sessions": active_session_count(),
        "ptz_mode": ptz_state["mode"],
        "ocr_enabled": ptz_state["ocr_enabled"],
        "seconds_since_last_view": seconds_since_last_view,
    }


def main() -> None:
    init_db()
    configure_telegram_commands()
    mqtt_thread = threading.Thread(target=start_mqtt_loop, daemon=True)
    mqtt_thread.start()
    alert_thread = threading.Thread(target=alert_loop, daemon=True)
    alert_thread.start()
    state_thread = threading.Thread(target=ensure_state_publish_loop, daemon=True)
    state_thread.start()
    auto_return_thread = threading.Thread(target=auto_return_loop, daemon=True)
    auto_return_thread.start()
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
