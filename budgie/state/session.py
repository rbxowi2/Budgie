"""
state/session.py — SessionStore: token TTL, viewer tracking, per-SID state.

No imports from app.py or plugins/.  Cross-module events emitted via bus.
"""

import secrets
import threading
import time

from ..bus import bus
from ..config import SESSION_TTL_SECONDS


class SessionStore:
    """Manages authentication tokens and per-viewer state."""

    def __init__(self):
        self._lock = threading.Lock()

        # Token store
        self._valid_tokens:   set  = set()
        self._token_is_admin: dict = {}
        self._token_expiry:   dict = {}   # token → expiry unix timestamp

        # Viewer tracking
        self._viewers:   set  = set()
        self._sid_token: dict = {}        # sid → token

        # Per-SID UI state
        self._sid_selected_cam: dict = {}  # sid → cam_id

        # Per-(cam_id, sid) streaming state
        self._stream_sizes:  dict = {}    # (cam_id, sid) → (w, h)
        self._stream_paused: dict = {}    # (cam_id, sid) → bool

    # ── Token management ───────────────────────────────────────────────────────

    def create_token(self, is_admin: bool = False) -> str:
        token = secrets.token_hex(16)
        with self._lock:
            self._valid_tokens.add(token)
            self._token_is_admin[token] = is_admin
            self._token_expiry[token]   = time.time() + SESSION_TTL_SECONDS
        return token

    def revoke_token(self, token: str) -> list:
        """Remove token and return list of SIDs that held it."""
        with self._lock:
            self._valid_tokens.discard(token)
            self._token_is_admin.pop(token, None)
            self._token_expiry.pop(token, None)
            sids = [sid for sid, t in self._sid_token.items() if t == token]
        return sids

    def is_valid_token(self, token: str) -> bool:
        with self._lock:
            if token not in self._valid_tokens:
                return False
            if time.time() > self._token_expiry.get(token, 0):
                self._valid_tokens.discard(token)
                self._token_is_admin.pop(token, None)
                self._token_expiry.pop(token, None)
                return False
            return True

    def is_admin_token(self, token: str) -> bool:
        with self._lock:
            return self._token_is_admin.get(token, False)

    def get_token_for_sid(self, sid: str) -> str:
        with self._lock:
            return self._sid_token.get(sid, "")

    def get_admin_sids(self) -> list:
        with self._lock:
            return [s for s, t in self._sid_token.items()
                    if self._token_is_admin.get(t, False)]

    # ── Viewer tracking ────────────────────────────────────────────────────────

    def add_viewer(self, sid: str, token: str):
        with self._lock:
            self._viewers.add(sid)
            self._sid_token[sid] = token

    def remove_viewer(self, sid: str):
        with self._lock:
            self._viewers.discard(sid)
            self._sid_token.pop(sid, None)
            self._sid_selected_cam.pop(sid, None)
            stale = [k for k in self._stream_sizes  if k[1] == sid]
            for k in stale:
                del self._stream_sizes[k]
            stale_p = [k for k in self._stream_paused if k[1] == sid]
            for k in stale_p:
                del self._stream_paused[k]
        # Notify CameraRegistry to try auto-close if no viewers remain
        if self.viewer_count == 0:
            bus.emit("viewer.zero")

    @property
    def viewer_count(self) -> int:
        return len(self._viewers)

    @property
    def viewer_sids(self) -> list:
        with self._lock:
            return list(self._viewers)

    # ── Per-SID camera selection ───────────────────────────────────────────────

    def get_selected_cam(self, sid: str, open_cam_ids: list,
                         available_cameras: list) -> str:
        """Return the camera selected by this SID.

        Falls back to the first open camera if the selection is stale.
        Single lock — eliminates the TOCTOU window between the old two-lock pattern.
        """
        with self._lock:
            sel        = self._sid_selected_cam.get(sid, "")
            known      = (
                {c["device_id"] for c in available_cameras}
                | set(open_cam_ids)
            )
            first_open = open_cam_ids[0] if open_cam_ids else ""
            if not sel or sel not in known:
                sel = first_open
                if sel:
                    self._sid_selected_cam[sid] = sel
                else:
                    self._sid_selected_cam.pop(sid, None)
        return sel

    def set_selected_cam(self, sid: str, cam_id: str):
        with self._lock:
            self._sid_selected_cam[sid] = cam_id

    # ── Stream size / pause ────────────────────────────────────────────────────

    def set_stream_size(self, cam_id: str, sid: str, w: int, h: int):
        with self._lock:
            self._stream_sizes[(cam_id, sid)] = (w, h)

    def get_stream_size(self, cam_id: str, sid: str):
        with self._lock:
            return self._stream_sizes.get((cam_id, sid))

    def set_stream_paused(self, cam_id: str, sid: str, paused: bool):
        with self._lock:
            self._stream_paused[(cam_id, sid)] = paused

    def is_stream_paused(self, cam_id: str, sid: str) -> bool:
        with self._lock:
            return self._stream_paused.get((cam_id, sid), False)

    # ── Called by CameraRegistry when a camera closes ─────────────────────────

    def on_camera_closed(self, cam_id: str, remaining_open: list):
        """Clean up per-camera stream state and re-map SID selections."""
        fallback = remaining_open[0] if remaining_open else ""
        with self._lock:
            stale = [k for k in self._stream_sizes  if k[0] == cam_id]
            for k in stale:
                del self._stream_sizes[k]
            stale_p = [k for k in self._stream_paused if k[0] == cam_id]
            for k in stale_p:
                del self._stream_paused[k]
            for sid, sel in list(self._sid_selected_cam.items()):
                if sel == cam_id:
                    self._sid_selected_cam[sid] = fallback
