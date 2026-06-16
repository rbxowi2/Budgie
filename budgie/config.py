"""
config.py — All application-wide constants and paths.

Edit WEB_USERS and WEB_PORT here to configure the server.
"""

import os
import secrets

# ── Identity ───────────────────────────────────────────────────────────────────
APP_NAME = "Budgie"
VERSION  = "1.3.9"

# ── User accounts: (username, password, is_admin) ─────────────────────────────
WEB_USERS = [
    ("admin",  "1234", True),
    ("viewer", "view1",  False),
]
WEB_PORT       = 45221
WEB_HTTP_PORT  = WEB_PORT - 1   # HTTP redirect → HTTPS
WEB_SECRET_KEY = secrets.token_hex(32)   # randomised each startup

# ── Session security ───────────────────────────────────────────────────────────
SESSION_TTL_SECONDS = 8 * 3600   # token + session lifetime (8 hours)

# ── Streaming output ───────────────────────────────────────────────────────────
STREAM_JPEG_Q = 85
STREAM_FPS    = 30

# ── Camera acquisition ─────────────────────────────────────────────────────────
STREAM_PREBUF     = 10
BUF_TIMEOUT_US    = 1_000_000
FPS_SAMPLE_FRAMES = 30

# ── Thread shutdown timeouts ───────────────────────────────────────────────────
CAM_JOIN_TIMEOUT = 2

# ── Paths ──────────────────────────────────────────────────────────────────────
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAPTURE_DIR  = os.path.join(PROJECT_ROOT, "captures")
TEMP_DIR     = os.path.join(PROJECT_ROOT, "temp")
CERT_FILE    = os.path.join(TEMP_DIR, "cert.pem")
KEY_FILE     = os.path.join(TEMP_DIR, "key.pem")

os.makedirs(CAPTURE_DIR, exist_ok=True)
os.makedirs(TEMP_DIR,    exist_ok=True)

# ── Adaptive streaming ─────────────────────────────────────────────────────────
ADAPTIVE_STREAM = True

# ── Security ───────────────────────────────────────────────────────────────────
SECURITY_BLACKLIST_FILE = os.path.join(PROJECT_ROOT, "security_blacklist.json")
SECURITY_LOG_FILE       = os.path.join(PROJECT_ROOT, "security_log.json")
FAIL_MAX_ATTEMPTS       = 3
