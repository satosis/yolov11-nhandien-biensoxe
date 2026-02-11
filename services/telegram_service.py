import os
import time
import shutil
import requests
import threading

from core.config import TOKEN, CHAT_IMPORTANT, CHAT_REGULAR, FACES_DIR, normalize_plate


def notify_telegram(message, important=False):
    """Gửi thông báo qua Telegram."""
    chat_id = CHAT_IMPORTANT if important else CHAT_REGULAR
    prefix = "🚨 [QUAN TRỌNG] " if important else "ℹ️ [THÔNG BÁO] "
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": prefix + message})
    except Exception as e:
        print(f"Lỗi gửi Telegram: {e}")


def handle_telegram_command(text, chat_id, user_id, db, load_faces_fn, mqtt_manager):
    """Xử lý lệnh từ Telegram."""
    parts = text.strip().split()
    if not parts:
        return

    cmd = parts[0].lower()

    # Lệnh mở/đóng cửa
    if cmd == "/open":
        print(f"Telegram CMD: OPEN from {user_id}")
        mqtt_manager.publish_trigger_open()
        notify_telegram(f"Đã gửi lệnh MỞ cửa theo yêu cầu của {user_id}")
        return

    # Lệnh duyệt biển số
    if cmd in ["/staff", "/reject", "/mine"]:
        if len(parts) < 2:
            notify_telegram(f"Lỗi: Thiếu biển số. VD: {cmd} 29A12345")
            return

        plate_raw = parts[1]
        plate_norm = normalize_plate(plate_raw)

        if cmd == "/mine":
            if db.upsert_vehicle_whitelist(plate_norm, "mine", str(user_id)):
                db.update_pending_status(plate_norm, "approved_mine", str(user_id))
                notify_telegram(f"✅ Đã thêm {plate_norm} vào danh sách CỦA TÔI.")
            else:
                notify_telegram(f"⚠️ Lỗi khi thêm {plate_norm}.")
        elif cmd == "/staff":
            if db.upsert_vehicle_whitelist(plate_norm, "staff", str(user_id)):
                db.update_pending_status(plate_norm, "approved_staff", str(user_id))
                notify_telegram(f"✅ Đã thêm {plate_norm} vào danh sách NHÂN VIÊN.")
            else:
                notify_telegram(f"⚠️ Lỗi khi thêm {plate_norm}.")

        elif cmd == "/reject":
            db.update_pending_status(plate_norm, "rejected", str(user_id))
            notify_telegram(f"🚫 Đã từ chối biển số {plate_norm}.")

    # Lệnh xem các biển số đang chờ duyệt
    if cmd == "/pending":
        pending_plates = db.get_pending_plates()
        if pending_plates:
            msg = "Các biển số đang chờ duyệt:\n"
            for plate_norm, plate_raw, first_seen_utc in pending_plates:
                msg += f"- `{plate_norm}` (raw: {plate_raw}, từ: {first_seen_utc})\n"
            notify_telegram(msg)
        else:
            notify_telegram("Không có biển số nào đang chờ duyệt.")

    # Lệnh duyệt khuôn mặt
    if cmd == "/staff_face":
        if len(parts) < 3:
            notify_telegram("Lỗi cú pháp: /staff_face [ID_TAM] [TEN_NHAN_VIEN]")
            return

        face_id = parts[1]
        staff_name = parts[2].replace(" ", "_")

        temp_path = f"./config/faces/temp/{face_id}.jpg"
        target_path = f"./config/faces/{staff_name}.jpg"

        if os.path.exists(temp_path):
            try:
                os.rename(temp_path, target_path)
                notify_telegram(f"✅ Đã thêm nhân viên: {staff_name}")
                load_faces_fn()
            except Exception as e:
                notify_telegram(f"⚠️ Lỗi khi lưu ảnh: {e}")
        else:
            notify_telegram(f"⚠️ Không tìm thấy ảnh tạm: {face_id}")

    # Lệnh dọn dẹp
    if cmd == "/cleanup":
        if len(parts) < 2:
            notify_telegram("Lỗi cú pháp: /cleanup [faces|active_learning|db]")
            return

        target = parts[1].lower()
        if target == "faces":
            try:
                if os.path.exists(FACES_DIR):
                    shutil.rmtree(FACES_DIR)
                    os.makedirs(FACES_DIR)
                    notify_telegram("✅ Đã dọn dẹp thư mục khuôn mặt.")
                    load_faces_fn()
                else:
                    notify_telegram("Thư mục khuôn mặt không tồn tại.")
            except Exception as e:
                notify_telegram(f"⚠️ Lỗi khi dọn dẹp khuôn mặt: {e}")
        elif target == "active_learning":
            try:
                al_dir = "./data/active_learning"
                if os.path.exists(al_dir):
                    shutil.rmtree(al_dir)
                    os.makedirs(al_dir)
                    notify_telegram("✅ Đã dọn dẹp thư mục active learning.")
                else:
                    notify_telegram("Thư mục active learning không tồn tại.")
            except Exception as e:
                notify_telegram(f"⚠️ Lỗi khi dọn dẹp active learning: {e}")
        elif target == "db":
            try:
                db_path = db.path
                if os.path.exists(db_path):
                    os.remove(db_path)
                    db.init_db()
                    notify_telegram("✅ Đã dọn dẹp cơ sở dữ liệu.")
                else:
                    notify_telegram("Tệp cơ sở dữ liệu không tồn tại.")
            except Exception as e:
                notify_telegram(f"⚠️ Lỗi khi dọn dẹp cơ sở dữ liệu: {e}")
        else:
            notify_telegram("⚠️ Mục tiêu dọn dẹp không hợp lệ. Chọn: faces, active_learning, db.")


