"""
Vietnamese License Plate OCR using PaddleOCR
Optimized for 2-line yellow truck plates (e.g., 88C 073.04)

Target: Ubuntu/Linux environment
"""

import os
import re
import logging
import cv2
import numpy as np

# Disable model source check for faster startup
os.environ.setdefault('PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK', 'True')

try:
    from paddleocr import PaddleOCR
    PADDLEOCR_AVAILABLE = True
except ImportError:
    PADDLEOCR_AVAILABLE = False
    print("⚠️ PaddleOCR chưa cài đặt. Chạy: pip install paddlepaddle paddleocr")

logger = logging.getLogger("ocr_utils")


class VNPlateOCR:
    """PaddleOCR wrapper optimized for Vietnamese license plates"""

    # Aspect ratio threshold: 2-line plates have height/width > 0.6
    TWO_LINE_RATIO_THRESHOLD = 0.6
    
    # Valid Vietnamese plate characters
    VALID_CHARS = set("ABCDEFGHKLMNPSTUVXYZ0123456789")

    def __init__(self, use_gpu: bool = False):
        """
        Initialize PaddleOCR for Vietnamese license plates.
        
        Args:
            use_gpu: Whether to use GPU acceleration
        """
        if not PADDLEOCR_AVAILABLE:
            raise RuntimeError("PaddleOCR is not installed. Run: pip install paddlepaddle paddleocr")
        
        # PaddleOCR 3.x API for Ubuntu/Linux
        self.ocr = PaddleOCR(
            lang='en',  # English works well for alphanumeric plates
            use_textline_orientation=True,  # Handle rotated text
        )
        logger.info("VNPlateOCR initialized successfully")

    def is_two_line_plate(self, plate_img: np.ndarray) -> bool:
        """
        Detect if a license plate is 2-line based on aspect ratio.
        
        Yellow truck plates (2-line) have height/width ratio > 0.6
        White/regular plates (1-line) have ratio < 0.5
        """
        if plate_img is None or plate_img.size == 0:
            return False
        
        h, w = plate_img.shape[:2]
        if w == 0:
            return False
        
        ratio = h / w
        return ratio > self.TWO_LINE_RATIO_THRESHOLD

    def preprocess_plate(self, plate_img: np.ndarray) -> np.ndarray:
        """
        Preprocess plate image for better OCR accuracy.
        
        Steps:
        1. Convert to grayscale
        2. Apply adaptive threshold
        3. Denoise
        """
        if plate_img is None or plate_img.size == 0:
            return plate_img
        
        # Convert to grayscale
        if len(plate_img.shape) == 3:
            gray = cv2.cvtColor(plate_img, cv2.COLOR_BGR2GRAY)
        else:
            gray = plate_img
        
        # Resize if too small (OCR works better with larger images)
        h, w = gray.shape[:2]
        if w < 100:
            scale = 100 / w
            gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        
        # Apply adaptive threshold for better contrast
        thresh = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
        )
        
        # Denoise
        denoised = cv2.fastNlMeansDenoising(thresh, h=10)
        
        return denoised

    def segment_two_line(self, plate_img: np.ndarray) -> tuple:
        """
        Split a 2-line license plate into top and bottom line images.
        
        Uses 5% overlap to ensure no characters are cut off.
        """
        if plate_img is None or plate_img.size == 0:
            return None, None
        
        h, w = plate_img.shape[:2]
        mid = h // 2
        overlap = int(h * 0.05)  # 5% overlap
        
        top_line = plate_img[:mid + overlap, :]
        bottom_line = plate_img[mid - overlap:, :]
        
        return top_line, bottom_line

    def ocr_single_image(self, img: np.ndarray) -> str:
        """
        Perform OCR on a single image using PaddleOCR.
        """
        if img is None or img.size == 0:
            return ""
        
        try:
            # PaddleOCR expects BGR or RGB image
            if len(img.shape) == 2:
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
            
            # PaddleOCR 3.x uses predict() method
            results = list(self.ocr.predict(img))
            
            if not results:
                return ""
            
            # Parse PaddleOCR 3.x results format
            texts = []
            for result in results:
                # Check for rec_texts attribute (PaddleOCR 3.x format)
                if hasattr(result, 'rec_texts') and result.rec_texts:
                    texts.extend(result.rec_texts)
                elif isinstance(result, dict):
                    if 'rec_texts' in result and result['rec_texts']:
                        texts.extend(result['rec_texts'])
                    elif 'text' in result:
                        texts.append(result['text'])
            
            return "".join(texts)
        except Exception as e:
            logger.warning(f"OCR failed: {e}")
            return ""

    def normalize_plate(self, text: str) -> str:
        """
        Normalize Vietnamese license plate text.
        
        - Remove invalid characters
        - Convert common OCR mistakes (O->0, I->1, etc.)
        - Format: Province code (2 chars) + Letter + Numbers
        """
        if not text:
            return ""
        
        # Uppercase and remove spaces/special chars
        text = text.upper()
        
        # Common OCR corrections
        corrections = {
            'O': '0',  # Letter O to zero (in number positions)
            'I': '1',
            'L': '1',
            'Z': '2',
            'S': '5',
            'B': '8',
            'G': '6',
            'Q': '0',
            '.': '',
            '-': '',
            ' ': '',
        }
        
        result = []
        for char in text:
            if char in corrections:
                result.append(corrections[char])
            elif char in self.VALID_CHARS:
                result.append(char)
        
        normalized = "".join(result)
        return normalized

    def read_plate(self, plate_img: np.ndarray, preprocess: bool = True) -> str:
        """
        Main OCR function with automatic 2-line detection.
        
        Args:
            plate_img: Cropped license plate image (BGR)
            preprocess: Whether to apply preprocessing
            
        Returns:
            Recognized and normalized plate number
        """
        if plate_img is None or plate_img.size == 0:
            return ""
        
        try:
            # Check if 2-line plate
            if self.is_two_line_plate(plate_img):
                logger.debug("Detected 2-line plate, segmenting...")
                top, bottom = self.segment_two_line(plate_img)
                
                # Preprocess each line
                if preprocess:
                    top = self.preprocess_plate(top)
                    bottom = self.preprocess_plate(bottom)
                
                # OCR each line separately
                top_text = self.ocr_single_image(top)
                bottom_text = self.ocr_single_image(bottom)
                
                # Combine results
                combined = top_text + bottom_text
                logger.debug(f"2-line OCR: '{top_text}' + '{bottom_text}' = '{combined}'")
            else:
                # Single line plate
                if preprocess:
                    plate_img = self.preprocess_plate(plate_img)
                combined = self.ocr_single_image(plate_img)
                logger.debug(f"1-line OCR: '{combined}'")
            
            # Normalize the result
            normalized = self.normalize_plate(combined)
            return normalized
            
        except Exception as e:
            logger.error(f"read_plate failed: {e}")
            return ""


# Backward compatibility: simple function interface
_ocr_instance = None

def read_vietnamese_plate(plate_img: np.ndarray) -> str:
    """
    Simple function to read Vietnamese license plates.
    
    Args:
        plate_img: Cropped license plate image (BGR)
        
    Returns:
        Normalized plate number
    """
    global _ocr_instance
    if _ocr_instance is None:
        _ocr_instance = VNPlateOCR()
    return _ocr_instance.read_plate(plate_img)
