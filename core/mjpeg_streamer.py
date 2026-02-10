import cv2
import threading
import time
import queue

class MJPEGStreamer:
    def __init__(self):
        self.frame = None
        self.lock = threading.Lock()
        self.stop_event = threading.Event()

    def update_frame(self, frame):
        """Cập nhật frame mới nhất từ Main Loop"""
        if frame is None:
            return
        
        # Resize nếu cần để giảm băng thông (VD: 720p)
        # frame = cv2.resize(frame, (1280, 720))
        
        with self.lock:
            self.frame = frame.copy()

    def generate(self):
        """Generator trả về chuỗi byte MJPEG cho client"""
        while not self.stop_event.is_set():
            with self.lock:
                if self.frame is None:
                    time.sleep(0.1)
                    continue
                
                # Encode JPEG
                (flag, encodedImage) = cv2.imencode(".jpg", self.frame)
                if not flag:
                    continue
                
            # Yield frame data
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + bytearray(encodedImage) + b'\r\n')
            
            # Giới hạn FPS gửi đi (VD: 10 FPS)
            time.sleep(0.1)

    def get_snapshot(self):
        """Trả về ảnh tĩnh (bytes)"""
        with self.lock:
            if self.frame is None:
                return None
            (flag, encodedImage) = cv2.imencode(".jpg", self.frame)
            if not flag:
                return None
            return bytearray(encodedImage)
