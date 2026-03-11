"""
parking_hpc/ui_server.py
Process 3 — Flask-SocketIO Dashboard

Responsibilities:
  - Consume InferenceResult objects from result_queue
  - JPEG-encode annotated frames and push via SocketIO (low-latency streaming)
  - Serve event log, snapshot gallery, and live stats
  - Bind to 0.0.0.0 so it's reachable via Tailscale IP

Accessible at: http://<tailscale-ip>:5050
"""
import os
import time
import signal
import logging
import threading
import base64
from multiprocessing import Queue, Event
from collections import deque
from typing import Optional

import cv2
import numpy as np
from flask import Flask, render_template_string, jsonify
from flask_socketio import SocketIO

from parking_hpc import config as cfg
from parking_hpc.inference import InferenceResult

logger = logging.getLogger("ui_server")

# ── Flask app ─────────────────────────────────────────────────────────────────
flask_app = Flask(__name__)
flask_app.config["SECRET_KEY"] = os.urandom(24)
socketio = SocketIO(flask_app, cors_allowed_origins="*", async_mode="threading")

# Shared state (written by background thread, read by Flask routes)
_latest_frames: dict[str, Optional[np.ndarray]] = {"cam1": None, "cam2": None}
_event_log: deque = deque(maxlen=50)
_stats: dict = {"plates_today": 0, "faces_today": 0, "snapshots": []}
_lock = threading.Lock()


# ── Background consumer thread ────────────────────────────────────────────────

def _consume_results(result_queue: Queue, stop_event: Event):
    """Drain result_queue, update shared state, push frames via SocketIO."""
    frame_interval = 1.0 / cfg.UI_STREAM_FPS
    t_last_push: dict[str, float] = {}

    while not stop_event.is_set():
        try:
            result: InferenceResult = result_queue.get(timeout=0.3)
        except Exception:
            continue

        cam_id = result.cam_id

        with _lock:
            if result.annotated_frame is not None:
                _latest_frames[cam_id] = result.annotated_frame.copy()

            if result.plate_text:
                _stats["plates_today"] += 1
                entry = {
                    "ts": time.strftime("%H:%M:%S", time.localtime(result.ts)),
                    "cam": cam_id,
                    "type": "PLATE",
                    "value": f"{result.plate_text} ({result.plate_conf:.2f})",
                    "snapshot": result.snapshot_path,
                }
                _event_log.appendleft(entry)
                if result.snapshot_path:
                    _stats["snapshots"].insert(0, result.snapshot_path)
                    _stats["snapshots"] = _stats["snapshots"][:20]

            if result.face_name and result.face_name != "STRANGER":
                _stats["faces_today"] += 1
                _event_log.appendleft({
                    "ts": time.strftime("%H:%M:%S", time.localtime(result.ts)),
                    "cam": cam_id,
                    "type": "FACE",
                    "value": f"{result.face_name} ({result.face_conf:.2f})",
                    "snapshot": "",
                })

        # Throttle frame push per camera
        now = time.monotonic()
        if now - t_last_push.get(cam_id, 0) >= frame_interval:
            t_last_push[cam_id] = now
            frame = _latest_frames.get(cam_id)
            if frame is not None:
                _push_frame(cam_id, frame)


def _push_frame(cam_id: str, frame: np.ndarray):
    """JPEG-encode frame and emit via SocketIO."""
    ok, buf = cv2.imencode(
        ".jpg", frame,
        [cv2.IMWRITE_JPEG_QUALITY, cfg.UI_JPEG_QUALITY],
    )
    if not ok:
        return
    b64 = base64.b64encode(buf.tobytes()).decode("ascii")
    socketio.emit("frame", {"cam": cam_id, "data": b64})


# ── Flask routes ──────────────────────────────────────────────────────────────

@flask_app.route("/")
def index():
    return render_template_string(_DASHBOARD_HTML)


@flask_app.route("/api/events")
def api_events():
    with _lock:
        return jsonify(list(_event_log))


@flask_app.route("/api/stats")
def api_stats():
    with _lock:
        return jsonify({
            "plates_today": _stats["plates_today"],
            "faces_today": _stats["faces_today"],
            "snapshot_count": len(_stats["snapshots"]),
        })


