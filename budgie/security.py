"""
security.py — SecurityManager: login failure tracking, IP blacklist, event log, notifications.
"""

import json
import os
import secrets
import threading
from datetime import datetime

from .config import FAIL_MAX_ATTEMPTS, SECURITY_BLACKLIST_FILE, SECURITY_LOG_FILE


class SecurityManager:
    """Tracks cumulative login failures per IP, maintains blacklist, event log, and
    a pending-notifications queue that admins must confirm."""

    def __init__(self):
        self._lock              = threading.Lock()
        self._fail_counts:   dict = {}   # ip -> cumulative fail count (memory cache)
        self._blacklist_info: dict = {}  # ip -> {blocked_at, reason, attempts}
        self._notifications:  list = []  # pending notifications (unconfirmed)
        self._load_state()

    # ── State persistence ──────────────────────────────────────────────────────

    def _load_state(self):
        if not os.path.exists(SECURITY_BLACKLIST_FILE):
            return
        try:
            with open(SECURITY_BLACKLIST_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            bl = data.get("blacklist", {})
            self._blacklist_info = bl if isinstance(bl, dict) else {}
            self._fail_counts    = {k: int(v) for k, v in data.get("attempts", {}).items()}
            self._notifications  = data.get("notifications", [])
        except Exception as e:
            print(f"[SECURITY] State load failed: {e}", flush=True)

    def _save_state(self):
        try:
            data = {
                "blacklist":     self._blacklist_info,
                "attempts":      self._fail_counts,
                "notifications": self._notifications,
            }
            with open(SECURITY_BLACKLIST_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[SECURITY] File write failed: {e}", flush=True)

    # ── Event log ──────────────────────────────────────────────────────────────

    def _write_log(self, event: str, ip: str, detail: str = ""):
        now  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[SECURITY] {event} | IP: {ip} | Time: {now}"
        if detail:
            line += f" | {detail}"
        print(line, flush=True)

        entry = {"event": event, "ip": ip, "time": now}
        if detail:
            entry["detail"] = detail

        logs = []
        if os.path.exists(SECURITY_LOG_FILE):
            try:
                with open(SECURITY_LOG_FILE, "r", encoding="utf-8") as f:
                    logs = json.load(f)
            except Exception:
                logs = []
        logs.append(entry)
        try:
            with open(SECURITY_LOG_FILE, "w", encoding="utf-8") as f:
                json.dump(logs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[SECURITY] Log write failed: {e}", flush=True)

    # ── Public interface ───────────────────────────────────────────────────────

    def is_blacklisted(self, ip: str) -> bool:
        with self._lock:
            return ip in self._blacklist_info

    def record_blacklisted_attempt(self, ip: str):
        self._write_log("Blacklisted IP login attempt", ip)

    def record_success(self, ip: str):
        self._write_log("Login successful", ip)

    def record_logout(self, ip: str):
        self._write_log("Logout", ip)

    def record_fail(self, ip: str) -> tuple:
        """Record one failure (lifetime cumulative). Returns (count, notif_dict|None)."""
        with self._lock:
            count = self._fail_counts.get(ip, 0) + 1
            self._fail_counts[ip] = count
            should_block = (count >= FAIL_MAX_ATTEMPTS)
            new_notif = None
            if should_block:
                reason = f"Blocked after {count} cumulative failures"
                info   = {
                    "blocked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "reason":     reason,
                    "attempts":   count,
                }
                self._blacklist_info[ip] = info
                new_notif = {
                    "id":   secrets.token_hex(8),
                    "type": "block",
                    "ip":   ip,
                    "time": info["blocked_at"],
                    "msg":  f"IP {ip} blocked after {count} failed login attempts",
                }
                self._notifications.append(new_notif)
            self._save_state()

        self._write_log("Login failed", ip, f"Cumulative failure #{count}")
        if should_block:
            self._write_log("IP added to blacklist", ip,
                            f"Blocked after {count} cumulative failures")

        return count, new_notif

    def confirm_notification(self, notif_id: str) -> bool:
        with self._lock:
            before = len(self._notifications)
            self._notifications = [n for n in self._notifications if n["id"] != notif_id]
            changed = len(self._notifications) < before
            if changed:
                self._save_state()
        return changed

    def confirm_all_notifications(self):
        with self._lock:
            self._notifications = []
            self._save_state()

    def clear_blacklist(self):
        with self._lock:
            self._blacklist_info = {}
            self._save_state()

    def clear_notifications(self):
        with self._lock:
            self._notifications = []
            self._save_state()

    def get_pending_notifications(self) -> list:
        with self._lock:
            return list(self._notifications)

    def get_blacklist_info(self) -> dict:
        with self._lock:
            return dict(self._blacklist_info)
