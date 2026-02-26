#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_PATH="${DB_PATH:-${BASE_DIR}/data/event_bridge/events.db}"

usage() {
  cat <<'USAGE'
Usage: ./cmd <command> [args]

Commands:
  up                Resolve CAMERA_IP, start services, auto-start Tailscale if TS_AUTHKEY set
  down              Stop services (docker compose down)
  logs [service]    Tail logs (default: all services)
  stats             Show event counts by label
  today             Show today's events (UTC) summary by label
  last <N>          Show last N events (most recent first)
  whitelist         List vehicle whitelist
  pending           List pending plate confirmations
  counters          Show current counters_state
  sessions          List active vehicle exit sessions
  counter_events    Show last 50 counter events
  gate              Show gate_state + people_count
  alerts            Show last_sent for no_one_gate_open
  report-month YYYY-MM
  chart-month YYYY-MM
  test-ptz [--fast]
  webcam-people [args]  Run webcam people detector (for laptop/PC debug)
  remote-check          Check remote Home Assistant prerequisites
  remote-up             Start Tailscale profile for HA remote access
USAGE
}

ensure_db() {
  if [[ ! -f "$DB_PATH" ]]; then
    echo "Database not found at: $DB_PATH"
    echo "Update DB_PATH env or ensure event_bridge writes to this location."
    exit 1
  fi
}

read_env_value() {
  local key="$1"
  local env_file="${BASE_DIR}/.env"
  if [[ ! -f "$env_file" ]]; then
    return 0
  fi
  python3 - "$env_file" "$key" <<'PYENV'
from pathlib import Path
import sys
path = Path(sys.argv[1])
key = sys.argv[2]
value = ""
for raw in path.read_text(encoding="utf-8").splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    k, v = line.split("=", 1)
    if k.strip() == key:
        value = v.strip().strip('"').strip("'")
        break
print(value)
PYENV
}

