#!/usr/bin/env python3
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT_DIR / "data" / "event_bridge"
TEST_DB = DATA_DIR / "test_events.db"
ENV_TEST = ROOT_DIR / ".env.test"
ENV_TEST_FAIL = ROOT_DIR / ".env.test.fail"
ENV_TEST_NO_PRESET = ROOT_DIR / ".env.test.nopreset"


def run(cmd, check=True, capture=True, env=None):
    result = subprocess.run(
        cmd,
        check=False,
        capture_output=capture,
        text=True,
        env=env,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(cmd)}\n{result.stdout}\n{result.stderr}"
        )
    return result


def compose_base(env_file):
    return [
        "docker",
        "compose",
        "--env-file",
        str(env_file),
        "-f",
        str(ROOT_DIR / "docker-compose.yml"),
        "-f",
        str(ROOT_DIR / "docker-compose.test.yml"),
    ]


def compose_cmd(env_file, *args, env=None):
    return run(compose_base(env_file) + list(args), env=env)


def mosquitto_pub(env_file, topic, payload):
    compose_cmd(
        env_file,
        "exec",
        "-T",
        "mosquitto",
        "mosquitto_pub",
        "-t",
        topic,
        "-m",
        payload,
    )


def mosquitto_sub(env_file, topic, timeout=5):
    result = compose_cmd(
        env_file,
        "exec",
        "-T",
        "mosquitto",
        "mosquitto_sub",
        "-t",
        topic,
        "-C",
        "1",
        "-W",
        str(timeout),
        "-v",
    )
    return result.stdout.strip()


def wait_for_health():
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            result = run(["curl", "-sf", "http://127.0.0.1:18000/health"])
            if result.stdout:
                return json.loads(result.stdout)
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("event_bridge health check failed")


def db_query(query, params=()):
    conn = sqlite3.connect(TEST_DB)
    cursor = conn.cursor()
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return rows


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)


def write_env_file(path, extra):
    base = (
        "EVENT_BRIDGE_TEST_MODE=1\n"
        "DB_PATH=/data/test_events.db\n"
        "ONVIF_HOST=127.0.0.1\n"
        "ONVIF_PORT=80\n"
        "ONVIF_USER=test\n"
        "ONVIF_PASS=test\n"
        "ONVIF_PRESET_GATE=gate\n"
        "ONVIF_PRESET_PANORAMA=panorama\n"
    )
    path.write_text(base + extra)


def setup_env_files(fast):
    auto_return = 5 if fast else 8
    write_env_file(
        ENV_TEST,
        f"PTZ_AUTO_RETURN_SECONDS={auto_return}\nONVIF_SIMULATE_FAIL=0\n",
    )
    write_env_file(
        ENV_TEST_FAIL,
        f"PTZ_AUTO_RETURN_SECONDS={auto_return}\nONVIF_SIMULATE_FAIL=1\n",
    )
    write_env_file(
        ENV_TEST_NO_PRESET,
        f"PTZ_AUTO_RETURN_SECONDS={auto_return}\nONVIF_PRESET_PANORAMA=\nONVIF_SIMULATE_FAIL=0\n",
    )


def start_stack(env_file):
    compose_cmd(env_file, "up", "-d", "mosquitto", "event_bridge")
    wait_for_health()


def stop_stack(env_file):
    compose_cmd(env_file, "down")


def recreate_event_bridge(env_file):
    compose_cmd(env_file, "up", "-d", "--force-recreate", "--no-deps", "event_bridge")
    wait_for_health()


def test_default_state():
    rows = db_query("SELECT mode, ocr_enabled FROM ptz_state WHERE id = 1")
    assert_true(rows, "ptz_state row missing")
    mode, ocr_enabled = rows[0]
    assert_true(mode == "gate", f"Expected default mode gate, got {mode}")
    assert_true(ocr_enabled == 1, f"Expected ocr_enabled=1, got {ocr_enabled}")


