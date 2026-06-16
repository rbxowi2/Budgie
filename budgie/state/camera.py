"""
state/camera.py — CameraRegistry: camera lifecycle, frame buffer, auto-close.

No imports from app.py.  All outgoing communication uses bus.py events:

  camera.opened        (cam_id)       — after driver.start()
  camera.pre_close     (cam_id)       — before stop()/join()  [stream removal]
  camera.closed        (cam_id)       — after cleanup          [state broadcast]
  camera.disconnected  (cam_id)       — hardware drop from driver thread
  plugin.idle          (cam_id)       — plugin finished, no viewers

Bus event ordering contract:
  camera.pre_close MUST be emitted before camera.closed in the same call.

Phase 6 performance design:
  _on_frame()       — driver thread stores raw frame + sets _frame_events[cam_id]
  _PipelineThread   — waits on _frame_events, runs frame_processor, sets _display_events[cam_id]
  _CamStreamThread  — waits on _display_events, reads _display_frames via get_display_frame_ref()
"""

import threading
from typing import Optional

import numpy as np

from ..bus import bus
from ..config import CAM_JOIN_TIMEOUT
from ..drivers import CameraDriver, DRIVER_MAP, DRIVERS, scan_all_devices
from ..utils import log


class _PipelineThread(threading.Thread):
    """Per-camera thread: runs frame_processor so the driver acquisition thread stays free."""

    def __init__(self, cam_id: str, cam_reg: "CameraRegistry"):
        super().__init__(daemon=True, name=f"pipeline-{cam_id}")
        self._cam_id  = cam_id
        self._cam_reg = cam_reg
        self._running = False

    def stop(self):
        self._running = False
        # Wake thread if it is waiting on the raw-frame event
        evt = self._cam_reg._frame_events.get(self._cam_id)
        if evt:
            evt.set()

    def run(self):
        cam_id  = self._cam_id
        cam_reg = self._cam_reg
        self._running = True

        while self._running:
            evt = cam_reg._frame_events.get(cam_id)
            if evt is None:
                break
            if not evt.wait(timeout=1.0):
                continue          # timeout — re-check _running
            if not self._running:
                break
            evt.clear()

            raw = cam_reg._raw_frames.get(cam_id)
            if raw is None:
                continue
            frame, hw_ts_ns = raw

            processor = cam_reg.frame_processor
            if processor:
                pipeline_frame, display_frame = processor(frame, hw_ts_ns, cam_id)
            else:
                pipeline_frame = frame
                display_frame  = frame

            # GIL-safe atomic reference replacement
            cam_reg._pipeline_frames[cam_id] = pipeline_frame
            cam_reg._display_frames[cam_id]  = display_frame

            # Signal streaming thread that a fresh display frame is ready
            disp_evt = cam_reg._display_events.get(cam_id)
            if disp_evt:
                disp_evt.set()


