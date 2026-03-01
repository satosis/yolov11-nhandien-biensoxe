"""
core/tripwire.py – Tripwire IN/OUT tracker

Cách dùng:
    tracker = TripwireTracker(line_y_fn=lambda: resolve_line_y(frame.shape[0]))
    direction = tracker.update(obj_id, center_y)  # "IN", "OUT", hoặc None
    tracker.cleanup_stale(active_obj_ids)          # gọi cuối mỗi frame

Logic:
    - Mỗi object (obj_id) có một buffer N frames tích lũy vị trí (trên/dưới vạch).
    - Khi tất cả N frame cuối nhất nhất quán sang phía khác → fire event.
    - Cooldown per-object tránh fire lặp khi object đứng trên vạch.
    - Stale cleanup xóa tracking của object đã biến mất.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Callable


class TripwireTracker:
    """
    Theo dõi chiều di chuyển của object qua một vạch ngang.

    Args:
        line_y_fn:      Callable trả về tọa độ y của vạch (pixel) tại runtime.
        buffer_frames:  Số frame liên tiếp cùng phía để xác nhận hướng (giảm noise).
        cooldown_secs:  Thời gian (giây) chờ sau khi fire trước khi fire lại cùng object.
        stale_secs:     Sau bao nhiêu giây không thấy object thì xóa trạng thái.
    """

    def __init__(
        self,
        line_y_fn: Callable[[], int],
        buffer_frames: int = 3,
        cooldown_secs: float = 3.0,
        stale_secs: float = 30.0,
    ) -> None:
        self._line_y_fn = line_y_fn
        self._buffer = max(1, buffer_frames)
        self._cooldown = cooldown_secs
        self._stale = stale_secs

        # obj_id -> deque of bool: True = bên dưới vạch (inward side), False = bên trên
        self._positions: dict[int, deque[bool]] = {}
        # obj_id -> thời điểm fire lần cuối
        self._last_fire: dict[int, float] = {}
        # obj_id -> side đã confirmed lần cuối (để phát hiện thay đổi)
        self._confirmed_side: dict[int, bool | None] = {}
        # obj_id -> last seen time
        self._last_seen: dict[int, float] = {}

    # ------------------------------------------------------------------
    def update(self, obj_id: int, center_y: int) -> str | None:
        """
        Cập nhật vị trí object và trả về hướng nếu vừa vượt qua vạch.

        Returns:
            "IN"  – object đi từ trên (ngoài) xuống dưới (vào kho)
            "OUT" – object đi từ dưới (trong) lên trên (ra ngoài)
            None  – chưa xác định / đang ở giữa
        """
        now = time.monotonic()
        line_y = self._line_y_fn()
        below = center_y >= line_y  # True = bên trong (phía dưới)

        # Khởi tạo buffer nếu là object mới
        if obj_id not in self._positions:
            self._positions[obj_id] = deque(maxlen=self._buffer)
            self._confirmed_side[obj_id] = None

        buf = self._positions[obj_id]
        buf.append(below)
        self._last_seen[obj_id] = now

        # Chưa đủ frame thì chờ
        if len(buf) < self._buffer:
            return None

        # Kiểm tra N frame cuối có nhất quán không
        all_below = all(buf)
        all_above = not any(buf)
        consistent = all_below or all_above
        if not consistent:
            return None

        new_side = all_below  # True = dưới, False = trên

        # Nếu phía không đổi so với lần confirmed trước → không fire
        prev_side = self._confirmed_side[obj_id]
        if prev_side is None:
            # Lần đầu xác định phía, ghi nhận nhưng không fire
            self._confirmed_side[obj_id] = new_side
            return None

        if new_side == prev_side:
            return None

        # Phía đã thay đổi → cập nhật confirmed_side ngay (không phụ thuộc cooldown)
        # để round-trip IN→OUT→IN được track đúng
        self._confirmed_side[obj_id] = new_side

        # Check cooldown trước khi fire event
        last = self._last_fire.get(obj_id, 0.0)
        if now - last < self._cooldown:
            return None

        # Fire!
        self._last_fire[obj_id] = now
        return "IN" if new_side else "OUT"

    # ------------------------------------------------------------------
    def cleanup_stale(self, active_ids: set[int] | None = None) -> None:
        """
        Xóa tracking của các object đã biến mất quá `stale_secs`.
        Truyền None để chỉ dựa vào timeout (không cần biết active_ids).
        """
        now = time.monotonic()
        stale_ids = [
            oid
            for oid, ts in self._last_seen.items()
            if now - ts > self._stale or (active_ids is not None and oid not in active_ids)
        ]
        for oid in stale_ids:
            self._positions.pop(oid, None)
            self._confirmed_side.pop(oid, None)
            self._last_fire.pop(oid, None)
            self._last_seen.pop(oid, None)

    # ------------------------------------------------------------------
    def active_count(self) -> int:
        """Số lượng object đang được tracking."""
        return len(self._positions)
