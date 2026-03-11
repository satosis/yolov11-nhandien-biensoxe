import os
import secrets
import sqlite3
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from core.config import DB_PATH

app = FastAPI()

# --- Auth ---
_sessions: set[str] = set()
_UI_USER = "admin"
_UI_PASS = "changeme"
_SNAPSHOT_DIR = "./data/snapshots"

_UNPROTECTED = {"/login", "/favicon.ico"}


def _is_authed(request: Request) -> bool:
    token = request.cookies.get("session_token", "")
    return token in _sessions


def _auth_redirect(request: Request):
    """Trả về redirect /login nếu chưa đăng nhập, None nếu đã đăng nhập."""
    if not _is_authed(request):
        return RedirectResponse(url="/login", status_code=302)
    return None


def create_api_server(streamer, get_state_fn, mqtt_manager, camera_manager=None, settings_store=None):
    """Tạo API server với dashboard và endpoints.

    Args:
        streamer: MJPEGStreamer instance (camera "main", backward compat)
        get_state_fn: Hàm trả về (person_count, truck_count, door_open)
        mqtt_manager: MQTTManager instance
        camera_manager: CameraManager instance (optional, multi-camera)
    """

    # ── Auth routes ──────────────────────────────────────────────────────────

    @app.get("/login", response_class=HTMLResponse)
    def login_page(error: str = ""):
        err_html = f'<p style="color:#f44;margin-top:8px">{error}</p>' if error else ""
        return HTMLResponse(_LOGIN_HTML.replace("{{ERROR}}", err_html))

    @app.post("/login")
    async def login_submit(request: Request):
        form = await request.form()
        username = form.get("username", "")
        password = form.get("password", "")
        if username == _UI_USER and password == _UI_PASS:
            token = secrets.token_hex(32)
            _sessions.add(token)
            resp = RedirectResponse(url="/dashboard", status_code=302)
            resp.set_cookie("session_token", token, httponly=True, samesite="lax")
            return resp
        return RedirectResponse(url="/login?error=Sai+tên+đăng+nhập+hoặc+mật+khẩu", status_code=302)

    @app.get("/logout")
    def logout(request: Request):
        token = request.cookies.get("session_token", "")
        _sessions.discard(token)
        resp = RedirectResponse(url="/login", status_code=302)
        resp.delete_cookie("session_token")
        return resp

    # ── Dashboard ─────────────────────────────────────────────────────────────

    @app.get("/")
    def root(request: Request):
        if not _is_authed(request):
            return RedirectResponse(url="/login", status_code=302)
        return RedirectResponse(url="/dashboard", status_code=302)

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request):
        redir = _auth_redirect(request)
        if redir:
            return redir

        # Build camera list for grid
        if camera_manager:
            cams = camera_manager.get_all_status()
        else:
            cams = [{"id": "main", "name": "Camera Chính", "online": True}]

        # Pad to 4 slots for 2x2 grid
        while len(cams) < 4:
            cams.append(None)

        cells_html = ""
        for cam in cams:
            if cam is None:
                cells_html += '<div class="cell empty"><span class="empty-label">Không có camera</span></div>\n'
            else:
                cells_html += _camera_cell_html(cam["id"], cam["name"])

        return HTMLResponse(_DASHBOARD_HTML.replace("{{CELLS}}", cells_html))

    # ── Video feeds ───────────────────────────────────────────────────────────

    @app.get("/video_feed")
    def video_feed_legacy(request: Request):
        """Backward-compat: stream camera "main"."""
        redir = _auth_redirect(request)
        if redir:
            return redir
        return StreamingResponse(
            streamer.generate(),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    @app.get("/video_feed/{cam_id}")
    def video_feed(cam_id: str, request: Request):
        redir = _auth_redirect(request)
        if redir:
            return redir
        if camera_manager:
            s = camera_manager.get_streamer(cam_id)
        else:
            s = streamer if cam_id == "main" else None
        if s is None:
            return JSONResponse({"error": "camera not found"}, status_code=404)
        return StreamingResponse(
            s.generate(),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    # ── Snapshot ──────────────────────────────────────────────────────────────

    @app.get("/snapshot/{cam_id}")
    def snapshot(cam_id: str, request: Request):
        redir = _auth_redirect(request)
        if redir:
            return redir

        if camera_manager:
            data = camera_manager.snapshot(cam_id)
        elif cam_id == "main":
            data = streamer.get_snapshot()
        else:
            data = None

        if data is None:
            return JSONResponse({"error": "no frame available"}, status_code=503)

        # Lưu file
        import time
        os.makedirs(_SNAPSHOT_DIR, exist_ok=True)
        filename = f"{cam_id}_{int(time.time())}.jpg"
        filepath = os.path.join(_SNAPSHOT_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(data)

        return Response(
            content=bytes(data),
            media_type="image/jpeg",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ── Status APIs ───────────────────────────────────────────────────────────

    @app.get("/api/status")
    def get_api_status(request: Request):
        redir = _auth_redirect(request)
        if redir:
            return redir
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT timestamp, event_type, description FROM events ORDER BY id DESC LIMIT 5")
        logs = cursor.fetchall()
        conn.close()

        mqtt_manager.publish_heartbeat()
        person_count, truck_count, door_open = get_state_fn()

        return {
            "people": person_count,
            "trucks": truck_count,
            "door": door_open,
            "ocr_enabled": mqtt_manager.ocr_enabled,
            "ptz_mode": mqtt_manager.ptz_mode,
            "recent_logs": logs,
        }

    @app.get("/api/cameras/status")
    def cameras_status(request: Request):
        redir = _auth_redirect(request)
        if redir:
            return redir
        if camera_manager:
            return camera_manager.get_all_status()
        return [{"id": "main", "name": "Camera Chính", "online": True, "last_frame_age": None}]

    @app.post("/api/ptz/{command}")
    def ptz_control(command: str, request: Request):
        redir = _auth_redirect(request)
        if redir:
            return redir
        if command == "panorama":
            mqtt_manager.client.publish("shed/cmd/ptz_panorama", "1")
        elif command == "gate":
            mqtt_manager.client.publish("shed/cmd/ptz_gate", "1")
        return {"status": "sent"}

    # ── Settings ──────────────────────────────────────────────────────────────

    _PASSWORD_KEYS = set()

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, saved: str = ""):
        redir = _auth_redirect(request)
        if redir:
            return redir

        store = settings_store
        current = store.get_all() if store else {}

        flash = '<div class="flash">✅ Đã lưu cài đặt thành công.</div>' if saved == "1" else ""
        warning = ""
        if store and not store.available:
            warning = '<div class="warn">⚠ Không kết nối được PostgreSQL — hiển thị từ biến môi trường, không thể lưu.</div>'

        groups = [
            ("📸 OCR Camera Configuration", [
                ("OCR_SOURCE", "OCR Source", "select"),
            ]),
        ]

        fields_html = ""
        for group_name, fields in groups:
            fields_html += f'<h3 class="group-title">{group_name}</h3>\n'
            for key, label, ftype in fields:
                val = current.get(key, "")
                if ftype == "password":
                    ph = "••••••••" if val else "(chưa đặt)"
                    fields_html += f'<label>{label}<input type="password" name="{key}" placeholder="{ph}" autocomplete="new-password"></label>\n'
                elif ftype == "select":
                    sel_rtsp = 'selected' if val in ("", "rtsp") else ""
                    sel_web = 'selected' if val == "webcam" else ""
                    fields_html += f'<label>{label}<select name="{key}"><option value="rtsp" {sel_rtsp}>rtsp</option><option value="webcam" {sel_web}>webcam</option></select></label>\n'
                elif ftype == "number":
                    fields_html += f'<label>{label}<input type="number" step="0.01" min="0" max="1" name="{key}" value="{val}"></label>\n'
                else:
                    fields_html += f'<label>{label}<input type="text" name="{key}" value="{val}"></label>\n'

        html = _SETTINGS_HTML.replace("{{FLASH}}", flash).replace("{{WARNING}}", warning).replace("{{FIELDS}}", fields_html)
        return HTMLResponse(html)

    @app.post("/settings")
    async def settings_save(request: Request):
        redir = _auth_redirect(request)
        if redir:
            return redir

        form = await request.form()
        data = {k: v for k, v in form.items() if v}  # skip empty values

        if settings_store:
            settings_store.set_many(data)

        # Hot-reload UI credentials
        global _UI_USER, _UI_PASS
        if "CAMERA_UI_USER" in data:
            _UI_USER = data["CAMERA_UI_USER"]
        if "CAMERA_UI_PASS" in data:
            _UI_PASS = data["CAMERA_UI_PASS"]

        return RedirectResponse(url="/settings?saved=1", status_code=302)

    return app


def start_api_server(streamer, get_state_fn, mqtt_manager, camera_manager=None):
    """Khởi chạy API server trên port 8080."""
    create_api_server(streamer, get_state_fn, mqtt_manager, camera_manager)
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")


# ── HTML Templates ────────────────────────────────────────────────────────────

def _camera_cell_html(cam_id: str, name: str) -> str:
    return f"""
<div class="cell" id="cell-{cam_id}">
  <div class="cam-header">
    <span class="cam-name">{name}</span>
    <span class="badge offline" id="badge-{cam_id}">OFFLINE</span>
  </div>
  <div class="cam-wrap" id="wrap-{cam_id}">
    <img src="/video_feed/{cam_id}" id="img-{cam_id}" alt="{name}" onerror="scheduleReload('{cam_id}')">
    <div class="offline-overlay" id="overlay-{cam_id}">OFFLINE</div>
  </div>
  <div class="cam-actions">
    <button onclick="takeSnapshot('{cam_id}')">📷 Snapshot</button>
    <button onclick="goFullscreen('{cam_id}')">⛶ Fullscreen</button>
  </div>
</div>
"""


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Đăng nhập — Camera</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0d0d0d;color:#eee;font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}
  .box{background:#1a1a1a;padding:40px 36px;border-radius:12px;width:340px;box-shadow:0 8px 32px rgba(0,0,0,.6)}
  h2{color:#00e676;margin-bottom:24px;text-align:center}
  label{display:block;font-size:.85rem;color:#aaa;margin-bottom:4px}
  input{width:100%;padding:10px 12px;background:#262626;border:1px solid #333;border-radius:6px;color:#eee;font-size:1rem;margin-bottom:16px}
  input:focus{outline:none;border-color:#00e676}
  button{width:100%;padding:12px;background:#00e676;border:none;border-radius:6px;color:#000;font-weight:bold;font-size:1rem;cursor:pointer}
  button:hover{background:#00c853}
</style>
</head>
<body>
<div class="box">
  <h2>🎥 Bãi Giữ Xe</h2>
  <form method="post" action="/login">
    <label>Tên đăng nhập</label>
    <input name="username" type="text" autocomplete="username" required>
    <label>Mật khẩu</label>
    <input name="password" type="password" autocomplete="current-password" required>
    <button type="submit">Đăng nhập</button>
  </form>
  {{ERROR}}
</div>
</body>
</html>"""


_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Camera Dashboard — Bãi Giữ Xe</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0d0d0d;color:#eee;font-family:sans-serif}
  header{display:flex;align-items:center;justify-content:space-between;padding:12px 20px;background:#111;border-bottom:1px solid #222}
  header h1{color:#00e676;font-size:1.1rem}
  header a{color:#aaa;font-size:.85rem;text-decoration:none}
  header a:hover{color:#fff}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:12px;height:calc(100vh - 52px)}
  .cell{background:#1a1a1a;border-radius:8px;overflow:hidden;display:flex;flex-direction:column;border:1px solid #2a2a2a}
  .cell.empty{align-items:center;justify-content:center;border:1px dashed #333}
  .empty-label{color:#444;font-size:.9rem}
  .cam-header{display:flex;align-items:center;justify-content:space-between;padding:6px 10px;background:#111}
  .cam-name{font-size:.85rem;font-weight:600;color:#ccc}
  .badge{font-size:.7rem;padding:2px 8px;border-radius:10px;font-weight:700;letter-spacing:.5px}
  .badge.online{background:#00e676;color:#000}
  .badge.offline{background:#f44336;color:#fff}
  .cam-wrap{position:relative;flex:1;overflow:hidden;background:#000}
  .cam-wrap img{width:100%;height:100%;object-fit:contain;display:block}
  .offline-overlay{position:absolute;inset:0;background:rgba(0,0,0,.65);display:none;align-items:center;justify-content:center;font-size:1.4rem;font-weight:700;color:#f44;letter-spacing:2px}
  .offline-overlay.show{display:flex}
  .cam-actions{display:flex;gap:6px;padding:6px 10px;background:#111}
  .cam-actions button{flex:1;padding:5px 0;background:#262626;border:1px solid #333;border-radius:5px;color:#ccc;font-size:.8rem;cursor:pointer}
  .cam-actions button:hover{background:#333;color:#fff}
  @media(max-width:600px){.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<header>
  <h1>🎥 Bãi Giữ Xe — Camera Dashboard</h1>
  <div style="display:flex;gap:16px;align-items:center">
    <a href="/settings">⚙ Cài đặt</a>
    <a href="/logout">Đăng xuất</a>
  </div>
</header>
<div class="grid">
{{CELLS}}
</div>
<script>
async function pollStatus() {
  try {
    const res = await fetch('/api/cameras/status');
    const cams = await res.json();
    cams.forEach(cam => {
      const badge = document.getElementById('badge-' + cam.id);
      const overlay = document.getElementById('overlay-' + cam.id);
      if (!badge) return;
      if (cam.online) {
        badge.className = 'badge online';
        badge.textContent = 'ONLINE';
        if (overlay) overlay.classList.remove('show');
      } else {
        badge.className = 'badge offline';
        badge.textContent = 'OFFLINE';
        if (overlay) overlay.classList.add('show');
      }
    });
  } catch(e) {}
}

function scheduleReload(camId) {
  setTimeout(() => {
    const img = document.getElementById('img-' + camId);
    if (img) img.src = '/video_feed/' + camId + '?t=' + Date.now();
  }, 30000);
}

function takeSnapshot(camId) {
  const a = document.createElement('a');
  a.href = '/snapshot/' + camId;
  a.download = '';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

function goFullscreen(camId) {
  const wrap = document.getElementById('wrap-' + camId);
  if (wrap) wrap.requestFullscreen().catch(() => {});
}

pollStatus();
setInterval(pollStatus, 5000);
</script>
</body>
</html>"""


_SETTINGS_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Cài đặt — Camera</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0d0d0d;color:#eee;font-family:sans-serif}
  header{display:flex;align-items:center;justify-content:space-between;padding:12px 20px;background:#111;border-bottom:1px solid #222}
  header h1{color:#00e676;font-size:1.1rem}
  header a{color:#aaa;font-size:.85rem;text-decoration:none}
  header a:hover{color:#fff}
  .container{max-width:640px;margin:32px auto;padding:0 16px}
  .flash{background:#1b3a1b;border:1px solid #00e676;color:#00e676;padding:10px 14px;border-radius:6px;margin-bottom:16px}
  .warn{background:#3a2a00;border:1px solid #f90;color:#f90;padding:10px 14px;border-radius:6px;margin-bottom:16px}
  .group-title{color:#00e676;font-size:.9rem;margin:24px 0 10px;text-transform:uppercase;letter-spacing:.5px}
  label{display:flex;flex-direction:column;gap:4px;margin-bottom:12px;font-size:.85rem;color:#aaa}
  input,select{padding:9px 12px;background:#1e1e1e;border:1px solid #333;border-radius:6px;color:#eee;font-size:.95rem}
  input:focus,select:focus{outline:none;border-color:#00e676}
  .actions{margin-top:28px;display:flex;gap:10px}
  button[type=submit]{padding:11px 28px;background:#00e676;border:none;border-radius:6px;color:#000;font-weight:700;font-size:.95rem;cursor:pointer}
  button[type=submit]:hover{background:#00c853}
  .back{padding:11px 20px;background:#262626;border:1px solid #333;border-radius:6px;color:#ccc;font-size:.95rem;text-decoration:none;display:inline-flex;align-items:center}
  .back:hover{background:#333;color:#fff}
</style>
</head>
<body>
<header>
  <h1>⚙ Cài đặt hệ thống</h1>
  <div style="display:flex;gap:16px;align-items:center">
    <a href="/dashboard">📹 Dashboard</a>
    <a href="/logout">Đăng xuất</a>
  </div>
</header>
<div class="container">
  {{FLASH}}
  {{WARNING}}
  <form method="post" action="/settings">
    {{FIELDS}}
    <div class="actions">
      <button type="submit">💾 Lưu cài đặt</button>
      <a href="/dashboard" class="back">← Quay lại</a>
    </div>
  </form>
</div>
</body>
</html>"""
