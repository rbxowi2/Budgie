"""
ssl_utils.py — Self-signed SSL certificate generation.

Extracted from u3v_webui app.py so app.py has no subprocess dependency.
"""

import os
import subprocess

from .config import CERT_FILE, KEY_FILE, TEMP_DIR
from .utils import log


def ensure_ssl_cert(local_ip: str) -> bool:
    """Generate a self-signed cert if one does not already exist.

    Returns True on success, False if openssl is unavailable.
    """
    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        return True

    log("Generating self-signed SSL certificate...")
    san      = f"subjectAltName=IP:{local_ip},IP:127.0.0.1"
    base_cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", KEY_FILE, "-out", CERT_FILE,
        "-days", "3650", "-nodes",
        f"-subj=/CN={local_ip}",
        "-addext", san,
    ]
    try:
        subprocess.run(base_cmd, check=True, capture_output=True)
        log(f"SSL certificate generated -> {CERT_FILE}")
        return True
    except Exception:
        pass

    # Fallback: older openssl without -addext
    try:
        cfg_path = os.path.join(TEMP_DIR, "_ssl_tmp.cnf")
        with open(cfg_path, "w") as f:
            f.write(f"[req]\ndistinguished_name=req\n[SAN]\n{san}\n")
        alt_cmd = [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", KEY_FILE, "-out", CERT_FILE,
            "-days", "3650", "-nodes", f"-subj=/CN={local_ip}",
            "-extensions", "SAN", "-config", cfg_path,
        ]
        subprocess.run(alt_cmd, check=True, capture_output=True)
        os.remove(cfg_path)
        log(f"SSL certificate generated -> {CERT_FILE}")
        return True
    except Exception:
        pass

    log("SSL certificate generation failed.")
    return False
