# OCR + Frigate Stack (Orange Pi)

Run everything with one command:

```bash
bash install.sh
```

## Configure secrets
Edit `.env` (created from `.env.example` on first install):

```
TELEGRAM_TOKEN=
TELEGRAM_CHAT_IMPORTANT=
TELEGRAM_CHAT_NONIMPORTANT=
TELEGRAM_WEBHOOK_SECRET_PATH=
TELEGRAM_SECRET_TOKEN=
```

## Frigate RTSP config
Edit `./deploy/frigate/config.yml` and replace the RTSP placeholders for main/sub streams.

## Counting heuristic (vehicle exit + people)
- When a vehicle exits, `vehicle_count` is decremented and `people_count` is immediately decremented by 1 (driver on the right side).
- A short window then allows extra person exits to be subtracted (left side) up to a configurable cap.

Tune in `.env`:
- `LEFT_EXIT_WINDOW_SECONDS` (default 30)
- `LEFT_EXIT_MAX_EXTRA_PEOPLE` (default 2)
- `MAX_ACTIVE_VEHICLE_EXIT_SESSIONS` (default 2)
- `VIRTUAL_GATE_LINE_X`, `INSIDE_SIDE`, `GATE_DEBOUNCE_UPDATES`, `TRACK_TTL_SECONDS`

## Gate commands (Telegram)
Use in group chat:
- `/gate_closed` -> set gate closed
- `/gate_open` -> set gate open
- `/gate_status` -> show gate state + people_count

Alert rule: when `people_count == 0` and gate is open, an IMPORTANT alert is sent with a snapshot (cooldown via `ALERT_COOLDOWN_SECONDS`).

## Driver attribution (heuristic)
- When a vehicle IN/OUT is detected, the system links it to the most recent person IN/OUT within `DRIVER_LINK_WINDOW_SECONDS`.
- If no person event is nearby, it records `unknown_person`.
- Duplicate attributions for the same person/vehicle/direction within `DEDUPE_SECONDS` are collapsed.

## Monthly reports
- Text report:
  ```bash
  ./cmd report-month YYYY-MM
  ```
- Chart (PNG under `./data/event_bridge/reports/`):
  ```bash
  ./cmd chart-month YYYY-MM
  ```

## Home Assistant
Open:
- http://<pi-ip>:8123

Install the Home Assistant iOS app, add the Frigate integration via UI, then add camera + events cards.

## Ops commands
```
./cmd stats
./cmd today
./cmd last 50
./cmd pending
./cmd whitelist
./cmd counters
./cmd sessions
./cmd counter_events
./cmd gate
./cmd alerts
./cmd report-month YYYY-MM
./cmd chart-month YYYY-MM
```

## Troubleshooting
- RTSP issues: verify camera IP/user/pass and main/sub stream paths.
- MQTT issues: check `mosquitto` container logs.
- event_bridge issues: `./cmd logs event_bridge`.
