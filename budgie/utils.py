"""utils.py — Shared utility functions."""

import os
from datetime import datetime

import cv2
import numpy as np


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def disk_free_gb(path: str) -> float:
    try:
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize / 1024 ** 3
    except Exception:
        return 0.0


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def elapsed_str(start_ns: int, ts_ns: int) -> str:
    ms  = (ts_ns - start_ns) // 1_000_000
    h   = ms // 3_600_000
    m   = (ms % 3_600_000) // 60_000
    s   = (ms % 60_000) // 1000
    mms = ms % 1000
    return f"{h:02d}_{m:02d}_{s:02d}_{mms:03d}"


def imwrite_fmt(path: str, frame: np.ndarray, fmt: str, jpeg_quality: int = 85):
    if fmt == "JPG":
        cv2.imwrite(path, frame, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    else:
        cv2.imwrite(path, frame)
