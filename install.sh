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

install_python_deps() {
  log "Installing Python dependencies..."
  
  # Install system deps for OpenCV, dlib, PaddleOCR
  apt_install python3 python3-pip python3-venv python3-setuptools python3-dev \
    libgl1 libglib2.0-0 \
    cmake build-essential libboost-all-dev
  
  # Create venv if not exists
  if [[ ! -d "${ROOT_DIR}/venv" ]]; then
    python3 -m venv "${ROOT_DIR}/venv"
    log "Created virtual environment."
  fi
  
  source "${ROOT_DIR}/venv/bin/activate"
  pip install --upgrade pip setuptools wheel
  pip install -r "${ROOT_DIR}/requirements.txt"
  
  log "Python dependencies installed."
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


cleanup_old_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    return
  fi

  log "Cleaning old Docker state for this project only..."
  require_sudo

  # Only clean this compose project (avoid deleting unrelated containers/images)
  sudo docker compose -f "${ROOT_DIR}/docker-compose.yml" down --remove-orphans --volumes || true
  sudo docker compose -f "${ROOT_DIR}/docker-compose.yml" rm -f || true
  sudo docker image prune -f || true
  sudo docker builder prune -af || true
}

start_docker_stack() {
  if ! command -v docker >/dev/null 2>&1; then
    return
  fi

  log "Starting new Docker stack..."
  require_sudo
  sudo docker compose -f "${ROOT_DIR}/docker-compose.yml" up -d --build || true
}

restore_core_config_fallback() {
  cat > "${ROOT_DIR}/core/config.py" <<'PYCONF'
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


def load_env_file(path: str, override: bool = False) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if override or key not in os.environ:
            os.environ[key] = value


# Load môi trường (.env trước, rồi runtime .camera.env để override CAMERA_IP nếu có)
load_env_file(".env")
load_env_file(".camera.env", override=True)
logging.getLogger("ultralytics").setLevel(logging.WARNING)

# --- Telegram ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_IMPORTANT = os.getenv("TELEGRAM_CHAT_IMPORTANT")
CHAT_REGULAR = os.getenv("TELEGRAM_CHAT_NONIMPORTANT")

# --- Database ---
DB_PATH = "./db/door_events.db"

# --- Model Paths ---
PLATE_MODEL_PATH = "./models/bien_so_xe.pt"
GENERAL_MODEL_PATH = "./models/bien_so_xe.pt"
DOOR_MODEL_PATH = "./models/door_model.pt"

# --- Detection ---
LINE_Y = 300
CAMERA_IP = os.getenv("CAMERA_IP", "")
_RTSP_URL_RAW = os.getenv("RTSP_URL", "")


def resolve_rtsp_url(rtsp_url: str, camera_ip: str) -> str:
    if not rtsp_url or not camera_ip:
        return rtsp_url
    if "{CAMERA_IP}" in rtsp_url:
        return rtsp_url.replace("{CAMERA_IP}", camera_ip)

    parsed = urlsplit(rtsp_url)
    if not parsed.scheme.startswith("rtsp"):
        return rtsp_url

    if not parsed.hostname:
        return rtsp_url

    auth = ""
    if parsed.username:
        auth = parsed.username
        if parsed.password:
            auth += f":{parsed.password}"
        auth += "@"

    port = f":{parsed.port}" if parsed.port else ""
    new_netloc = f"{auth}{camera_ip}{port}"
    return urlunsplit((parsed.scheme, new_netloc, parsed.path, parsed.query, parsed.fragment))


RTSP_URL = resolve_rtsp_url(_RTSP_URL_RAW, CAMERA_IP)
OCR_SOURCE = "rtsp"
SIGNAL_LOSS_TIMEOUT = 30

# --- Cửa cuốn (Brightness-based fallback) ---
DOOR_ROI = (100, 50, 540, 400)
BRIGHTNESS_THRESHOLD = 80
USE_AI_DOOR_DETECTION = os.path.exists(DOOR_MODEL_PATH)

# --- Authorized list ---
CONFIG_PATH = "./config/authorized.json"
FACES_DIR = "./config/faces"

authorized_plates = []
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
        authorized_plates = [p.upper().replace(" ", "") for p in config.get("plates", [])]
        print(f"✅ Loaded {len(authorized_plates)} authorized plates: {authorized_plates}")

# --- Face recognition (optional) ---
try:
    import face_recognition

    FACE_RECOGNITION_AVAILABLE = True
except ImportError:
    FACE_RECOGNITION_AVAILABLE = False


# --- Utility ---
def normalize_plate(plate_text):
    return re.sub(r"[^A-Z0-9]", "", plate_text.upper())
PYCONF
}

