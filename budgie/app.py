"""
app.py — Flask application, HTTP routes, SocketIO events (Budgie 1.0.0).

Architecture vs v6
------------------
AppState → CameraRegistry (cam_reg) + SessionStore (session_store)
PluginManager → PluginRegistry (plugin_reg) + PluginRunner (plugin_runner)

Bus subscriptions (task 3.11):
  camera.opened    — notify plugins + start stream thread
  camera.pre_close — stop stream thread          (before join)
  camera.closed    — broadcast state to viewers  (after cleanup)
  camera.disconnected — status emit to all viewers

Plugin routes (4.6, 4.7) are registered here, not in PluginRegistry.
"""

import hmac
import json
import logging
import os
import secrets
import socket
import sys
import threading
import time
from datetime import timedelta
from functools import wraps
from http.server import BaseHTTPRequestHandler

from flask import (Flask, Response, abort, jsonify, redirect,
                   render_template, request, send_file, session, url_for)
from flask_socketio import SocketIO, emit

from .bus import bus
from .config import (
    APP_NAME, CERT_FILE, FAIL_MAX_ATTEMPTS, KEY_FILE,
    PROJECT_ROOT, SECURITY_LOG_FILE, SESSION_TTL_SECONDS,
    VERSION, WEB_HTTP_PORT, WEB_PORT, WEB_SECRET_KEY, WEB_USERS,
)
from .plugins import PluginRunner, registry as plugin_reg
from .security import SecurityManager
from .ssl_utils import ensure_ssl_cert
from .state import CameraRegistry, SessionStore
from .streaming import StreamManager
from .utils import log


# ── Login page ASCII art (loaded once at startup) ─────────────────────────────

_BUDGIE_DIR = os.path.dirname(os.path.abspath(__file__))

def _read_art(name: str) -> str:
    try:
        with open(os.path.join(_BUDGIE_DIR, f"{name}.txt"), encoding="utf-8") as f:
            return "\n".join(line.rstrip() for line in f)
    except Exception:
        return ""

_ART_STAND = _read_art("budgie_stand")
_ART_FLY   = _read_art("budgie_fly")

# ── Application globals ────────────────────────────────────────────────────────

app = Flask(__name__, template_folder="templates")
app.secret_key = WEB_SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY   = True,
    SESSION_COOKIE_SECURE     = True,
    SESSION_COOKIE_SAMESITE   = "Strict",
    PERMANENT_SESSION_LIFETIME = timedelta(seconds=SESSION_TTL_SECONDS),
)

sio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
               ping_interval=25, ping_timeout=60)

session_store  = SessionStore()
cam_reg        = CameraRegistry(session_store)
security       = SecurityManager()
stream_mgr     = StreamManager()
plugin_runner  = PluginRunner(plugin_reg)

# Inject plugin_runner into CameraRegistry so auto-close can check busy state
cam_reg.plugin_runner = plugin_runner

# Inject frame_processor into CameraRegistry
cam_reg.frame_processor = plugin_runner.process_frame_for_camera

# Per-camera operation locks
_cam_op_locks:      dict = {}
_cam_op_locks_meta       = threading.Lock()


@app.after_request
def _strip_server(resp):
    resp.headers["Server"] = ""
    return resp


# ── Bus subscriptions (task 3.11) ──────────────────────────────────────────────

def _on_camera_opened(cam_id: str):
    cam_info = cam_reg._cam_infos.get(cam_id, {})
    plugin_reg.notify_camera_open(cam_id, cam_info)
    stream_mgr.add_stream(cam_id, cam_reg, session_store, sio, plugin_runner)
    # Rebroadcast state so async auto-reopen reaches viewers (symmetric with closed)
    _emit_state_all()


def _on_camera_pre_close(cam_id: str):
    stream_mgr.remove_stream(cam_id)


def _on_camera_closed(cam_id: str):
    _emit_state_all()


def _on_camera_disconnected(cam_id: str):
    sio.emit("status", {"msg": f"Camera disconnected: {cam_id}"})


bus.on("camera.opened",       _on_camera_opened)
bus.on("camera.pre_close",    _on_camera_pre_close)
bus.on("camera.closed",       _on_camera_closed)
bus.on("camera.disconnected", _on_camera_disconnected)
# camera.need_reopen subscribed after _do_auto_reopen is defined (below)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _is_admin_sid() -> bool:
    token = session_store.get_token_for_sid(request.sid)
    return session_store.is_admin_token(token)


