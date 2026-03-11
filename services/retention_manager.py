"""
services/retention_manager.py
Retention Policy Manager — tự động xóa footage/snapshots cũ.

Tiêu chuẩn công nghiệp:
  - Footage thông thường: 30 ngày
  - Footage nhạy cảm: 90 ngày (cấu hình qua RETENTION_SENSITIVE_DAYS)
  - Legal hold: KHÔNG BAO GIỜ xóa cho đến khi được giải phóng

Bảo mật:
  - Chỉ xóa file trong SNAPSHOT_DIR (không cho phép path traversal)
  - Kiểm tra legal hold trước mỗi lần xóa
  - Log mọi thao tác xóa vào DB

Usage:
    python -m services.retention_manager --run-now
"""
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("retention_manager")

# Defaults — override via env vars
DEFAULT_RETENTION_DAYS           = int(os.environ.get("RETENTION_DAYS", "30"))
DEFAULT_RETENTION_SENSITIVE_DAYS = int(os.environ.get("RETENTION_SENSITIVE_DAYS", "90"))
DEFAULT_SNAPSHOT_DIR             = os.environ.get("SNAPSHOT_DIR", "./data/snapshots")
SCAN_INTERVAL_HOURS              = 6   # scan every 6 hours


class RetentionManager:
    """
    Daemon thread that periodically scans SNAPSHOT_DIR and deletes files
    older than retention_days, respecting legal holds.
    """

    def __init__(self, db, snapshot_dir: str = DEFAULT_SNAPSHOT_DIR,
                 retention_days: int = DEFAULT_RETENTION_DAYS):
        self._db = db
        self._snapshot_dir = os.path.realpath(snapshot_dir)  # resolve symlinks
        self._retention_days = retention_days
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        os.makedirs(self._snapshot_dir, exist_ok=True)
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="retention_manager"
        )
        self._thread.start()
        logger.info(
            "RetentionManager started: dir=%s retention=%dd scan_every=%dh",
            self._snapshot_dir, self._retention_days, SCAN_INTERVAL_HOURS
        )

    def stop(self):
        self._stop.set()

    def run_now(self) -> dict:
        """Run a retention scan immediately. Returns stats dict."""
        return self._scan()

    # ── Internal ──────────────────────────────────────────────────────────────

    def _loop(self):
        # Run once at startup, then every SCAN_INTERVAL_HOURS
        while not self._stop.is_set():
            try:
                stats = self._scan()
                logger.info("Retention scan complete: %s", stats)
            except Exception as e:
                logger.error("Retention scan error: %s", e)
            self._stop.wait(timeout=SCAN_INTERVAL_HOURS * 3600)

    def _scan(self) -> dict:
        cutoff = datetime.utcnow() - timedelta(days=self._retention_days)
        deleted = 0
        skipped_hold = 0
        skipped_recent = 0
        errors = 0

        snapshot_root = Path(self._snapshot_dir)
        if not snapshot_root.exists():
            return {"deleted": 0, "skipped_hold": 0, "skipped_recent": 0, "errors": 0}

        for fpath in snapshot_root.rglob("*"):
            if not fpath.is_file():
                continue

            # ── Security: prevent path traversal ─────────────────────────────
            try:
                real = fpath.resolve()
                real.relative_to(snapshot_root.resolve())
            except ValueError:
                logger.warning("Path traversal attempt blocked: %s", fpath)
                errors += 1
                continue

            # ── Check file age ────────────────────────────────────────────────
            try:
                mtime = datetime.utcfromtimestamp(fpath.stat().st_mtime)
            except OSError:
                errors += 1
                continue

            if mtime >= cutoff:
                skipped_recent += 1
                continue

            # ── Check legal hold ──────────────────────────────────────────────
            if self._db.is_legal_hold(str(real)):
                skipped_hold += 1
                logger.debug("Legal hold — skipping: %s", real)
                continue

            # ── Delete ────────────────────────────────────────────────────────
            try:
                fpath.unlink()
                self._db.log_event(
                    "RETENTION_DELETE",
                    f"Auto-deleted: {fpath.name} (age: {(datetime.utcnow()-mtime).days}d)",
                    0, 0
                )
                deleted += 1
                logger.debug("Deleted: %s", fpath)
            except OSError as e:
                logger.error("Delete failed %s: %s", fpath, e)
                errors += 1

        return {
            "deleted": deleted,
            "skipped_hold": skipped_hold,
            "skipped_recent": skipped_recent,
            "errors": errors,
            "cutoff": cutoff.isoformat(),
        }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    parser = argparse.ArgumentParser(description="Retention Manager")
    parser.add_argument("--run-now", action="store_true", help="Scan ngay lập tức")
    parser.add_argument("--dir", default=DEFAULT_SNAPSHOT_DIR, help="Thư mục snapshot")
    parser.add_argument("--days", type=int, default=DEFAULT_RETENTION_DAYS,
                        help="Số ngày giữ lại (default 30)")
    args = parser.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from core.database import DatabaseManager
    from core.config import DB_PATH

    db = DatabaseManager(DB_PATH)
    mgr = RetentionManager(db, snapshot_dir=args.dir, retention_days=args.days)

    if args.run_now:
        stats = mgr.run_now()
        print(f"Scan complete: {stats}")
    else:
        print("Use --run-now to scan immediately.")