install_hacs() {
  local ha_custom_dir="${ROOT_DIR}/data/homeassistant/custom_components"
  local hacs_dir="${ha_custom_dir}/hacs"

  if [[ -d "${hacs_dir}" ]]; then
    log "HACS already installed at ${hacs_dir}."
    return
  fi

  if ! mkdir -p "${ha_custom_dir}" 2>/dev/null; then
    log "⚠️ Cannot create ${ha_custom_dir} (permission denied). Skipping HACS auto-install."
    log "⚠️ Fix with: sudo chown -R ${RUN_USER}:${RUN_USER} ${ROOT_DIR}/data/homeassistant"
    return
  fi

  local tmp_dir
  tmp_dir="$(mktemp -d)"

  log "Installing HACS into Home Assistant config..."
  local hacs_zip="${tmp_dir}/hacs.zip"
  local hacs_downloaded=0
  local hacs_urls=(
    "https://github.com/hacs/integration/releases/latest/download/hacs.zip"
    "https://codeload.github.com/hacs/integration/zip/refs/heads/main"
    "https://codeload.github.com/hacs/integration/zip/refs/heads/master"
  )
  for url in "${hacs_urls[@]}"; do
    if curl -fsSL "${url}" -o "${hacs_zip}" 2>/dev/null; then
      hacs_downloaded=1
      break
    fi
  done

  if [[ "${hacs_downloaded}" -ne 1 ]]; then
    log "⚠️ Could not download HACS package from known URLs. Skipping HACS install."
    rm -rf "${tmp_dir}"
    return
  fi

  if ! unzip -q "${hacs_zip}" -d "${tmp_dir}"; then
    log "⚠️ Could not extract HACS package. Skipping HACS install."
    rm -rf "${tmp_dir}"
    return
  fi

  local src_dir
  src_dir="$(find "${tmp_dir}" -type d -path "*/custom_components/hacs" | head -n 1)"
  if [[ -z "${src_dir}" || ! -d "${src_dir}" ]]; then
    if [[ -d "${tmp_dir}/hacs" ]]; then
      src_dir="${tmp_dir}/hacs"
    else
      log "⚠️ HACS archive layout not recognized. Trying git fallback..."
      if git clone --depth 1 https://github.com/hacs/integration.git "${tmp_dir}/hacs-repo" >/dev/null 2>&1; then
        if [[ -d "${tmp_dir}/hacs-repo/custom_components/hacs" ]]; then
          src_dir="${tmp_dir}/hacs-repo/custom_components/hacs"
        else
          log "⚠️ HACS git fallback also missing custom_components/hacs. Skipping HACS install."
          rm -rf "${tmp_dir}"
          return
        fi
      else
        log "⚠️ Could not clone HACS repository for fallback. Skipping HACS install."
        rm -rf "${tmp_dir}"
        return
      fi
    fi
  fi

  if ! cp -r "${src_dir}" "${ha_custom_dir}/" 2>/dev/null; then
    log "⚠️ Cannot copy HACS into ${ha_custom_dir} (permission denied). Skipping HACS auto-install."
    log "⚠️ Fix with: sudo chown -R ${RUN_USER}:${RUN_USER} ${ROOT_DIR}/data/homeassistant"
    rm -rf "${tmp_dir}"
    return
  fi

  if [[ ! -d "${hacs_dir}" ]]; then
    log "⚠️ HACS copy step completed but destination not found (${hacs_dir})."
    rm -rf "${tmp_dir}"
    return
  fi

  rm -rf "${tmp_dir}"
  log "HACS installed: ${hacs_dir}"
}