class CameraRegistry:
    """Manages open cameras, frame buffers, and auto-open/close logic."""

    def __init__(self, session_store):
        self._session = session_store
        self._lock    = threading.Lock()

        self._cameras:   dict = {}  # cam_id → CameraDriver
        self._cam_infos: dict = {}  # cam_id → info dict

        self._raw_frames:      dict = {}  # cam_id → (np.ndarray, hw_ts_ns)  [latest unprocessed]
        self._pipeline_frames: dict = {}  # cam_id → np.ndarray
        self._display_frames:  dict = {}  # cam_id → np.ndarray

        # Per-camera events for latency-free pipeline + streaming (Phase 6)
        self._frame_events:     dict = {}  # cam_id → threading.Event  (raw frame ready)
        self._display_events:   dict = {}  # cam_id → threading.Event  (display frame ready)
        self._pipeline_threads: dict = {}  # cam_id → _PipelineThread

        self.available_cameras: list = []

        # cam_ids explicitly opened by admin — survive auto-close
        self._auto_reopen_cams: set = set()

        # cam_ids whose driver thread is inside stop()/join() — excluded from reopen
        self._closing_cams: set = set()

        # Pending native mode request: cam_id → {width, height, fps}
        self._pending_native_modes: dict = {}

        # Injected by app.py after PluginRunner is ready (Phase 4).
        # Signature: (frame, hw_ts_ns, cam_id) → (pipeline_frame, display_frame)
        self.frame_processor = None

        # Injected by app.py after PluginRunner is ready (Phase 4).
        self.plugin_runner = None

        bus.on("viewer.zero", self.try_auto_close_all)
        bus.on("plugin.idle", self._auto_close_camera)

    # ── Camera scan ────────────────────────────────────────────────────────────

    def scan_cameras(self) -> list:
        try:
            fresh = scan_all_devices()
        except Exception as e:
            log(f"Camera scan failed: {e}")
            return self.available_cameras

        fresh_ids = {c["device_id"] for c in fresh}
        with self._lock:
            open_ids = set(self._cameras.keys())
        for cam_id in open_ids:
            if cam_id not in fresh_ids:
                old = next((c for c in self.available_cameras
                            if c["device_id"] == cam_id), None)
                if old:
                    fresh.append(old)
                    log(f"Camera scan: preserved open camera [{cam_id}]")

        self.available_cameras = fresh
        log(f"Camera scan: {len(fresh)} found")
        return fresh

    # ── Camera open / close ────────────────────────────────────────────────────

    def open_camera(self, cam_id: str) -> tuple:
        """Open a camera.  Idempotent — no-op if already open."""
        with self._lock:
            if cam_id in self._cameras:
                return False, "Camera already open"

        device_entry = next(
            (c for c in self.available_cameras if c["device_id"] == cam_id), None
        )
        if device_entry is None:
            with self._lock:
                self._auto_reopen_cams.discard(cam_id)   # BUG-006
            return False, f"Unknown camera: {cam_id}"

        driver_name = device_entry.get("driver", "")
        drv_cls = DRIVER_MAP.get(driver_name) or (DRIVERS[0] if DRIVERS else None)
        if drv_cls is None:
            return False, "No camera driver available"

        drv = drv_cls()
        drv._init_params = dict(drv.DEFAULT_PARAMS)

        with self._lock:
            pending = self._pending_native_modes.pop(cam_id, None)
        if pending:
            drv._init_params.update(pending)

        try:
            info = drv.open(cam_id)
        except Exception as e:
            return False, f"Open failed: {e}"

        try:
            native_modes = drv.query_native_modes()
        except Exception:
            native_modes = []
        info["native_modes"]     = native_modes
        info["supports_audio"]   = drv.supports_audio
        info["default_params"]   = dict(drv.DEFAULT_PARAMS)
        info["supported_params"] = list(drv.SUPPORTED_PARAMS)
        info["cam_id"]           = cam_id

        with self._lock:
            self._cameras[cam_id]   = drv
            self._cam_infos[cam_id] = info

        drv.on_frame      = lambda frame, ts: self._on_frame(frame, ts, cam_id)
        drv.on_disconnect = lambda: threading.Thread(
            target=self._handle_disconnect, args=(cam_id,), daemon=True
        ).start()

        # Start PipelineThread before driver so it's ready when the first frame arrives
        self._frame_events[cam_id]   = threading.Event()
        self._display_events[cam_id] = threading.Event()
        pt = _PipelineThread(cam_id, self)
        pt.start()
        self._pipeline_threads[cam_id] = pt

        drv.start()

        with self._lock:
            self._auto_reopen_cams.add(cam_id)

        bus.emit("camera.opened", cam_id)
        log(f"Camera open  [{cam_id}]  {info['model']}  {info['width']}x{info['height']}")
        return True, f"Connected: {info['model']}"

    def close_camera(self, cam_id: str) -> str:
        """Admin-initiated close — clears auto-reopen intent."""
        with self._lock:
            self._auto_reopen_cams.discard(cam_id)
        self._close_camera_internal(cam_id, notify_plugins=True)
        return f"Camera {cam_id} closed"

    def close_all_cameras(self) -> str:
        cam_ids = list(self._cameras.keys())
        with self._lock:
            self._auto_reopen_cams.clear()
        for cid in cam_ids:
            self._close_camera_internal(cid, notify_plugins=True)
        return f"Closed {len(cam_ids)} camera(s)"

    def _close_camera_internal(self, cam_id: str, notify_plugins: bool = True):
        # Guard: if already closing or already gone, bail out immediately.
        # Prevents double-close when two threads (e.g. two simultaneous disconnects)
        # both call try_auto_close_all and enter _close_camera_internal for the same cam.
        with self._lock:
            if cam_id in self._closing_cams or cam_id not in self._cameras:
                return
            drv = self._cameras[cam_id]
            self._closing_cams.add(cam_id)

        if notify_plugins and self.plugin_runner:
            self.plugin_runner.notify_camera_close(cam_id)

        # Stream removal must happen before join (pre_close subscribers must be fast)
        bus.emit("camera.pre_close", cam_id)

        if drv:
            drv.stop()
            drv.join(timeout=CAM_JOIN_TIMEOUT)

        # Stop PipelineThread after driver (no more _on_frame calls now)
        pt = self._pipeline_threads.pop(cam_id, None)
        if pt:
            pt.stop()
            pt.join(timeout=1.0)
        self._frame_events.pop(cam_id, None)
        self._display_events.pop(cam_id, None)
        self._raw_frames.pop(cam_id, None)

        with self._lock:
            self._cameras.pop(cam_id, None)
            self._closing_cams.discard(cam_id)
            self._cam_infos.pop(cam_id, None)
            self._pipeline_frames.pop(cam_id, None)
            self._display_frames.pop(cam_id, None)
            remaining = list(self._cameras.keys())

        # Let SessionStore clean up its per-camera state
        self._session.on_camera_closed(cam_id, remaining)

        log(f"Camera closed [{cam_id}]")
        bus.emit("camera.closed", cam_id)

    # ── Auto-close logic ───────────────────────────────────────────────────────

    def try_auto_close_all(self):
        """Close all non-busy cameras when viewer count drops to zero."""
        if self._session.viewer_count > 0:
            return
        cam_ids = list(self._cameras.keys())
        if not cam_ids:
            return

        runner = self.plugin_runner
        if runner:
            busy_cams = {c for c in cam_ids if runner.collect_busy_for_camera(c)}
            held: set = set()
            for cam_id in busy_cams:
                held |= runner.collect_held_cam_ids_for_camera(cam_id)
            keep = busy_cams | held
        else:
            keep = set()

        for cam_id in cam_ids:
            if cam_id in keep:
                continue
            log(f"No viewers — auto-closing camera [{cam_id}]")
            self._close_camera_internal(cam_id, notify_plugins=True)

        # BUG-009: a viewer may have connected during a drv.join() window,
        # seen cam_id still in _cameras, skipped reopen, then got state with
        # cameras={}.  Re-trigger reopen now that all closes are complete.
        if self._session.viewer_count > 0:
            bus.emit("camera.need_reopen")

    def _auto_close_camera(self, cam_id: str):
        """Close one camera whose plugin just went idle (no viewers)."""
        if self._session.viewer_count > 0:
            return
        if cam_id not in self._cameras:
            return
        runner = self.plugin_runner
        if runner and runner.collect_busy_for_camera(cam_id):
            return
        log(f"Plugin idle, no viewers — auto-closing camera [{cam_id}]")
        self._close_camera_internal(cam_id, notify_plugins=True)
        # BUG-009: same join-window race as try_auto_close_all
        if self._session.viewer_count > 0:
            bus.emit("camera.need_reopen")

    def _handle_disconnect(self, cam_id: str):
        """Called from driver thread on unexpected hardware disconnect."""
        log(f"Camera hardware disconnected [{cam_id}]")
        with self._lock:
            self._auto_reopen_cams.discard(cam_id)
        if cam_id not in self._cameras:
            return
        self._close_camera_internal(cam_id, notify_plugins=True)
        bus.emit("camera.disconnected", cam_id)

    # ── Frame callback (runs in driver acquisition thread) ─────────────────────

    def _on_frame(self, frame: np.ndarray, hw_ts_ns: int, cam_id: str):
        # Latest frame wins — PipelineThread picks it up asynchronously
        self._raw_frames[cam_id] = (frame, hw_ts_ns)
        evt = self._frame_events.get(cam_id)
        if evt:
            evt.set()

    # ── Frame accessors ────────────────────────────────────────────────────────

    def get_latest_frame(self, cam_id: str) -> Optional[np.ndarray]:
        f = self._pipeline_frames.get(cam_id)
        if f is not None:
            return f.copy()
        drv = self._cameras.get(cam_id)
        return drv.latest_frame if drv else None

    def get_display_frame(self, cam_id: str) -> Optional[np.ndarray]:
        """Return a defensive copy for callers that may modify the array."""
        f = self._display_frames.get(cam_id)
        if f is not None:
            return f.copy()
        return self.get_latest_frame(cam_id)

    def get_display_frame_ref(self, cam_id: str) -> Optional[np.ndarray]:
        """Return display frame reference without copying — caller must not modify the array."""
        f = self._display_frames.get(cam_id)
        if f is not None:
            return f
        drv = self._cameras.get(cam_id)
        return drv.latest_frame if drv else None

    # ── Accessors ──────────────────────────────────────────────────────────────

    def get_driver(self, cam_id: str) -> Optional[CameraDriver]:
        return self._cameras.get(cam_id)

    @property
    def open_cam_ids(self) -> list:
        return list(self._cameras.keys())
