"""
plugins/params/basic.py — BasicParamsPlugin (6.1.0)

Local plugin: one instance per camera assignment.
Owns all camera parameter state: exposure, gain, fps, auto modes, and bounds.
On FIRST open: seeds from driver DEFAULT_PARAMS.
On REOPEN (auto-close/reopen cycle): applies preserved values to the new driver.
Routes set_param calls to the active driver.
"""

from ..base import PluginBase
from .defaults import (
    FALLBACK_EXP_MAX, FALLBACK_EXP_MIN,
    FALLBACK_EXP_AUTO_UPPER,
    FALLBACK_GAIN_MAX, FALLBACK_GAIN_MIN,
    FALLBACK_FPS_MAX, FALLBACK_FPS_MIN,
)


class BasicParamsPlugin(PluginBase):
    """
    Manages exposure, gain, FPS, and auto-mode state for one camera.

    Parameter state is seeded from the driver's DEFAULT_PARAMS each time
    a camera is opened.  Bounds are read from the cam_info dict provided
    by state.open_camera() (which calls driver.read_hw_bounds()).
    """

    _PARAM_KEYS = frozenset({
        "exposure", "gain", "fps",
        "exposure_auto", "gain_auto", "exp_auto_upper",
    })

    def __init__(self):
        # Injected by PluginManager (used in frame_payload to read live driver values)
        self._state = None

        self._exposure       = 0.0
        self._gain           = 0.0
        self._fps            = 0.0
        self._exposure_auto  = False
        self._gain_auto      = False
        self._exp_auto_upper = FALLBACK_EXP_AUTO_UPPER

        self._exp_min  = FALLBACK_EXP_MIN
        self._exp_max  = FALLBACK_EXP_MAX
        self._gain_min = FALLBACK_GAIN_MIN
        self._gain_max = FALLBACK_GAIN_MAX
        self._fps_min  = FALLBACK_FPS_MIN
        self._fps_max  = FALLBACK_FPS_MAX

        self._supported_params: frozenset = frozenset()

        self._native_modes: list        = []
        self._selected_native_mode: int = 0

        # False → first open: seed from DEFAULT_PARAMS.
        # True  → reopen after auto-close: apply preserved values to driver.
        self._has_params: bool = False

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "BasicParams"

    @property
    def version(self) -> str:
        return "2.0.0"

    @property
    def description(self) -> str:
        return "Exposure, gain, FPS, auto modes"


    # ── Lifecycle hooks ───────────────────────────────────────────────────────

    def on_camera_open(self, cam_info: dict, cam_id: str = "", driver=None):
        """
        Seed or restore params when a camera opens.

        First open  (_has_params=False): values seeded from driver DEFAULT_PARAMS.
        Reopen      (_has_params=True):  saved values are re-applied to the new driver
                                         so the hardware matches what the user set before
                                         the auto-close.
        """
        # Always refresh hardware-dependent fields
        self._exp_min  = float(cam_info.get("exp_min",  FALLBACK_EXP_MIN))
        self._exp_max  = float(cam_info.get("exp_max",  FALLBACK_EXP_MAX))
        self._gain_min = float(cam_info.get("gain_min", FALLBACK_GAIN_MIN))
        self._gain_max = float(cam_info.get("gain_max", FALLBACK_GAIN_MAX))
        self._supported_params = frozenset(cam_info.get("supported_params", []))
        if "fps" in self._supported_params:
            self._fps_min = float(cam_info.get("fps_min", FALLBACK_FPS_MIN))
            self._fps_max = float(cam_info.get("fps_max", FALLBACK_FPS_MAX))
        self._native_modes     = cam_info.get("native_modes", [])
        cur_w = cam_info.get("width", 0)
        cur_h = cam_info.get("height", 0)
        self._selected_native_mode = 0
        for i, m in enumerate(self._native_modes):
            if m["width"] == cur_w and m["height"] == cur_h:
                self._selected_native_mode = i
                break

        if not self._has_params:
            # First open: seed from driver defaults
            defaults = cam_info.get("default_params", {})
            self._exposure       = float(defaults.get("exposure",       0.0))
            self._gain           = float(defaults.get("gain",           0.0))
            if "fps" in self._supported_params:
                self._fps = float(defaults.get("fps", 30.0))
            self._exposure_auto  = bool(defaults.get("exposure_auto",   False))
            self._gain_auto      = bool(defaults.get("gain_auto",       False))
            self._exp_auto_upper = float(defaults.get("exp_auto_upper", 100000.0))
            self._has_params = True
        else:
            # Reopen after auto-close: push saved values into the new driver
            if driver is not None:
                driver.set_param("exposure_auto",  self._exposure_auto)
                driver.set_param("gain_auto",      self._gain_auto)
                driver.set_param("exp_auto_upper", self._exp_auto_upper)
                if not self._exposure_auto:
                    driver.set_param("exposure", self._exposure)
                if not self._gain_auto:
                    driver.set_param("gain", self._gain)
                if "fps" in self._supported_params:
                    driver.set_param("fps", self._fps)

    def on_camera_close(self, cam_id: str = ""):
        # Preserve param values (_exposure, _gain, etc.) across auto-close.
        # Only reset hardware-query fields that come from cam_info on next open.
        self._supported_params     = frozenset()
        self._native_modes         = []
        self._selected_native_mode = 0
        # _has_params intentionally NOT reset here.

    # ── State contribution ────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        state = {
            "exposure":               self._exposure,
            "gain":                   self._gain,
            "gain_auto":              self._gain_auto,
            "exposure_auto":          self._exposure_auto,
            "exp_auto_upper":         self._exp_auto_upper,
            "exp_min":                self._exp_min,
            "exp_max":                self._exp_max,
            "gain_min":               self._gain_min,
            "gain_max":               self._gain_max,
            "cam_supported_params":   list(self._supported_params),
            "native_modes":           self._native_modes,
            "selected_native_mode":   self._selected_native_mode,
        }
        if "fps" in self._supported_params:
            state["fps"]     = self._fps
            state["fps_min"] = self._fps_min
            state["fps_max"] = self._fps_max
        return state

    # ── Frame payload ─────────────────────────────────────────────────────────

    def frame_payload(self, cam_id: str = "") -> dict:
        """Attach live gain/exposure readbacks when auto modes are active."""
        if not (self._gain_auto or self._exposure_auto):
            return {}
        drv = self._state.get_driver(cam_id) if self._state else None
        if drv is None:
            return {}
        result = {}
        if self._gain_auto:
            result["current_gain"] = round(drv.current_gain, 2)
        if self._exposure_auto:
            result["current_exposure"] = round(drv.current_exposure, 1)
        return result

    # ── Parameter dispatch ────────────────────────────────────────────────────

    def handle_set_param(self, key: str, value, driver) -> bool:
        if key not in self._PARAM_KEYS:
            return False
        if key == "fps" and "fps" not in self._supported_params:
            return False
        if key == "exposure_auto":
            self._exposure_auto = bool(value)
        elif key == "exp_auto_upper":
            self._exp_auto_upper = float(value)
        elif key == "exposure":
            self._exposure = float(value)
        elif key == "gain":
            self._gain = float(value)
        elif key == "gain_auto":
            self._gain_auto = bool(value)
        elif key == "fps":
            self._fps = float(value)
        if driver is not None:
            driver.set_param(key, value)
        return True
