"""plugins/looprecord/defaults.py — LoopRecord constants."""

CHUNK_DURATION_MIN  = 10     # minutes per chunk
LOOP_DURATION_MIN   = 60     # total loop window (minutes)
QUALITY_PRESET      = "medium"
CUSTOM_BITRATE_KBPS = 1000
ENCODER_QUEUE_SIZE  = 4      # max queued frames before dropping
DISK_GUARD_MB       = 500    # stop if free disk drops below this
FFMPEG_PRESET       = "ultrafast"

QUALITY_BITRATES: dict = {
    "low":    512,
    "medium": 1000,
    "high":   2000,
}

# (target_w, target_h) — 0,0 means native passthrough
RES_PRESETS: dict = {
    "native": (0, 0),
    "1080p":  (1920, 1080),
    "720p":   (1280, 720),
    "480p":   (640, 480),
}
