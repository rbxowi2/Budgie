"""
plugins/registry.py — PluginRegistry: plugin discovery, lifecycle, ordering.

Responsibilities
----------------
- Scan plugins/*/plugin.py and populate _available.
- Instantiate, inject context, and lifecycle-manage plugin instances.
- Maintain the per-camera ordered pipeline list.
- Expose introspection APIs used by app.py and PluginRunner.

Does NOT import or hold Flask / SocketIO.
Runtime handles (sio, emit_state, state) are injected by app.py via inject_runtime().
Cross-module notifications are emitted via bus.py (e.g. "plugin.idle").
"""

import importlib
import inspect
import os
import pathlib
import threading
from typing import Dict, List, Optional

from ..bus import bus
from ..utils import log
from .base import PluginBase


def _idle_notifier(cam_id: str = ""):
    """Injected into every plugin as _notify_idle.  Emits bus event."""
    bus.emit("plugin.idle", cam_id)


class PluginRegistry:
    """Runtime plugin registry: scan, instantiate, lifecycle, ordering."""

    def __init__(self):
        self._lock = threading.Lock()

        # Scanned (available) plugins: name → {cls, version, description, dir, ...}
        self._available: dict = {}

        # Active instances: cam_id → {instance_key → PluginBase}
        self._local: Dict[str, Dict[str, PluginBase]] = {}

        # Per-camera ordered pipeline:
        # cam_id → [{"name": str, "instance_key": str, "mode": "pipeline"|"display"}]
        self._pipeline: Dict[str, list] = {}

        # Injected by app.py via inject_runtime()
        self._sio        = None
        self._emit_state = None
        self._state      = None   # CameraRegistry

    # ── Discovery ──────────────────────────────────────────────────────────────

    def scan(self):
        """Scan plugins/*/plugin.py and collect available plugin metadata."""
        plugins_dir = pathlib.Path(__file__).parent
        for plugin_dir in sorted(plugins_dir.iterdir()):
            if not plugin_dir.is_dir() or plugin_dir.name.startswith("_"):
                continue
            if not (plugin_dir / "plugin.py").exists():
                continue
            mod_name = f"{__package__}.{plugin_dir.name}.plugin"
            try:
                mod            = importlib.import_module(mod_name)
                cls            = getattr(mod, "PLUGIN_CLASS",          None)
                name           = getattr(mod, "PLUGIN_NAME",           None)
                ver            = getattr(mod, "PLUGIN_VERSION",        "0.1.0")
                desc           = getattr(mod, "PLUGIN_DESCRIPTION",    "")
                allow_multiple = getattr(mod, "PLUGIN_ALLOW_MULTIPLE", False)
                default_mode   = getattr(mod, "PLUGIN_MODE",           "pipeline")
                if default_mode not in ("pipeline", "display"):
                    default_mode = "pipeline"
                if cls is None or name is None:
                    log(f"[Plugin] {plugin_dir.name}: missing PLUGIN_CLASS/PLUGIN_NAME — skipped")
                    continue
                cls_dir = str(os.path.dirname(os.path.abspath(inspect.getfile(cls))))
                self._available[name] = {
                    "cls": cls, "version": ver,
                    "description": desc, "dir": cls_dir,
                    "allow_multiple": allow_multiple,
                    "default_mode":   default_mode,
                }
                log(f"[Plugin] Available: {name} v{ver}")
            except Exception as e:
                log(f"[Plugin] Failed to scan {plugin_dir.name}: {e}")

    # ── Runtime injection ──────────────────────────────────────────────────────

    def inject_runtime(self, sio, emit_state, state):
        """Called by app.py after SocketIO initialisation.

        Propagates handles to all already-active plugin instances.
        """
        self._sio        = sio
        self._emit_state = emit_state
        self._state      = state
        with self._lock:
            all_instances = [
                inst for d in self._local.values() for inst in d.values()
            ]
        for inst in all_instances:
            self._inject_ctx(inst)

    # ── Plugin add / remove ────────────────────────────────────────────────────

    def add_plugin(self, plugin_name: str, cam_id: str = "") -> tuple:
        """Instantiate and activate a plugin for cam_id."""
        info = self._available.get(plugin_name)
        if info is None:
            return False, f"Unknown plugin: {plugin_name}"
        if not cam_id:
            return False, "cam_id required"

        cls            = info["cls"]
        allow_multiple = info.get("allow_multiple", False)
        default_mode   = info.get("default_mode", "pipeline")

        with self._lock:
            self._local.setdefault(cam_id, {})
            if not allow_multiple:
                if plugin_name in self._local[cam_id]:
                    return False, f"{plugin_name} already active for {cam_id}"
                instance_key = plugin_name
            else:
                existing = set(self._local[cam_id].keys())
                if plugin_name not in existing:
                    instance_key = plugin_name
                else:
                    n = 2
                    while f"{plugin_name}_{n}" in existing:
                        n += 1
                    instance_key = f"{plugin_name}_{n}"

            instance = cls()
            instance._instance_key = instance_key
            self._local[cam_id][instance_key] = instance

            if cam_id not in self._pipeline:
                self._pipeline[cam_id] = []
            self._pipeline[cam_id].append({
                "name": plugin_name, "instance_key": instance_key,
                "mode": default_mode,
            })

        self._inject_ctx(instance)

        try:
            instance.on_load()
        except Exception as e:
            log(f"[Plugin] {plugin_name} on_load error: {e}")

        if self._state is not None and cam_id in self._state._cam_infos:
            drv = self._state.get_driver(cam_id)
            try:
                instance.on_camera_open(self._state._cam_infos[cam_id], cam_id, drv)
            except Exception as e:
                log(f"[Plugin] {plugin_name} on_camera_open error: {e}")

        log(f"[Plugin] Added: {plugin_name} [{cam_id}]")
        return True, f"Plugin {plugin_name} added"

    def remove_plugin(self, plugin_name: str, cam_id: str = "",
                      instance_key: str = "") -> tuple:
        """Remove and deactivate a plugin instance."""
        ikey = instance_key or plugin_name

        with self._lock:
            instance = self._local.get(cam_id, {}).pop(ikey, None)
            if cam_id in self._pipeline:
                self._pipeline[cam_id] = [
                    e for e in self._pipeline[cam_id]
                    if e.get("instance_key", e["name"]) != ikey
                ]

        if instance is None:
            return False, f"{plugin_name} ({ikey}) not active"

        if self._state is not None and cam_id:
            try:
                instance.on_camera_close(cam_id)
            except Exception as e:
                log(f"[Plugin] {plugin_name} on_camera_close error: {e}")
        try:
            instance.on_unload()
        except Exception as e:
            log(f"[Plugin] {plugin_name} on_unload error: {e}")

        log(f"[Plugin] Removed: {ikey} [{cam_id}]")
        return True, f"Plugin {ikey} removed"

    def unregister_all(self):
        """Unload all active plugins (server shutdown)."""
        with self._lock:
            all_instances = [
                inst for d in self._local.values() for inst in d.values()
            ]
        for p in reversed(all_instances):
            try:
                p.on_unload()
            except Exception as e:
                log(f"[Plugin] {p.name} on_unload error: {e}")
        with self._lock:
            self._local.clear()
            self._pipeline.clear()

    # ── Camera lifecycle notifications ─────────────────────────────────────────

    def notify_camera_open(self, cam_id: str, cam_info: dict):
        """Notify all active plugins that cam_id has opened."""
        with self._lock:
            self._pipeline.setdefault(cam_id, [])
        driver = self._state.get_driver(cam_id) if self._state else None
        for p in self.iter_instances(cam_id):
            try:
                p.on_camera_open(cam_info, cam_id, driver)
            except Exception as e:
                log(f"[Plugin] {p.name} on_camera_open error: {e}")

    def notify_camera_close(self, cam_id: str):
        """Notify all active plugins that cam_id is closing.

        Instances are KEPT (not unloaded) to survive a close/reopen cycle.
        """
        for p in self.iter_instances(cam_id):
            try:
                p.on_camera_close(cam_id)
            except Exception as e:
                log(f"[Plugin] {p.name} on_camera_close error: {e}")

    # ── Pipeline ordering / mode ──────────────────────────────────────────────

    def reorder_plugins(self, cam_id: str, names: List[str]):
        """Reorder the pipeline for cam_id.  names is a list of instance_keys."""
        with self._lock:
            pipeline = self._pipeline.get(cam_id)
            if pipeline is None:
                return
            key_to_entry = {e.get("instance_key", e["name"]): e for e in pipeline}
            new_pl = [key_to_entry[n] for n in names if n in key_to_entry]
            mentioned = set(names)
            for e in pipeline:
                if e.get("instance_key", e["name"]) not in mentioned:
                    new_pl.append(e)
            self._pipeline[cam_id] = new_pl
        log(f"[Plugin] Pipeline reordered [{cam_id}]: {names}")

    def set_plugin_mode(self, cam_id: str, instance_key: str, mode: str):
        """Set pipeline mode ("pipeline" or "display") for a plugin instance."""
        if mode not in ("pipeline", "display"):
            return
        with self._lock:
            for entry in self._pipeline.get(cam_id, []):
                if entry.get("instance_key", entry["name"]) == instance_key:
                    entry["mode"] = mode
                    break
        log(f"[Plugin] Mode [{cam_id}] {instance_key} → {mode}")

    # ── Introspection ─────────────────────────────────────────────────────────

    def list_available(self) -> list:
        return [
            {
                "name":           n,
                "version":        i["version"],
                "description":    i["description"],
                "allow_multiple": i.get("allow_multiple", False),
            }
            for n, i in self._available.items()
        ]

    def get_assignments(self) -> dict:
        """Return current plugin assignments: {cam_id: [instance_keys]}."""
        with self._lock:
            return {cam_id: list(d.keys()) for cam_id, d in self._local.items()}

    def get_pipeline_state(self) -> dict:
        """Return full ordered pipeline state per camera.

        Format: {cam_id: [{"name": str, "instance_key": str, "mode": str}], ...}
        """
        with self._lock:
            return {cam_id: [dict(e) for e in pl]
                    for cam_id, pl in self._pipeline.items()}

    def list_js_urls(self) -> list:
        """Return JS URLs for all available plugins (pre-loaded at page startup)."""
        urls = []
        for name, info in self._available.items():
            path = os.path.join(info["dir"], "ui.js")
            if os.path.exists(path):
                slug = name.lower().replace(" ", "")
                urls.append(f"/plugin/{slug}/ui.js")
        return urls

    def find_by_slug(self, slug: str) -> Optional[dict]:
        """Find available plugin info by URL slug (normalised lowercase)."""
        slug_norm = slug.lower().replace("-", "").replace("_", "")
        for name, info in self._available.items():
            name_norm = name.lower().replace(" ", "").replace("-", "").replace("_", "")
            if name_norm == slug_norm:
                return info
        return None

    # ── Called once per plugin class at startup (by app.py) ───────────────────

    def call_register_routes_all(self, app, sio, ctx):
        """Call register_routes() once per plugin class for HTTP endpoints."""
        done: set = set()
        for pname, pinfo in self._available.items():
            pcls = pinfo["cls"]
            if id(pcls) in done:
                continue
            done.add(id(pcls))
            tmp = pcls()
            self._inject_ctx(tmp)
            try:
                tmp.register_routes(app, sio, ctx)
            except Exception as e:
                log(f"[Plugin] {pname} register_routes error: {e}")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def iter_pipeline(self, cam_id: str) -> list:
        """Return snapshot [(inst, mode), ...] in pipeline order for cam_id."""
        with self._lock:
            pipeline   = list(self._pipeline.get(cam_id, []))
            local_snap = dict(self._local.get(cam_id, {}))
        result = []
        for entry in pipeline:
            ikey = entry.get("instance_key", entry["name"])
            inst = local_snap.get(ikey)
            if inst is not None:
                result.append((inst, entry["mode"]))
        return result

    def iter_instances(self, cam_id: str) -> list:
        """Return ordered plugin instances for cam_id (no mode info)."""
        return [inst for inst, _mode in self.iter_pipeline(cam_id)]

    def _inject_ctx(self, instance: PluginBase):
        """Inject sio / emit_state / state / idle callback into a plugin instance."""
        if self._sio is not None and hasattr(instance, "_sio"):
            instance._sio = self._sio
        if self._emit_state is not None and hasattr(instance, "_emit_state"):
            instance._emit_state = self._emit_state
        if self._state is not None and hasattr(instance, "_state"):
            instance._state = self._state
        instance._notify_idle = _idle_notifier