def _get_cam_op_lock(cam_id: str) -> threading.Lock:
    with _cam_op_locks_meta:
        if cam_id not in _cam_op_locks:
            _cam_op_locks[cam_id] = threading.Lock()
        return _cam_op_locks[cam_id]


def _do_auto_reopen():
    with cam_reg._lock:
        excluded = set(cam_reg._cameras.keys()) | cam_reg._closing_cams
        to_open  = list(cam_reg._auto_reopen_cams - excluded)
    for cam_id in to_open:
        ok, msg = cam_reg.open_camera(cam_id)
        if not ok and "BUSY" in msg.upper():
            time.sleep(1.0)
            ok, msg = cam_reg.open_camera(cam_id)
        if ok:
            log(f"Auto-reopen [{cam_id}]: {msg}")
        else:
            log(f"Auto-reopen [{cam_id}] failed: {msg}")
    # Note: stream threads are started via bus.on("camera.opened")


# BUG-009: registered here so _do_auto_reopen is already defined
bus.on("camera.need_reopen", _do_auto_reopen)


def _build_state(sid: str = None) -> dict:
    cameras_data = {}
    for cam_id in cam_reg.open_cam_ids:
        drv      = cam_reg.get_driver(cam_id)
        cam_info = cam_reg._cam_infos.get(cam_id, {})
        plugin_s = plugin_runner.collect_state_for_camera(cam_id)
        entry = {
            "cam_info":         cam_info,
            "cap_fps":          round(drv.cap_fps, 2) if drv else 0.0,
            "current_gain":     round(drv.current_gain, 2) if drv else 0.0,
            "current_exposure": round(drv.current_exposure, 1) if drv else 0.0,
        }
        entry.update(plugin_s)
        cameras_data[cam_id] = entry

    selected = (
        session_store.get_selected_cam(sid, cam_reg.open_cam_ids, cam_reg.available_cameras)
        if sid else ""
    )

    return {
        "selected_cam_id":       selected,
        "cameras":               cameras_data,
        "available_cameras":     cam_reg.available_cameras,
        "plugin_assignments":    plugin_reg.get_assignments(),
        "plugin_pipeline":       plugin_reg.get_pipeline_state(),
        "available_plugins":     plugin_reg.list_available(),
        "viewer_count":          session_store.viewer_count,
        "pending_notifications": security.get_pending_notifications(),
    }


def _emit_state_all():
    for sid in list(session_store.viewer_sids):
        sio.emit("state", _build_state(sid), to=sid)


def _push_new_notification_to_admins(notif: dict):
    for sid in session_store.get_admin_sids():
        sio.emit("security_notification", notif, to=sid)


