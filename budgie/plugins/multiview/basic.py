"""
plugins/multiview/basic.py — MultiViewPlugin (1.1.1)

Source plugin for virtual cameras: ignores the incoming dummy frame and
composites frames from up to four real cameras into a split-screen layout.

Layouts:
  "2h" — two cameras side by side       [1 | 2]
  "2v" — two cameras stacked            [1 / 2]
  "4"  — 2×2 grid                       [1 2 / 3 4]

Settings (via set_param):
  multiview_layout   — "2h" | "2v" | "4"
  multiview_cam_1 … multiview_cam_4   — cam_id for each slot ("" = black)
  multiview_src_1 … multiview_src_4   — "pipeline" | "display" per slot
  multiview_res      — "auto" | "3840x2160" | "1920x1080" | "1280x720" |
                       "854x480" | "640x360"
                       "auto": slot size = largest source frame; each source
                       is letterboxed to preserve aspect ratio.
                       Fixed: total canvas = preset; each slot gets its share.
"""

import threading
from typing import Optional

import cv2
import numpy as np

from ..base import PluginBase

_RES_PRESETS: dict = {
    "3840x2160": (3840, 2160),
    "1920x1080": (1920, 1080),
    "1280x720":  (1280, 720),
    "854x480":   (854,  480),
    "640x360":   (640,  360),
}


class MultiViewPlugin(PluginBase):
    """Split-screen multi-camera compositor for virtual cameras."""

    def __init__(self):
        self._emit_state = None
        self._state      = None   # injected by PluginManager

        self._layout: str       = "2h"
        self._cams:   list[str] = ["", "", "", ""]
        self._srcs:   list[str] = ["pipeline", "pipeline", "pipeline", "pipeline"]
        self._res:    str       = "auto"

        self._lock = threading.Lock()

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "MultiView"

    @property
    def version(self) -> str:
        return "1.1.1"

    @property
    def description(self) -> str:
        return "Split-screen multi-camera compositor for virtual cameras"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_load(self):
        pass

    def on_unload(self):
        pass

    # ── Dependency guard ──────────────────────────────────────────────────────

    def held_cam_ids(self) -> set:
        with self._lock:
            return {c for c in self._cams if c}

    # ── Frame processing ──────────────────────────────────────────────────────

    def on_frame(self, frame: np.ndarray, hw_ts_ns: int,
                 cam_id: str = "") -> Optional[np.ndarray]:
        """Ignore the virtual camera's dummy frame; return a composed layout."""
        with self._lock:
            layout = self._layout
            cams   = list(self._cams)
            srcs   = list(self._srcs)
            res    = self._res

        if self._state is None:
            return None

        n = 4 if layout == "4" else 2
        frames = [
            _slot_frame(self._state, cams[i], srcs[i]) if cams[i] != cam_id else None
            for i in range(n)
        ]

        # Determine per-slot canvas size.
        if res == "auto":
            max_w = max_h = 0
            for f in frames:
                if f is not None:
                    fh, fw = f.shape[:2]
                    max_w = max(max_w, fw)
                    max_h = max(max_h, fh)
            if max_w == 0:
                max_h, max_w = frame.shape[:2]
            sw, sh = max_w, max_h
        else:
            tw, th = _RES_PRESETS.get(res, (1920, 1080))
            if layout == "4":
                sw, sh = tw // 2, th // 2
            elif layout == "2h":
                sw, sh = tw // 2, th
            else:                   # 2v
                sw, sh = tw, th // 2

        tiles = [_fit_slot(f, sw, sh) for f in frames]

        if layout == "2h":
            return np.hstack(tiles)
        elif layout == "2v":
            return np.vstack(tiles)
        elif layout == "4":
            return np.vstack([np.hstack(tiles[:2]), np.hstack(tiles[2:])])

        return None

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        return {
            "multiview_layout": self._layout,
            "multiview_cam_1":  self._cams[0],
            "multiview_cam_2":  self._cams[1],
            "multiview_cam_3":  self._cams[2],
            "multiview_cam_4":  self._cams[3],
            "multiview_src_1":  self._srcs[0],
            "multiview_src_2":  self._srcs[1],
            "multiview_src_3":  self._srcs[2],
            "multiview_src_4":  self._srcs[3],
            "multiview_res":    self._res,
        }

    # ── Parameter dispatch ────────────────────────────────────────────────────

    def handle_set_param(self, key: str, value, driver) -> bool:
        with self._lock:
            if key == "multiview_layout":
                if value in ("2h", "2v", "4"):
                    self._layout = value
            elif key.startswith("multiview_cam_"):
                idx = _slot_index(key, "multiview_cam_")
                if idx is not None:
                    self._cams[idx] = str(value)
            elif key.startswith("multiview_src_"):
                idx = _slot_index(key, "multiview_src_")
                if idx is not None and value in ("pipeline", "display"):
                    self._srcs[idx] = value
            elif key == "multiview_res":
                if value == "auto" or value in _RES_PRESETS:
                    self._res = value
            else:
                return False
        if self._emit_state:
            self._emit_state()
        return True


# ── Helpers ───────────────────────────────────────────────────────────────────

def _slot_index(key: str, prefix: str) -> Optional[int]:
    try:
        n = int(key[len(prefix):])
        if 1 <= n <= 4:
            return n - 1
    except ValueError:
        pass
    return None


def _slot_frame(state, cam_id: str, source: str) -> Optional[np.ndarray]:
    """Fetch the raw frame for a slot; returns None when unavailable."""
    if not cam_id:
        return None
    return state.get_display_frame(cam_id) if source == "display" \
        else state.get_latest_frame(cam_id)


def _fit_slot(f: Optional[np.ndarray], sw: int, sh: int) -> np.ndarray:
    """Letterbox/pillarbox f into a (sw × sh) black canvas, preserving aspect ratio."""
    canvas = np.zeros((sh, sw, 3), dtype=np.uint8)
    if f is None or sw <= 0 or sh <= 0:
        return canvas
    fh, fw = f.shape[:2]
    if fw == 0 or fh == 0:
        return canvas
    scale = min(sw / fw, sh / fh)
    nw = max(1, int(fw * scale))
    nh = max(1, int(fh * scale))
    resized = cv2.resize(f, (nw, nh))
    x0 = (sw - nw) // 2
    y0 = (sh - nh) // 2
    canvas[y0:y0 + nh, x0:x0 + nw] = resized
    return canvas
