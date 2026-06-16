"""
plugins/overlaytext/basic.py — OverlayTextPlugin (1.1.0)

Local plugin: renders user-supplied text on the camera frame.
Supports configurable color, font scale, and anchor position.
Supports multiple instances per camera via PLUGIN_ALLOW_MULTIPLE.
"""

from typing import Optional

import cv2
import numpy as np

from ..base import PluginBase

_POSITIONS = (
    "center",
    "top-left", "top-center", "top-right",
    "bottom-left", "bottom-center", "bottom-right",
)


class OverlayTextPlugin(PluginBase):
    """Renders text overlay on the camera frame with configurable style."""

    def __init__(self):
        self._text: str = ""
        self._color: list = [255, 255, 255]   # RGB
        self._font_scale: float = 0.0          # 0 = auto
        self._position: str = "center"

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "OverlayText"

    @property
    def version(self) -> str:
        return "1.1.0"

    @property
    def description(self) -> str:
        return "Text overlay on camera frame (color, size, position)"


    # ── Key helpers ───────────────────────────────────────────────────────────

    def _sk(self, field: str) -> str:
        """State/param key for this field, namespaced by instance_key."""
        ik = self._instance_key or self.name
        suffix = "" if ik == self.name else f"_{ik}"
        return f"overlay_{field}{suffix}"

    # ── Frame hook ────────────────────────────────────────────────────────────

    def on_frame(self, frame: np.ndarray, hw_ts_ns: int,
                 cam_id: str = "") -> Optional[np.ndarray]:
        text = self._text.strip()
        if not text:
            return None

        out = frame.copy()
        h, w = out.shape[:2]

        font = cv2.FONT_HERSHEY_DUPLEX
        fs = self._font_scale if self._font_scale > 0 else max(0.6, w / 800.0)
        thickness = max(1, int(fs * 2))

        (tw, th), _ = cv2.getTextSize(text, font, fs, thickness)
        margin = max(10, int(h * 0.02))

        pos = self._position
        if pos == "center":
            x, y = (w - tw) // 2, (h + th) // 2
        elif pos == "top-left":
            x, y = margin, th + margin
        elif pos == "top-center":
            x, y = (w - tw) // 2, th + margin
        elif pos == "top-right":
            x, y = w - tw - margin, th + margin
        elif pos == "bottom-left":
            x, y = margin, h - margin
        elif pos == "bottom-center":
            x, y = (w - tw) // 2, h - margin
        elif pos == "bottom-right":
            x, y = w - tw - margin, h - margin
        else:
            x, y = (w - tw) // 2, (h + th) // 2

        r, g, b = (int(c) for c in self._color)
        cv2.putText(out, text, (x, y), font, fs,
                    (0, 0, 0), thickness + 2, cv2.LINE_AA)
        cv2.putText(out, text, (x, y), font, fs,
                    (b, g, r), thickness, cv2.LINE_AA)
        return out

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        return {
            self._sk("text"):       self._text,
            self._sk("color"):      self._color,
            self._sk("font_scale"): self._font_scale,
            self._sk("position"):   self._position,
        }

    # ── Parameter dispatch ────────────────────────────────────────────────────

    def handle_set_param(self, key: str, value, driver) -> bool:
        if key == self._sk("text"):
            self._text = str(value)
        elif key == self._sk("color"):
            if isinstance(value, list) and len(value) == 3:
                self._color = [int(c) for c in value]
        elif key == self._sk("font_scale"):
            self._font_scale = max(0.0, float(value))
        elif key == self._sk("position"):
            if value in _POSITIONS:
                self._position = value
        else:
            return False
        return True