def _read_log() -> list:
    if not os.path.exists(SECURITY_LOG_FILE):
        return []
    try:
        with open(SECURITY_LOG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


# ── Login decorator ────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        if not session_store.is_valid_token(session.get("token", "")):
            session.clear()
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── HTTP routes ────────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    ip = request.remote_addr

    if request.method == "GET":
        session["csrf_token"] = secrets.token_urlsafe(32)
        return render_template("login.html", csrf_token=session["csrf_token"],
                               art_stand=_ART_STAND, art_fly=_ART_FLY)

    time.sleep(1)

    form_token = request.form.get("csrf_token", "")
    sess_token = session.get("csrf_token", "")
    if not sess_token or not hmac.compare_digest(form_token, sess_token):
        return jsonify({"ok": False, "error": "Invalid request", "count": 0}), 403

    if security.is_blacklisted(ip):
        security.record_blacklisted_attempt(ip)
        return jsonify({"ok": False, "error": "Access denied", "count": 0}), 403

    u = request.form.get("username", "")
    p = request.form.get("password", "")

    user_record = next((rec for rec in WEB_USERS if rec[0] == u and rec[1] == p), None)
    if user_record:
        _, _, is_admin = user_record
        security.record_success(ip)
        token_val = session_store.create_token(is_admin)
        session.permanent    = True
        session["logged_in"] = True
        session["is_admin"]  = is_admin
        session["token"]     = token_val
        session["csrf_token"] = secrets.token_urlsafe(32)
        return jsonify({"ok": True, "redirect": "/" if is_admin else "/viewer"})

    count, new_notif = security.record_fail(ip)
    if new_notif:
        _push_new_notification_to_admins(new_notif)
    if count >= FAIL_MAX_ATTEMPTS:
        msg = "Too many failures. Your IP has been blocked."
    else:
        msg = f"Wrong username or password. Attempt {count}/{FAIL_MAX_ATTEMPTS}."
    return jsonify({"ok": False, "error": msg, "count": count})


@app.route("/logout")
def logout():
    token = session.get("token")
    ip    = request.remote_addr
    if token:
        sids = session_store.revoke_token(token)
        security.record_logout(ip)
        log(f"Logout <- {ip}  kicked connections: {len(sids)}")
        def _kick(sids):
            for sid in sids:
                try:
                    sio.disconnect(sid)
                except Exception:
                    pass
        threading.Thread(target=_kick, args=(sids,), daemon=True).start()
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    if not session.get("is_admin"):
        return redirect(url_for("viewer"))
    return render_template("index.html",
                           version=VERSION,
                           plugin_js_urls=plugin_reg.list_js_urls(),
                           is_admin=True)


@app.route("/viewer")
@login_required
def viewer():
    if session.get("is_admin"):
        return redirect(url_for("index"))
    return render_template("index.html",
                           version=VERSION,
                           plugin_js_urls=plugin_reg.list_js_urls(),
                           is_admin=False)


# ── Static plugin assets (4.7) ────────────────────────────────────────────────

@app.route("/plugin/<plugin_name>/ui.js")
def plugin_js(plugin_name):
    info = plugin_reg.find_by_slug(plugin_name)
    if info:
        path = os.path.join(info["dir"], "ui.js")
        if os.path.exists(path):
            return send_file(path, mimetype="application/javascript")
    abort(404)


@app.route("/plugin/<plugin_name>/ui.html")
def plugin_html(plugin_name):
    info = plugin_reg.find_by_slug(plugin_name)
    if info:
        path = os.path.join(info["dir"], "ui.html")
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                return Response(f.read(), mimetype="text/html")
    abort(404)


# ── SocketIO — connection ──────────────────────────────────────────────────────

@sio.on("connect")
def on_connect():
    token = session.get("token", "")
    if not session_store.is_valid_token(token):
        log(f"Connection rejected (invalid token) <- {request.remote_addr}")
        return False
    session_store.add_viewer(request.sid, token)
    log(f"Connected <- {request.remote_addr}  online: {session_store.viewer_count}")
    # Reopen runs in the background: opening a driver is slow and must not block
    # the handshake.  camera.opened rebroadcasts state to this viewer once ready.
    threading.Thread(target=_do_auto_reopen, daemon=True).start()
    emit("state", _build_state(request.sid))
    sio.emit("viewer_count", session_store.viewer_count)


@sio.on("disconnect")
def on_disconnect():
    session_store.remove_viewer(request.sid)
    log(f"Disconnected <- {request.remote_addr}  online: {session_store.viewer_count}")
    sio.emit("viewer_count", session_store.viewer_count)
    # viewer.zero bus event triggers try_auto_close_all via CameraRegistry subscription


# ── SocketIO — camera management ───────────────────────────────────────────────

@sio.on("scan_cameras")
def on_scan_cameras():
    if not _is_admin_sid():
        return
    cam_reg.scan_cameras()
    _emit_state_all()


@sio.on("open_camera")
def on_open_camera(data: dict):
    if not _is_admin_sid():
        return
    cam_id = data.get("cam_id", "")
    if not cam_id:
        emit("status", {"msg": "No camera selected"})
        return
    lock = _get_cam_op_lock(cam_id)
    if not lock.acquire(timeout=5):
        emit("status", {"msg": "Camera operation in progress, please try again"})
        return
    try:
        ok, msg = cam_reg.open_camera(cam_id)
        emit("status", {"msg": msg})
        _emit_state_all()
        # stream thread started via bus camera.opened
    finally:
        lock.release()


@sio.on("close_camera")
def on_close_camera(data: dict):
    if not _is_admin_sid():
        return
    cam_id = data.get("cam_id", "")
    if not cam_id:
        return
    lock = _get_cam_op_lock(cam_id)
    if not lock.acquire(timeout=5):
        emit("status", {"msg": "Camera operation in progress, please try again"})
        return
    try:
        # stream thread stopped via bus camera.pre_close
        msg = cam_reg.close_camera(cam_id)
        emit("status", {"msg": msg})
        # state broadcast via bus camera.closed
    finally:
        lock.release()


@sio.on("close_all_cameras")
def on_close_all_cameras():
    if not _is_admin_sid():
        return
    msg = cam_reg.close_all_cameras()
    emit("status", {"msg": msg})


@sio.on("select_camera")
def on_select_camera(data: dict):
    cam_id = data.get("cam_id", "")
    valid  = ({c["device_id"] for c in cam_reg.available_cameras}
              | set(cam_reg._cameras.keys()))
    if cam_id in valid:
        session_store.set_selected_cam(request.sid, cam_id)
    emit("state", _build_state(request.sid))


@sio.on("apply_native_mode")
def on_apply_native_mode(data: dict):
    if not _is_admin_sid():
        return
    cam_id = (data.get("cam_id")
              or session_store.get_selected_cam(request.sid,
                                                cam_reg.open_cam_ids,
                                                cam_reg.available_cameras))
    index = int(data.get("index", 0))

    cam_state    = plugin_runner.collect_state_for_camera(cam_id)
    native_modes = cam_state.get("native_modes", [])
    if not native_modes or not (0 <= index < len(native_modes)):
        emit("status", {"msg": "Invalid native mode selection"})
        return

    mode = native_modes[index]
    emit("status", {"msg": f"Applying {mode['width']}x{mode['height']} @ {mode['fps']} fps..."})

    lock = _get_cam_op_lock(cam_id)
    if not lock.acquire(timeout=5):
        emit("status", {"msg": "Camera operation in progress, please try again"})
        return
    try:
        cam_reg.close_camera(cam_id)
        with cam_reg._lock:
            cam_reg._pending_native_modes[cam_id] = mode
        ok, msg = cam_reg.open_camera(cam_id)
        emit("status", {"msg": msg})
        # state broadcast via bus camera.closed / camera.opened
    finally:
        lock.release()


# ── SocketIO — adaptive stream ─────────────────────────────────────────────────

@sio.on("set_stream_size")
def on_set_stream_size(data: dict):
    cam_id = data.get("cam_id", "")
    w = int(data.get("w", 0))
    h = int(data.get("h", 0))
    if cam_id and w > 0 and h > 0:
        session_store.set_stream_size(cam_id, request.sid, w, h)


@sio.on("set_stream_paused")
def on_set_stream_paused(data: dict):
    cam_id = data.get("cam_id", "")
    paused = bool(data.get("paused", False))
    if cam_id:
        session_store.set_stream_paused(cam_id, request.sid, paused)


# ── SocketIO — plugin management (4.6) ────────────────────────────────────────

@sio.on("add_plugin")
def on_add_plugin(data: dict):
    if not _is_admin_sid():
        return
    plugin_name = data.get("plugin_name", "")
    cam_id      = data.get("cam_id", "")
    ok, msg = plugin_reg.add_plugin(plugin_name, cam_id)
    emit("status", {"msg": msg})
    _emit_state_all()


@sio.on("remove_plugin")
def on_remove_plugin(data: dict):
    if not _is_admin_sid():
        return
    plugin_name  = data.get("plugin_name", "")
    cam_id       = data.get("cam_id", "")
    instance_key = data.get("instance_key", "")
    ok, msg = plugin_reg.remove_plugin(plugin_name, cam_id, instance_key)
    emit("status", {"msg": msg})
    _emit_state_all()


@sio.on("plugin_action")
def on_plugin_action(data: dict):
    if not _is_admin_sid():
        return
    cam_id = (data.get("cam_id")
              or session_store.get_selected_cam(request.sid,
                                                cam_reg.open_cam_ids,
                                                cam_reg.available_cameras))
    action = data.get("action", "")
    driver = cam_reg.get_driver(cam_id)
    ok, msg = plugin_runner.dispatch_action_for_camera(cam_id, action, data, driver)
    sio.emit("status", {"msg": msg})
    if ok:
        _emit_state_all()


@sio.on("set_param")
def on_set_param(data: dict):
    if not _is_admin_sid():
        return
    cam_id = (data.get("cam_id")
              or session_store.get_selected_cam(request.sid,
                                                cam_reg.open_cam_ids,
                                                cam_reg.available_cameras))
    key   = data.get("key")
    value = data.get("value")
    if key is None or value is None:
        return
    driver = cam_reg.get_driver(cam_id)
    plugin_runner.dispatch_set_param_for_camera(cam_id, key, value, driver)


@sio.on("reorder_plugins")
def on_reorder_plugins(data: dict):
    if not _is_admin_sid():
        return
    cam_id = data.get("cam_id", "")
    names  = data.get("names", [])
    if cam_id and isinstance(names, list):
        plugin_reg.reorder_plugins(cam_id, names)
        _emit_state_all()


@sio.on("set_plugin_mode")
def on_set_plugin_mode(data: dict):
    if not _is_admin_sid():
        return
    cam_id       = data.get("cam_id", "")
    instance_key = data.get("instance_key") or data.get("plugin_name", "")
    mode         = data.get("mode", "pipeline")
    if cam_id and instance_key:
        plugin_reg.set_plugin_mode(cam_id, instance_key, mode)
        _emit_state_all()


# ── SocketIO — security / notifications ────────────────────────────────────────

@sio.on("confirm_notification")
def on_confirm_notification(data: dict):
    if not _is_admin_sid():
        return
    notif_id = data.get("id", "")
    if notif_id:
        security.confirm_notification(notif_id)
    emit("notifications_update", {"notifications": security.get_pending_notifications()})


@sio.on("confirm_all_notifications")
def on_confirm_all_notifications():
    if not _is_admin_sid():
        return
    security.confirm_all_notifications()
    emit("notifications_update", {"notifications": []})


@sio.on("get_security_records")
def on_get_security_records():
    if not _is_admin_sid():
        return
    emit("security_records", {
        "blacklist": security.get_blacklist_info(),
        "log":       _read_log(),
    })


@sio.on("clear_blacklist")
def on_clear_blacklist():
    if not _is_admin_sid():
        return
    ip = request.environ.get("REMOTE_ADDR", "unknown")
    security.clear_blacklist()
    security._write_log("Blacklist cleared by admin", ip)
    emit("security_records", {
        "blacklist": {},
        "log":       _read_log(),
    })


@sio.on("clear_notifications")
def on_clear_notifications():
    if not _is_admin_sid():
        return
    ip = request.environ.get("REMOTE_ADDR", "unknown")
    security.clear_notifications()
    security._write_log("Notifications cleared by admin", ip)
    emit("notifications_update", {"notifications": []})


# ── HTTP → HTTPS redirect server ──────────────────────────────────────────────

class _HttpRedirectHandler(BaseHTTPRequestHandler):
    def _redirect(self):
        host = (self.headers.get("Host") or "127.0.0.1").split(":")[0]
        location = f"https://{host}:{WEB_PORT}{self.path}"
        self.send_response(301)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  self._redirect()
    def do_POST(self): self._redirect()
    def do_HEAD(self): self._redirect()

    def log_message(self, *args): pass


def _start_http_redirect():
    from http.server import HTTPServer
    try:
        srv = HTTPServer(("0.0.0.0", WEB_HTTP_PORT), _HttpRedirectHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return True
    except OSError as e:
        log(f"HTTP redirect server could not bind port {WEB_HTTP_PORT}: {e}")
        return False


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    logging.getLogger("werkzeug").setLevel(logging.ERROR)

    plugin_reg.scan()

    ctx = {
        "sio":        sio,
        "state":      cam_reg,
        "is_admin":   _is_admin_sid,
        "emit_state": _emit_state_all,
    }

    # Inject runtime handles into plugin instances (sio, emit_state, cam_reg)
    plugin_reg.inject_runtime(sio, _emit_state_all, cam_reg)

    # Call each plugin class's register_routes once for plugin-specific HTTP routes
    plugin_reg.call_register_routes_all(app, sio, ctx)

    local_ip = _get_local_ip()

    if not ensure_ssl_cert(local_ip):
        print("ERROR: HTTPS certificate generation failed.")
        print(f"       Check write permissions on: temp/")
        sys.exit(1)

    cam_reg.scan_cameras()

    http_ok = _start_http_redirect()

    print("=" * 52)
    print(f"  {APP_NAME}  v{VERSION}")
    print(f"  Local:   https://127.0.0.1:{WEB_PORT}")
    print(f"  Network: https://{local_ip}:{WEB_PORT}")
    if http_ok:
        print(f"  HTTP redirect: :{WEB_HTTP_PORT} → https://...:{WEB_PORT}")
    print("  Encryption: HTTPS (self-signed — trust on first visit)")
    admins = [u[0] for u in WEB_USERS if u[2]]
    print(f"  Admin accounts: {', '.join(admins)}")
    print("=" * 52)

    sio.run(app, host="0.0.0.0", port=WEB_PORT, debug=False,
            ssl_context=(CERT_FILE, KEY_FILE))


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