case "${1:-}" in
  up)
    if [[ -f "${BASE_DIR}/.env" ]]; then
      python3 "${BASE_DIR}/deploy/scripts/resolve_camera_ip.py" --env-file "${BASE_DIR}/.env" --out-env-file "${BASE_DIR}/.camera.env"
    fi
    docker compose up -d

    ts_auth="$(read_env_value TS_AUTHKEY)"
    docker compose --profile remote_ha_tailscale up -d tailscale
    if [[ -n "$ts_auth" ]]; then
      echo "[cmd] ✅ Tailscale remote profile started (TS_AUTHKEY detected, only needed for first login)."
    else
      echo "[cmd] ℹ️ TS_AUTHKEY trống: vẫn bật tailscale bằng state đã lưu (nếu có)."
    fi

    echo "[cmd] Waiting for Frigate health check..."
    status=""
    for _ in $(seq 1 30); do
      container_id="$(docker compose ps -q frigate 2>/dev/null || true)"
      if [[ -n "$container_id" ]]; then
        status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
      else
        status=""
      fi

      if [[ "$status" == "healthy" || "$status" == "running" ]]; then
        echo "[cmd] ✅ Frigate is ready (status: $status)."
        break
      fi
      sleep 2
    done

    if [[ "$status" != "healthy" && "$status" != "running" ]]; then
      echo "[cmd] ⚠️ Frigate is not healthy yet (status: ${status:-unknown})."
      echo "[cmd] Tip: run './cmd logs frigate' to inspect stream/connectivity errors."
    fi
    ;;
  down)
    docker compose down
    ;;
  logs)
    shift
    if [[ -n "${1:-}" ]]; then
      docker compose logs -f --tail=200 "$1"
    else
      docker compose logs -f --tail=200
    fi
    ;;
  stats)
    ensure_db
    sqlite3 -header -column "$DB_PATH" \
      "select label, count(*) as total from events group by label order by total desc;"
    ;;
  today)
    ensure_db
    sqlite3 -header -column "$DB_PATH" \
      "select label, count(*) as total from events where date(ts_utc)=date('now') group by label order by total desc;"
    ;;
  last)
    ensure_db
    shift
    limit="${1:-10}"
    sqlite3 -header -column "$DB_PATH" \
      "select id, ts_utc, camera, event_type, label, sub_label, score, zone from events order by id desc limit ${limit};"
    ;;
  whitelist)
    ensure_db
    sqlite3 -header -column "$DB_PATH" \
      "select plate_norm, label, added_at_utc, added_by, note from vehicle_whitelist order by added_at_utc desc;"
    ;;
  pending)
    ensure_db
    sqlite3 -header -column "$DB_PATH" \
      "select pending_id, event_id, plate_raw, plate_norm, first_seen_utc, status from pending_plates where status = 'pending' order by first_seen_utc desc;"
    ;;
  counters)
    ensure_db
    sqlite3 -header -column "$DB_PATH" \
      "select id, people_count, vehicle_count, updated_at_utc from counters_state where id = 1;"
    ;;
  sessions)
    ensure_db
    sqlite3 -header -column "$DB_PATH" \
      "select session_id, started_at_utc, camera, vehicle_track_key, left_person_decrements, max_left_person_decrements from vehicle_exit_sessions where active = 1 order by started_at_utc desc;"
    ;;
  counter_events)
    ensure_db
    sqlite3 -header -column "$DB_PATH" \
      "select ts_utc, label, direction, delta, new_count, track_key, source, note from counter_events order by id desc limit 50;"
    ;;
  gate)
    ensure_db
    sqlite3 -header -column "$DB_PATH" \
      "select gate_state.gate_closed, gate_state.updated_at_utc, gate_state.updated_by, counters_state.people_count from gate_state join counters_state on counters_state.id = 1 where gate_state.id = 1;"
    ;;
  alerts)
    ensure_db
    sqlite3 -header -column "$DB_PATH" \
      "select alert_key, last_sent_utc from alerts where alert_key = 'no_one_gate_open';"
    ;;
  report-month)
    ensure_db
    month="${2:-}"
    if [[ -z "$month" ]]; then
      echo "Usage: ./cmd report-month YYYY-MM"
      exit 1
    fi
    python3 "${BASE_DIR}/deploy/reporting/monthly_chart.py" --db "$DB_PATH" --month "$month" --report
    ;;
  chart-month)
    ensure_db
    month="${2:-}"
    if [[ -z "$month" ]]; then
      echo "Usage: ./cmd chart-month YYYY-MM"
      exit 1
    fi
    chart_output="$(python3 "${BASE_DIR}/deploy/reporting/monthly_chart.py" --db "$DB_PATH" --month "$month" --chart)"
    if [[ -n "$chart_output" && -f "$chart_output" ]]; then
      report_dir="${BASE_DIR}/data/homeassistant/www/reports"
      mkdir -p "$report_dir"
      cp "$chart_output" "${report_dir}/$(basename "$chart_output")"
      echo "$chart_output"
    else
      echo "$chart_output"
    fi
    ;;
  test-ptz)
    shift
    if [[ "${1:-}" == "--fast" ]]; then
      python3 "${BASE_DIR}/deploy/tests/test_ptz.py" --fast
    else
      python3 "${BASE_DIR}/deploy/tests/test_ptz.py"
    fi
    ;;
  remote-up)
    env_file="${BASE_DIR}/.env"
    if [[ ! -f "$env_file" ]]; then
      echo "Missing .env at $env_file"
      exit 1
    fi
    ts_auth="$(read_env_value TS_AUTHKEY)"
    if [[ -z "$ts_auth" ]]; then
      echo "[cmd] TS_AUTHKEY trống. Tiếp tục bật tailscale bằng state đã lưu (không cần key mới nếu đã login trước đó)."
    fi

    docker compose --profile remote_ha_tailscale up -d tailscale
    echo "[cmd] Tailscale started. Remote URL hint:"
    docker exec tailscale tailscale status --json 2>/dev/null | python3 -c 'import json,sys; data=json.load(sys.stdin); self=data.get("Self",{}); dns=(self.get("DNSName") or "").rstrip("."); print(f"http://{dns}:8123" if dns else "(cannot determine DNSName)")' || true
    ;;
  remote-check)
    env_file="${BASE_DIR}/.env"
    if [[ ! -f "$env_file" ]]; then
      echo "Missing .env at $env_file"
      exit 1
    fi
    python3 "${BASE_DIR}/deploy/scripts/check_remote_ha.py" --env-file "$env_file"
    ;;
  webcam-people)
    shift || true
    python3 "${BASE_DIR}/deploy/utils/webcam_people_counter.py" "$@"
    ;;
  *)
    usage
    exit 1
    ;;
esac
