"""
plugins/runner.py — PluginRunner: pipeline execution, state collection, dispatch.

Does NOT import or hold Flask / SocketIO.
Works exclusively through PluginRegistry.iter_pipeline() / iter_instances().
"""

from typing import Optional

import numpy as np

from ..utils import log
from .registry import PluginRegistry


class PluginRunner:
    """Executes plugin pipelines and dispatches actions/params."""

    def __init__(self, registry: PluginRegistry):
        self._registry = registry

    # ── Frame pipeline ────────────────────────────────────────────────────────

    def process_frame_for_camera(
        self, frame: np.ndarray, hw_ts_ns: int, cam_id: str
    ) -> tuple:
        """Two-phase pipeline execution.

        Phase 1 — pipeline plugins (in list order):
            on_frame modifies shared pipeline_frame.  Affects recording/photo.

        Phase 2 — display plugins (in list order):
            on_frame starts from a copy of the final pipeline_frame.
            Modifications reach only the stream; recording is unaffected.

        Returns (pipeline_frame, display_frame).
        """
        pipeline_frame = frame

        for inst, mode in self._registry.iter_pipeline(cam_id):
            if mode != "pipeline":
                continue
            try:
                result = inst.on_frame(pipeline_frame, hw_ts_ns, cam_id)
                if result is not None:
                    pipeline_frame = result
            except Exception as e:
                log(f"[Plugin] {inst.name} on_frame error: {e}")

        display_frame: Optional[np.ndarray] = None
        for inst, mode in self._registry.iter_pipeline(cam_id):
            if mode != "display":
                continue
            try:
                if display_frame is None:
                    display_frame = pipeline_frame.copy()
                result = inst.on_frame(display_frame, hw_ts_ns, cam_id)
                if result is not None:
                    display_frame = result
            except Exception as e:
                log(f"[Plugin] {inst.name} on_frame error: {e}")

        final_display = display_frame if display_frame is not None else pipeline_frame
        return pipeline_frame, final_display

    # ── State collection ──────────────────────────────────────────────────────

    def collect_state_for_camera(self, cam_id: str) -> dict:
        """Merge get_state() from all active plugins for cam_id."""
        result = {}
        for p in self._registry.iter_instances(cam_id):
            try:
                result.update(p.get_state(cam_id))
            except Exception as e:
                log(f"[Plugin] {p.name} get_state error: {e}")
        return result

    def collect_frame_payload_for_camera(self, cam_id: str) -> dict:
        """Merge frame_payload() from all active plugins for cam_id."""
        result = {}
        for p in self._registry.iter_instances(cam_id):
            try:
                result.update(p.frame_payload(cam_id))
            except Exception as e:
                log(f"[Plugin] {p.name} frame_payload error: {e}")
        return result

    # ── Action / parameter dispatch ───────────────────────────────────────────

    def dispatch_action_for_camera(self, cam_id: str, action: str,
                                   data: dict, driver) -> tuple:
        """Route action to first plugin that handles it."""
        for p in self._registry.iter_instances(cam_id):
            try:
                result = p.handle_action(action, data, driver)
                if result is not None:
                    return result
            except Exception as e:
                log(f"[Plugin] {p.name} handle_action({action}) error: {e}")
        return False, f"No handler for action: {action}"

    def dispatch_set_param_for_camera(self, cam_id: str, key: str,
                                      value, driver) -> bool:
        """Route parameter change to first plugin that claims it."""
        for p in self._registry.iter_instances(cam_id):
            try:
                if p.handle_set_param(key, value, driver):
                    return True
            except Exception as e:
                log(f"[Plugin] {p.name} handle_set_param({key}) error: {e}")
        return False

    # ── Camera lifecycle ──────────────────────────────────────────────────────

    def notify_camera_close(self, cam_id: str):
        """Delegate to registry so camera.py only needs to hold a PluginRunner reference."""
        self._registry.notify_camera_close(cam_id)

    # ── Busy guard ────────────────────────────────────────────────────────────

    def collect_busy_any(self) -> bool:
        """Return True if any plugin on any camera is busy."""
        with self._registry._lock:
            all_instances = [
                inst for d in self._registry._local.values() for inst in d.values()
            ]
        return any(p.is_busy() for p in all_instances)

    def collect_busy_for_camera(self, cam_id: str) -> bool:
        """Return True if any plugin assigned to cam_id is busy."""
        return any(p.is_busy(cam_id) for p in self._registry.iter_instances(cam_id))

    def collect_held_cam_ids_for_camera(self, cam_id: str) -> set:
        """Return cam_ids that plugins assigned to cam_id require to stay open."""
        held: set = set()
        for inst in self._registry.iter_instances(cam_id):
            held |= inst.held_cam_ids()
        return held
