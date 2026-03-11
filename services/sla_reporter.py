"""
services/sla_reporter.py
SLA Daily Reporter — tính toán và lưu SLA metrics hàng ngày.

Chạy như daemon thread, tính lúc 00:05 mỗi ngày (hoặc on-demand).
Metrics: uptime %, gap count, gap total seconds, offline count per camera.

Usage (standalone):
    python -m services.sla_reporter --run-now
"""
import logging
import os
import sys
import threading
import time
from datetime import date, datetime, timedelta

logger = logging.getLogger("sla_reporter")


class SLAReporter:
    """
    Tính SLA metrics từ camera_health_log và lưu vào sla_daily.
    Chạy tự động lúc 00:05 mỗi ngày.
    """

    REPORT_HOUR   = 0   # 00:xx
    REPORT_MINUTE = 5   # xx:05

    def __init__(self, db, camera_manager=None):
        """
        db: DatabaseManager instance
        camera_manager: CameraManager instance (để lấy danh sách cam_id)
        """
        self._db = db
        self._cam_mgr = camera_manager
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        """Khởi động daemon thread tự động báo cáo hàng ngày."""
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="sla_reporter"
        )
        self._thread.start()
        logger.info("SLA Reporter started (daily at %02d:%02d)", self.REPORT_HOUR, self.REPORT_MINUTE)

    def stop(self):
        self._stop.set()

    def run_now(self, report_date: date = None):
        """Tính SLA cho ngày hôm qua (hoặc ngày chỉ định) ngay lập tức."""
        target = report_date or (date.today() - timedelta(days=1))
        self._compute_and_save(target)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _loop(self):
        while not self._stop.is_set():
            now = datetime.now()
            # Tính thời gian đến lần chạy tiếp theo
            next_run = now.replace(
                hour=self.REPORT_HOUR, minute=self.REPORT_MINUTE,
                second=0, microsecond=0
            )
            if next_run <= now:
                next_run += timedelta(days=1)
            wait_secs = (next_run - now).total_seconds()
            logger.debug("Next SLA report in %.0f seconds", wait_secs)
            self._stop.wait(timeout=wait_secs)
            if self._stop.is_set():
                break
            yesterday = date.today() - timedelta(days=1)
            try:
                self._compute_and_save(yesterday)
            except Exception as e:
                logger.error("SLA compute error: %s", e)

    def _compute_and_save(self, report_date: date):
        """Tính uptime % và gap metrics cho từng camera trong ngày report_date."""
        date_str = report_date.isoformat()
        logger.info("Computing SLA for %s", date_str)

        # Lấy danh sách cam_id từ camera_manager hoặc từ health log
        if self._cam_mgr:
            cam_ids = [s["id"] for s in self._cam_mgr.get_all_status()]
        else:
            # Fallback: lấy từ DB
            cam_ids = self._cam_ids_from_db(date_str)

        if not cam_ids:
            logger.warning("No cameras found for SLA report %s", date_str)
            return

        day_seconds = 86400.0  # giây trong 1 ngày

        for cam_id in cam_ids:
            events = self._db.get_camera_health(cam_id=cam_id, hours=48)
            # Lọc events trong ngày report_date
            day_events = [
                e for e in events
                if e.get("started_at", "").startswith(date_str)
            ]

            offline_count = sum(1 for e in day_events if e["event_type"] == "OFFLINE")
            gap_events    = [e for e in day_events if e["event_type"] == "GAP"]
            gap_count     = len(gap_events)
            gap_total_s   = sum(
                float(e.get("duration_seconds") or 0) for e in gap_events
            )

            # Uptime = (86400 - total_offline_seconds) / 86400 * 100
            offline_events = [e for e in day_events if e["event_type"] == "OFFLINE"]
            offline_total_s = sum(
                float(e.get("duration_seconds") or 0) for e in offline_events
            )
            uptime_pct = max(0.0, (day_seconds - offline_total_s) / day_seconds * 100)

            self._db.upsert_sla_daily(
                report_date=date_str,
                cam_id=cam_id,
                uptime_pct=round(uptime_pct, 4),
                gap_count=gap_count,
                gap_total_seconds=round(gap_total_s, 1),
                offline_count=offline_count,
            )
            logger.info(
                "  %s: uptime=%.2f%% gaps=%d offline=%d",
                cam_id, uptime_pct, gap_count, offline_count
            )

    def _cam_ids_from_db(self, date_str: str) -> list[str]:
        """Lấy danh sách cam_id từ camera_health_log nếu không có camera_manager."""
        try:
            events = self._db.get_camera_health(hours=48)
            return list({e["cam_id"] for e in events if e.get("started_at", "").startswith(date_str)})
        except Exception:
            return []


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    parser = argparse.ArgumentParser(description="SLA Reporter")
    parser.add_argument("--run-now", action="store_true", help="Tính SLA ngay cho ngày hôm qua")
    parser.add_argument("--date", help="Ngày cụ thể (YYYY-MM-DD)", default=None)
    args = parser.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from core.database import DatabaseManager
    from core.config import DB_PATH

    db = DatabaseManager(DB_PATH)
    reporter = SLAReporter(db)

    if args.run_now or args.date:
        target = date.fromisoformat(args.date) if args.date else None
        reporter.run_now(target)
        print("Done.")
    else:
        print("Use --run-now to compute SLA immediately, or import SLAReporter and call .start()")
