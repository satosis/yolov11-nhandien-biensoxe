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
```

## Troubleshooting
- RTSP issues: verify camera IP/user/pass and main/sub stream paths.
- MQTT issues: check `mosquitto` container logs.
- event_bridge issues: `./cmd logs event_bridge`.
