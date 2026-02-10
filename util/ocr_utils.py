"""
Vietnamese License Plate OCR using PaddleOCR
Tối ưu cho biển số xe tải 2 dòng màu vàng (VD: 88C 073.04)

Target: Ubuntu/Linux
"""

import os
import re
import logging
import cv2
import numpy as np
from paddleocr import PaddleOCR

# Tắt check model source để khởi động nhanh hơn
os.environ.setdefault('PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK', 'True')

logger = logging.getLogger("ocr_utils")


def normalize_plate(text: str) -> str:
    """Chuẩn hóa biển số xe - chỉ giữ chữ và số."""
    return re.sub(r'[^A-Z0-9]', '', text.upper())


class VNPlateOCR:
    """PaddleOCR wrapper cho biển số xe Việt Nam"""

    # Ngưỡng aspect ratio: biển 2 dòng có height/width > 0.6
    TWO_LINE_RATIO_THRESHOLD = 0.6
    
    # Ký tự hợp lệ cho biển số VN
    VALID_CHARS = set("ABCDEFGHKLMNPSTUVXYZ0123456789")

    def __init__(self):
        """Khởi tạo PaddleOCR cho biển số VN."""
        self.ocr = PaddleOCR(
            lang='en',
            use_textline_orientation=True,
        )
        logger.info("VNPlateOCR initialized")

    def is_two_line_plate(self, plate_img: np.ndarray) -> bool:
        """
        Phát hiện biển số 2 dòng dựa trên aspect ratio.
        Biển vàng xe tải (2 dòng) có ratio > 0.6
        """
        if plate_img is None or plate_img.size == 0:
            return False
        
        h, w = plate_img.shape[:2]
        if w == 0:
            return False
        
        return (h / w) > self.TWO_LINE_RATIO_THRESHOLD

    def preprocess(self, plate_img: np.ndarray) -> np.ndarray:
        """Tiền xử lý ảnh để tăng độ chính xác OCR."""
        if plate_img is None or plate_img.size == 0:
            return plate_img
        
        # Chuyển grayscale
        if len(plate_img.shape) == 3:
            gray = cv2.cvtColor(plate_img, cv2.COLOR_BGR2GRAY)
        else:
            gray = plate_img
        
        # Phóng to nếu ảnh quá nhỏ
        h, w = gray.shape[:2]
        if w < 100:
            scale = 100 / w
            gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        
        # Adaptive threshold
        thresh = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
        )
        
        # Khử nhiễu
        denoised = cv2.fastNlMeansDenoising(thresh, h=10)
        
        return denoised

    def segment_two_line(self, plate_img: np.ndarray) -> tuple:
        """Tách biển 2 dòng thành dòng trên và dòng dưới."""
        if plate_img is None or plate_img.size == 0:
            return None, None
        
        h, w = plate_img.shape[:2]
        mid = h // 2
        overlap = int(h * 0.05)  # 5% overlap
        
        top = plate_img[:mid + overlap, :]
        bottom = plate_img[mid - overlap:, :]
        
        return top, bottom

    def ocr_image(self, img: np.ndarray) -> tuple:
        """OCR một ảnh đơn. Trả về (text, prob)"""
        if img is None or img.size == 0:
            return "", 0.0
        
        # Chuyển sang BGR nếu là grayscale
        if len(img.shape) == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        
        # PaddleOCR dùng ocr() hoặc predict(). Trong script này dùng ocr([..., det=True, rec=True, cls=True])
        # PaddleOCR mặc định truyền list các boxes: [ [ [coords], (text, score) ], ... ]
        result = self.ocr.ocr(img, det=True, rec=True, cls=True)
        
        if not result or not result[0]:
            return "", 0.0
        
        texts = []
        scores = []
        for line in result[0]:
            texts.append(line[1][0])
            scores.append(line[1][1])
        
        avg_score = sum(scores) / len(scores) if scores else 0.0
        return "".join(texts), avg_score

    def normalize_result(self, text: str) -> str:
        """Chuẩn hóa kết quả OCR."""
        if not text:
            return ""
        
        text = text.upper()
        
        # Sửa lỗi OCR phổ biến
        corrections = {
            'O': '0', 'I': '1', 'L': '1', 'Z': '2',
            'S': '5', 'B': '8', 'G': '6', 'Q': '0',
            '.': '', '-': '', ' ': '',
        }
        
        result = []
        for char in text:
            if char in corrections:
                result.append(corrections[char])
            elif char in self.VALID_CHARS:
                result.append(char)
        
        return "".join(result)

    def read_plate_with_prob(self, plate_img: np.ndarray, preprocess: bool = True) -> tuple:
        """
        Đọc biển số xe kèm theo độ tin cậy.
        Trả về: (biển_số_chuẩn_hóa, độ_tin_cậy_trung_bình)
        """
        if plate_img is None or plate_img.size == 0:
            return "", 0.0
        
        if self.is_two_line_plate(plate_img):
            top, bottom = self.segment_two_line(plate_img)
            if preprocess:
                top = self.preprocess(top)
                bottom = self.preprocess(bottom)
            
            t1, s1 = self.ocr_image(top)
            t2, s2 = self.ocr_image(bottom)
            combined_text = self.normalize_result(t1 + t2)
            avg_score = (s1 + s2) / 2
        else:
            if preprocess:
                plate_img = self.preprocess(plate_img)
            t, s = self.ocr_image(plate_img)
            combined_text = self.normalize_result(t)
            avg_score = s
            
        return combined_text, avg_score

    def read_plate(self, plate_img: np.ndarray, preprocess: bool = True) -> str:
        """
        Đọc biển số xe với tự động phát hiện biển 2 dòng.
        
        Args:
            plate_img: Ảnh biển số (BGR)
            preprocess: Có tiền xử lý ảnh hay không
            
        Returns:
            Biển số đã chuẩn hóa (VD: "88C07304")
        """
        if plate_img is None or plate_img.size == 0:
            return ""
        
        # Kiểm tra biển 2 dòng
        if self.is_two_line_plate(plate_img):
            logger.debug("Detected 2-line plate")
            top, bottom = self.segment_two_line(plate_img)
            
            if preprocess:
                top = self.preprocess(top)
                bottom = self.preprocess(bottom)
            
            # OCR từng dòng
            top_text = self.ocr_image(top)
            bottom_text = self.ocr_image(bottom)
            combined = top_text + bottom_text
            logger.debug(f"2-line: '{top_text}' + '{bottom_text}' = '{combined}'")
        else:
            # Biển 1 dòng
            if preprocess:
                plate_img = self.preprocess(plate_img)
            combined = self.ocr_image(plate_img)
            logger.debug(f"1-line: '{combined}'")
        
        return self.normalize_result(combined)
