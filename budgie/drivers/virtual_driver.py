"""
drivers/virtual_driver.py — Virtual camera driver (6.6.0).

Generates static 8-colour SMPTE colour bars (no animation).
Fixed frame output eliminates per-frame rendering work.

scan_devices() exposes one additional pending camera each time all
previously-seen cameras have been opened at least once.

cam_ids  : virtual://0, virtual://1, …
max count: VIRTUAL_MAX  (class constant, reset on server restart)
"""

import threading
import time
from typing import Optional

import numpy as np

from .base import CameraDriver

# ── Module-level constants ─────────────────────────────────────────────────────
VIRTUAL_MAX  = 8
DEFAULT_W    = 640
DEFAULT_H    = 480
DEFAULT_FPS  = 30.0

# 8-colour SMPTE bars in BGR order
_SMPTE_BGR = [
    (235, 235, 235),   # white
    ( 16, 235, 235),   # yellow  (high-luma, low blue)
    (235, 235,  16),   # cyan
    ( 16, 235,  16),   # green
    (235,  16, 235),   # magenta
    ( 16,  16, 235),   # red
    (235,  16,  16),   # blue
    ( 16,  16,  16),   # black
]


def _make_entry(num: int) -> dict:
    return {
        "device_id": f"virtual://{num}",
        "model":     f"VirtualCam-{num}",
        "serial":    f"VIRT{num:04d}",
        "label":     f"VirtualCam-{num}",
        "driver":    "VirtualCameraDriver",
    }


def _build_bars(w: int, h: int) -> np.ndarray:
    """Build a static SMPTE colour-bar frame."""
    frame  = np.zeros((h, w, 3), dtype=np.uint8)
    bar_w  = w // 8
    for i, bgr in enumerate(_SMPTE_BGR):
        x0 = i * bar_w
        x1 = (i + 1) * bar_w if i < 7 else w
        frame[:, x0:x1] = bgr
    return frame


class VirtualCameraDriver(CameraDriver):
    """Virtual camera: static SMPTE colour bars, no animation."""

    SUPPORTED_PARAMS = frozenset({"fps"})
    DEFAULT_PARAMS   = {"fps": DEFAULT_FPS}

    # ── Class-level scan state (reset when the server restarts) ───────────────
    _cls_lock    = threading.Lock()
    _seen_count: int      = 0   # cameras that have appeared in scan results
    _opened_nums: set     = set()   # camera numbers ever opened this session

    # ── scan_devices ──────────────────────────────────────────────────────────

    @staticmethod
    def scan_devices() -> list:
        cls = VirtualCameraDriver
        with cls._cls_lock:
            sc = cls._seen_count
            on = cls._opened_nums
            entries = [_make_entry(i) for i in range(sc)]
            # Add one new pending entry when ALL seen cameras have been opened
            all_seen_opened = sc == 0 or all(i in on for i in range(sc))
            if all_seen_opened and sc < VIRTUAL_MAX:
                entries.append(_make_entry(sc))
                cls._seen_count = sc + 1
        return entries

    # ── Instance lifecycle ────────────────────────────────────────────────────

    def __init__(self):
        super().__init__()
        self._num      = -1
        self._fps      = DEFAULT_FPS
        self._lock     = threading.Lock()
        self._running  = False
        self._latest: Optional[np.ndarray] = None
        self._cap_fps  = 0.0
        self._bars: Optional[np.ndarray] = None

    def open(self, device_id=None) -> dict:
        num = int(device_id.split("://")[1])
        self._num = num

        # Apply init_params (fps only for virtual cameras)
        fps = float(getattr(self, "_init_params", {}).get("fps", DEFAULT_FPS))
        self._fps = max(1.0, fps)

        with VirtualCameraDriver._cls_lock:
            VirtualCameraDriver._opened_nums.add(num)

        self._bars = _build_bars(DEFAULT_W, DEFAULT_H)

        return {
            "model":    f"VirtualCam-{num}",
            "serial":   f"VIRT{num:04d}",
            "width":    DEFAULT_W,
            "height":   DEFAULT_H,
            "exp_min":  100,
            "exp_max":  1_000_000,
            "gain_min": 0.0,
            "gain_max": 24.0,
            "fps_min":  1.0,
            "fps_max":  120.0,
        }

    def close(self):
        self._running = False

    def stop(self):
        self._running = False

    def read_hw_bounds(self) -> dict:
        return {
            "exp_min":  100, "exp_max":  1_000_000,
            "gain_min": 0.0, "gain_max": 24.0,
            "fps_min":  1.0, "fps_max":  120.0,
        }

    def set_param(self, key: str, value):
        if key == "fps":
            with self._lock:
                self._fps = max(1.0, float(value))

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def latest_frame(self):
        return self._latest

    @property
    def cap_fps(self) -> float:
        return self._cap_fps

    @property
    def current_gain(self) -> float:
        return 0.0

    @property
    def current_exposure(self) -> float:
        return 0.0

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Acquisition loop ──────────────────────────────────────────────────────

    def run(self):
        self._running = True
        t_last      = time.perf_counter()
        fps_t0      = t_last
        frame_count = 0

        while self._running:
            with self._lock:
                fps = self._fps
            interval = 1.0 / fps

            t_now   = time.perf_counter()
            elapsed = t_now - t_last
            if elapsed < interval:
                time.sleep(interval - elapsed)
                t_now = time.perf_counter()
            t_last = t_now

            # Static colour bars — no per-frame rendering
            frame = self._bars.copy()
            self._latest = frame

            frame_count += 1
            dt = t_now - fps_t0
            if dt >= 1.0:
                self._cap_fps = round(frame_count / dt, 1)
                frame_count   = 0
                fps_t0        = t_now

            ts_ns = int(t_now * 1_000_000_000)
            if self.on_frame:
                self.on_frame(frame, ts_ns)

        self._running = False
