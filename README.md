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

Home Assistant entities (MQTT discovery):
- `sensor.shed_people_count`, `sensor.shed_vehicle_count`
- `binary_sensor.shed_gate_closed`
- `button.shed_gate_open`, `button.shed_gate_closed`
- `button.shed_ptz_panorama`, `button.shed_ptz_gate`
- `sensor.shed_ptz_mode`, `binary_sensor.shed_ocr_enabled`

Monthly report image:
- `./cmd chart-month YYYY-MM` copies the PNG to `./data/homeassistant/www/reports/`
- Lovelace uses `/local/reports/trips_YYYY-MM.png`

## PTZ presets (IMOU 360 PTZ)
Set these in `.env` to enable PTZ control from Home Assistant:
- `ONVIF_HOST`, `ONVIF_PORT`, `ONVIF_USER`, `ONVIF_PASS`
- `ONVIF_PRESET_GATE` (gate view preset)
- `ONVIF_PRESET_PANORAMA` (panorama view preset)
- Optional: `ONVIF_PROFILE_TOKEN` (if your camera exposes multiple profiles)

Behavior:
- Panorama mode disables OCR in `event_bridge`.
- If no viewer heartbeat is received for `PTZ_AUTO_RETURN_SECONDS`, the camera auto-returns to Gate view and OCR re-enables.
- Home Assistant sends heartbeats every `HEARTBEAT_INTERVAL_SECONDS` (default 30) while in panorama mode.

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
