"""
streaming.py — StreamManager: per-camera JPEG push threads.

Adapted for Budgie's split state:
  - cam_reg  (CameraRegistry) — driver and frame access
  - session  (SessionStore)   — viewer list, per-(cam, sid) pause/size state
  - runner   (PluginRunner)   — frame payload collection

Phase 6 optimizations applied here:
  6.1 — REVERTED in 1.3.9: event-driven waiting removed the STREAM_FPS cap and
        flooded at the full pipeline rate.  Back to v6's time.sleep(interval)
        fixed-rate sampler (latest frame each tick, intermediate frames dropped).
  6.2 — get_display_frame_ref() (no redundant copy)
  6.3 — emit raw JPEG bytes instead of base64 string (~25% bandwidth saving)

Server-side backpressure (the only robust fix for accumulating latency):
  In async_mode="threading" each client has an UNBOUNDED engineio send queue
  drained by its own writer thread.  Pushing frames at camera FPS with no
  backpressure lets a slow client's queue grow without bound — it forever
  renders stale frames.  Client-side stale-drop cannot fix this: the stale
  frames sit in the SERVER queue and must still cross the slow link to be seen
  and discarded.  So before emitting we inspect the client's queue depth and
  skip the frame if it is already backed up — bounding latency to ~1-2 frames
  per client regardless of link speed, with no clock sync and no client help.
"""

import threading
import time

import cv2

from .config import ADAPTIVE_STREAM, STREAM_FPS, STREAM_JPEG_Q

# Skip a client whose engineio send queue already holds this many packets —
# it has not drained the previous frame, so sending more only grows latency.
_MAX_QUEUE_DEPTH = 2


def _client_queue_depth(sio, sid: str) -> int:
    """Return the engineio send-queue depth for a Socket.IO sid (0 if unknown).

    Reaches into python-socketio/engineio internals (pinned: socketio 5.16,
    engineio 4.13).  Any lookup failure means the socket is gone — return 0 so
    the caller proceeds and the emit harmlessly no-ops.
    """
    try:
        eio_sid = sio.server.manager.eio_sid_from_sid(sid, "/")
        if eio_sid is None:
            return 0
        return sio.server.eio._get_socket(eio_sid).queue.qsize()
    except Exception:
        return 0


class _CamStreamThread(threading.Thread):
    """Push thread for a single camera."""

    def __init__(self, cam_id: str, cam_reg, session_store, sio, runner):
        super().__init__(daemon=True)
        self._cam_id  = cam_id
        self._cam_reg = cam_reg
        self._session = session_store
        self._sio     = sio
        self._runner  = runner
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        interval = 1.0 / STREAM_FPS
        cam_id   = self._cam_id

        while self._running:
            # Fixed-rate sampler (matches v6): cap output at STREAM_FPS and always
            # grab the latest display frame, dropping intermediate frames.  Event-
            # driven waiting (old Phase 6) removed the cap and pushed at the full
            # pipeline rate, flooding bandwidth/CPU and accumulating latency.
            time.sleep(interval)

            if not self._running:
                break

            viewers = self._session.viewer_sids
            if not viewers:
                continue

            drv = self._cam_reg.get_driver(cam_id)
            if drv is None:
                break

            # 6.2: Reference only — streaming thread reads for encoding, never modifies
            frame = self._cam_reg.get_display_frame_ref(cam_id)
            if frame is None:
                continue

            fh, fw = frame.shape[:2]

            frame_meta = (
                self._runner.collect_frame_payload_for_camera(cam_id)
                if self._runner else {}
            )

            base_payload = {
                "cam_id":  cam_id,
                "cap_fps": round(drv.cap_fps, 2),
                **frame_meta,
            }

            encode_cache: dict = {}

            for sid in viewers:
                if self._session.is_stream_paused(cam_id, sid):
                    continue

                # Backpressure: skip clients that have not drained the previous
                # frame, so a slow link cannot accumulate unbounded latency.
                if _client_queue_depth(self._sio, sid) >= _MAX_QUEUE_DEPTH:
                    continue

                size_key = (fw, fh)
                if ADAPTIVE_STREAM:
                    target = self._session.get_stream_size(cam_id, sid)
                    if target is None:
                        continue   # client handshake not complete — skip until set_stream_size arrives
                    tw, th = target
                    if tw < fw or th < fh:
                        scale    = min(tw / fw, th / fh)
                        size_key = (max(1, int(fw * scale)),
                                    max(1, int(fh * scale)))

                if size_key not in encode_cache:
                    nw, nh = size_key
                    scaled = (cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_AREA)
                              if size_key != (fw, fh) else frame)
                    ok, buf = cv2.imencode(
                        ".jpg", scaled, [cv2.IMWRITE_JPEG_QUALITY, STREAM_JPEG_Q]
                    )
                    # 6.3: Raw bytes — Socket.IO binary protocol saves ~25% vs base64
                    encode_cache[size_key] = buf.tobytes() if ok else None

                img_bytes = encode_cache[size_key]
                if img_bytes is None:
                    continue

                payload = dict(base_payload)
                payload["img"] = img_bytes
                self._sio.emit("frame", payload, to=sid)


class StreamManager:
    """Manages one push thread per open camera."""

    def __init__(self):
        self._streams: dict = {}
        self._lock = threading.Lock()

    def add_stream(self, cam_id: str, cam_reg, session_store, sio, runner) -> None:
        """Start a push thread for cam_id.  No-op if a live thread already exists."""
        with self._lock:
            existing = self._streams.get(cam_id)
            if existing is not None and existing.is_alive():
                return
            t = _CamStreamThread(cam_id, cam_reg, session_store, sio, runner)
            self._streams[cam_id] = t
        t.start()

    def remove_stream(self, cam_id: str) -> None:
        """Stop and discard the push thread for cam_id."""
        with self._lock:
            t = self._streams.pop(cam_id, None)
        if t:
            t.stop()

    def remove_all(self) -> None:
        """Stop all push threads."""
        with self._lock:
            threads = list(self._streams.values())
            self._streams.clear()
        for t in threads:
            t.stop()
