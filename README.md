# Budgie — User Guide

Budgie is a multi-camera LAN web interface for industrial and standard cameras.
It streams live video to any browser on your local network, supports multiple
simultaneous viewers, and is extended through a drop-in plugin system
(recording, motion detection, overlays, multi-view, and more).

This guide takes you from a clean Linux machine to a running server with a
USB3 Vision (U3V) camera.

---

## 1. Overview

| | |
|---|---|
| **Access** | Web browser, over HTTPS, on the local network |
| **Roles** | Administrator (full camera control) and Viewer (stream only) |
| **Cameras** | USB3 Vision / GigE (via Aravis), UVC webcams, Raspberry Pi, and a built-in virtual test camera |
| **Run command** | `python run.py` |

You do not need to install anything on the client devices — only a browser.

---

## 2. System Requirements

Budgie is developed and supported on **Linux**. Ubuntu 22.04 LTS or newer is
recommended; other Debian-based distributions work with equivalent packages.

| Component | Minimum |
|-----------|---------|
| OS        | Linux (Ubuntu 22.04+ recommended) |
| Python    | 3.10+ |
| OpenSSL   | Any version (used for HTTPS) |
| RAM       | 2 GB (4 GB+ recommended for multiple cameras/viewers) |

For USB3 Vision cameras, plug the camera into a **USB 3 port** (blue connector)
for full bandwidth.

---

## 3. Installing System Packages

