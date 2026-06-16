"""plugins/motiondetect/motion.py — MotionDetect plugin (1.0.3)

Local plugin: one instance per camera.
Motion detection uses MOG2 on a downscaled frame (DETECT_WIDTH px wide).
Full-resolution frames are captured for photos; pipeline/recording unaffected.
"""

import json
import os
import threading
import time
from datetime import datetime
from typing import Optional

import cv2
import numpy as np

from ..base import PluginBase
from ...config import CAPTURE_DIR
from ...utils import disk_free_gb, imwrite_fmt, log

DETECT_WIDTH = 640
ZONES_DIR    = os.path.join(CAPTURE_DIR, "motdet_zones")


class MotionDetect(PluginBase):
    """Zone-based motion detection using MOG2. Local plugin — one instance per camera."""

    def __init__(self):
        self._sio        = None
        self._emit_state = None
        self._state      = None

        self._enabled         = False
        self._var_threshold   = 25
        self._min_pixel_count = 500
        self._cooldown_sec    = 3.0

        self._zones: list = []
        self._cam_id      = ""
        self._lock        = threading.Lock()

        self._mog2: Optional[cv2.BackgroundSubtractor] = None
        self._running   = False
        self._stop_evt  = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_trigger = 0.0

        self._pending_photo = False
        self._photo_lock    = threading.Lock()

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "MotionDetect"

    @property
    def version(self) -> str:
        return "1.0.3"

    @property
    def description(self) -> str:
        return "Zone-based motion detection (MOG2)"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_load(self):
        pass

    def on_unload(self):
        self._stop_thread()

    def on_camera_open(self, cam_info: dict, cam_id: str = "", driver=None):
        self._cam_id = cam_id
        self._load_zones(cam_id)
        if self._enabled:
            self._start_thread()

    def on_camera_close(self, cam_id: str = ""):
        self._stop_thread()
        with self._lock:
            self._mog2 = None

    # ── Frame hook — captures triggered photo at full resolution ──────────────

    def on_frame(self, frame: np.ndarray, hw_ts_ns: int,
                 cam_id: str = "") -> Optional[np.ndarray]:
        with self._photo_lock:
            if not self._pending_photo:
                return None
            self._pending_photo = False
        threading.Thread(
            target=self._save_photo, args=(frame.copy(),), daemon=True
        ).start()
        return None

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        with self._lock:
            return {
                "motdet_enabled":         self._enabled,
                "motdet_var_threshold":   self._var_threshold,
                "motdet_min_pixel_count": self._min_pixel_count,
                "motdet_cooldown_sec":    self._cooldown_sec,
                "motdet_zones":           list(self._zones),
            }

    # ── Actions ───────────────────────────────────────────────────────────────

    def handle_action(self, action: str, data: dict, driver) -> "tuple | None":
        if action != "motdet_save_zones":
            return None
        zones = data.get("zones", [])
        with self._lock:
            self._zones = zones
        self._save_zones(self._cam_id)
        return True, "Zones saved"

    def is_busy(self, cam_id: str = "") -> bool:
        return self._enabled and self._running

    def handle_set_param(self, key: str, value, driver) -> bool:
        if key == "motdet_enabled":
            enabled = bool(value)
            with self._lock:
                self._enabled = enabled
            if enabled:
                self._start_thread()
            else:
                self._stop_thread()
                self._mark_idle(self._cam_id)
            return True
        if key == "motdet_var_threshold":
            v = int(value)
            with self._lock:
                self._var_threshold = v
                if self._mog2 is not None:
                    self._mog2.setVarThreshold(v)
            return True
        if key == "motdet_min_pixel_count":
            with self._lock:
                self._min_pixel_count = int(value)
            return True
        if key == "motdet_cooldown_sec":
            with self._lock:
                self._cooldown_sec = float(value)
            return True
        return False

    # ── HTTP snapshot route ───────────────────────────────────────────────────

    def register_routes(self, app, sio, ctx):
        from flask import Response, abort, request, session

        state = ctx["state"]

        @app.route("/plugin/motiondetect/snapshot",
                   endpoint="motdet_snapshot")
        def _motdet_snapshot():
            if not session.get("logged_in"):
                abort(401)
            cam_id = request.args.get("cam_id", "")
            frame = state.get_display_frame(cam_id)
            if frame is None:
                abort(404)
            _, buf = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85]
            )
            return Response(buf.tobytes(), mimetype="image/jpeg")

    # ── Detection thread ──────────────────────────────────────────────────────

    def _start_thread(self):
        if self._running:
            return
        # Wait for any lingering thread from a previous stop to exit
        prev = self._thread
        if prev is not None and prev.is_alive():
            prev.join(timeout=0.3)
        self._stop_evt.clear()
        with self._lock:
            self._mog2 = cv2.createBackgroundSubtractorMOG2(
                history=500,
                varThreshold=self._var_threshold,
                detectShadows=True,
            )
        self._running = True
        self._thread  = threading.Thread(
            target=self._detect_loop, daemon=True, name=f"motdet-{self._cam_id}"
        )
        self._thread.start()
        log(f"[MotionDetect] Detection started [{self._cam_id}]")

    def _stop_thread(self):
        self._running = False
        self._stop_evt.set()
        t = self._thread
        self._thread = None
        if t is not None and t.is_alive():
            t.join(timeout=0.3)
        log(f"[MotionDetect] Detection stopped [{self._cam_id}]")

    def _detect_loop(self):
        last_sample = 0.0
        while self._running and not self._stop_evt.is_set():
            now = time.monotonic()
            if now - last_sample < 0.1:
                self._stop_evt.wait(0.01)
                continue
            last_sample = now

            if self._state is None:
                continue
            frame = self._state.get_latest_frame(self._cam_id)
            if frame is None:
                continue

            # Downscale for MOG2 — does not touch pipeline or photo frames
            h, w  = frame.shape[:2]
            scale = DETECT_WIDTH / w
            dw    = DETECT_WIDTH
            dh    = max(1, int(h * scale))
            small = cv2.resize(frame, (dw, dh), interpolation=cv2.INTER_AREA)

            with self._lock:
                mog2   = self._mog2
                zones  = list(self._zones)
                min_px = self._min_pixel_count
                cd     = self._cooldown_sec

            if mog2 is None:
                continue

            fg_mask = mog2.apply(small)
            # Binarise: keep only definite foreground (>200), discard shadows (127)
            _, fg_bin = cv2.threshold(fg_mask, 200, 255, cv2.THRESH_BINARY)

            if not zones:
                continue

            with self._lock:
                last_t = self._last_trigger
            if now - last_t < cd:
                continue

            triggered = False
            for zone in zones:
                pts = zone.get("points", [])
                if len(pts) < 3:
                    continue
                px_pts = np.array(
                    [[int(p[0] * dw), int(p[1] * dh)] for p in pts],
                    dtype=np.int32,
                ).reshape(-1, 1, 2)   # fillPoly requires (N,1,2)
                zone_mask = np.zeros((dh, dw), dtype=np.uint8)
                cv2.fillPoly(zone_mask, [px_pts], 255)
                count = int(cv2.countNonZero(
                    cv2.bitwise_and(fg_bin, fg_bin, mask=zone_mask)
                ))
                if count >= min_px:
                    triggered = True
                    break

            if triggered:
                with self._lock:
                    self._last_trigger = now
                with self._photo_lock:
                    self._pending_photo = True
                if self._sio:
                    self._sio.emit("status", {"msg": "Motion detected — capturing photo"})

    # ── Photo ─────────────────────────────────────────────────────────────────

    def _save_photo(self, frame: np.ndarray):
        now    = datetime.now()
        subdir = os.path.join(CAPTURE_DIR, now.strftime("%Y%m%d"))
        os.makedirs(subdir, exist_ok=True)
        safe   = self._cam_id.replace("/", "_").replace(":", "_").replace(" ", "_")
        fname  = f"motdet_{safe}_{now.strftime('%Y%m%d_%H%M%S_%f')}.jpg"
        path   = os.path.join(subdir, fname)
        imwrite_fmt(path, frame, "JPG", 85)
        size_mb = os.path.getsize(path) / 1024 ** 2
        free_gb = disk_free_gb(CAPTURE_DIR)
        log(f"MotionDetect  {fname}  {size_mb:.2f} MB")
        if self._sio:
            self._sio.emit("status", {
                "msg": (f"Motion photo: {now.strftime('%Y%m%d')}/{fname}"
                        f" ({size_mb:.2f} MB, {free_gb:.1f} GB free)")
            })

    # ── Zone persistence ──────────────────────────────────────────────────────

    def _zones_path(self, cam_id: str) -> str:
        os.makedirs(ZONES_DIR, exist_ok=True)
        safe = cam_id.replace("/", "_").replace(":", "_").replace(" ", "_")
        return os.path.join(ZONES_DIR, f"{safe}_zones.json")

    def _save_zones(self, cam_id: str):
        if not cam_id:
            return
        with self._lock:
            zones = list(self._zones)
        try:
            with open(self._zones_path(cam_id), "w", encoding="utf-8") as f:
                json.dump(zones, f, ensure_ascii=False, indent=2)
        except Exception as e:
            log(f"[MotionDetect] save_zones error: {e}")

    def _load_zones(self, cam_id: str):
        if not cam_id:
            return
        path = self._zones_path(cam_id)
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as f:
                zones = json.load(f)
            with self._lock:
                self._zones = zones
            log(f"[MotionDetect] Loaded {len(zones)} zone(s) for [{cam_id}]")
        except Exception as e:
            log(f"[MotionDetect] load_zones error: {e}")