def test_discovery(env_file):
    payload = mosquitto_sub(env_file, "homeassistant/button/shed_ptz_panorama/config")
    assert_true(payload, "Missing discovery payload for ptz_panorama")
    _, raw = payload.split(" ", 1)
    doc = json.loads(raw)
    for key in ("unique_id", "name", "command_topic", "device"):
        assert_true(key in doc, f"Discovery payload missing {key}")


def test_panorama_command(env_file):
    mosquitto_pub(env_file, "shed/cmd/ptz_panorama", "1")
    time.sleep(1)
    rows = db_query("SELECT mode, ocr_enabled FROM ptz_state WHERE id = 1")
    mode, ocr_enabled = rows[0]
    assert_true(mode == "panorama", f"Expected panorama mode, got {mode}")
    assert_true(ocr_enabled == 0, f"Expected ocr_enabled=0, got {ocr_enabled}")
    state = mosquitto_sub(env_file, "shed/state/ptz_mode")
    assert_true(state.endswith("panorama"), f"Retained ptz_mode not panorama: {state}")
    state = mosquitto_sub(env_file, "shed/state/ocr_enabled")
    assert_true(state.endswith("0"), f"Retained ocr_enabled not 0: {state}")
    calls = db_query("SELECT preset, success FROM ptz_test_calls ORDER BY id DESC LIMIT 1")
    assert_true(calls and calls[0][0] == "panorama", "PTZ call not recorded for panorama")
    assert_true(calls[0][1] == 1, "PTZ panorama call should succeed")


def test_gate_command(env_file):
    mosquitto_pub(env_file, "shed/cmd/ptz_gate", "1")
    time.sleep(1)
    rows = db_query("SELECT mode, ocr_enabled FROM ptz_state WHERE id = 1")
    mode, ocr_enabled = rows[0]
    assert_true(mode == "gate", f"Expected gate mode, got {mode}")
    assert_true(ocr_enabled == 1, f"Expected ocr_enabled=1, got {ocr_enabled}")
    state = mosquitto_sub(env_file, "shed/state/ptz_mode")
    assert_true(state.endswith("gate"), f"Retained ptz_mode not gate: {state}")


def test_heartbeat(env_file):
    mosquitto_pub(env_file, "shed/cmd/ptz_panorama", "1")
    time.sleep(1)
    mosquitto_pub(env_file, "shed/cmd/view_heartbeat", "1")
    time.sleep(1)
    rows = db_query("SELECT last_view_utc FROM ptz_state WHERE id = 1")
    assert_true(rows and rows[0][0], "last_view_utc not updated in panorama")
    last_view = rows[0][0]
    state = mosquitto_sub(env_file, "shed/state/last_view_utc")
    assert_true(last_view in state, "last_view_utc retained state mismatch")

    mosquitto_pub(env_file, "shed/cmd/ptz_gate", "1")
    time.sleep(1)
    mosquitto_pub(env_file, "shed/cmd/view_heartbeat", "1")
    time.sleep(1)
    rows_after = db_query("SELECT last_view_utc FROM ptz_state WHERE id = 1")
    assert_true(rows_after[0][0] == last_view, "Heartbeat updated last_view_utc while gate")


def test_auto_return(env_file, auto_return_seconds):
    mosquitto_pub(env_file, "shed/cmd/ptz_panorama", "1")
    time.sleep(auto_return_seconds + 2)
    rows = db_query("SELECT mode, ocr_enabled FROM ptz_state WHERE id = 1")
    mode, ocr_enabled = rows[0]
    assert_true(mode == "gate", f"Auto-return failed, mode={mode}")
    assert_true(ocr_enabled == 1, f"Auto-return did not enable OCR, ocr={ocr_enabled}")
    events = db_query(
        "SELECT action FROM ptz_events WHERE action = 'auto_return' ORDER BY id DESC LIMIT 1"
    )
    assert_true(events, "Auto-return did not log ptz_events")


