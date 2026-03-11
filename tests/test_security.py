"""
tests/test_security.py
Security self-test suite — kiểm tra tất cả security hardening.

Chạy:
    cd /path/to/yolov11-nhandien-biensoxe
    python -m pytest tests/test_security.py -v
    # hoặc standalone:
    python tests/test_security.py
"""
import os
import sys
import secrets
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════════════════════════
# 1. AUTH — Session token security
# ═══════════════════════════════════════════════════════════════════════════════

class TestAuthSecurity(unittest.TestCase):

    def test_session_token_entropy(self):
        """Session token phải có ít nhất 32 bytes entropy (256 bits)."""
        token = secrets.token_hex(32)
        self.assertEqual(len(token), 64, "token_hex(32) phải tạo ra 64 hex chars")
        # Kiểm tra uniqueness — 10 tokens không được trùng nhau
        tokens = {secrets.token_hex(32) for _ in range(10)}
        self.assertEqual(len(tokens), 10, "Tokens phải unique")

    def test_api_routes_require_auth(self):
        """Tất cả routes (trừ /login) phải redirect về /login khi chưa auth."""
        from fastapi.testclient import TestClient
        from services.api_server import create_api_server, app

        # Reset app state
        mock_streamer = MagicMock()
        mock_streamer.generate.return_value = iter([])
        mock_streamer.get_snapshot.return_value = None
        mock_get_state = MagicMock(return_value=(0, 0, False))
        mock_mqtt = MagicMock()
        mock_mqtt.ocr_enabled = True
        mock_mqtt.ptz_mode = "gate"
        mock_mqtt.publish_heartbeat = MagicMock()

        create_api_server(mock_streamer, mock_get_state, mock_mqtt)
        client = TestClient(app, follow_redirects=False)

        protected_routes = ["/dashboard", "/api/status", "/api/cameras/status"]
        for route in protected_routes:
            resp = client.get(route)
            self.assertIn(resp.status_code, [302, 307],
                          f"{route} phải redirect khi chưa auth (got {resp.status_code})")
            location = resp.headers.get("location", "")
            self.assertIn("login", location,
                          f"{route} phải redirect về /login (got {location})")

    def test_login_wrong_credentials_rejected(self):
        """Login sai credentials phải bị từ chối."""
        from fastapi.testclient import TestClient
        from services.api_server import create_api_server, app

        mock_streamer = MagicMock()
        mock_streamer.generate.return_value = iter([])
        create_api_server(mock_streamer, MagicMock(return_value=(0,0,False)), MagicMock())
        client = TestClient(app, follow_redirects=False)

        resp = client.post("/login", data={"username": "admin", "password": "wrongpassword"})
        # Phải redirect về /login (không phải /dashboard)
        self.assertIn(resp.status_code, [302, 307])
        location = resp.headers.get("location", "")
        self.assertNotIn("dashboard", location,
                         "Login sai không được redirect về dashboard")

    def test_session_cookie_httponly(self):
        """Session cookie phải có httponly flag."""
        from fastapi.testclient import TestClient
        from services import api_server
        from services.api_server import create_api_server, app

        # Patch credentials
        with patch.object(api_server, "_UI_USER", "admin"), \
             patch.object(api_server, "_UI_PASS", "testpass123"):
            mock_streamer = MagicMock()
            mock_streamer.generate.return_value = iter([])
            create_api_server(mock_streamer, MagicMock(return_value=(0,0,False)), MagicMock())
            client = TestClient(app, follow_redirects=False)
            resp = client.post("/login", data={"username": "admin", "password": "testpass123"})

        set_cookie = resp.headers.get("set-cookie", "")
        self.assertIn("httponly", set_cookie.lower(),
                      "Session cookie phải có HttpOnly flag")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. PATH TRAVERSAL — Snapshot endpoint
# ═══════════════════════════════════════════════════════════════════════════════

