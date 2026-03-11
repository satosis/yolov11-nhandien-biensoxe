import os
import sqlite3
from datetime import datetime


class DatabaseManager:
    def __init__(self, path):
        self.path = path
        db_dir = os.path.dirname(self.path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        self.init_db()

    def init_db(self):
        conn = sqlite3.connect(self.path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME, event_type TEXT, description TEXT,
                truck_count INTEGER, person_count INTEGER
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS vehicle_whitelist (
                plate_norm TEXT PRIMARY KEY,
                label TEXT,
                added_at_utc TEXT,
                added_by TEXT,
                note TEXT
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_plates (
                pending_id TEXT PRIMARY KEY,
                event_id INTEGER,
                plate_raw TEXT,
                plate_norm TEXT,
                first_seen_utc TEXT,
                status TEXT,
                confirmed_at_utc TEXT,
                confirmed_by TEXT
            )
        ''')
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_vehicle_whitelist_plate_norm ON vehicle_whitelist (plate_norm)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_pending_plates_plate_norm ON pending_plates (plate_norm)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_pending_plates_status ON pending_plates (status)'
        )
        # Camera health & recording continuity log
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS camera_health_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cam_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                duration_seconds REAL,
                notes TEXT
            )
        ''')
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_health_cam_id ON camera_health_log (cam_id)'
        )
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_health_event_type ON camera_health_log (event_type)'
        )
        # Legal hold — footage that must NOT be auto-deleted
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS legal_hold (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL UNIQUE,
                reason TEXT,
                held_by TEXT,
                held_at TEXT NOT NULL,
                released_at TEXT
            )
        ''')
        # SLA daily metrics
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sla_daily (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date TEXT NOT NULL,
                cam_id TEXT NOT NULL,
                uptime_pct REAL,
                gap_count INTEGER,
                gap_total_seconds REAL,
                offline_count INTEGER,
                UNIQUE(report_date, cam_id)
            )
        ''')
        conn.commit()
        conn.close()

    def is_plate_whitelisted(self, plate_norm):
        try:
            conn = sqlite3.connect(self.path)
            cursor = conn.cursor()
            cursor.execute('SELECT 1 FROM vehicle_whitelist WHERE plate_norm = ? LIMIT 1', (plate_norm,))
            row = cursor.fetchone()
            conn.close()
            return row is not None
        except sqlite3.Error:
            return False

    def add_pending_plate(self, pending_id, event_id, plate_raw, plate_norm, first_seen_utc):
        try:
            conn = sqlite3.connect(self.path)
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT OR IGNORE INTO pending_plates (
                    pending_id, event_id, plate_raw, plate_norm, first_seen_utc, status
                ) VALUES (?, ?, ?, ?, ?, ?)
                ''',
                (pending_id, event_id, plate_raw, plate_norm, first_seen_utc, "pending")
            )
            conn.commit()
            conn.close()
        except sqlite3.Error:
            pass

    def upsert_vehicle_whitelist(self, plate_norm, label, added_by, note=None):
        try:
            conn = sqlite3.connect(self.path)
            cursor = conn.cursor()
            cursor.execute(
                '''
                INSERT INTO vehicle_whitelist (plate_norm, label, added_at_utc, added_by, note)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(plate_norm) DO UPDATE SET
                    label=excluded.label,
                    added_at_utc=excluded.added_at_utc,
                    added_by=excluded.added_by,
                    note=excluded.note
                ''',
                (plate_norm, label, datetime.utcnow().isoformat(), added_by, note)
            )
            conn.commit()
            conn.close()
            return True
        except sqlite3.Error:
            return False

    def update_pending_status(self, plate_norm, status, confirmed_by):
        try:
            conn = sqlite3.connect(self.path)
            cursor = conn.cursor()
            cursor.execute(
                '''
                UPDATE pending_plates
                SET status = ?, confirmed_at_utc = ?, confirmed_by = ?
                WHERE plate_norm = ? AND status = 'pending'
                ''',
                (status, datetime.utcnow().isoformat(), confirmed_by, plate_norm)
            )
            conn.commit()
            conn.close()
            return True
        except sqlite3.Error:
            return False

    def log_event(self, event_type, description, trucks, people):
        conn = sqlite3.connect(self.path)
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO events (timestamp, event_type, description, truck_count, person_count) VALUES (?, ?, ?, ?, ?)',
            (datetime.now(), event_type, description, trucks, people)
        )
        event_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return event_id

    def get_stats(self):
        conn = sqlite3.connect(self.path)
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*), event_type FROM events GROUP BY event_type')
        stats = cursor.fetchall()
        conn.close()
        return stats

    def get_pending_plates(self):
        conn = sqlite3.connect(self.path)
        cursor = conn.cursor()
        cursor.execute("SELECT plate_norm, plate_raw, first_seen_utc FROM pending_plates WHERE status = 'pending'")
        pending = cursor.fetchall()
        conn.close()
        return pending

    # ── Camera health log ─────────────────────────────────────────────────────

    def log_camera_event(self, cam_id: str, event_type: str, started_at: str,
                         ended_at: str = None, duration_seconds: float = None,
                         notes: str = None) -> int:
        """Log a camera health event (OFFLINE, GAP, SHIFT, etc.)."""
        conn = sqlite3.connect(self.path)
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO camera_health_log
               (cam_id, event_type, started_at, ended_at, duration_seconds, notes)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (cam_id, event_type, started_at, ended_at, duration_seconds, notes)
        )
        row_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return row_id

    def get_camera_health(self, cam_id: str = None, hours: int = 24) -> list[dict]:
        """Return health events for the last N hours, optionally filtered by cam_id."""
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        if cam_id:
            cursor.execute(
                '''SELECT * FROM camera_health_log
                   WHERE cam_id = ?
                   AND started_at >= datetime('now', ?)
                   ORDER BY started_at DESC''',
                (cam_id, f'-{hours} hours')
            )
        else:
            cursor.execute(
                '''SELECT * FROM camera_health_log
                   WHERE started_at >= datetime('now', ?)
                   ORDER BY started_at DESC''',
                (f'-{hours} hours',)
            )
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return rows

    # ── Legal hold ────────────────────────────────────────────────────────────

    def add_legal_hold(self, file_path: str, reason: str, held_by: str) -> bool:
        """Mark a file as legally held — retention manager must not delete it."""
        try:
            conn = sqlite3.connect(self.path)
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT OR IGNORE INTO legal_hold (file_path, reason, held_by, held_at)
                   VALUES (?, ?, ?, ?)''',
                (file_path, reason, held_by, datetime.utcnow().isoformat())
            )
            conn.commit()
            conn.close()
            return True
        except sqlite3.Error:
            return False

    def release_legal_hold(self, file_path: str) -> bool:
        try:
            conn = sqlite3.connect(self.path)
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE legal_hold SET released_at = ? WHERE file_path = ? AND released_at IS NULL",
                (datetime.utcnow().isoformat(), file_path)
            )
            conn.commit()
            conn.close()
            return True
        except sqlite3.Error:
            return False

    def is_legal_hold(self, file_path: str) -> bool:
        try:
            conn = sqlite3.connect(self.path)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM legal_hold WHERE file_path = ? AND released_at IS NULL LIMIT 1",
                (file_path,)
            )
            row = cursor.fetchone()
            conn.close()
            return row is not None
        except sqlite3.Error:
            return False

    # ── SLA daily ─────────────────────────────────────────────────────────────

    def upsert_sla_daily(self, report_date: str, cam_id: str, uptime_pct: float,
                         gap_count: int, gap_total_seconds: float, offline_count: int):
        try:
            conn = sqlite3.connect(self.path)
            cursor = conn.cursor()
            cursor.execute(
                '''INSERT INTO sla_daily
                   (report_date, cam_id, uptime_pct, gap_count, gap_total_seconds, offline_count)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(report_date, cam_id) DO UPDATE SET
                       uptime_pct=excluded.uptime_pct,
                       gap_count=excluded.gap_count,
                       gap_total_seconds=excluded.gap_total_seconds,
                       offline_count=excluded.offline_count''',
                (report_date, cam_id, uptime_pct, gap_count, gap_total_seconds, offline_count)
            )
            conn.commit()
            conn.close()
        except sqlite3.Error:
            pass

    def get_sla_daily(self, days: int = 30) -> list[dict]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            '''SELECT * FROM sla_daily
               WHERE report_date >= date('now', ?)
               ORDER BY report_date DESC, cam_id''',
            (f'-{days} days',)
        )
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return rows
