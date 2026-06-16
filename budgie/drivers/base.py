"""
drivers/base.py — Abstract CameraDriver interface.

All concrete drivers (Aravis, UVC, RPi, …) must subclass CameraDriver and
implement every @abstractmethod.  The driver is also a Thread: calling
start() begins the acquisition loop; stop()/join() terminate it.
"""

import threading
from abc import ABC, abstractmethod
from typing import Callable, Optional

import numpy as np


class CameraDriver(threading.Thread, ABC):
    """
    Base class for all camera backends.

    Lifecycle
    ---------
    1. ``driver = SomeDriver()``
    2. ``info = driver.open(device_id)``   # configure hardware
    3. ``driver.on_frame = callback``       # wire up frame consumer
    4. ``driver.start()``                  # begin acquisition loop
    5. ``driver.stop(); driver.join()``    # shut down

    Frame callback
    --------------
    ``on_frame(frame: np.ndarray, hw_ts_ns: int)`` is called from the
    acquisition thread for every captured frame.  The callback must be
    fast; heavy work (saving, encoding) should be done elsewhere.

    ``on_frame`` may be ``None`` (frames are silently discarded).
    """

    # Declare which parameter keys this driver honours.
    # Frontend uses this to show/hide controls that the driver does not support.
    # Possible keys: exposure, gain, fps, exposure_auto, gain_auto, exp_auto_upper
    SUPPORTED_PARAMS: frozenset = frozenset({
        "exposure", "gain", "fps",
        "exposure_auto", "gain_auto", "exp_auto_upper",
    })

    # Safe initial parameter values for this driver's protocol.
    # New drivers must override this with values appropriate for their hardware.
    # state.py reads this on first open (or driver-type change) to seed session state.
    DEFAULT_PARAMS: dict = {}

    def __init__(self):
        threading.Thread.__init__(self, daemon=True)
        self.on_frame:      Optional[Callable[[np.ndarray, int], None]] = None
        self.on_disconnect: Optional[Callable[[], None]]                = None
        self._injected_raw:     Optional[np.ndarray] = None
        self._injected_raw_fmt: Optional[str]        = None
        self._inject_lock = threading.Lock()

    # ── Device discovery ───────────────────────────────────────────────────────

    @staticmethod
    def scan_devices() -> list:
        """
        Return a list of available devices.

        Each entry is a dict with at least:
          ``{"device_id": str, "model": str, "serial": str, "label": str}``

        Returns an empty list if no devices are found or the backend is
        unavailable.
        """
        return []

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    @abstractmethod
    def open(self, device_id: Optional[str] = None) -> dict:
        """
        Open and configure the camera hardware.

        Returns a dict with camera info and parameter bounds:
          ``{model, serial, width, height,
             exp_min, exp_max, gain_min, gain_max, fps_min, fps_max}``

        Raises RuntimeError if the camera cannot be opened.
        """

    @abstractmethod
    def close(self):
        """Stop acquisition and release all hardware resources."""

    # ── Parameter control ──────────────────────────────────────────────────────

    @abstractmethod
    def read_hw_bounds(self) -> dict:
        """
        Query actual hardware limits after ``open()`` has been called.

        Returns a dict with keys:
          ``{exp_min, exp_max, gain_min, gain_max, fps_min, fps_max}``

        Must provide internal fallback values so callers never receive KeyError.
        Drivers that cannot query the hardware return safe protocol-specific constants.
        """

    @abstractmethod
    def set_param(self, key: str, value):
        """
        Queue a camera-parameter change to be applied on the next frame loop.

        Supported keys (drivers may ignore unsupported ones):
          exposure, gain, fps, exposure_auto, gain_auto, exp_auto_upper
        """

    # ── Live readings (thread-safe properties) ─────────────────────────────────

    @property
    @abstractmethod
    def latest_frame(self) -> Optional[np.ndarray]:
        """Most recently captured frame, or None if no frame is available yet."""

    @property
    @abstractmethod
    def cap_fps(self) -> float:
        """Measured capture frame rate (frames/second)."""

    @property
    @abstractmethod
    def current_gain(self) -> float:
        """Current gain value read back from the camera (dB)."""

    @property
    @abstractmethod
    def current_exposure(self) -> float:
        """Current exposure value read back from the camera (µs)."""

    @property
    @abstractmethod
    def is_running(self) -> bool:
        """True while the acquisition loop is active."""

    # ── Raw frame channel ─────────────────────────────────────────────────────

    @property
    def latest_raw_frame(self) -> Optional[np.ndarray]:
        """
        Most recent frame in the driver's native pixel format, before any
        conversion to BGR uint8.  Returns ``None`` if this driver does not
        expose raw data.

        Injected synthetic frames (from inject_raw) take precedence when no
        hardware raw is available.  Consumers must check ``raw_frame_format``.
        """
        with self._inject_lock:
            if self._injected_raw is not None:
                return self._injected_raw
        return None

    @property
    def raw_frame_format(self) -> Optional[str]:
        """
        Format string for ``latest_raw_frame``.  ``None`` when raw data is
        not available.

        Standard format strings (mirrors V4L2 fourcc / GenICam convention):

        =========  =================================================
        ``Z16``    16-bit depth, device units (uint16, H×W)
        ``Y10``    10-bit greyscale in 16-bit container (uint16, H×W)
        ``Y16``    16-bit greyscale (uint16, H×W)
        ``MONO8``  8-bit greyscale (uint8, H×W)
        ``MONO12`` 12-bit greyscale in 16-bit container (uint16, H×W)
        ``BayerRG8``   8-bit Bayer RGGB (uint8, H×W)
        ``BayerRG12``  12-bit Bayer RGGB in 16-bit container (uint16, H×W)
        ``YUYV``   Packed YUV 4:2:2 (uint8, H×W×2)
        =========  =================================================
        """
        with self._inject_lock:
            if self._injected_raw_fmt is not None:
                return self._injected_raw_fmt
        return None

    def inject_raw(self, frame: np.ndarray, fmt: str) -> None:
        """Store a synthetic raw frame to be returned by latest_raw_frame."""
        with self._inject_lock:
            self._injected_raw     = frame
            self._injected_raw_fmt = fmt

    def clear_injected_raw(self) -> None:
        """Remove any previously injected synthetic raw frame."""
        with self._inject_lock:
            self._injected_raw     = None
            self._injected_raw_fmt = None

    # ── Native mode query ──────────────────────────────────────────────────────

    def query_native_modes(self) -> list:
        """
        Return the list of native (hardware-supported) capture modes.

        Each entry is a dict: ``{"width": int, "height": int, "fps": float}``.
        Sorted best-first (largest area, then highest fps).

        Returns an empty list if the driver cannot enumerate modes.
        The default implementation returns ``[]``; override in subclasses.
        """
        return []

    # ── Audio abstraction ──────────────────────────────────────────────────────

    @property
    def supports_audio(self) -> bool:
        """
        True if this camera has integrated audio capture hardware.

        Drivers that support audio (e.g. UAC, UVC with audio) should override
        this property to return True and implement audio_start / audio_stop.
        """
        return False

    def audio_start(self) -> None:
        """
        Begin audio capture.  Called by the recording plugin when recording starts.
        Override in drivers where ``supports_audio`` is True.
        """

    def audio_stop(self) -> None:
        """
        Stop audio capture.  Called by the recording plugin when recording stops.
        Override in drivers where ``supports_audio`` is True.
        """