@flask_app.route("/api/snapshot/latest/<cam_id>")
def latest_snapshot(cam_id: str):
    """Return base64 JPEG of the latest frame for a camera."""
    frame = _latest_frames.get(cam_id)
    if frame is None:
        return jsonify({"error": "no frame"}), 404
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
    if not ok:
        return jsonify({"error": "encode failed"}), 500
    return jsonify({"data": base64.b64encode(buf.tobytes()).decode("ascii")})


# ── Process entry point ───────────────────────────────────────────────────────

def ui_process(result_queue: Queue, stop_event: Event):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

    # Start consumer thread inside this process
    consumer = threading.Thread(
        target=_consume_results, args=(result_queue, stop_event), daemon=True
    )
    consumer.start()

    logger.info("UI server starting on %s:%d", cfg.UI_HOST, cfg.UI_PORT)
    socketio.run(flask_app, host=cfg.UI_HOST, port=cfg.UI_PORT, use_reloader=False, log_output=False)


# ── Dashboard HTML ────────────────────────────────────────────────────────────

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Parking Monitor — HPC</title>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0d0d0d;color:#eee;font-family:monospace}
  header{padding:10px 18px;background:#111;border-bottom:1px solid #222;display:flex;align-items:center;gap:16px}
  header h1{color:#00e676;font-size:1rem}
  .stat{font-size:.8rem;color:#aaa}
  .stat span{color:#fff;font-weight:700}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;padding:10px}
  .cam-box{background:#111;border-radius:6px;overflow:hidden;border:1px solid #222}
  .cam-box img{width:100%;display:block;background:#000;min-height:200px}
  .cam-label{padding:4px 8px;font-size:.75rem;color:#888}
  .log-panel{padding:10px 18px}
  .log-panel h3{color:#00e676;font-size:.85rem;margin-bottom:6px}
  table{width:100%;border-collapse:collapse;font-size:.78rem}
  th{text-align:left;color:#555;padding:3px 6px;border-bottom:1px solid #222}
  td{padding:3px 6px;border-bottom:1px solid #1a1a1a}
  .badge-plate{color:#ff9800}
  .badge-face{color:#00bcd4}
</style>
</head>
<body>
<header>
  <h1>🅿 Parking Monitor HPC</h1>
  <div class="stat">Biển số hôm nay: <span id="s-plates">0</span></div>
  <div class="stat">Khuôn mặt: <span id="s-faces">0</span></div>
  <div class="stat">Snapshots: <span id="s-snaps">0</span></div>
</header>
<div class="grid">
  <div class="cam-box">
    <img id="img-cam1" alt="Camera 1">
    <div class="cam-label">Camera 1</div>
  </div>
  <div class="cam-box">
    <img id="img-cam2" alt="Camera 2">
    <div class="cam-label">Camera 2</div>
  </div>
</div>
<div class="log-panel">
  <h3>Sự kiện gần đây</h3>
  <table>
    <thead><tr><th>Giờ</th><th>Cam</th><th>Loại</th><th>Giá trị</th></tr></thead>
    <tbody id="log-body"></tbody>
  </table>
</div>
<script>
const socket = io();
socket.on('frame', ({cam, data}) => {
  const img = document.getElementById('img-' + cam);
  if (img) img.src = 'data:image/jpeg;base64,' + data;
});

async function refreshStats() {
  const [ev, st] = await Promise.all([
    fetch('/api/events').then(r=>r.json()),
    fetch('/api/stats').then(r=>r.json()),
  ]);
  document.getElementById('s-plates').textContent = st.plates_today;
  document.getElementById('s-faces').textContent = st.faces_today;
  document.getElementById('s-snaps').textContent = st.snapshot_count;
  const tbody = document.getElementById('log-body');
  tbody.innerHTML = ev.slice(0,20).map(e => `
    <tr>
      <td>${e.ts}</td><td>${e.cam}</td>
      <td class="${e.type==='PLATE'?'badge-plate':'badge-face'}">${e.type}</td>
      <td>${e.value}</td>
    </tr>`).join('');
}
setInterval(refreshStats, 3000);
refreshStats();
</script>
</body>
</html>"""
