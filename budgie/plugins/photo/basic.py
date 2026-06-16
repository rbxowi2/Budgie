"""plugins/photo/basic.py — BasicPhoto local plugin (1.1.0)

Local plugin: one instance per camera.
Handles single-shot photo capture only.

Capture is triggered via handle_action("take_photo") which sets a pending flag.
The actual frame is sampled in on_frame at BasicPhoto's position in the pipeline,
so plugins placed after BasicPhoto are excluded from the saved image — consistent
with how BasicRecord works.
"""

import os
import threading
from datetime import datetime
from typing import Optional

import numpy as np

from ..base import PluginBase
from ...config import CAPTURE_DIR
from ...utils import disk_free_gb, imwrite_fmt, log
from .defaults import JPEG_QUALITY


class BasicPhoto(PluginBase):
    """Single-shot photo capture.  Local plugin — one instance per camera."""

    def __init__(self):
        self._sio        = None
        self._emit_state = None
        self._state      = None
        self._fmt        = "BMP"
        self._pending    = False
        self._lock       = threading.Lock()

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "BasicPhoto"

    @property
    def version(self) -> str:
        return "1.1.0"

    @property
    def description(self) -> str:
        return "Single-shot photo capture"


    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_camera_open(self, cam_info: dict, cam_id: str = "", driver=None):
        pass  # fmt preserved across reopen

    # ── Frame hook ────────────────────────────────────────────────────────────

    def on_frame(self, frame: np.ndarray, hw_ts_ns: int,
                 cam_id: str = "") -> Optional[np.ndarray]:
        with self._lock:
            if not self._pending:
                return None
            self._pending = False
        # Save in background to avoid blocking the acquisition thread
        snapshot = frame.copy()
        threading.Thread(target=self._save, args=(snapshot,), daemon=True).start()
        return None

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        return {"photo_fmt": self._fmt}

    # ── Action / param ────────────────────────────────────────────────────────

    def handle_action(self, action: str, data: dict, driver) -> "tuple | None":
        if action != "take_photo":
            return None
        with self._lock:
            self._pending = True
        return True, "Capturing..."

    def handle_set_param(self, key: str, value, driver) -> bool:
        if key != "photo_fmt":
            return False
        self._fmt = str(value)
        return True

    # ── Save ──────────────────────────────────────────────────────────────────

    def _save(self, frame: np.ndarray):
        ext = ".jpg" if self._fmt == "JPG" else ".bmp"
        now = datetime.now()
        subdir = os.path.join(CAPTURE_DIR, now.strftime("%Y%m%d"))
        os.makedirs(subdir, exist_ok=True)
        path = os.path.join(subdir, f"photo_{now.strftime('%Y%m%d_%H%M%S')}{ext}")
        imwrite_fmt(path, frame, self._fmt, JPEG_QUALITY)

        size_mb = os.path.getsize(path) / 1024 ** 2
        free_gb = disk_free_gb(CAPTURE_DIR)
        msg = (f"Saved: {now.strftime('%Y%m%d')}/{os.path.basename(path)}"
               f" ({size_mb:.2f} MB, {free_gb:.1f} GB free)")
        log(f"Photo  {os.path.basename(path)}  {size_mb:.2f} MB")
        if self._sio:
            self._sio.emit("status", {"msg": msg})
