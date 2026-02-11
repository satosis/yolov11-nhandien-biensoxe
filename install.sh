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
  apt_install python3 python3-pip python3-venv \
    libgl1 libglib2.0-0 \
    cmake build-essential libboost-all-dev
  
  # Create venv if not exists
  if [[ ! -d "${ROOT_DIR}/venv" ]]; then
    python3 -m venv "${ROOT_DIR}/venv"
    log "Created virtual environment."
  fi
  
  source "${ROOT_DIR}/venv/bin/activate"
  pip install --upgrade pip
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
  
  # Install Python dependencies
  install_python_deps
  
  ensure_dirs
  ensure_env

  # Check and optimize model
  MODEL_PATH="${ROOT_DIR}/models/bien_so_xe.pt"
  if [[ -f "$MODEL_PATH" ]]; then
      log "Found bien_so_xe.pt, optimizing to ONNX..."
      python3 "${DEPLOY_DIR}/utils/export_model.py" "$MODEL_PATH" "onnx"
  else
      log "⚠️ No bien_so_xe.pt found. Optimization skipped."
  fi

  log "Installation complete!"
  echo ""
  echo "Run the application:"
  echo "  source venv/bin/activate"
  echo "  python main.py"
  echo ""
  echo "Note: Ensure your Camera RTSP URL and other configs are set in .env"
}

main "$@"
