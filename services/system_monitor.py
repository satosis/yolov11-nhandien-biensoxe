import time
import subprocess
import psutil
from services.telegram_service import notify_telegram


def get_cpu_temp():
    """Lấy nhiệt độ CPU (Linux/Raspberry Pi/Orange Pi)."""
    try:
        result = subprocess.run(['vcgencmd', 'measure_temp'], capture_output=True, text=True)
        if result.returncode == 0:
            return float(result.stdout.split('=')[1].split("'")[0])
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            return int(f.read()) / 1000.0
    except (FileNotFoundError, IndexError, ValueError):
        return None


def system_monitor_loop():
    """Vòng lặp giám sát hệ thống, cảnh báo nếu CPU/RAM/Disk quá cao."""
    while True:
        cpu_percent = psutil.cpu_percent(interval=1)
        mem_info = psutil.virtual_memory()
        disk_info = psutil.disk_usage('/')

        if cpu_percent > 90:
            notify_telegram(f"CẢNH BÁO: CPU đang ở mức cao: {cpu_percent:.1f}%", important=True)
        if mem_info.percent > 90:
            notify_telegram(f"CẢNH BÁO: RAM đang ở mức cao: {mem_info.percent:.1f}%", important=True)
        if disk_info.percent > 90:
            notify_telegram(f"CẢNH BÁO: Đĩa cứng đầy: {disk_info.percent:.1f}%", important=True)

        time.sleep(60)
