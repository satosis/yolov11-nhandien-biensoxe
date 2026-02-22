import sqlite3
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from core.config import DB_PATH

app = FastAPI()


def create_api_server(streamer, get_state_fn, mqtt_manager):
    """Tạo API server với dashboard và endpoints.
    
    Args:
        streamer: MJPEGStreamer instance
        get_state_fn: Hàm trả về (person_count, truck_count, door_open)
        mqtt_manager: MQTTManager instance
    """

    @app.get("/")
    def dashboard():
        html_content = """
        <html>
            <head>
                <title>Smart Door Monitoring Dashboard</title>
                <style>
                    body { font-family: sans-serif; background: #121212; color: white; text-align: center; }
                    .container { display: flex; flex-direction: column; align-items: center; margin-top: 20px; }
                    img { border: 5px solid #333; border-radius: 10px; max-width: 90%; }
                    .stats { margin-top: 20px; display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; width: 60%; }
                    .card { background: #1e1e1e; padding: 20px; border-radius: 10px; box-shadow: 0 4px 8px rgba(0,0,0,0.5); }
                    h1 { color: #00e676; }
                    .logs { width: 80%; background: #222; margin-top: 30px; text-align: left; padding: 20px; border-radius: 10px; }
                </style>
            </head>
            <body>
                <h1>🚪 Smart Door AI Dashboard</h1>
                <div class="container">
                    <img src="/video_feed" alt="Live View">
                    <div class="stats" id="stats-container">
                        <div class="card"><h3>Người</h3><p id="p-count">0</p></div>
                        <div class="card"><h3>Xe Tải</h3><p id="t-count">0</p></div>
                        <div class="card"><h3>Cửa</h3><p id="door-status">...</p></div>
                        <div class="card"><h3>OCR</h3><p id="ocr-status">...</p></div>
                        <div class="card"><h3>Camera</h3><p id="ptz-mode">...</p></div>
                    </div>
                    <div style="margin-top: 20px;">
                        <button onclick="ptzCmd('panorama')" style="padding: 10px 20px; cursor: pointer; background: #00bcd4; border: none; border-radius: 5px; color: white;">🔭 Xoay Toàn Cảnh</button>
                        <button onclick="ptzCmd('gate')" style="padding: 10px 20px; cursor: pointer; background: #ff9800; border: none; border-radius: 5px; color: white;">🏠 Về Cổng OCR</button>
                    </div>
                    <div class="logs">
                        <h3>Hoạt động gần đây:</h3>
                        <ul id="log-list"></ul>
                    </div>
                </div>
                <script>
                    async function update() {
                        const res = await fetch('/api/status');
                        const data = await res.json();
                        document.getElementById('p-count').innerText = data.people;
                        document.getElementById('t-count').innerText = data.trucks;
                        document.getElementById('door-status').innerText = data.door ? "🔓 MỞ" : "🔒 ĐÓNG";
                        document.getElementById('ocr-status').innerText = data.ocr_enabled ? "✅ BẬT" : "❌ TẮT";
                        document.getElementById('ptz-mode').innerText = data.ptz_mode.toUpperCase();
                        
                        const logList = document.getElementById('log-list');
                        logList.innerHTML = data.recent_logs.map(l => `<li>[${l[0]}] <b>${l[1]}</b>: ${l[2]}</li>`).join('');
                    }
                    
                    async function ptzCmd(cmd) {
                        await fetch(`/api/ptz/${cmd}`, { method: 'POST' });
                    }

                    setInterval(update, 2000);
                </script>
            </body>
        </html>
        """
        return HTMLResponse(content=html_content)

    @app.get("/api/status")
    def get_api_status():
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
            "recent_logs": logs
        }

    @app.post("/api/ptz/{command}")
    def ptz_control(command: str):
        if command == "panorama":
            mqtt_manager.client.publish("shed/cmd/ptz_panorama", "1")
        elif command == "gate":
            mqtt_manager.client.publish("shed/cmd/ptz_gate", "1")
        return {"status": "sent"}

    @app.get("/video_feed")
    def video_feed():
        from starlette.responses import StreamingResponse
        return StreamingResponse(
            streamer.generate(),
            media_type="multipart/x-mixed-replace; boundary=frame"
        )

    return app


def start_api_server(streamer, get_state_fn, mqtt_manager):
    """Khởi chạy API server trên port 8080."""
    create_api_server(streamer, get_state_fn, mqtt_manager)
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="warning")
