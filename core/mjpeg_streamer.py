import cv2
import threading
import time
import queue

class MJPEGStreamer:
    def __init__(self, stream_width=960, fps=8, jpeg_quality=68):
        self.frame = None
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.stream_width = max(0, int(stream_width))
        self.frame_interval = 1.0 / max(1, int(fps))
        self.jpeg_quality = max(35, min(90, int(jpeg_quality)))

    def update_frame(self, frame):
        """Cập nhật frame mới nhất từ Main Loop"""
        if frame is None:
            return
        
        frame_to_store = frame
        if self.stream_width > 0 and frame.shape[1] > self.stream_width:
            ratio = self.stream_width / float(frame.shape[1])
            new_h = max(1, int(frame.shape[0] * ratio))
            frame_to_store = cv2.resize(frame, (self.stream_width, new_h), interpolation=cv2.INTER_AREA)

        with self.lock:
            self.frame = frame_to_store.copy()

    def generate(self):
        """Generator trả về chuỗi byte MJPEG cho client"""
        while not self.stop_event.is_set():
            with self.lock:
                if self.frame is None:
                    time.sleep(0.1)
                    continue
                
                # Encode JPEG
                (flag, encodedImage) = cv2.imencode(".jpg", self.frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
                if not flag:
                    continue
                
            # Yield frame data
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + bytearray(encodedImage) + b'\r\n')
            
            # Giới hạn FPS gửi đi
            time.sleep(self.frame_interval)

    def get_snapshot(self):
        """Trả về ảnh tĩnh (bytes)"""
        with self.lock:
            if self.frame is None:
                return None
            (flag, encodedImage) = cv2.imencode(".jpg", self.frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality])
            if not flag:
                return None
            return bytearray(encodedImage)
