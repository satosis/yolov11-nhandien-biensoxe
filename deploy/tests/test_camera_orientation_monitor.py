import cv2
import numpy as np

from core.camera_orientation_monitor import CameraOrientationMonitor


def _make_pattern() -> np.ndarray:
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    rng = np.random.default_rng(42)
    for _ in range(220):
        x = int(rng.integers(20, 620))
        y = int(rng.integers(20, 460))
        radius = int(rng.integers(2, 5))
        color = int(rng.integers(120, 255))
        cv2.circle(img, (x, y), radius, (color, color, color), -1)
    cv2.rectangle(img, (120, 130), (520, 360), (255, 255, 255), 2)
    return img


def _shifted(img: np.ndarray) -> np.ndarray:
    m = cv2.getRotationMatrix2D((320, 240), 7.0, 1.0)
    m[:, 2] += np.array([30.0, 10.0])
    return cv2.warpAffine(img, m, (640, 480))


def test_camera_orientation_monitor_detects_shift_and_stable():
    base = _make_pattern()
    moved = _shifted(base)

    monitor = CameraOrientationMonitor(
        check_every_n_frames=1,
        max_rotation_deg=3.5,
        max_translation_px=15,
        required_consecutive_alerts=2,
        min_keypoints=30,
    )
    assert monitor.set_baseline(base)

    stable_result = monitor.evaluate(base)
    assert stable_result is not None
    assert not stable_result.is_shifted

    moved_first = monitor.evaluate(moved)
    assert moved_first is not None
    assert not moved_first.is_shifted

    moved_second = monitor.evaluate(moved)
    assert moved_second is not None
    assert moved_second.is_shifted
