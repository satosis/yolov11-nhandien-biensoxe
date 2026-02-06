#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${ROOT_DIR}/data"
DEPLOY_DIR="${ROOT_DIR}/deploy"
RUN_USER="${SUDO_USER:-$USER}"

log() {
  echo "[install] $*"
}

require_sudo() {
  if [[ "$EUID" -ne 0 ]]; then
    if ! command -v sudo >/dev/null 2>&1; then
      echo "sudo is required but not installed."
      exit 1
    fi
  fi
}

apt_install() {
  local packages=("$@")
  require_sudo
  sudo apt-get update
  sudo apt-get install -y "${packages[@]}"
}

install_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    log "Installing Docker..."
    curl -fsSL https://get.docker.com | sudo sh
  fi

  if ! command -v docker >/dev/null 2>&1; then
    echo "Docker install failed. Please check network and retry."
    exit 1
  fi

  if ! docker compose version >/dev/null 2>&1; then
    log "Installing docker compose plugin..."
    sudo apt-get install -y docker-compose-plugin
  fi

  sudo systemctl enable --now docker
}

ensure_docker_group() {
  if [[ "$RUN_USER" == "root" ]]; then
    return
  fi
  if ! id -nG "$RUN_USER" | grep -qw docker; then
    log "Adding $RUN_USER to docker group..."
    sudo usermod -aG docker "$RUN_USER"
  fi

  if ! id -nG "$RUN_USER" | grep -qw docker; then
    echo "Docker group change not applied yet. Please reboot or re-login, then re-run install.sh."
    exit 0
  fi
}

setup_timezone() {
  if command -v timedatectl >/dev/null 2>&1; then
    sudo timedatectl set-timezone Asia/Ho_Chi_Minh || true
  fi
}

ensure_dirs() {
  mkdir -p "${DEPLOY_DIR}/frigate" \
    "${DEPLOY_DIR}/mosquitto" \
    "${DEPLOY_DIR}/event_bridge" \
    "${DATA_DIR}/frigate" \
    "${DATA_DIR}/homeassistant" \
    "${DATA_DIR}/mosquitto" \
    "${DATA_DIR}/mosquitto-log" \
    "${DATA_DIR}/event_bridge"
}

ensure_env() {
  if [[ ! -f "${ROOT_DIR}/.env" ]]; then
    cp "${ROOT_DIR}/.env.example" "${ROOT_DIR}/.env"
    log "Created .env from .env.example."
  fi
}

install_tailscale() {
  if ! command -v tailscale >/dev/null 2>&1; then
    log "Installing Tailscale..."
    curl -fsSL https://tailscale.com/install.sh | sudo sh
  fi
  sudo systemctl enable --now tailscaled
}

ensure_tailscale_auth() {
  if tailscale status --json >/dev/null 2>&1; then
    return
  fi
  log "Tailscale not authenticated. Running tailscale up..."
  if ! sudo tailscale up; then
    echo "Tailscale login required. Complete login and re-run install.sh."
    exit 0
  fi
}

enable_funnel_and_webhook() {
  if [[ ! -f "${ROOT_DIR}/.env" ]]; then
    return
  fi
  set -a
  # shellcheck disable=SC1090
  source "${ROOT_DIR}/.env"
  set +a

  if [[ -z "${TELEGRAM_TOKEN:-}" || -z "${TELEGRAM_WEBHOOK_SECRET_PATH:-}" || -z "${TELEGRAM_SECRET_TOKEN:-}" ]]; then
    log "Telegram webhook secrets not set. Skipping webhook setup."
    return
  fi

  log "Enabling Tailscale Funnel on port 8000..."
  sudo tailscale funnel --bg 8000 || true

  local funnel_json
  funnel_json="$(tailscale funnel status --json 2>/dev/null || true)"
  local public_url
  public_url="$(echo "$funnel_json" | jq -r '.. | .URL? // empty' | head -n1)"

  if [[ -z "$public_url" ]]; then
    log "Could not determine public Funnel URL. Skipping webhook setup."
    return
  fi

  local webhook_url
  webhook_url="${public_url%/}/telegram/webhook/${TELEGRAM_WEBHOOK_SECRET_PATH}"

  log "Setting Telegram webhook..."
  curl -fsSL -X POST "https://api.telegram.org/bot${TELEGRAM_TOKEN}/setWebhook" \
    -H "Content-Type: application/json" \
    -d "{\"url\":\"${webhook_url}\",\"secret_token\":\"${TELEGRAM_SECRET_TOKEN}\"}" >/dev/null || true

  log "Webhook info:"
  curl -fsSL "https://api.telegram.org/bot${TELEGRAM_TOKEN}/getWebhookInfo" || true

  echo "Webhook URL: ${webhook_url}"
}

main() {
  log "Installing base packages..."
  apt_install ca-certificates curl git jq sqlite3 unzip

  setup_timezone
  install_docker
  ensure_docker_group

  ensure_dirs
  ensure_env

  install_tailscale
  ensure_tailscale_auth

  log "Starting containers..."
  docker compose -f "${ROOT_DIR}/docker-compose.yml" pull
  docker compose -f "${ROOT_DIR}/docker-compose.yml" build
  docker compose -f "${ROOT_DIR}/docker-compose.yml" up -d

  log "Checking event_bridge health..."
  if ! curl -sf http://127.0.0.1:8000/health >/dev/null; then
    echo "event_bridge health check failed on http://127.0.0.1:8000/health"
    exit 1
  fi

  enable_funnel_and_webhook

  local lan_ip
  lan_ip="$(ip route get 1.1.1.1 2>/dev/null | awk '/src/ {for (i=1;i<=NF;i++) if ($i==\"src\") print $(i+1)}' | head -n1)"
  if [[ -z "$lan_ip" ]]; then
    lan_ip="$(hostname -I | awk '{print $1}')"
  fi

  local tailscale_ip
  tailscale_ip="$(tailscale ip -4 2>/dev/null | head -n1)"

  cat <<EOF
Install complete.

Access:
- Frigate: http://${lan_ip}:5000
- Home Assistant: http://${lan_ip}:8123
EOF

  if [[ -n "${tailscale_ip}" ]]; then
    cat <<EOF
- Frigate (Tailscale): http://${tailscale_ip}:5000
- Home Assistant (Tailscale): http://${tailscale_ip}:8123
EOF
  fi

  cat <<EOF

Useful commands:
- ./cmd logs event_bridge
- ./cmd stats
- ./cmd pending
- ./cmd whitelist

If docker commands fail, reboot or re-login to apply docker group changes.
EOF
}

main "$@"