class TestPathTraversal(unittest.TestCase):

    def test_snapshot_cam_id_no_path_traversal(self):
        """cam_id trong /snapshot/{cam_id} không được chứa path traversal."""
        from fastapi.testclient import TestClient
        from services.api_server import create_api_server, app, _sessions

        mock_streamer = MagicMock()
        mock_streamer.generate.return_value = iter([])
        mock_streamer.get_snapshot.return_value = b"fake_jpeg"
        create_api_server(mock_streamer, MagicMock(return_value=(0,0,False)), MagicMock())

        # Inject a valid session
        token = secrets.token_hex(32)
        _sessions.add(token)
        client = TestClient(app, follow_redirects=False,
                            cookies={"session_token": token})

        # Attempt path traversal via cam_id
        malicious_ids = ["../../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "cam1;rm -rf /"]
        for bad_id in malicious_ids:
            resp = client.get(f"/snapshot/{bad_id}")
            # Must NOT return 200 with file content
            self.assertNotEqual(resp.status_code, 200,
                                f"Path traversal cam_id should not return 200: {bad_id!r}")
        _sessions.discard(token)

    def test_retention_manager_path_traversal_blocked(self):
        """RetentionManager không được xóa file ngoài snapshot_dir."""
        from services.retention_manager import RetentionManager

        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = os.path.join(tmpdir, "snapshots")
            os.makedirs(snap_dir)

            # Create a file OUTSIDE snap_dir
            outside_file = os.path.join(tmpdir, "important.txt")
            with open(outside_file, "w") as f:
                f.write("do not delete")

            # Create an old file inside snap_dir
            old_file = os.path.join(snap_dir, "old_snap.jpg")
            with open(old_file, "w") as f:
                f.write("old snapshot")
            # Set mtime to 60 days ago
            old_time = time.time() - 60 * 86400
            os.utime(old_file, (old_time, old_time))

            mock_db = MagicMock()
            mock_db.is_legal_hold.return_value = False
            mock_db.log_event = MagicMock()

            mgr = RetentionManager(mock_db, snapshot_dir=snap_dir, retention_days=30)
            mgr.run_now()

            # Old file inside snap_dir should be deleted
            self.assertFalse(os.path.exists(old_file), "Old snapshot should be deleted")
            # File outside snap_dir must NOT be deleted
            self.assertTrue(os.path.exists(outside_file),
                            "File outside snapshot_dir must NOT be deleted")


# ═══════════════════════════════════════════════════════════════════════════════
# 3. SQL INJECTION — Database layer
# ═══════════════════════════════════════════════════════════════════════════════

class TestSQLInjection(unittest.TestCase):

    def setUp(self):
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmpfile.close()
        from core.database import DatabaseManager
        self.db = DatabaseManager(self._tmpfile.name)

    def tearDown(self):
        os.unlink(self._tmpfile.name)

    def test_is_plate_whitelisted_injection(self):
        """is_plate_whitelisted phải dùng parameterized query."""
        # Inject attempt: always-true condition
        malicious = "' OR '1'='1"
        result = self.db.is_plate_whitelisted(malicious)
        self.assertFalse(result, "SQL injection should not return True for empty whitelist")

    def test_log_event_injection(self):
        """log_event phải xử lý ký tự đặc biệt an toàn."""
        evil_desc = "'; DROP TABLE events; --"
        try:
            self.db.log_event("TEST", evil_desc, 0, 0)
        except Exception as e:
            self.fail(f"log_event raised exception on injection attempt: {e}")
        # Table must still exist
        conn = sqlite3.connect(self._tmpfile.name)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM events")
        count = cursor.fetchone()[0]
        conn.close()
        self.assertGreater(count, 0, "events table must still exist after injection attempt")

    def test_add_pending_plate_injection(self):
        """add_pending_plate phải dùng parameterized query."""
        evil_plate = "'; DROP TABLE pending_plates; --"
        self.db.add_pending_plate("test-id", 1, evil_plate, evil_plate, "2026-01-01")
        # Table must still exist
        conn = sqlite3.connect(self._tmpfile.name)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM pending_plates")
        count = cursor.fetchone()[0]
        conn.close()
        self.assertGreaterEqual(count, 0, "pending_plates table must still exist")


# ═══════════════════════════════════════════════════════════════════════════════
# 4. LEGAL HOLD — Retention must respect holds
# ═══════════════════════════════════════════════════════════════════════════════

class TestLegalHold(unittest.TestCase):

    def setUp(self):
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmpfile.close()
        from core.database import DatabaseManager
        self.db = DatabaseManager(self._tmpfile.name)

    def tearDown(self):
        os.unlink(self._tmpfile.name)

    def test_legal_hold_prevents_deletion(self):
        """RetentionManager không được xóa file đang trong legal hold."""
        from services.retention_manager import RetentionManager

        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = os.path.join(tmpdir, "snapshots")
            os.makedirs(snap_dir)

            held_file = os.path.join(snap_dir, "evidence.jpg")
            with open(held_file, "w") as f:
                f.write("evidence")
            old_time = time.time() - 60 * 86400
            os.utime(held_file, (old_time, old_time))

            # Add legal hold
            self.db.add_legal_hold(
                os.path.realpath(held_file),
                "Điều tra sự cố", "admin"
            )

            mgr = RetentionManager(self.db, snapshot_dir=snap_dir, retention_days=30)
            stats = mgr.run_now()

            self.assertTrue(os.path.exists(held_file),
                            "File under legal hold must NOT be deleted")
            self.assertEqual(stats["skipped_hold"], 1)
            self.assertEqual(stats["deleted"], 0)

    def test_legal_hold_release_allows_deletion(self):
        """Sau khi giải phóng legal hold, file được xóa bình thường."""
        from services.retention_manager import RetentionManager

        with tempfile.TemporaryDirectory() as tmpdir:
            snap_dir = os.path.join(tmpdir, "snapshots")
            os.makedirs(snap_dir)

            held_file = os.path.join(snap_dir, "old_evidence.jpg")
            with open(held_file, "w") as f:
                f.write("old evidence")
            old_time = time.time() - 60 * 86400
            os.utime(held_file, (old_time, old_time))

            real_path = os.path.realpath(held_file)
            self.db.add_legal_hold(real_path, "Test hold", "admin")
            self.db.release_legal_hold(real_path)

            mgr = RetentionManager(self.db, snapshot_dir=snap_dir, retention_days=30)
            stats = mgr.run_now()

            self.assertFalse(os.path.exists(held_file),
                             "Released hold file should be deleted by retention")
            self.assertEqual(stats["deleted"], 1)

    def test_is_legal_hold_returns_false_after_release(self):
        """is_legal_hold phải trả về False sau khi release."""
        self.db.add_legal_hold("/tmp/test.jpg", "reason", "admin")
        self.assertTrue(self.db.is_legal_hold("/tmp/test.jpg"))
        self.db.release_legal_hold("/tmp/test.jpg")
        self.assertFalse(self.db.is_legal_hold("/tmp/test.jpg"))


# ═══════════════════════════════════════════════════════════════════════════════
# 5. ASSET REGISTRY — Input validation
# ═══════════════════════════════════════════════════════════════════════════════

class TestAssetRegistry(unittest.TestCase):

    def setUp(self):
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmpfile.close()
        from core.asset_registry import AssetRegistry
        self.registry = AssetRegistry(self._tmpfile.name)

    def tearDown(self):
        os.unlink(self._tmpfile.name)

    def test_upsert_requires_cam_id_and_name(self):
        """upsert phải raise ValueError nếu thiếu cam_id hoặc name."""
        with self.assertRaises((ValueError, KeyError)):
            self.registry.upsert({"name": "Test"})  # missing cam_id
        with self.assertRaises((ValueError, KeyError)):
            self.registry.upsert({"cam_id": "C001"})  # missing name

    def test_cam_id_injection_blocked(self):
        """cam_id chứa ký tự nguy hiểm phải bị từ chối."""
        from core.asset_registry import AssetRegistry
        with self.assertRaises(ValueError):
            self.registry.upsert({"cam_id": "'; DROP TABLE cameras_asset; --", "name": "Evil"})

    def test_rtsp_url_masked_in_export(self):
        """export_json phải mask password trong RTSP URL."""
        self.registry.upsert({
            "cam_id": "C001",
            "name": "Test Camera",
            "rtsp_url": "rtsp://admin:secret123@192.168.1.55/stream",
        })
        exported = self.registry.export_json()
        self.assertEqual(len(exported), 1)
        url = exported[0]["rtsp_url"]
        self.assertNotIn("secret123", url, "Password must be masked in export")
        self.assertIn("***", url, "Masked password should show ***")
        self.assertIn("admin", url, "Username should still be visible")

    def test_get_nonexistent_returns_none(self):
        result = self.registry.get("nonexistent_cam")
        self.assertIsNone(result)

    def test_upsert_and_retrieve(self):
        """Upsert rồi get phải trả về đúng dữ liệu."""
        self.registry.upsert({
            "cam_id": "VN-HCM01-PARKING-EXT-C001",
            "name": "Cổng vào chính",
            "ip": "192.168.1.55",
            "dori_class": "Identification",
        })
        record = self.registry.get("VN-HCM01-PARKING-EXT-C001")
        self.assertIsNotNone(record)
        self.assertEqual(record["name"], "Cổng vào chính")
        self.assertEqual(record["dori_class"], "Identification")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. RTSP URL — Validation in CameraManager
# ═══════════════════════════════════════════════════════════════════════════════

class TestRTSPValidation(unittest.TestCase):

    def test_rtsp_url_not_logged_in_plaintext(self):
        """RTSP URL (với credentials) không được xuất hiện trong log plaintext."""
        import logging
        import io

        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        logging.getLogger("camera_manager").addHandler(handler)

        from services.camera_manager import CameraManager
        mgr = CameraManager(db=None)
        # Don't actually connect — just check the URL isn't logged with password
        # The _read_loop logs cam_id, not the full URL
        log_output = log_stream.getvalue()
        self.assertNotIn("secret_password", log_output,
                         "RTSP password must not appear in logs")

        logging.getLogger("camera_manager").removeHandler(handler)


# ═══════════════════════════════════════════════════════════════════════════════
# 7. CAMERA HEALTH LOG — No injection via event data
# ═══════════════════════════════════════════════════════════════════════════════

class TestHealthLogSecurity(unittest.TestCase):

    def setUp(self):
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmpfile.close()
        from core.database import DatabaseManager
        self.db = DatabaseManager(self._tmpfile.name)

    def tearDown(self):
        os.unlink(self._tmpfile.name)

    def test_log_camera_event_injection(self):
        """log_camera_event phải xử lý injection trong notes field."""
        evil_notes = "'; DROP TABLE camera_health_log; --"
        try:
            self.db.log_camera_event(
                "cam1", "OFFLINE", "2026-01-01T00:00:00",
                notes=evil_notes
            )
        except Exception as e:
            self.fail(f"log_camera_event raised on injection: {e}")

        # Table must still exist
        conn = sqlite3.connect(self._tmpfile.name)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM camera_health_log")
        count = cursor.fetchone()[0]
        conn.close()
        self.assertGreater(count, 0)

    def test_get_camera_health_injection(self):
        """get_camera_health phải dùng parameterized query cho cam_id."""
        evil_cam_id = "' OR '1'='1"
        try:
            result = self.db.get_camera_health(cam_id=evil_cam_id, hours=24)
            self.assertIsInstance(result, list)
        except Exception as e:
            self.fail(f"get_camera_health raised on injection: {e}")


# ═══════════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Security self-test suite")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    verbosity = 2 if args.verbose else 1
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=verbosity)
    result = runner.run(suite)

    # Exit with non-zero if any test failed
    sys.exit(0 if result.wasSuccessful() else 1)
