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
