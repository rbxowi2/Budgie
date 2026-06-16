"""plugins/base.py — Abstract PluginBase interface (6.0.1)."""

import inspect
import os
from abc import ABC, abstractmethod
from typing import Optional

import numpy as np


class PluginBase(ABC):
    """
    Base class for all U3V WebUI plugins.

    Minimal implementation: override ``name`` and the hooks your plugin needs.
    """

    # Injected by PluginManager after instantiation.
    # Call _mark_idle(cam_id) to signal that this plugin finished a busy task.
    _notify_idle = None
    # Unique key for this instance within a camera's plugin dict.
    # Equals plugin name for single-instance plugins; auto-suffixed for multi-instance.
    _instance_key: str = ""

    def _mark_idle(self, cam_id: str = ""):
        """Notify the system that this plugin is no longer busy."""
        if self._notify_idle is not None:
            self._notify_idle(cam_id)

    # ── Identity ───────────────────────────────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique plugin identifier (used in logs, registry, and URL slugs)."""

    @property
    def version(self) -> str:
        return "0.1.0"

    @property
    def description(self) -> str:
        return ""

    # ── Lifecycle hooks ────────────────────────────────────────────────────────

    def on_load(self):
        """Called once when the plugin is registered."""

    def on_unload(self):
        """Called when the plugin is removed or the server shuts down."""

    def on_camera_open(self, cam_info: dict, cam_id: str = "", driver=None):
        """
        Called after a camera is successfully opened.

        Local plugins:  cam_id matches the camera this instance is bound to.
        Global plugins: cam_id identifies which camera just opened.
        driver: the live CameraDriver instance (may be None if unavailable).
        """

    def on_camera_close(self, cam_id: str = ""):
        """
        Called just before a camera is closed.

        Local plugins:  cam_id matches the camera this instance is bound to.
        Global plugins: cam_id identifies which camera is closing.
        """

    # ── Frame hook ─────────────────────────────────────────────────────────────

    def on_frame(self, frame: np.ndarray, hw_ts_ns: int,
                 cam_id: str = "") -> Optional[np.ndarray]:
        """
        Called for every acquired frame in the driver thread.

        Returns a modified frame or None to pass the original unchanged.
        Keep this fast — it runs in the acquisition thread.
        """
        return None

    # ── State contribution ─────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        """
        Contribute fields to the state snapshot for the given camera.

        Local plugins:  cam_id is typically ignored (instance is already bound).
        Global plugins: use cam_id to return per-camera sub-state.
        """
        return {}

    def frame_payload(self, cam_id: str = "") -> dict:
        """
        Contribute extra fields to every "frame" SocketIO event payload.

        Called by streaming.py once per frame tick, merged into the payload
        sent to viewers.  streaming.py does not know the field names — each
        plugin is responsible for declaring what it needs.

        Keep this fast — it is called from the stream push thread.
        Return an empty dict if this plugin adds nothing to the frame payload.
        """
        return {}

    # ── Action / parameter dispatch ────────────────────────────────────────────

    def handle_action(self, action: str, data: dict, driver) -> "tuple | None":
        """
        Handle a named action (e.g. ``"take_photo"``).

        ``data`` always contains ``cam_id``.
        Return ``(ok: bool, msg: str)`` or ``None`` to pass to the next plugin.
        """
        return None

    def handle_set_param(self, key: str, value, driver) -> bool:
        """
        Handle a camera parameter change.

        Return True if consumed, False to let the next plugin try.
        """
        return False

    # ── Busy guard ─────────────────────────────────────────────────────────────

    def is_busy(self, cam_id: str = "") -> bool:
        """
        True if performing a long-running task that should prevent camera close.

        Local plugins:  cam_id ignored.
        Global plugins: cam_id indicates which camera is being queried.
        """
        return False

    def held_cam_ids(self) -> set:
        """
        Return cam_ids this plugin requires to stay open.

        Used by try_auto_close_all() to protect cameras from being auto-closed
        while a plugin on another camera depends on their frames.
        Override in cross-camera plugins (e.g. MultiView) to declare dependencies.
        """
        return set()

    # ── Route registration ─────────────────────────────────────────────────────

    def register_routes(self, app, sio, ctx):
        """
        Called once at startup to register Flask routes or SocketIO events.

        In 6.0.0, plugins should NOT register SocketIO events here — the
        PluginManager registers generic ``plugin_action`` and ``set_param``
        handlers that dispatch to ``handle_action`` / ``handle_set_param``.

        Override only to add plugin-specific Flask HTTP routes.
        ``ctx`` keys: sio, state, is_admin, emit_state, post_save_callback.
        """

    # ── Plugin UI (server-side available, client-side injected) ───────────────

    def _plugin_directory(self) -> str:
        return os.path.dirname(os.path.abspath(inspect.getfile(type(self))))

    def render_ui(self) -> str:
        path = os.path.join(self._plugin_directory(), "ui.html")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return f.read()
        return ""

    def ui_js_path(self) -> str:
        path = os.path.join(self._plugin_directory(), "ui.js")
        return path if os.path.exists(path) else ""

    def ui_js_url(self) -> str:
        if not self.ui_js_path():
            return ""
        slug = self.name.lower().replace(" ", "")
        return f"/plugin/{slug}/ui.js"
