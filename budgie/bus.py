"""
bus.py — Lightweight synchronous event bus.

Breaks the circular import between state/ and app.py.
All callbacks are invoked synchronously in registration order.

Event contract (see REFACTOR_PLAN.md §3.2 for ordering rules):

  camera.opened        (cam_id: str)
  camera.pre_close     (cam_id: str)   ← stream removal must happen here
  camera.closed        (cam_id: str)   ← state broadcast happens here
  camera.disconnected  (cam_id: str)
  camera.need_reopen   ()              ← BUG-009: viewers connected during join window
  viewer.zero          ()
  plugin.idle          (cam_id: str)
"""

import threading
from typing import Callable


class EventBus:
    def __init__(self):
        self._handlers: dict[str, list[Callable]] = {}
        self._lock = threading.Lock()

    def on(self, event: str, handler: Callable) -> None:
        with self._lock:
            self._handlers.setdefault(event, []).append(handler)

    def off(self, event: str, handler: Callable) -> None:
        with self._lock:
            lst = self._handlers.get(event, [])
            try:
                lst.remove(handler)
            except ValueError:
                pass

    def emit(self, event: str, *args, **kwargs) -> None:
        with self._lock:
            handlers = list(self._handlers.get(event, []))
        for h in handlers:
            try:
                h(*args, **kwargs)
            except Exception as e:
                # Bus errors must not propagate — log and continue
                from .utils import log
                log(f"[Bus] handler error on '{event}': {e}")


bus = EventBus()