def telegram_polling_loop(db, load_faces_fn, mqtt_manager):
    """Vòng lặp nhận tin nhắn từ Telegram."""
    if not TOKEN:
        return

    last_update_id = 0
    print("🤖 Telegram Bot listening...")

    while True:
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
            params = {"offset": last_update_id + 1, "timeout": 30}
            resp = requests.get(url, params=params, timeout=40)

            if resp.status_code == 200:
                data = resp.json()
                if data.get("ok"):
                    for update in data.get("result", []):
                        last_update_id = update["update_id"]

                        if "message" in update and "text" in update["message"]:
                            msg = update["message"]
                            text = msg["text"]
                            chat_id_msg = msg["chat"]["id"]
                            user_id = msg["from"]["id"]

                            if str(chat_id_msg) in [CHAT_IMPORTANT, CHAT_REGULAR]:
                                handle_telegram_command(text, chat_id_msg, user_id, db, load_faces_fn, mqtt_manager)

            time.sleep(1)
        except Exception as e:
            print(f"Telegram polling error: {e}")
            time.sleep(5)


def telegram_bot_handler(db, get_cpu_temp_fn, get_state_fn):
    """Handler Telegram bot với lệnh /stats, /sys, /cleanup, /mine, /staff, /reject."""
    last_update_id = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=10"
            r = requests.get(url, timeout=15).json()
            if r.get("ok"):
                for update in r["result"]:
                    last_update_id = update["update_id"]
                    msg = update.get("message", {})
                    text = msg.get("text", "")
                    chat_id = msg.get("chat", {}).get("id")
                    user = msg.get("from", {})
                    user_label = user.get("username") or str(user.get("id") or "unknown")

                    if not text or not text.startswith("/"):
                        continue

                    parts = text.strip().split(maxsplit=1)
                    cmd = parts[0].split("@")[0].lower()
                    plate_raw = parts[1] if len(parts) > 1 else ""
                    plate_norm = normalize_plate(plate_raw)

                    truck_count, person_count = get_state_fn()

                    if cmd == "/stats":
                        rows = db.get_stats()
                        stat_text = "📊 Thống kê hôm nay:\n"
                        for row in rows:
                            stat_text += f"- {row[1]}: {row[0]} lần\n"
                        stat_text += f"\nHiện tại: {truck_count} xe, {person_count} người."

                        temp = get_cpu_temp_fn()
                        temp_str = f"{temp:.1f}°C" if temp else "N/A"
                        import psutil
                        disk = psutil.disk_usage('/')
                        stat_text += f"\n\n🖥 Hệ thống:\n- Temp: {temp_str}\n- Disk: {disk.percent}%"

                        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                                      json={"chat_id": chat_id, "text": stat_text})
                        continue

                    if cmd == "/sys":
                        temp = get_cpu_temp_fn()
                        temp_str = f"{temp:.1f}°C" if temp else "N/A"
                        import psutil
                        notify_telegram(f"🖥 Hệ thống: {temp_str} | Disk: {psutil.disk_usage('/').percent}%")
                        continue

                    if cmd == "/cleanup":
                        try:
                            al_dir = "./data/active_learning"
                            if os.path.exists(al_dir):
                                shutil.rmtree(al_dir)
                                os.makedirs(al_dir)
                            notify_telegram("✅ Đã dọn dẹp bộ nhớ đệm (Active Learning).")
                        except Exception as e:
                            notify_telegram(f"⚠️ Lỗi: {e}")
                        continue

                    if cmd in {"/mine", "/staff", "/reject"} and not plate_norm:
                        requests.post(
                            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                            json={"chat_id": chat_id, "text": "Thiếu biển số. Ví dụ: /mine 51A12345"}
                        )
                        continue

                    if cmd == "/mine":
                        if db.upsert_vehicle_whitelist(plate_norm, "mine", user_label):
                            db.update_pending_status(plate_norm, "approved_mine", user_label)
                            reply = f"✅ Đã thêm {plate_norm} vào whitelist (mine)."
                        else:
                            reply = f"⚠️ Không thể cập nhật whitelist cho {plate_norm}."
                        requests.post(
                            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                            json={"chat_id": chat_id, "text": reply}
                        )
                    elif cmd == "/staff":
                        if db.upsert_vehicle_whitelist(plate_norm, "staff", user_label):
                            db.update_pending_status(plate_norm, "approved_staff", user_label)
                            reply = f"✅ Đã thêm {plate_norm} vào whitelist (staff)."
                        else:
                            reply = f"⚠️ Không thể cập nhật whitelist cho {plate_norm}."
                        requests.post(
                            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                            json={"chat_id": chat_id, "text": reply}
                        )
                    elif cmd == "/reject":
                        db.update_pending_status(plate_norm, "rejected", user_label)
                        requests.post(
                            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                            json={"chat_id": chat_id, "text": f"✅ Đã từ chối {plate_norm}."}
                        )
        except:
            pass
        time.sleep(2)


def start_telegram_threads(db, load_faces_fn, mqtt_manager, get_cpu_temp_fn, get_state_fn):
    """Khởi chạy tất cả telegram threads."""
    threading.Thread(target=telegram_polling_loop, args=(db, load_faces_fn, mqtt_manager), daemon=True).start()
    threading.Thread(target=telegram_bot_handler, args=(db, get_cpu_temp_fn, get_state_fn), daemon=True).start()
