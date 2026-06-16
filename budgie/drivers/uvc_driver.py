"""
uvc_driver.py — UVC (USB Video Class) / built-in camera driver via OpenCV.

Requires:  opencv-python  (already a core dependency)

Works on Linux, macOS, and Windows.  On Linux /dev/video* devices are
enumerated via sysfs; on other platforms indices 0-9 are probed.

Native mode query
-----------------
On Linux, available resolutions and frame rates are queried via V4L2 ioctls
(VIDIOC_ENUM_FRAMESIZES / VIDIOC_ENUM_FRAMEINTERVALS) — no extra dependencies.
On non-Linux platforms, only the current resolution is returned.

Open deadlock protection
------------------------
cv2.VideoCapture.open() and cap.set() can deadlock on some V4L2 devices when
an unsupported resolution is requested.  A watchdog thread with _OPEN_TIMEOUT_S
timeout aborts the open and raises TimeoutError, preventing the process from
hanging indefinitely.

Exposure note
-------------
V4L2 (Linux) reports exposure via CAP_PROP_EXPOSURE in 100 µs units.
This driver converts to/from µs:
  set: cap.set(CAP_PROP_EXPOSURE, value_us / 100)
  get: cap.get(CAP_PROP_EXPOSURE) * 100

On Windows / macOS the units may differ by camera.
"""

import glob
import os
import platform
import struct
import threading
import time
from collections import deque
from typing import Optional

import cv2
import numpy as np

from .base import CameraDriver
from ..config import FPS_SAMPLE_FRAMES

# Watchdog timeout for camera open+configure (seconds).
# Protects against V4L2 driver deadlock when applying an unsupported resolution.
_OPEN_TIMEOUT_S = 10

# Consecutive cap.read() failures before the camera is declared disconnected.
_READ_FAIL_LIMIT = 30

# V4L2 ioctl constants (x86-64 Linux, little-endian)
# Computed as _IOWR('V', nr, sizeof(struct)):
#   (3 << 30) | (sizeof << 16) | (ord('V') << 8) | nr
_VIDIOC_ENUM_FRAMESIZES     = 0xC02C564A  # sizeof=44, nr=74
_VIDIOC_ENUM_FRAMEINTERVALS = 0xC034564B  # sizeof=52, nr=75
_V4L2_PIX_FMT_MJPEG = 0x47504A4D        # 'MJPG'
_V4L2_PIX_FMT_YUYV  = 0x56595559        # 'YUYV'
_V4L2_FRMSIZE_TYPE_DISCRETE  = 1
_V4L2_FRMIVAL_TYPE_DISCRETE  = 1