install_frigate_ha_integration() {
  local ha_custom_dir="${ROOT_DIR}/data/homeassistant/custom_components"
  local frigate_dir="${ha_custom_dir}/frigate"

  if [[ -d "${frigate_dir}" ]]; then
    log "Frigate HA integration already installed at ${frigate_dir}."
    return
  fi

  if ! mkdir -p "${ha_custom_dir}" 2>/dev/null; then
    log "⚠️ Cannot create ${ha_custom_dir} (permission denied). Skipping Frigate HA integration install."
    log "⚠️ Fix with: sudo chown -R ${RUN_USER}:${RUN_USER} ${ROOT_DIR}/data/homeassistant"
    return
  fi

  local tmp_dir
  tmp_dir="$(mktemp -d)"

  log "Installing Frigate Home Assistant integration..."
  local frigate_zip="${tmp_dir}/frigate-ha.zip"
  local downloaded=0
  local frigate_urls=(
    "https://github.com/blakeblackshear/frigate-hass-integration/archive/refs/heads/main.zip"
    "https://github.com/blakeblackshear/frigate-hass-integration/archive/refs/heads/master.zip"
    "https://github.com/blakeblackshear/frigate-hass-integration/releases/latest/download/frigate-hass-integration.zip"
    "https://codeload.github.com/blakeblackshear/frigate-hass-integration/zip/refs/heads/main"
    "https://codeload.github.com/blakeblackshear/frigate-hass-integration/zip/refs/heads/master"
  )
  for url in "${frigate_urls[@]}"; do
    if curl -fsSL "${url}" -o "${frigate_zip}" 2>/dev/null; then
      downloaded=1
      break
    fi
  done
  if [[ "${downloaded}" -ne 1 ]]; then
    log "⚠️ Could not download Frigate HA integration from known URLs. Skipping install."
    rm -rf "${tmp_dir}"
    return
  fi

  if ! unzip -q "${frigate_zip}" -d "${tmp_dir}"; then
    log "⚠️ Could not extract Frigate HA integration package. Skipping install."
    rm -rf "${tmp_dir}"
    return
  fi

  local src_dir
  src_dir="$(find "${tmp_dir}" -type d -path "*/custom_components/frigate" | head -n 1)"
  if [[ -z "${src_dir}" || ! -d "${src_dir}" ]]; then
    log "⚠️ Invalid Frigate HA integration package (missing custom_components/frigate)."
    rm -rf "${tmp_dir}"
    return
  fi

  if ! cp -r "${src_dir}" "${ha_custom_dir}/" 2>/dev/null; then
    log "⚠️ Cannot copy Frigate integration into ${ha_custom_dir} (permission denied)."
    log "⚠️ Fix with: sudo chown -R ${RUN_USER}:${RUN_USER} ${ROOT_DIR}/data/homeassistant"
    rm -rf "${tmp_dir}"
    return
  fi

  rm -rf "${tmp_dir}"
  log "Frigate HA integration installed: ${frigate_dir}"
}

ensure_homeassistant_permissions() {
  local ha_dir="${ROOT_DIR}/data/homeassistant"

  mkdir -p "${ha_dir}"

  if [[ -w "${ha_dir}" ]]; then
    return
  fi

  log "Fixing ownership for ${ha_dir} ..."
  require_sudo
  sudo chown -R "${RUN_USER}:${RUN_USER}" "${ha_dir}" || true
}

setup_timezone() {
  if command -v timedatectl >/dev/null 2>&1; then
    sudo timedatectl set-timezone Asia/Ho_Chi_Minh || true
  fi
}

ensure_dirs() {
  mkdir -p "${ROOT_DIR}/models" \
    "${ROOT_DIR}/config" \
    "${ROOT_DIR}/config/faces" \
    "${ROOT_DIR}/db"
}

ensure_env() {
  if [[ ! -f "${ROOT_DIR}/.env" ]]; then
    log "Creating .env from .env.example..."
    cp "${ROOT_DIR}/.env.example" "${ROOT_DIR}/.env"
  fi
}

main() {
  log "Installing base packages..."
  apt_install ca-certificates curl git jq sqlite3 unzip

  setup_timezone

  install_docker
  ensure_docker_group
  cleanup_old_docker
  
  # Install Python dependencies
  install_python_deps
  
  ensure_dirs
  ensure_env
  ensure_homeassistant_permissions
  install_hacs
  install_frigate_ha_integration

  # Sanity-check local python files to catch corrupted edits/merge issues
  if ! python3 -m py_compile "${ROOT_DIR}/core/config.py"; then
      log "Detected syntax issue in core/config.py. Trying auto-restore from git HEAD..."
      if git -C "${ROOT_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git -C "${ROOT_DIR}" checkout -- core/config.py || true
      fi

      if ! python3 -m py_compile "${ROOT_DIR}/core/config.py"; then
        log "git restore did not fix core/config.py. Applying built-in fallback template..."
        restore_core_config_fallback
      fi

      if ! python3 -m py_compile "${ROOT_DIR}/core/config.py"; then
        echo "❌ Syntax error in core/config.py. Please run: git pull && python3 -m py_compile core/config.py"
        exit 1
      fi

      log "core/config.py was restored successfully."
  fi

  # Check and optimize model
  MODEL_PATH="${ROOT_DIR}/models/bien_so_xe.pt"
  if [[ -f "$MODEL_PATH" ]]; then
      log "Found bien_so_xe.pt, optimizing to ONNX..."
      python3 "${DEPLOY_DIR}/utils/export_model.py" "$MODEL_PATH" "onnx"
  else
      log "⚠️ No bien_so_xe.pt found. Optimization skipped."
  fi

  start_docker_stack

  log "Installation complete!"
  echo ""
  echo "Run the application:"
  echo "  source venv/bin/activate"
  echo "  python main.py"
  echo ""
  echo "Note: Ensure your Camera RTSP URL and other configs are set in .env"
  echo "Home Assistant integrations:"
  echo "  1) docker compose ps (kiểm tra homeassistant/frigate/event_bridge đang Up)"
  echo "  2) Vào Settings > Devices & Services > Add Integration > HACS"
  echo "  3) Sau khi HACS xong, Add Integration > Frigate"
}

main "$@"
