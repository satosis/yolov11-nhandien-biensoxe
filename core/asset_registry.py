"""
core/asset_registry.py
Camera Asset Registry — CMDB (Configuration Management Database)

Lưu trữ thông tin đầy đủ của từng camera theo tiêu chuẩn công nghiệp:
MAC, IP, serial, model, firmware, tọa độ GPS, chiều cao gắn, FoV, DORI class.
"""
import sqlite3
from datetime import datetime
from typing import Optional


class AssetRegistry:
    TABLE = "cameras_asset"

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_table()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_table(self):
        with self._conn() as conn:
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self.TABLE} (
                    cam_id          TEXT PRIMARY KEY,
                    name            TEXT NOT NULL,
                    ip              TEXT,
                    mac             TEXT,
                    serial          TEXT,
                    model           TEXT,
                    firmware        TEXT,
                    rtsp_url        TEXT,
                    install_date    TEXT,
                    location_lat    REAL,
                    location_lon    REAL,
                    mount_height_m  REAL,
                    fov_deg         REAL,
                    dori_class      TEXT,
                    notes           TEXT,
                    updated_at      TEXT
                )
            """)
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_asset_ip ON {self.TABLE} (ip)"
            )
            conn.commit()

    def upsert(self, data: dict) -> bool:
        """Insert or update a camera record. cam_id and name are required."""
        required = {"cam_id", "name"}
        if not required.issubset(data):
            raise ValueError(f"Missing required fields: {required - data.keys()}")

        # Sanitise cam_id: only allow safe characters
        cam_id = str(data["cam_id"]).strip()
        if not cam_id or any(c in cam_id for c in (";", "'", '"', "\n", "\r")):
            raise ValueError(f"Invalid cam_id: {cam_id!r}")

        data["updated_at"] = datetime.utcnow().isoformat()
        fields = [
            "cam_id", "name", "ip", "mac", "serial", "model", "firmware",
            "rtsp_url", "install_date", "location_lat", "location_lon",
            "mount_height_m", "fov_deg", "dori_class", "notes", "updated_at",
        ]
        values = [data.get(f) for f in fields]
        placeholders = ", ".join("?" * len(fields))
        updates = ", ".join(f"{f}=excluded.{f}" for f in fields if f != "cam_id")

        try:
            with self._conn() as conn:
                conn.execute(
                    f"""INSERT INTO {self.TABLE} ({', '.join(fields)})
                        VALUES ({placeholders})
                        ON CONFLICT(cam_id) DO UPDATE SET {updates}""",
                    values,
                )
                conn.commit()
            return True
        except sqlite3.Error:
            return False

    def get(self, cam_id: str) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT * FROM {self.TABLE} WHERE cam_id = ?", (cam_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_all(self) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM {self.TABLE} ORDER BY cam_id"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, cam_id: str) -> bool:
        try:
            with self._conn() as conn:
                conn.execute(
                    f"DELETE FROM {self.TABLE} WHERE cam_id = ?", (cam_id,)
                )
                conn.commit()
            return True
        except sqlite3.Error:
            return False

    def export_json(self) -> list[dict]:
        """Export all records as JSON-serialisable list (masks rtsp_url credentials)."""
        records = self.get_all()
        for r in records:
            url = r.get("rtsp_url") or ""
            # Mask password in rtsp://user:PASS@host/path
            if "://" in url and "@" in url:
                scheme, rest = url.split("://", 1)
                if "@" in rest:
                    creds, host_path = rest.split("@", 1)
                    if ":" in creds:
                        user, _ = creds.split(":", 1)
                        r["rtsp_url"] = f"{scheme}://{user}:***@{host_path}"
        return records
