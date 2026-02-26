from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np


@dataclass
class CameraShiftResult:
    is_shifted: bool
    rotation_deg: float = 0.0
    translation_px: float = 0.0
    scale_delta: float = 0.0
    inlier_ratio: float = 0.0
    reason: str = ""


class CameraOrientationMonitor:
    """Giám sát camera có lệch khỏi góc gốc ban đầu hay không.

    - Lưu một baseline frame khi hệ thống ổn định.
    - So khớp ORB + RANSAC giữa frame hiện tại và baseline.
    - Cảnh báo khi vượt ngưỡng liên tiếp N frame để chống false-positive.
    """

    def __init__(
        self,
        check_every_n_frames: int = 8,
        min_inlier_ratio: float = 0.18,
        max_rotation_deg: float = 3.5,
        max_translation_px: float = 18.0,
        max_scale_delta: float = 0.08,
        required_consecutive_alerts: int = 3,
        min_keypoints: int = 80,
    ) -> None:
        self.check_every_n_frames = max(1, check_every_n_frames)
        self.min_inlier_ratio = min_inlier_ratio
        self.max_rotation_deg = max_rotation_deg
        self.max_translation_px = max_translation_px
        self.max_scale_delta = max_scale_delta
        self.required_consecutive_alerts = max(1, required_consecutive_alerts)
        self.min_keypoints = min_keypoints

        self._baseline_gray: Optional[np.ndarray] = None
        self._frame_counter = 0
        self._consecutive_alerts = 0

        self._orb = cv2.ORB_create(nfeatures=600)
        self._matcher = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)

    def set_baseline(self, frame_bgr: np.ndarray) -> bool:
        gray = self._preprocess(frame_bgr)
        keypoints, _ = self._orb.detectAndCompute(gray, None)
        if keypoints is None or len(keypoints) < self.min_keypoints:
            return False
        self._baseline_gray = gray
        self._consecutive_alerts = 0
        return True

    def evaluate(self, frame_bgr: np.ndarray) -> Optional[CameraShiftResult]:
        self._frame_counter += 1
        if self._frame_counter % self.check_every_n_frames != 0:
            return None
        if self._baseline_gray is None:
            return None

        gray = self._preprocess(frame_bgr)
        kp1, des1 = self._orb.detectAndCompute(self._baseline_gray, None)
        kp2, des2 = self._orb.detectAndCompute(gray, None)

        if (
            kp1 is None
            or kp2 is None
            or des1 is None
            or des2 is None
            or len(kp1) < self.min_keypoints
            or len(kp2) < self.min_keypoints
        ):
            return self._update_alarm(
                CameraShiftResult(is_shifted=False, reason="insufficient_keypoints")
            )

        knn_matches = self._matcher.knnMatch(des1, des2, k=2)
        good = []
        for pair in knn_matches:
            if len(pair) < 2:
                continue
            m, n = pair
            if m.distance < 0.75 * n.distance:
                good.append(m)

        if len(good) < 12:
            return self._update_alarm(
                CameraShiftResult(is_shifted=False, reason="insufficient_matches")
            )

        src_pts = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        dst_pts = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

        affine, inliers = cv2.estimateAffinePartial2D(
            src_pts,
            dst_pts,
            method=cv2.RANSAC,
            ransacReprojThreshold=3.0,
            maxIters=2000,
            confidence=0.99,
        )
        if affine is None or inliers is None:
            return self._update_alarm(
                CameraShiftResult(is_shifted=False, reason="affine_failed")
            )

        inlier_ratio = float(inliers.sum() / max(1, len(inliers)))
        a, b, tx = affine[0]
        c, d, ty = affine[1]
        rotation_deg = float(np.degrees(np.arctan2(c, a)))
        scale_x = float(np.sqrt(a * a + c * c))
        scale_y = float(np.sqrt(b * b + d * d))
        scale_delta = float(max(abs(scale_x - 1.0), abs(scale_y - 1.0)))
        translation_px = float(np.sqrt(tx * tx + ty * ty))

        shifted_now = (
            inlier_ratio < self.min_inlier_ratio
            or abs(rotation_deg) > self.max_rotation_deg
            or translation_px > self.max_translation_px
            or scale_delta > self.max_scale_delta
        )
        return self._update_alarm(
            CameraShiftResult(
                is_shifted=shifted_now,
                rotation_deg=rotation_deg,
                translation_px=translation_px,
                scale_delta=scale_delta,
                inlier_ratio=inlier_ratio,
            )
        )

    def _update_alarm(self, result: CameraShiftResult) -> CameraShiftResult:
        if result.is_shifted:
            self._consecutive_alerts += 1
        else:
            self._consecutive_alerts = 0

        result.is_shifted = self._consecutive_alerts >= self.required_consecutive_alerts
        return result

    @staticmethod
    def _preprocess(frame_bgr: np.ndarray) -> np.ndarray:
        resized = cv2.resize(frame_bgr, (640, int(640 * frame_bgr.shape[0] / frame_bgr.shape[1])))
        gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY)
        return cv2.GaussianBlur(gray, (3, 3), 0)
