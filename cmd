#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_PATH="${DB_PATH:-${BASE_DIR}/data/event_bridge/events.db}"

usage() {
  cat <<'USAGE'
Usage: ./cmd <command> [args]

Commands:
  up                Start services (docker compose up -d)
  down              Stop services (docker compose down)
  logs [service]    Tail logs (default: all services)
  stats             Show event counts by label
  today             Show today's events (UTC) summary by label
  last <N>          Show last N events (most recent first)
  whitelist         List vehicle whitelist
  pending           List pending plate confirmations
USAGE
}

ensure_db() {
  if [[ ! -f "$DB_PATH" ]]; then
    echo "Database not found at: $DB_PATH"
    echo "Update DB_PATH env or ensure event_bridge writes to this location."
    exit 1
  fi
}

case "${1:-}" in
  up)
    docker compose up -d
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
  *)
    usage
    exit 1
    ;;
esac