class UVCDriver(CameraDriver):
    """
    UVC camera driver using cv2.VideoCapture.

    Supports standard USB webcams, built-in laptop cameras, and any
    V4L2-compatible video device on Linux.

    Supported parameters: exposure, fps, exposure_auto.
    Gain is not universally available on UVC devices.
    """

    SUPPORTED_PARAMS: frozenset = frozenset({
        "exposure", "fps", "exposure_auto",
    })

    DEFAULT_PARAMS: dict = {
        "exposure":      10000.0,
        "exposure_auto": False,
        "fps":           30.0,
    }

    # V4L2 rarely exposes exact bounds; use safe protocol-level fallbacks
    _FALLBACK_BOUNDS: dict = {
        "exp_min":  100.0,  "exp_max":  200_000.0,
        "gain_min": 0.0,    "gain_max": 0.0,   # gain not supported on UVC
        "fps_min":  1.0,    "fps_max":  120.0,
    }

    def __init__(self):
        super().__init__()
        self._cap: Optional[cv2.VideoCapture] = None
        self._lock = threading.Lock()
        self._pending: dict = {}

        self._init_params: dict = dict(self.DEFAULT_PARAMS)
        self._dev_idx: int = 0   # stored in open() for query_native_modes()

        self._exposure_auto    = False
        self._current_exposure = self.DEFAULT_PARAMS["exposure"]
        self._current_gain     = 0.0
        self._running          = False

        self._fps_buf: deque      = deque()
        self._cap_fps_val: float  = 0.0
        self._latest: Optional[np.ndarray] = None

    # ── Device discovery ───────────────────────────────────────────────────────

    @staticmethod
    def scan_devices() -> list:
        """Enumerate available UVC / V4L2 video devices."""
        if platform.system() == "Linux":
            return UVCDriver._scan_linux()
        return UVCDriver._scan_indices()

    @staticmethod
    def _scan_linux() -> list:
        result = []
        for path in sorted(glob.glob("/dev/video*")):
            try:
                idx = int(path.replace("/dev/video", ""))
            except ValueError:
                continue
            name_path = f"/sys/class/video4linux/video{idx}/name"
            try:
                with open(name_path) as f:
                    model = f.read().strip()
            except Exception:
                model = f"UVC Camera {idx}"
            try:
                cap = cv2.VideoCapture(idx, cv2.CAP_V4L2)
                if not cap.isOpened():
                    cap.release()
                    continue
                w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
                cap.release()
                if w <= 0:
                    continue
            except Exception:
                continue
            result.append({
                "device_id": str(idx),
                "model":     model,
                "serial":    f"/dev/video{idx}",
                "label":     f"{model} (/dev/video{idx})",
            })
        return result

    @staticmethod
    def _scan_indices() -> list:
        result = []
        for idx in range(10):
            try:
                cap = cv2.VideoCapture(idx)
                if not cap.isOpened():
                    cap.release()
                    continue
                ret, _ = cap.read()
                cap.release()
                if not ret:
                    continue
                result.append({
                    "device_id": str(idx),
                    "model":     f"UVC Camera {idx}",
                    "serial":    str(idx),
                    "label":     f"UVC Camera {idx}",
                })
            except Exception:
                continue
        return result

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def open(self, device_id: Optional[str] = None) -> dict:
        idx = int(device_id) if device_id is not None else 0
        self._dev_idx = idx
        backend = cv2.CAP_V4L2 if platform.system() == "Linux" else cv2.CAP_ANY

        # All capture setup runs inside a watchdog thread to prevent V4L2 deadlock.
        result: list = [None]
        error:  list = [None]
        init = self._init_params

        def _do_open():
            try:
                cap = cv2.VideoCapture(idx, backend)
                if not cap.isOpened():
                    error[0] = RuntimeError(f"Cannot open UVC device index {idx}")
                    return
                # Apply resolution if a native mode was requested
                if "width" in init and "height" in init:
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  float(init["width"]))
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, float(init["height"]))
                cap.set(cv2.CAP_PROP_FPS, float(init.get("fps", 30.0)))
                # Apply exposure mode
                if init.get("exposure_auto"):
                    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 3)   # aperture-priority
                else:
                    cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)   # manual
                    cap.set(cv2.CAP_PROP_EXPOSURE, init["exposure"] / 100.0)
                result[0] = cap
            except Exception as e:
                error[0] = e

        t = threading.Thread(target=_do_open, daemon=True)
        t.start()
        t.join(timeout=_OPEN_TIMEOUT_S)

        if t.is_alive():
            raise TimeoutError(
                f"Camera open timed out after {_OPEN_TIMEOUT_S}s — "
                "V4L2 driver may be deadlocked. "
                "Try unplugging and reconnecting the camera."
            )
        if error[0] is not None:
            raise error[0]

        cap = result[0]
        self._cap = cap
        self._exposure_auto = bool(init.get("exposure_auto", False))

        width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        exp_raw = cap.get(cv2.CAP_PROP_EXPOSURE)
        self._current_exposure = exp_raw * 100.0 if exp_raw > 0 else init["exposure"]

        model = self._get_model_name(idx)
        return {
            "model":  model,
            "serial": f"/dev/video{idx}" if platform.system() == "Linux" else str(idx),
            "width":  width,
            "height": height,
            **self.read_hw_bounds(),
        }

    def read_hw_bounds(self) -> dict:
        """Return exposure/fps bounds; falls back to protocol-level constants."""
        bounds = dict(self._FALLBACK_BOUNDS)
        if self._cap is not None:
            try:
                fps = self._cap.get(cv2.CAP_PROP_FPS)
                if fps > 0:
                    bounds["fps_max"] = float(fps)
            except Exception:
                pass
        return bounds

    def close(self):
        self._running = False

    # ── Native mode query ──────────────────────────────────────────────────────

    def query_native_modes(self) -> list:
        """
        Query available resolutions and frame rates.

        On Linux, uses V4L2 VIDIOC_ENUM_FRAMESIZES / VIDIOC_ENUM_FRAMEINTERVALS.
        On non-Linux platforms, returns only the current resolution.
        """
        if platform.system() == "Linux":
            try:
                modes = self._query_v4l2_modes(self._dev_idx)
                if modes:
                    return modes
            except Exception:
                pass
        # Fallback: report only the current resolution
        if self._cap is not None:
            w = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            fps = self._cap.get(cv2.CAP_PROP_FPS) or 30.0
            if w > 0 and h > 0:
                return [{"width": w, "height": h, "fps": round(fps, 3)}]
        return []

    def _query_v4l2_modes(self, dev_idx: int) -> list:
        """Enumerate modes via V4L2 ioctls (Linux only)."""
        import fcntl

        dev_path = f"/dev/video{dev_idx}"
        modes: list = []
        seen: set   = set()

        with open(dev_path, "rb") as f:
            fd = f.fileno()
            for fmt in [_V4L2_PIX_FMT_MJPEG, _V4L2_PIX_FMT_YUYV]:
                s_idx = 0
                while True:
                    # v4l2_frmsizeenum: index(I) pixel_format(I) type(I) union(24s) reserved(II) = 44 bytes
                    buf = bytearray(44)
                    struct.pack_into("<III", buf, 0, s_idx, fmt, 0)
                    try:
                        fcntl.ioctl(fd, _VIDIOC_ENUM_FRAMESIZES, buf)
                    except OSError:
                        break  # EINVAL → no more entries

                    fs_type = struct.unpack_from("<I", buf, 8)[0]
                    if fs_type == _V4L2_FRMSIZE_TYPE_DISCRETE:
                        w = struct.unpack_from("<I", buf, 12)[0]
                        h = struct.unpack_from("<I", buf, 16)[0]
                        self._enum_frame_intervals(fd, fmt, w, h, modes, seen)
                    s_idx += 1

        # Sort: largest area first, then highest fps
        modes.sort(key=lambda m: (-m["width"] * m["height"], -m["fps"]))
        return modes

    def _enum_frame_intervals(self, fd, fmt: int, w: int, h: int,
                               modes: list, seen: set):
        """Enumerate discrete frame intervals for one frame size."""
        import fcntl
        fi_idx = 0
        while True:
            # v4l2_frmivalenum: index(I) pixel_format(I) width(I) height(I) type(I) union(24s) reserved(II) = 52 bytes
            buf = bytearray(52)
            struct.pack_into("<IIIII", buf, 0, fi_idx, fmt, w, h, 0)
            try:
                fcntl.ioctl(fd, _VIDIOC_ENUM_FRAMEINTERVALS, buf)
            except OSError:
                break

            fi_type = struct.unpack_from("<I", buf, 16)[0]
            if fi_type == _V4L2_FRMIVAL_TYPE_DISCRETE:
                # union at offset 20: numerator(I) + denominator(I) = interval as fraction
                num = struct.unpack_from("<I", buf, 20)[0]
                den = struct.unpack_from("<I", buf, 24)[0]
                if num > 0:
                    fps = round(den / num, 3)
                    key = (w, h, fps)
                    if key not in seen:
                        seen.add(key)
                        modes.append({"width": w, "height": h, "fps": fps})
            fi_idx += 1

    # ── Acquisition loop ───────────────────────────────────────────────────────

    def run(self):
        self._running = True
        _fail_count = 0
        try:
            while self._running:
                self._apply_pending()
                ret, frame = self._cap.read()
                if not ret:
                    _fail_count += 1
                    if _fail_count >= _READ_FAIL_LIMIT:
                        self._running = False
                        if self.on_disconnect is not None:
                            self.on_disconnect()
                        break
                    time.sleep(0.005)
                    continue
                _fail_count = 0
                hw_ts_ns = time.time_ns()
                exp_raw = self._cap.get(cv2.CAP_PROP_EXPOSURE)
                if exp_raw > 0:
                    self._current_exposure = exp_raw * 100.0
                self._latest = frame
                if self.on_frame is not None:
                    self.on_frame(frame, hw_ts_ns)
                self._update_cap_fps()
        finally:
            if self._cap:
                self._cap.release()

    def stop(self):
        self._running = False

    # ── Parameter control ──────────────────────────────────────────────────────

    def set_param(self, key: str, value):
        with self._lock:
            self._pending[key] = value

    def _apply_pending(self):
        with self._lock:
            pending, self._pending = self._pending.copy(), {}
        for key, value in pending.items():
            try:
                if key == "exposure_auto":
                    if value:
                        self._exposure_auto = True
                        self._cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 3)
                    else:
                        self._exposure_auto = False
                        self._cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)
                elif key == "exposure" and not self._exposure_auto:
                    self._cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)
                    self._cap.set(cv2.CAP_PROP_EXPOSURE, float(value) / 100.0)
                elif key == "fps":
                    self._cap.set(cv2.CAP_PROP_FPS, float(value))
            except Exception as e:
                print(f"[UVCDriver] set {key}={value} failed: {e}", flush=True)

    # ── Properties ─────────────────────────────────────────────────────────────

    @property
    def latest_frame(self) -> Optional[np.ndarray]:
        f = self._latest
        return f.copy() if f is not None else None

    @property
    def cap_fps(self) -> float:
        return self._cap_fps_val

    @property
    def current_gain(self) -> float:
        return self._current_gain

    @property
    def current_exposure(self) -> float:
        return self._current_exposure

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Internal helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _get_model_name(idx: int) -> str:
        if platform.system() == "Linux":
            name_path = f"/sys/class/video4linux/video{idx}/name"
            try:
                with open(name_path) as f:
                    return f.read().strip()
            except Exception:
                pass
        return f"UVC Camera {idx}"

    def _update_cap_fps(self):
        now = time.time()
        self._fps_buf.append(now)
        if len(self._fps_buf) > FPS_SAMPLE_FRAMES:
            self._fps_buf.popleft()
        n = len(self._fps_buf)
        if n >= 2:
            elapsed = self._fps_buf[-1] - self._fps_buf[0]
            self._cap_fps_val = (n - 1) / elapsed if elapsed > 0 else 0.0
