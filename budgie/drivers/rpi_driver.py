"""
drivers/rpi_driver.py — Raspberry Pi native camera driver via picamera2.

Requires:  picamera2  (apt install python3-picamera2  or  pip install picamera2)
           libcamera must be installed and functional.

Reopen safety
-------------
Every open() call instantiates a fresh Picamera2 object.  The run() finally
block always calls cam.stop() + cam.close() so libcamera fully releases the
device before the next open.

Multi-camera
------------
Pi 5 has two CSI ports; Pi 4 and below support only one CSI camera at a time.
This driver enumerates however many cameras global_camera_info() reports.
"""

import threading
import time
from collections import deque
from typing import Optional

import numpy as np

from .base import CameraDriver
from ..config import FPS_SAMPLE_FRAMES

# Consecutive capture failures before the camera is declared disconnected.
_FAIL_LIMIT = 30


class RPiDriver(CameraDriver):
    """
    Raspberry Pi CSI camera via picamera2 / libcamera.

    Tested with Camera Module 2 and Camera Module 3.
    Produces BGR888 frames (same convention as UVC/OpenCV drivers).
    """

    SUPPORTED_PARAMS: frozenset = frozenset({
        "exposure", "gain", "fps",
        "exposure_auto", "gain_auto",
    })

    DEFAULT_PARAMS: dict = {
        "exposure":      20000.0,   # µs
        "gain":          1.0,
        "fps":           30.0,
        "exposure_auto": True,
        "gain_auto":     True,
    }

    _FALLBACK_BOUNDS: dict = {
        "exp_min":  100.0,       "exp_max":  1_000_000.0,
        "gain_min": 1.0,         "gain_max": 16.0,
        "fps_min":  1.0,         "fps_max":  120.0,
    }

    def __init__(self):
        super().__init__()
        self._cam              = None   # Picamera2 instance; None when closed
        self._cam_num: int     = 0
        self._lock             = threading.Lock()
        self._pending: dict    = {}
        self._init_params: dict = dict(self.DEFAULT_PARAMS)

        self._exposure_auto    = True
        self._gain_auto        = True
        self._current_exposure = float(self.DEFAULT_PARAMS["exposure"])
        self._current_gain     = float(self.DEFAULT_PARAMS["gain"])
        self._running          = False

        self._fps_buf: deque        = deque()
        self._cap_fps_val: float    = 0.0
        self._latest: Optional[np.ndarray] = None

    # ── Device discovery ───────────────────────────────────────────────────────

    @staticmethod
    def scan_devices() -> list:
        """Enumerate available Pi CSI cameras via picamera2."""
        try:
            from picamera2 import Picamera2
            cam_infos = Picamera2.global_camera_info()
        except Exception:
            return []

        result = []
        for info in cam_infos:
            num   = info.get("Num", 0)
            model = info.get("Model", f"RPi Camera {num}")
            result.append({
                "device_id": f"rpi:{num}",
                "model":     model,
                "serial":    info.get("Id", f"rpi{num}"),
                "label":     f"{model} (CSI cam{num})",
            })
        return result

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    def open(self, device_id: Optional[str] = None) -> dict:
        from picamera2 import Picamera2

        if device_id and ":" in str(device_id):
            self._cam_num = int(str(device_id).split(":", 1)[1])
        else:
            self._cam_num = int(device_id) if device_id else 0

        init = self._init_params
        width  = int(init.get("width",  1920))
        height = int(init.get("height", 1080))
        fps    = float(init.get("fps", 30.0))
        self._exposure_auto = bool(init.get("exposure_auto", True))
        self._gain_auto     = bool(init.get("gain_auto", True))

        cam = Picamera2(camera_num=self._cam_num)

        # FrameDurationLimits in µs: (min, max) — both set equal to pin desired fps
        frame_dur = max(1, int(1_000_000 / fps))
        controls = {
            "FrameDurationLimits": (frame_dur, frame_dur),
        }
        if self._exposure_auto:
            controls["AeEnable"] = True
        else:
            controls["AeEnable"]     = False
            controls["ExposureTime"] = int(float(init.get("exposure", 20000)))
        if not self._gain_auto:
            controls["AnalogueGain"] = float(init.get("gain", 1.0))

        cfg = cam.create_video_configuration(
            main={"size": (width, height), "format": "RGB888"},
            controls=controls,
            buffer_count=4,
        )
        cam.configure(cfg)
        cam.start()
        self._cam = cam

        actual_w, actual_h = cam.camera_config["main"]["size"]
        model  = cam.camera_properties.get("Model", f"RPi Camera {self._cam_num}")
        bounds = self.read_hw_bounds()

        return {
            "model":  model,
            "serial": f"csi{self._cam_num}",
            "width":  actual_w,
            "height": actual_h,
            **bounds,
        }

    def close(self):
        """Signal acquisition thread to stop.  Actual cam.close() is in run() finally."""
        self._running = False

    def stop(self):
        self._running = False

    def read_hw_bounds(self) -> dict:
        bounds = dict(self._FALLBACK_BOUNDS)
        if self._cam is None:
            return bounds
        try:
            ctrl = self._cam.camera_controls
            if "ExposureTime" in ctrl:
                lo, hi, _ = ctrl["ExposureTime"]
                bounds["exp_min"] = max(1.0, float(lo))
                bounds["exp_max"] = float(hi)
            if "AnalogueGain" in ctrl:
                lo, hi, _ = ctrl["AnalogueGain"]
                bounds["gain_min"] = max(1.0, float(lo))
                bounds["gain_max"] = float(hi)
        except Exception:
            pass
        return bounds

    # ── Native mode query ──────────────────────────────────────────────────────

    def query_native_modes(self) -> list:
        if self._cam is None:
            return []
        try:
            modes = []
            for m in self._cam.sensor_modes:
                size = m.get("size") or m.get("output_size")
                if not size or size[0] <= 0:
                    continue
                w, h  = int(size[0]), int(size[1])
                fps   = float(m.get("fps", 30.0))
                modes.append({"width": w, "height": h, "fps": round(fps, 3)})
            modes.sort(key=lambda e: (-e["width"] * e["height"], -e["fps"]))
            return modes
        except Exception:
            w, h = self._cam.camera_config["main"]["size"]
            return [{"width": w, "height": h, "fps": 30.0}]

    # ── Acquisition loop ───────────────────────────────────────────────────────

    def run(self):
        self._running = True
        fail_count = 0
        try:
            while self._running:
                self._apply_pending()
                try:
                    req = self._cam.capture_request()
                except Exception:
                    if not self._running:
                        break
                    fail_count += 1
                    if fail_count >= _FAIL_LIMIT:
                        self._running = False
                        if self.on_disconnect is not None:
                            self.on_disconnect()
                        break
                    time.sleep(0.01)
                    continue

                if req is None:
                    time.sleep(0.005)
                    continue

                fail_count = 0
                try:
                    frame = req.make_array("main")
                    meta  = req.get_metadata()
                finally:
                    req.release()

                hw_ts_ns = time.time_ns()
                if meta:
                    if "ExposureTime" in meta:
                        self._current_exposure = float(meta["ExposureTime"])
                    if "AnalogueGain" in meta:
                        self._current_gain = float(meta["AnalogueGain"])

                self._latest = frame
                if self.on_frame is not None:
                    self.on_frame(frame, hw_ts_ns)
                self._update_cap_fps()
        finally:
            # Always fully release the camera so the next open() succeeds.
            cam, self._cam = self._cam, None
            if cam is not None:
                try:
                    cam.stop()
                except Exception:
                    pass
                try:
                    cam.close()
                except Exception:
                    pass

    # ── Parameter control ──────────────────────────────────────────────────────

    def set_param(self, key: str, value):
        with self._lock:
            self._pending[key] = value

    def _apply_pending(self):
        with self._lock:
            pending, self._pending = self._pending.copy(), {}
        if not pending or self._cam is None:
            return

        controls = {}
        for key, value in pending.items():
            try:
                if key == "exposure_auto":
                    self._exposure_auto = bool(value)
                    controls["AeEnable"] = self._exposure_auto
                elif key == "gain_auto":
                    self._gain_auto = bool(value)
                elif key == "exposure" and not self._exposure_auto:
                    controls["AeEnable"]     = False
                    controls["ExposureTime"] = int(float(value))
                elif key == "gain" and not self._gain_auto:
                    controls["AnalogueGain"] = float(value)
                elif key == "fps":
                    fd = max(1, int(1_000_000 / float(value)))
                    controls["FrameDurationLimits"] = (fd, fd)
            except Exception as e:
                print(f"[RPiDriver] set {key}={value} failed: {e}", flush=True)

        if controls:
            try:
                self._cam.set_controls(controls)
            except Exception as e:
                print(f"[RPiDriver] set_controls failed: {e}", flush=True)

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

    def _update_cap_fps(self):
        now = time.time()
        self._fps_buf.append(now)
        if len(self._fps_buf) > FPS_SAMPLE_FRAMES:
            self._fps_buf.popleft()
        n = len(self._fps_buf)
        if n >= 2:
            elapsed = self._fps_buf[-1] - self._fps_buf[0]
            self._cap_fps_val = (n - 1) / elapsed if elapsed > 0 else 0.0
