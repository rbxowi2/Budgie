"""plugins/looprecord/basic.py — LoopRecord: continuous loop-recording plugin (1.0.0).

Architecture:
  on_frame()      — pipeline thread: pushes raw frame to encoder queue,
                    returns frame copy with READY.../REC... overlay for downstream.
  _EncoderThread  — independent daemon: draws timestamp, encodes via ffmpeg pipe,
                    rotates chunks, manages ring-buffer deletion.
"""

import glob
import math
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from typing import Optional

import cv2
import numpy as np

from ..base import PluginBase
from ...config import CAPTURE_DIR
from ...utils import disk_free_gb, log
from .defaults import (
    CHUNK_DURATION_MIN, DISK_GUARD_MB, ENCODER_QUEUE_SIZE,
    FFMPEG_PRESET, LOOP_DURATION_MIN, QUALITY_BITRATES,
    QUALITY_PRESET, RES_PRESETS,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe_cam_id(cam_id: str) -> str:
    return re.sub(r'[^\w]', '_', cam_id).strip('_') or "cam"


def _resolve_rec_size(cam_w: int, cam_h: int, res_preset: str) -> tuple:
    """Return (w, h) for recording; never upscales beyond native."""
    if res_preset not in RES_PRESETS or res_preset == "native":
        return cam_w, cam_h
    pw, ph = RES_PRESETS[res_preset]
    if cam_w <= pw and cam_h <= ph:
        return cam_w, cam_h
    scale = min(pw / cam_w, ph / cam_h)
    return max(1, int(cam_w * scale)), max(1, int(cam_h * scale))


# ── Encoder thread ─────────────────────────────────────────────────────────────

class _EncoderThread(threading.Thread):
    """One ffmpeg process per chunk. Rotates on wall-clock timer."""

    def __init__(self, cam_id_safe: str, w: int, h: int, fps: float,
                 bitrate_kbps: int, chunk_sec: int, output_dir: str,
                 on_chunk_done, on_stop):
        super().__init__(daemon=True, name=f"loopenc-{cam_id_safe}")
        self._cam_id_safe  = cam_id_safe
        self._w            = w
        self._h            = h
        self._fps          = max(fps, 1.0)
        self._bitrate_kbps = bitrate_kbps
        self._chunk_sec    = chunk_sec
        self._output_dir   = output_dir
        self._on_chunk_done = on_chunk_done
        self._on_stop       = on_stop

        self._q            = queue.Queue(maxsize=ENCODER_QUEUE_SIZE)
        self._running      = True
        self._proc: Optional[subprocess.Popen] = None
        self._chunk_start  = 0.0
        self._current_path = ""
        self._elapsed_lock = threading.Lock()
        self._chunk_elapsed = 0.0

    @property
    def chunk_elapsed(self) -> float:
        with self._elapsed_lock:
            return self._chunk_elapsed

    def push(self, frame: np.ndarray) -> bool:
        """Non-blocking; drops frame silently if queue is full."""
        try:
            self._q.put_nowait(frame.copy())
            return True
        except queue.Full:
            return False

    def run(self):
        if not self._start_chunk():
            return

        while self._running:
            try:
                frame = self._q.get(timeout=0.2)
            except queue.Empty:
                self._update_elapsed()
                if self._should_rotate() and not self._rotate():
                    break
                continue

            # Resize if recording resolution differs from incoming frame
            fh, fw = frame.shape[:2]
            if (fw, fh) != (self._w, self._h):
                frame = cv2.resize(frame, (self._w, self._h),
                                   interpolation=cv2.INTER_AREA)

            self._draw_timestamp(frame)

            if self._proc and self._proc.stdin:
                try:
                    self._proc.stdin.write(frame.tobytes())
                except (BrokenPipeError, OSError):
                    log("[LoopRecord] ffmpeg pipe broken")
                    break

            self._update_elapsed()
            if self._should_rotate() and not self._rotate():
                break

        self._close_chunk(final=True)

    def _draw_timestamp(self, frame: np.ndarray):
        h  = frame.shape[0]
        sc = max(h / 720.0, 0.6)
        tk = max(int(sc * 2), 2)
        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        y  = h - 12
        cv2.putText(frame, ts, (10, y), cv2.FONT_HERSHEY_SIMPLEX,
                    sc, (0, 0, 0), tk + 2, cv2.LINE_AA)
        cv2.putText(frame, ts, (10, y), cv2.FONT_HERSHEY_SIMPLEX,
                    sc, (255, 255, 255), tk, cv2.LINE_AA)

    def _start_chunk(self) -> bool:
        free_mb  = disk_free_gb(self._output_dir) * 1024
        chunk_mb = self._bitrate_kbps * 1000 / 8 / 1024 / 1024 * self._chunk_sec
        if free_mb < chunk_mb + DISK_GUARD_MB:
            log(f"[LoopRecord] Disk full ({free_mb:.0f} MB free), stopping")
            self._on_stop("Disk full")
            return False

        now   = datetime.now()
        fname = (f"looprecord_{self._cam_id_safe}"
                 f"_{now.strftime('%Y%m%d_%H%M%S')}.ts")
        self._current_path = os.path.join(self._output_dir, fname)

        fps_int = str(int(round(self._fps)))
        cmd = [
            "ffmpeg", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{self._w}x{self._h}",
            "-r", fps_int,          # input frame rate
            "-i", "pipe:0",
            "-c:v", "libx264",
            "-preset", FFMPEG_PRESET,
            "-b:v", f"{self._bitrate_kbps}k",
            "-maxrate", f"{self._bitrate_kbps}k",
            "-bufsize", f"{self._bitrate_kbps * 2}k",
            "-r", fps_int,          # output frame rate — ensures correct mpegts timebase
            "-f", "mpegts",
            self._current_path,
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        except FileNotFoundError:
            log("[LoopRecord] ffmpeg not found — sudo apt install ffmpeg")
            self._on_stop("ffmpeg not installed")
            return False

        self._chunk_start = time.time()
        with self._elapsed_lock:
            self._chunk_elapsed = 0.0
        log(f"[LoopRecord] Chunk → {fname}")
        return True

    def _update_elapsed(self):
        with self._elapsed_lock:
            self._chunk_elapsed = time.time() - self._chunk_start

    def _should_rotate(self) -> bool:
        return (time.time() - self._chunk_start) >= self._chunk_sec

    def _rotate(self) -> bool:
        self._close_chunk(final=False)
        completed = self._current_path
        self._current_path = ""
        self._on_chunk_done(completed)
        return self._start_chunk()

    def _close_chunk(self, final: bool):
        if self._proc:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=10)
            except Exception:
                self._proc.kill()
            self._proc = None

        if final and self._current_path:
            self._on_chunk_done(self._current_path)
            self._current_path = ""

    def stop(self):
        self._running = False


# ── Plugin ─────────────────────────────────────────────────────────────────────

class LoopRecord(PluginBase):
    """Continuous loop-recording plugin. Local — one instance per camera."""

    def __init__(self):
        # Injected by registry
        self._sio        = None
        self._emit_state = None

        self._cam_id  = ""
        self._cam_w   = 0
        self._cam_h   = 0
        self._cam_fps = 30.0

        # Settings
        self._chunk_min    = CHUNK_DURATION_MIN
        self._loop_min     = LOOP_DURATION_MIN
        self._quality      = QUALITY_PRESET
        self._bitrate_kbps = QUALITY_BITRATES[QUALITY_PRESET]
        self._res_preset   = "native"

        # State
        self._recording  = False
        self._chunks:    list = []
        self._max_chunks = 0
        self._encoder: Optional[_EncoderThread] = None
        self._output_dir = ""
        self._ffmpeg_ok  = False
        self._driver     = None
        self._lock = threading.Lock()

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "LoopRecord"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return "Continuous loop recording with automatic chunk rotation"

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def on_camera_open(self, cam_info: dict, cam_id: str = "", driver=None):
        out        = os.path.join(CAPTURE_DIR, "looprecord", _safe_cam_id(cam_id))
        ffmpeg_ok  = shutil.which("ffmpeg") is not None
        os.makedirs(out, exist_ok=True)
        with self._lock:
            self._cam_id     = cam_id
            self._cam_w      = cam_info.get("width", 0)
            self._cam_h      = cam_info.get("height", 0)
            self._cam_fps    = float(cam_info.get("fps", 30.0))
            self._output_dir = out
            self._max_chunks = math.ceil(self._loop_min / self._chunk_min)
            self._ffmpeg_ok  = ffmpeg_ok
            self._driver     = driver
        if not ffmpeg_ok:
            log("[LoopRecord] ffmpeg not found — sudo apt install ffmpeg")
            if self._sio:
                self._sio.emit("status", {
                    "msg": "LoopRecord: ffmpeg not found — sudo apt install ffmpeg"
                })
        self._scan_existing_chunks()

    def on_camera_close(self, cam_id: str = ""):
        with self._lock:
            recording    = self._recording
            self._driver = None
        if recording:
            self._do_stop()

    # ── Frame hook ────────────────────────────────────────────────────────────

    def on_frame(self, frame: np.ndarray, hw_ts_ns: int,
                 cam_id: str = "") -> Optional[np.ndarray]:
        with self._lock:
            recording = self._recording
            enc       = self._encoder

        if recording and enc:
            enc.push(frame)

        # Draw READY.../REC... on a copy — returned to pipeline downstream
        out  = frame.copy()
        h    = out.shape[0]
        sc   = max(h / 540.0, 0.8)
        tk   = max(int(sc * 2), 2)
        label = "REC..." if recording else "READY..."
        color = (0, 0, 220) if recording else (0, 200, 0)   # BGR: red / green
        pos   = (10, int(38 * sc))

        cv2.putText(out, label, pos, cv2.FONT_HERSHEY_SIMPLEX,
                    sc, (0, 0, 0), tk + 2, cv2.LINE_AA)   # black shadow
        cv2.putText(out, label, pos, cv2.FONT_HERSHEY_SIMPLEX,
                    sc, color, tk, cv2.LINE_AA)

        return out

    # ── State ─────────────────────────────────────────────────────────────────

    def get_state(self, cam_id: str = "") -> dict:
        with self._lock:
            rec     = self._recording
            chunks  = len(self._chunks)
            max_c   = self._max_chunks
            enc     = self._encoder
            bk      = self._bitrate_kbps
            ch_min  = self._chunk_min
            lp_min  = self._loop_min
            qual    = self._quality
            res     = self._res_preset
            out_dir = self._output_dir
            cam_w   = self._cam_w
            cam_h   = self._cam_h
            cam_fps = self._cam_fps

        elapsed   = enc.chunk_elapsed if enc else 0.0
        chunk_sec = ch_min * 60
        chunk_mb  = bk * 1000 / 8 / 1024 / 1024 * chunk_sec
        total_gb  = chunk_mb * max_c / 1024
        daily_gb  = bk * 1000 / 8 / 1024 ** 3 * 86400
        free_gb   = disk_free_gb(out_dir) if out_dir else 0.0

        return {
            "loop_recording":      rec,
            "loop_chunks_count":   chunks,
            "loop_chunks_max":     max_c,
            "loop_chunk_elapsed":  round(elapsed),
            "loop_chunk_dur_sec":  chunk_sec,
            "loop_chunk_mb":       round(chunk_mb, 1),
            "loop_est_gb":         round(total_gb, 2),
            "loop_daily_write_gb": round(daily_gb, 1),
            "loop_disk_free_gb":   round(free_gb, 1),
            "loop_cam_w":          cam_w,
            "loop_cam_h":          cam_h,
            "loop_cam_fps":        round(cam_fps, 1),
            "loop_chunk_min":      ch_min,
            "loop_loop_min":       lp_min,
            "loop_quality":        qual,
            "loop_bitrate_kbps":   bk,
            "loop_res_preset":     res,
            "loop_ffmpeg_ok":      self._ffmpeg_ok,
        }

    def frame_payload(self, cam_id: str = "") -> dict:
        with self._lock:
            rec    = self._recording
            enc    = self._encoder
            chunks = len(self._chunks)
        if not rec:
            return {"loop_rec": False}
        return {
            "loop_rec":        True,
            "loop_rt_elapsed": round(enc.chunk_elapsed if enc else 0.0),
            "loop_rt_chunks":  chunks,
        }

    def is_busy(self, cam_id: str = "") -> bool:
        return self._recording

    # ── Actions / params ──────────────────────────────────────────────────────

    def handle_action(self, action: str, data: dict, driver) -> "tuple | None":
        if action == "start_loop_record":
            return self._do_start(data.get("cam_id", ""))
        if action == "stop_loop_record":
            return self._do_stop()
        return None

    def handle_set_param(self, key: str, value, driver) -> bool:
        changed = False
        with self._lock:
            if self._recording:
                return False
            if key == "loop_chunk_min":
                v = int(value)
                if 1 <= v <= 60 and v < self._loop_min:
                    self._chunk_min  = v
                    self._max_chunks = math.ceil(self._loop_min / v)
                    changed = True
            elif key == "loop_loop_min":
                v = int(value)
                if 10 <= v <= 10080 and v > self._chunk_min:
                    self._loop_min   = v
                    self._max_chunks = math.ceil(v / self._chunk_min)
                    changed = True
            elif key == "loop_quality":
                if value in QUALITY_BITRATES:
                    self._quality      = value
                    self._bitrate_kbps = QUALITY_BITRATES[value]
                    changed = True
            elif key == "loop_bitrate_kbps":
                v = int(value)
                if 128 <= v <= 20000:
                    self._quality      = "custom"
                    self._bitrate_kbps = v
                    changed = True
            elif key == "loop_res_preset":
                if value in RES_PRESETS:
                    self._res_preset = value
                    changed = True
        if changed and self._emit_state:
            self._emit_state()
        return changed

    # ── Recording ─────────────────────────────────────────────────────────────

    def _do_start(self, cam_id: str) -> tuple:
        with self._lock:
            if self._recording:
                return False, "Already recording"
            if not self._ffmpeg_ok:
                return False, "ffmpeg not found — sudo apt install ffmpeg"
            if not self._cam_w:
                return False, "Camera not ready"

            free_gb  = disk_free_gb(self._output_dir)
            chunk_gb = self._bitrate_kbps * 1000 / 8 / 1024 ** 3 * self._chunk_min * 60
            if free_gb < chunk_gb + DISK_GUARD_MB / 1024:
                return False, f"Disk too full ({free_gb:.1f} GB free)"

            rw, rh = _resolve_rec_size(self._cam_w, self._cam_h, self._res_preset)
            self._max_chunks = math.ceil(self._loop_min / self._chunk_min)

            # Prefer measured cap_fps over the stored default (cam_info has no fps field)
            fps = self._cam_fps
            drv = self._driver
            if drv is not None:
                try:
                    measured = float(drv.cap_fps)
                    if measured > 1.0:
                        fps = measured
                except Exception:
                    pass

            enc = _EncoderThread(
                cam_id_safe   = _safe_cam_id(self._cam_id),
                w             = rw,
                h             = rh,
                fps           = fps,
                bitrate_kbps  = self._bitrate_kbps,
                chunk_sec     = self._chunk_min * 60,
                output_dir    = self._output_dir,
                on_chunk_done = self._on_chunk_done,
                on_stop       = self._on_encoder_stop,
            )
            enc.start()
            self._encoder   = enc
            self._recording = True

        log(f"[LoopRecord] Start  loop={self._loop_min}min "
            f"chunk={self._chunk_min}min  {rw}x{rh}  {self._bitrate_kbps}kbps")
        return True, "Loop recording started"

    def _do_stop(self) -> tuple:
        with self._lock:
            if not self._recording:
                return False, "Not recording"
            enc             = self._encoder
            self._encoder   = None
            self._recording = False

        if enc:
            enc.stop()
            enc.join(timeout=15)

        self._mark_idle(self._cam_id)
        log("[LoopRecord] Stopped")
        return True, "Loop recording stopped"

    def _on_chunk_done(self, path: str):
        """Called from encoder thread each time a chunk file is finalised."""
        if not path or not os.path.exists(path):
            return
        with self._lock:
            self._chunks.append(path)
            while len(self._chunks) > self._max_chunks:
                old = self._chunks.pop(0)
                try:
                    os.remove(old)
                    log(f"[LoopRecord] Deleted: {os.path.basename(old)}")
                except Exception as e:
                    log(f"[LoopRecord] Delete failed: {e}")

    def _on_encoder_stop(self, reason: str):
        """Called from encoder thread on unexpected stop (disk full / ffmpeg error)."""
        with self._lock:
            self._encoder   = None
            self._recording = False
        self._mark_idle(self._cam_id)
        if self._sio:
            self._sio.emit("status", {"msg": f"[LoopRecord] Stopped: {reason}"})
        if self._emit_state:
            self._emit_state()

    def _scan_existing_chunks(self):
        """On camera open, rebuild chunk list from .ts files that are still within
        the loop window; delete those older than loop_duration."""
        with self._lock:
            out_dir  = self._output_dir
            loop_sec = self._loop_min * 60
            max_c    = self._max_chunks

        pattern = os.path.join(out_dir, "looprecord_*.ts")
        files   = sorted(glob.glob(pattern))
        cutoff  = time.time() - loop_sec
        valid   = []

        for f in files:
            try:
                if os.path.getmtime(f) > cutoff:
                    valid.append(f)
                else:
                    os.remove(f)
                    log(f"[LoopRecord] Pruned stale chunk: {os.path.basename(f)}")
            except Exception:
                pass

        while len(valid) > max_c:
            try:
                os.remove(valid.pop(0))
            except Exception:
                pass

        with self._lock:
            self._chunks = valid

        if valid:
            log(f"[LoopRecord] Resumed {len(valid)} existing chunks")
