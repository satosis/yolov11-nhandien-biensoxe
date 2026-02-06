#!/bin/bash

# === Cấu hình ===
N8N_BASE_DIR="$HOME/n8n"
N8N_VOLUME_DIR="$N8N_BASE_DIR/n8n_data"
DOCKER_COMPOSE_FILE="$N8N_BASE_DIR/docker-compose.yml"
DEFAULT_TZ="Asia/Ho_Chi_Minh"
BACKUP_DIR="$HOME/n8n-backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

# Màu sắc
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

set -e
set -u

print_section() { echo -e "${BLUE}>>> $1${NC}"; }
print_success() { echo -e "${GREEN}✓ $1${NC}"; }
print_error() { echo -e "${RED}✗ $1${NC}"; }

# --- Cài đặt n8n Local ---
install_n8n_local() {
    print_section "Cài đặt n8n Local 24/7 trên Orange Pi"
    
    # Kiểm tra Docker
    if ! command -v docker &> /dev/null; then
        echo ">>> Đang cài đặt Docker..."
        curl -sSL https://get.docker.com | sh
        systemctl enable docker
        systemctl start docker
    fi

    mkdir -p "$N8N_VOLUME_DIR"
    chown -R 1000:1000 "$N8N_VOLUME_DIR"

    # Tạo Docker Compose
    SYSTEM_TZ=$(cat /etc/timezone 2>/dev/null || echo "$DEFAULT_TZ")
    cat <<EOF > "$DOCKER_COMPOSE_FILE"
services:
  n8n:
    image: n8nio/n8n
    container_name: n8n
    restart: unless-stopped
    ports:
      - "5678:5678"
    environment:
      - TZ=${SYSTEM_TZ}
    volumes:
      - ./n8n_data:/home/node/.n8n
EOF

    echo ">>> Khởi động n8n container..."
    docker compose -f "$DOCKER_COMPOSE_FILE" up -d

    # Lấy IP Local
    LOCAL_IP=$(hostname -I | awk '{print $1}')
    
    print_success "Cài đặt hoàn tất!"
    echo "--------------------------------------------------"
    echo "n8n hiện đang chạy 24/7 (tự khởi động cùng Orange Pi)"
    echo "Truy cập tại: http://${LOCAL_IP}:5678"
    echo "--------------------------------------------------"
}

# --- Quản lý Backup ---
create_backup() {
    print_section "Đang tạo bản sao lưu dữ liệu..."
    mkdir -p "$BACKUP_DIR"
    BACKUP_FILE="n8n_local_backup_${TIMESTAMP}.tar.gz"
    
    docker compose -f "$DOCKER_COMPOSE_FILE" stop
    tar -czf "$BACKUP_DIR/$BACKUP_FILE" -C "$N8N_BASE_DIR" .
    docker compose -f "$DOCKER_COMPOSE_FILE" start
    
    print_success "Đã lưu bản backup tại: $BACKUP_DIR/$BACKUP_FILE"
}

# --- Cập nhật n8n ---
update_n8n() {
    print_section "Đang cập nhật n8n lên bản mới nhất..."
    docker compose -f "$DOCKER_COMPOSE_FILE" pull
    docker compose -f "$DOCKER_COMPOSE_FILE" up -d
    print_success "Đã cập nhật xong!"
}

# --- Menu chính ---
show_menu() {
    clear
    echo -e "${BLUE}================================================${NC}"
    echo -e "${BLUE}    N8N LOCAL MANAGEMENT (ORANGE PI)${NC}"
    echo -e "${BLUE}================================================${NC}"
    echo "1. 🚀 Cài đặt n8n Local mới (24/7)"
    echo "2. 💾 Sao lưu dữ liệu (Backup)"
    echo "3. 🔄 Cập nhật n8n lên bản mới"
    echo "4. 📊 Kiểm tra trạng thái hệ thống"
    echo "0. ❌ Thoát"
    echo ""
    read -p "Nhập lựa chọn: " choice
}

if [ $# -gt 0 ]; then
    case $1 in
        "install") install_n8n_local ;;
        "backup") create_backup ;;
        "update") update_n8n ;;
        *) echo "Sử dụng: $0 [install|backup|update]" ;;
    esac
else
    while true; do
        show_menu
        case $choice in
            1) install_n8n_local ;;
            2) create_backup ;;
            3) update_n8n ;;
            4) docker compose -f "$DOCKER_COMPOSE_FILE" ps ;;
            0) exit 0 ;;
        esac
        read -p "Nhấn Enter để tiếp tục..."
    done
fi