def test_no_auto_return_with_heartbeat(env_file, auto_return_seconds):
    mosquitto_pub(env_file, "shed/cmd/ptz_panorama", "1")
    start = time.time()
    while time.time() - start < auto_return_seconds - 1:
        mosquitto_pub(env_file, "shed/cmd/view_heartbeat", "1")
        time.sleep(1)
    rows = db_query("SELECT mode FROM ptz_state WHERE id = 1")
    assert_true(rows[0][0] == "panorama", "Panorama should persist with heartbeats")


def test_onvif_fail(env_file):
    mosquitto_pub(env_file, "shed/cmd/ptz_panorama", "1")
    time.sleep(1)
    rows = db_query("SELECT mode FROM ptz_state WHERE id = 1")
    assert_true(rows[0][0] == "gate", "Mode changed despite ONVIF failure")
    calls = db_query("SELECT preset, success FROM ptz_test_calls ORDER BY id DESC LIMIT 1")
    assert_true(calls and calls[0][1] == 0, "ONVIF failure not recorded")


def test_missing_preset(env_file):
    mosquitto_pub(env_file, "shed/cmd/ptz_panorama", "1")
    time.sleep(1)
    rows = db_query("SELECT mode FROM ptz_state WHERE id = 1")
    assert_true(rows[0][0] == "gate", "Mode changed despite missing preset")


def test_rapid_toggle(env_file):
    mosquitto_pub(env_file, "shed/cmd/ptz_panorama", "1")
    mosquitto_pub(env_file, "shed/cmd/ptz_gate", "1")
    mosquitto_pub(env_file, "shed/cmd/ptz_panorama", "1")
    time.sleep(1)
    rows = db_query("SELECT mode FROM ptz_state WHERE id = 1")
    assert_true(rows[0][0] == "panorama", "Rapid toggle did not end in panorama")


def test_ocr_gating(env_file):
    mosquitto_pub(env_file, "shed/cmd/ptz_panorama", "1")
    time.sleep(1)
    payload = json.dumps(
        {
            "camera": "cam1",
            "type": "new",
            "label": "car",
            "id": "evt1",
            "plate": "51A12345",
        }
    )
    mosquitto_pub(env_file, "frigate/events", payload)
    time.sleep(1)
    rows = db_query("SELECT COUNT(*) FROM pending_plates")
    assert_true(rows[0][0] == 0, "OCR gating failed: pending_plates created while OCR off")

    mosquitto_pub(env_file, "shed/cmd/ptz_gate", "1")
    time.sleep(1)
    mosquitto_pub(env_file, "frigate/events", payload)
    time.sleep(1)
    rows = db_query("SELECT COUNT(*) FROM pending_plates")
    assert_true(rows[0][0] >= 1, "OCR pipeline did not create pending plate when enabled")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", action="store_true")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if TEST_DB.exists():
        TEST_DB.unlink()

    setup_env_files(args.fast)
    env = os.environ.copy()

    try:
        start_stack(ENV_TEST)
        health = wait_for_health()
        assert_true(health.get("status") == "ok", "Health status not ok")

        auto_return_seconds = 5 if args.fast else 8

        test_default_state()
        test_discovery(ENV_TEST)
        test_panorama_command(ENV_TEST)
        test_gate_command(ENV_TEST)
        test_heartbeat(ENV_TEST)
        test_no_auto_return_with_heartbeat(ENV_TEST, auto_return_seconds)
        test_auto_return(ENV_TEST, auto_return_seconds)
        test_rapid_toggle(ENV_TEST)
        test_ocr_gating(ENV_TEST)

        recreate_event_bridge(ENV_TEST_FAIL)
        test_onvif_fail(ENV_TEST_FAIL)

        recreate_event_bridge(ENV_TEST_NO_PRESET)
        test_missing_preset(ENV_TEST_NO_PRESET)
    finally:
        stop_stack(ENV_TEST)
        for path in (ENV_TEST, ENV_TEST_FAIL, ENV_TEST_NO_PRESET):
            if path.exists():
                path.unlink()

    print("PASS: PTZ panorama workflow tests")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"FAIL: {exc}")
        sys.exit(1)