Budgie relies on a few packages that **cannot** be installed with `pip` — they
come from the system package manager (notably the Aravis camera bindings and
OpenCV's GObject introspection):

```bash
sudo apt-get update
sudo apt-get install -y \
    python3-opencv \
    python3-numpy \
    gir1.2-aravis-0.8 aravis-tools-cli \
    python3-gi python3-gi-cairo \
    openssl
```

| Package | Purpose |
|---------|---------|
| `gir1.2-aravis-0.8`, `aravis-tools-cli` | USB3 Vision / GigE camera support |
| `python3-gi`, `python3-gi-cairo` | GObject bindings used by Aravis |
| `python3-opencv`, `python3-numpy` | Image processing |
| `openssl` | HTTPS certificate generation |

> The Aravis packages (`gir1.2-aravis-0.8`, `aravis-tools-cli`, `python3-gi*`)
> are only needed for **USB3 Vision / GigE** cameras. If you will only use a UVC
> webcam or the built-in virtual camera, you can skip them.

---

## 4. Python Virtual Environment & Package Sharing

Budgie runs inside a **Python virtual environment** (venv) — an isolated Python
setup that keeps Budgie's pip packages separate from the system Python.

The Aravis bindings installed in Section 3 live in the **system** Python
site-packages and cannot be reinstalled with `pip`. To let the virtual
environment *see* them, create the venv with the **`--system-site-packages`**
flag — this is the "package sharing" that makes the camera bindings visible
inside the venv:

```bash
cd ~/budgie            # the folder that contains run.py and budgie/
python3 -m venv --system-site-packages venv
source venv/bin/activate
pip install flask flask-socketio
```

> Without `--system-site-packages`, the venv is isolated and Budgie will report
> that the Aravis driver is unavailable, even though the system packages are
> installed.

Activate the environment (`source venv/bin/activate`) in every new terminal
before running Budgie.

---

## 5. USB3 Vision Camera Setup (optional, one-time)

> **This section is only for USB3 Vision / GigE industrial cameras.** Standard
> UVC webcams and the built-in virtual camera need no setup here — skip straight
> to Section 6. Do this only if you have a U3V/GigE camera.

By default, USB devices are owned by `root`, so a normal user cannot open the
camera. Grant access with a udev rule for your camera's USB **Vendor ID**.

Find the Vendor ID (the `idVendor` field, first four hex digits) with `lsusb`.
The example below uses Hikvision's ID `2bdf` — **replace it with your camera's**:

```bash
sudo bash -c 'cat > /etc/udev/rules.d/99-u3vcam.rules << EOF
SUBSYSTEM=="usb", ATTRS{idVendor}=="2bdf", MODE="0666", GROUP="plugdev"
EOF'

sudo udevadm control --reload-rules
sudo udevadm trigger
sudo usermod -aG plugdev $USER
```

> The group change takes effect after you **log out and back in** (or reboot).

To verify the camera is detected by Aravis before starting Budgie:

```bash
arv-tool-0.8                 # lists discovered U3V/GigE cameras
```

---

## 6. Running the Server

```bash
cd ~/budgie
source venv/bin/activate
python run.py
```

On first start, Budgie generates a self-signed HTTPS certificate automatically.
Because it is self-signed, your browser will show a warning the **first** time
you connect — choose **Advanced → Continue** to proceed. This is expected for a
private LAN server.

> HTTPS is mandatory. If OpenSSL is missing or the certificate cannot be
> written, the server refuses to start.

### Accessing from other devices

1. Find the server's IP address:
   ```bash
   hostname -I
   ```
2. From any device on the same network, open:
   ```
   https://<server-ip>:45221
   ```
3. Log in with an account from the configuration (Section 7).

---

## 7. Configuration (`budgie/config.py`)

All settings live in `budgie/config.py`. Edit, save, and restart the server.

### Accounts and roles

```python
WEB_USERS = [
    ("admin",  "your_password", True),    # Administrator: full camera control
    ("viewer", "your_password", False),   # Viewer: stream only
]
```

Add as many accounts as you like. `True` = administrator, `False` = viewer.
Pick strong passwords before deploying on a shared network.

### Network ports

```python
WEB_PORT      = 45221    # HTTPS port the interface listens on
WEB_HTTP_PORT = WEB_PORT - 1   # plain-HTTP port that redirects to HTTPS
```

### Login session length

```python
SESSION_TTL_SECONDS = 8 * 3600   # how long a login stays valid (default 8 hours)
```

### Failed-login lockout

```python
FAIL_MAX_ATTEMPTS = 3    # an IP is blocked after this many failed logins
```

> To unblock an address: open the administrator interface, review the security
> notifications panel, and clear the entry from there.

### Streaming quality

```python
STREAM_JPEG_Q  = 85      # stream JPEG quality (0–100; does not affect recordings)
STREAM_FPS     = 30      # maximum frames per second sent to each viewer
ADAPTIVE_STREAM = True   # scale each viewer's frames to their window size (saves bandwidth)
```

### Camera tuning (advanced)

```python
CAM_JOIN_TIMEOUT  = 2          # seconds to wait for a camera thread to stop
STREAM_PREBUF     = 10         # acquisition pre-buffer depth
BUF_TIMEOUT_US    = 1_000_000  # per-frame acquisition timeout (microseconds)
FPS_SAMPLE_FRAMES = 30         # frames used to measure capture FPS
```

---

## 8. Cameras and Drivers

Budgie auto-detects connected cameras at startup and in the admin **Scan**
action. Supported camera types:

| Driver | Camera type |
|--------|-------------|
| Aravis | USB3 Vision / GigE Vision industrial cameras |
| UVC    | Standard USB webcams |
| Raspberry Pi | Pi Camera modules (on Pi hardware) |
| Virtual | Built-in test camera — no hardware required |

The **virtual camera** is useful for trying Budgie before you have hardware. It
appears in the camera list as `virtual://0`, `virtual://1`, … and streams a
static colour-bar image.

### Automatic camera open / close

To avoid keeping camera hardware busy when nobody is watching, Budgie manages
cameras automatically:

- **No viewers → auto-close.** When the last viewer disconnects, open cameras
  that are not doing background work are closed automatically, releasing the
  hardware.
- **A viewer returns → auto-reopen.** When someone connects again, any camera
  that was auto-closed is reopened, so the live view comes back without admin
  action.

A plugin can **override auto-close** to keep a camera running even with no
viewers — for example, a recorder that must keep capturing. See Section 9.2,
*Keeping a camera open*.

---

## 9. Extending Budgie — Plugin & Driver Development

Budgie is built so that both **plugins** (features) and **drivers** (camera
backends) are *drop-in*: you add a folder or a file and the application
discovers it at startup — no edits to the main program are required.

### 9.1 How the main program works (the parts you hook into)

Understanding the frame pipeline makes both plugins and drivers easy to write.

```
 Camera hardware
      │  (acquisition thread, per camera)
      ▼
 Driver.on_frame(frame, hw_ts_ns)         ← your DRIVER produces frames here
      │
      ▼
 CameraRegistry  ── stores latest raw frame, wakes the pipeline thread
      │
      ▼
 Pipeline thread ── runs each plugin's on_frame() in order ──► display frame
      │                    ▲  your PLUGIN can modify the frame here
      ▼
 StreamManager  ── samples the latest display frame at STREAM_FPS,
                   encodes JPEG, pushes "frame" events to each viewer
```

#### Pipeline (P) vs. Display (D) mode

Each plugin runs its `on_frame()` in one of two modes, set per camera and
toggleable from the admin UI. The pipeline thread runs the plugins in **two
passes**:

1. **Pipeline mode (P)** — runs first. These plugins form a chain: each one's
   output replaces the **shared frame** passed to the next. This frame is also
   what gets **recorded and photographed**. So a P-mode change is *baked in*
   everywhere — downstream plugins, the recording, and the live view all see it.

2. **Display mode (D)** — runs second, starting from a **copy** of the final
   pipeline frame. D-mode changes reach **only the live stream** sent to
   browsers; the recorded/saved frame is left untouched.

```
 raw frame ─► [P plugin] ─► [P plugin] ─► pipeline frame ─► recording / photo
                                              │ (copy)
                                              ▼
                                       [D plugin] ─► [D plugin] ─► display frame ─► viewers
```

Example: an overlay-text plugin in **P** mode burns the text into your
recordings; the same plugin in **D** mode shows the text in the live view only,
keeping recordings clean. Choose P when the effect must be permanent, D when it
is just an on-screen aid.

Alongside the frame path, the main program offers these touchpoints:

| Touchpoint | What it does |
|------------|--------------|
| **Event bus** (`bus.py`) | Decoupled notifications: `camera.opened`, `camera.pre_close`, `camera.closed`, `camera.disconnected`, `viewer.zero`, `plugin.idle`. Modules talk through events, never by importing `app.py`. |
| **State snapshot** | Built per camera and broadcast to viewers; plugins add fields via `get_state()`. |
| **Frame payload** | Per-frame metadata sent with each JPEG; plugins add fields via `frame_payload()`. |
| **Action / param dispatch** | The UI sends a named action or a parameter change; the main program routes it to every plugin's `handle_action()` / `handle_set_param()` until one consumes it. |
| **Busy guard / auto-close** | When the last viewer leaves, idle cameras auto-close. A plugin can keep a camera alive by returning `True` from `is_busy()` (or listing cam_ids in `held_cam_ids()`), and signal it has finished with `self._mark_idle(cam_id)`. |
| **Runtime handles** | At startup each plugin receives `sio` (SocketIO), `emit_state` (re-broadcast state), and the camera registry, so it can push updates and read camera state. |

### 9.2 Writing a plugin

A plugin lives in its own folder under `budgie/plugins/`:

```
budgie/plugins/myfeature/
├── __init__.py        # empty
├── plugin.py          # metadata export (discovered by the registry)
├── myfeature.py       # your PluginBase subclass
├── defaults.py        # optional constants
├── ui.html            # optional: control panel injected into the admin UI
└── ui.js              # optional: client-side behaviour for the panel
```

**`plugin.py`** exports the metadata the registry scans for:

```python
from .myfeature import MyFeature

PLUGIN_CLASS       = MyFeature
PLUGIN_NAME        = "MyFeature"
PLUGIN_VERSION     = "1.0.0"
PLUGIN_DESCRIPTION = "What this plugin does"
# PLUGIN_ALLOW_MULTIPLE = True   # optional: allow multiple instances per camera
```

**The implementation** subclasses `PluginBase` and overrides only the hooks it
needs. Every hook has a safe default, so a minimal plugin is tiny:

```python
import numpy as np
from ..base import PluginBase

class MyFeature(PluginBase):
    @property
    def name(self) -> str:
        return "MyFeature"

    # Modify or inspect every frame (runs in the acquisition thread — keep it fast)
    def on_frame(self, frame: np.ndarray, hw_ts_ns: int, cam_id: str = ""):
        return None            # return a new frame, or None to pass through

    # Handle a button/action coming from the UI
    def handle_action(self, action: str, data: dict, driver):
        if action == "do_something":
            # ... perform work ...
            return True, "Done"
        return None            # not ours — let the next plugin try
```

Hooks available (override what you need):

| Hook | Purpose |
|------|---------|
| `on_load` / `on_unload` | Plugin registered / removed |
| `on_camera_open` / `on_camera_close` | A camera opened / is about to close |
| `on_frame` | Inspect or modify each captured frame |
| `get_state` | Add fields to the camera state snapshot |
| `frame_payload` | Add fields to each frame's metadata |
| `handle_action` | Respond to a named UI action; return `(ok, msg)` or `None` |
| `handle_set_param` | Respond to a parameter change; return `True` if handled |
| `is_busy` / `held_cam_ids` | Protect a camera from auto-close while working |
| `register_routes` | Add plugin-specific Flask HTTP routes |
| `render_ui` / `ui_js_url` | Provide `ui.html` / `ui.js` (auto-used if present) |

**Local vs. global plugins:** a *local* plugin gets one instance per camera
(most plugins — recording, photo, overlay). A *global* / cross-camera plugin
(e.g. multi-view) handles several cameras at once and uses the `cam_id`
argument and `held_cam_ids()` to declare which cameras it depends on.

#### Keeping a camera open (overriding auto-close)

By default a camera is auto-closed once the last viewer leaves (Section 8). A
plugin that must keep capturing — a recorder, a time-lapse, a motion watcher —
tells Budgie it is busy so the camera stays open:

```python
class MyRecorder(PluginBase):
    def __init__(self):
        self._recording = False

    @property
    def name(self) -> str:
        return "MyRecorder"

    # While this returns True, the camera will NOT be auto-closed,
    # even with zero viewers online.
    def is_busy(self, cam_id: str = "") -> bool:
        return self._recording

    def handle_action(self, action, data, driver):
        if action == "start":
            self._recording = True
            return True, "Recording"
        if action == "stop":
            self._recording = False
            # Tell the system we are done; the camera may now auto-close
            # if no viewers remain.
            self._mark_idle(data.get("cam_id", ""))
            return True, "Stopped"
        return None
```

- **`is_busy()` → `True`** keeps the bound camera open while the work runs.
- **`self._mark_idle(cam_id)`** signals the work finished, allowing auto-close to
  proceed when no viewers are left.
- A **cross-camera** plugin that needs *other* cameras' frames (e.g. multi-view)
  returns those cam_ids from **`held_cam_ids()`** so they are protected too.

Drop the folder in, restart the server, and the plugin appears in the admin
plugin list — no other file changes needed.

### 9.3 Writing a driver

A driver is a single file `budgie/drivers/<name>_driver.py` containing exactly
one subclass of `CameraDriver`. The registry auto-discovers any file matching
`*_driver.py`; remove the file and the driver cleanly disappears.

A driver **is a thread**: `start()` begins the acquisition loop, which calls
`self.on_frame(frame, hw_ts_ns)` for each captured frame.

```python
import numpy as np
from .base import CameraDriver

class MyCamDriver(CameraDriver):
    SUPPORTED_PARAMS = frozenset({"exposure", "gain", "fps"})
    DEFAULT_PARAMS   = {"exposure": 10000, "gain": 0, "fps": 30}

    @staticmethod
    def scan_devices() -> list:
        # Return [{"device_id","model","serial","label"}, ...] or []
        return []

    def open(self, device_id=None) -> dict:
        # Configure hardware; return {model, serial, width, height,
        # exp_min, exp_max, gain_min, gain_max, fps_min, fps_max}
        ...

    def run(self):
        # Acquisition loop: capture frames and forward them
        while self.is_running:
            frame = ...                      # np.ndarray, BGR uint8
            if self.on_frame:
                self.on_frame(frame, hw_ts_ns)

    def close(self): ...
    def read_hw_bounds(self) -> dict: ...
    def set_param(self, key, value): ...
    # required read-back properties:
    #   latest_frame, cap_fps, current_gain, current_exposure, is_running
```

Required (`@abstractmethod`) members: `open`, `close`, `read_hw_bounds`,
`set_param`, and the read-back properties `latest_frame`, `cap_fps`,
`current_gain`, `current_exposure`, `is_running`. Provide `scan_devices()` so
the camera appears in the scan list.

Optional capabilities:

| Override | Adds |
|----------|------|
| `query_native_modes` | Expose hardware resolution/FPS modes to the UI |
| `supports_audio` + `audio_start` / `audio_stop` | Audio capture for the recording plugin |
| `latest_raw_frame` + `raw_frame_format` | Native pixel data (e.g. `Z16`, `MONO8`, `BayerRG8`) for plugins that need it |

`SUPPORTED_PARAMS` tells the UI which camera controls to show; `DEFAULT_PARAMS`
seeds safe initial values for your hardware. Drop the file in, restart, and the
new camera type is scanned automatically.

> Existing drivers (`aravis_driver.py`, `uvc_driver.py`, `virtual_driver.py`,
> the Raspberry Pi drivers) are the best worked examples — the **virtual
> driver** is the simplest and a good template to copy.

---

## 10. Troubleshooting

| Symptom | Check |
|---------|-------|
| Camera not listed | Run `lsusb`, then `arv-tool-0.8`. Confirm the udev rule and that the camera is on a USB 3 port (Section 5). |
| "Aravis driver unavailable" | The venv was created without `--system-site-packages` (Section 4). Recreate it. |
| Permission denied on camera | udev rule missing, or you have not logged out/in after `usermod -aG plugdev`. |
| Browser refuses to connect | Use `https://` (not `http://`) and accept the self-signed certificate on first visit. |
| Other devices can't reach the server | Confirm they are on the same network and the firewall allows the configured port. |
| Stutter / lag with U3V camera | Use a **USB 3** port (blue) and cable; USB 2 lacks the bandwidth. |

---

*Budgie — multi-camera LAN web interface.*
