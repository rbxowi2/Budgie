"""
aravis_driver.py — USB3 Vision / GigE camera driver via Aravis (GObject introspection).

Requires:  gir1.2-aravis-0.8  (system package)
           python3-gi           (system package)

This driver wraps Aravis.Camera to implement the CameraDriver interface.
It is the primary driver for U3V (USB3 Vision) industrial cameras.
"""

import time
from collections import deque
from typing import Optional

import numpy as np

try:
    import gi
    gi.require_version("Aravis", "0.8")
    from gi.repository import Aravis
    ARAVIS_AVAILABLE = True
except Exception:
    ARAVIS_AVAILABLE = False

from .base import CameraDriver
from ..config import BUF_TIMEOUT_US, FPS_SAMPLE_FRAMES, STREAM_PREBUF

# Consecutive buffer timeouts (each = BUF_TIMEOUT_US µs) before declaring disconnect.
_TIMEOUT_FAIL_LIMIT = 10

try:
    import cv2
except ImportError as e:
    raise ImportError("opencv-python is required: pip install opencv-python") from e


class AravisDriver(CameraDriver):
    """USB3 Vision / GigE camera driver backed by the Aravis library."""

    DEFAULT_PARAMS: dict = {
        "exposure":       1000000.0,
        "gain":           15.0,
        "gain_auto":      False,
        "exposure_auto":  True,
        "exp_auto_upper": 1000000.0,
        "fps":            30.0,
    }

    # Internal fallbacks used when hardware query fails
    _FALLBACK_BOUNDS: dict = {
        "exp_min":  100.0,   "exp_max":  200_000.0,
        "gain_min": 0.0,     "gain_max": 24.0,
        "fps_min":  1.0,     "fps_max":  1000.0,
    }

    def __init__(self):
        super().__init__()
        self._cam   = None
        self._lock  = __import__("threading").Lock()
        self._pending: dict = {}

        self._init_params: dict = dict(self.DEFAULT_PARAMS)

        self._gain_auto        = False
        self._exposure_auto    = False
        self._exp_auto_upper   = 100000.0
        self._current_gain     = self.DEFAULT_PARAMS["gain"]
        self._current_exposure = self.DEFAULT_PARAMS["exposure"]
        self._running          = False

        self._fps_buf: deque = deque()
        self._cap_fps_val: float = 0.0

        self._latest: Optional[np.ndarray] = None

    # ── Device discovery ───────────────────────────────────────────────────────

    @staticmethod
    def scan_devices() -> list:
        """Scan for USB3 Vision / GigE cameras via Aravis."""
        if not ARAVIS_AVAILABLE:
            return []
        try:
            Aravis.update_device_list()
            n = Aravis.get_n_devices()
            result = []
            for i in range(n):
                try:
                    device_id = Aravis.get_device_id(i)
                    try:
                        model = Aravis.get_device_model(i)
                    except Exception:
                        model = "Unknown"
                    try:
                        serial = Aravis.get_device_serial_nbr(i)
                    except Exception:
                        serial = device_id
                    result.append({
                        "device_id": device_id,
                        "model":     model,
                        "serial":    serial,
                        "label":     f"{model} — {serial}",
                    })
                except Exception:
                    pass
            return result
        except Exception:
            return []

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def open(self, device_id: Optional[str] = None) -> dict:
        if not ARAVIS_AVAILABLE:
            raise RuntimeError(
                "Aravis is not available. Install: gir1.2-aravis-0.8 python3-gi"
            )
        Aravis.update_device_list()
        if Aravis.get_n_devices() == 0:
            raise RuntimeError(
                "No camera found. Please check:\n"
                "  1. USB cable plugged into USB 3.0 port\n"
                "  2. udev rules configured\n"
                "  3. User in plugdev group (re-login required)"
            )
        self._cam = Aravis.Camera.new(device_id)
        init = self._init_params

        if init.get("exposure_auto"):
            self._exposure_auto  = True
            self._exp_auto_upper = float(init.get("exp_auto_upper", 100000.0))
            self._cam_set("ExposureAuto", "Continuous", "exposure mode")
            self._cam_set("AutoExposureTimeUpperLimit", int(self._exp_auto_upper), "auto exposure upper limit")
        else:
            self._cam_set("ExposureAuto", "Off", "exposure mode")
            self._cam_set("ExposureTime", init["exposure"], "exposure time")

        gain_auto = init.get("gain_auto", False)
        self._gain_auto = gain_auto
        self._cam_set("GainAuto", "Continuous" if gain_auto else "Off", "gain mode")
        if not gain_auto:
            self._cam_set("Gain", init["gain"], "gain")

        self._cam_set_fps(init["fps"])
        self._current_gain = init["gain"]

        return {
            "model":  self._cam.get_model_name(),
            "serial": self._cam.get_device_serial_number(),
            "width":  self._cam.get_integer("Width"),
            "height": self._cam.get_integer("Height"),
            **self.read_hw_bounds(),
        }

    def close(self):
        self._running = False

    # ── Thread acquisition loop ────────────────────────────────────────────────

    def run(self):
        self._running = True
        _timeout_count = 0
        stream  = self._cam.create_stream(None, None)
        payload = self._cam.get_payload()
        for _ in range(STREAM_PREBUF):
            stream.push_buffer(Aravis.Buffer.new_allocate(payload))

        self._cam.set_acquisition_mode(Aravis.AcquisitionMode.CONTINUOUS)
        self._cam.start_acquisition()

        # Re-apply init params post-stream-start (some cameras need this)
        init = self._init_params
        if init.get("exposure_auto"):
            self._exposure_auto  = True
            self._exp_auto_upper = float(init.get("exp_auto_upper", 100000.0))
            self._cam_set("ExposureAuto", "Continuous", "exposure mode")
            self._cam_set("AutoExposureTimeUpperLimit", int(self._exp_auto_upper), "auto exposure upper limit")
        else:
            self._cam_set("ExposureAuto", "Off", "exposure mode")
            self._cam_set("ExposureTime", init["exposure"], "exposure time")
        gain_auto = init.get("gain_auto", False)
        self._gain_auto = gain_auto
        self._cam_set("GainAuto", "Continuous" if gain_auto else "Off", "gain mode")
        if not gain_auto:
            self._cam_set("Gain", init["gain"], "gain")
        self._cam_set_fps(init["fps"])

        _disconnected = False
        try:
            while self._running:
                self._apply_pending()
                try:
                    buf = stream.timeout_pop_buffer(BUF_TIMEOUT_US)
                except Exception:
                    # GError or fatal Aravis exception → treat as disconnect
                    _disconnected = True
                    self._running = False
                    break
                if buf is None:
                    _timeout_count += 1
                    if _timeout_count >= _TIMEOUT_FAIL_LIMIT:
                        _disconnected = True
                        self._running = False
                        break
                    continue
                _timeout_count = 0
                hw_ts_ns = buf.get_timestamp()
                frame    = self._decode_frame(buf)
                stream.push_buffer(buf)

                self._latest = frame
                if self.on_frame is not None:
                    self.on_frame(frame, hw_ts_ns)

                self._update_cap_fps()

                if self._gain_auto:
                    try:
                        self._current_gain = self._cam.get_float("Gain")
                    except Exception:
                        pass
                if self._exposure_auto:
                    try:
                        self._current_exposure = self._cam.get_float("ExposureTime")
                    except Exception:
                        pass
        finally:
            try:
                self._cam.stop_acquisition()
            except Exception:
                pass
            # Explicitly drop the Aravis stream and camera GObjects so libusb
            # releases the USB control interface before this thread exits.
            # Without this, GC timing leaves LIBUSB_ERROR_BUSY for the next open.
            try:
                del stream
            except Exception:
                pass
            self._cam = None
        if _disconnected and self.on_disconnect is not None:
            self.on_disconnect()

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
                        self._cam.set_string("ExposureAuto", "Continuous")
                        self._cam.set_integer("AutoExposureTimeUpperLimit",
                                              int(self._exp_auto_upper))
                    else:
                        self._exposure_auto = False
                        self._cam.set_string("ExposureAuto", "Off")
                elif key == "exp_auto_upper":
                    self._exp_auto_upper = float(value)
                    self._cam.set_integer("AutoExposureTimeUpperLimit", int(value))
                elif key == "exposure":
                    self._cam.set_string("ExposureAuto", "Off")
                    self._cam.set_float("ExposureTime", float(value))
                elif key == "gain":
                    self._cam.set_string("GainAuto", "Off")
                    self._cam.set_float("Gain", float(value))
                elif key == "gain_auto":
                    mode = "Continuous" if value else "Off"
                    self._cam.set_string("GainAuto", mode)
                    self._gain_auto = bool(value)
                elif key == "fps":
                    self._cam_set_fps(float(value))
            except Exception as e:
                print(f"[AravisDriver] set {key}={value} failed: {e}", flush=True)

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

    def _cam_set(self, feature: str, value, label: str):
        try:
            if isinstance(value, str):
                self._cam.set_string(feature, value)
            elif isinstance(value, float):
                self._cam.set_float(feature, value)
            else:
                self._cam.set_integer(feature, int(value))
        except Exception as e:
            print(f"[AravisDriver] set {label} failed: {e}", flush=True)

    def _cam_set_fps(self, fps: float):
        # Some cameras require AcquisitionFrameRateEnable=true before the
        # frame rate register becomes writable.  Try silently; not all cameras
        # expose this feature.
        try:
            self._cam.set_boolean("AcquisitionFrameRateEnable", True)
        except Exception:
            pass
        self._cam_set("AcquisitionFrameRate", fps, "frame rate")

    def query_native_modes(self) -> list:
        """
        Return available fps values at the camera's current sensor resolution.

        Aravis industrial cameras have a fixed sensor size; resolution changes
        require ROI configuration which is out of scope here.  We enumerate
        discrete fps steps within the hardware-reported frame rate bounds.
        """
        if self._cam is None:
            return []
        try:
            w = self._cam.get_integer("Width")
            h = self._cam.get_integer("Height")
            try:
                fps_min, fps_max = self._cam.get_float_bounds("AcquisitionFrameRate")
                candidates = [1, 5, 10, 15, 20, 25, 30, 50, 60, 90, 100, 120, 200]
                fps_vals = [float(f) for f in candidates if fps_min <= f <= fps_max]
                if not fps_vals:
                    fps_vals = [float(fps_max)]
            except Exception:
                fps_vals = [float(self._init_params.get("fps", 30.0))]
            return [{"width": w, "height": h, "fps": fps} for fps in fps_vals]
        except Exception:
            return []

    def read_hw_bounds(self) -> dict:
        fb = self._FALLBACK_BOUNDS

        def get_bounds(feature, fmin, fmax):
            try:
                lo, hi = self._cam.get_float_bounds(feature)
                return lo, hi
            except Exception:
                return fmin, fmax

        exp_min,  exp_max  = get_bounds("ExposureTime",         fb["exp_min"],  fb["exp_max"])
        gain_min, gain_max = get_bounds("Gain",                 fb["gain_min"], fb["gain_max"])
        fps_min,  fps_max  = get_bounds("AcquisitionFrameRate", fb["fps_min"],  fb["fps_max"])
        return {
            "exp_min": exp_min, "exp_max": exp_max,
            "gain_min": gain_min, "gain_max": gain_max,
            "fps_min": fps_min,   "fps_max": fps_max,
        }

    def _update_cap_fps(self):
        now = time.time()
        self._fps_buf.append(now)
        if len(self._fps_buf) > FPS_SAMPLE_FRAMES:
            self._fps_buf.popleft()
        n = len(self._fps_buf)
        if n >= 2:
            elapsed = self._fps_buf[-1] - self._fps_buf[0]
            self._cap_fps_val = (n - 1) / elapsed if elapsed > 0 else 0.0

    @staticmethod
    def _decode_frame(buf) -> np.ndarray:
        h   = buf.get_image_height()
        w   = buf.get_image_width()
        fmt = buf.get_image_pixel_format()
        raw = buf.get_data()

        if fmt == Aravis.PIXEL_FORMAT_MONO_8:
            arr = np.frombuffer(raw, dtype=np.uint8).reshape((h, w))
            return cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
        if fmt == Aravis.PIXEL_FORMAT_MONO_16:
            arr = np.frombuffer(raw, dtype=np.uint16).reshape((h, w))
            return cv2.cvtColor((arr >> 4).astype(np.uint8), cv2.COLOR_GRAY2BGR)
        if fmt == Aravis.PIXEL_FORMAT_BAYER_RG_8:
            arr = np.frombuffer(raw, dtype=np.uint8).reshape((h, w))
            return cv2.cvtColor(arr, cv2.COLOR_BayerRG2BGR)
        if fmt == Aravis.PIXEL_FORMAT_BAYER_GB_8:
            arr = np.frombuffer(raw, dtype=np.uint8).reshape((h, w))
            return cv2.cvtColor(arr, cv2.COLOR_BayerGB2BGR)
        # Fallback: treat as mono-8
        arr = np.frombuffer(raw, dtype=np.uint8).reshape((h, w))
        return cv2.cvtColor(arr, cv2.COLOR_GRAY2BGR)
