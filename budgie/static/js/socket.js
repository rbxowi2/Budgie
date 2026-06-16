/* socket.js — Core socket connection, state/frame handlers, camera actions */

const socket = io({ transports: ['websocket'] });

// ── Global state (shared with mdi.js, plugin-ui.js, health.js) ───────────
let _selectedCamId       = "";
let _stateCache          = null;
let _openCamIds          = new Set();
const _mobileCamSelectedAt = {};   // cam_id → Date.now() when last selected (health grace period)

// ── Connection ────────────────────────────────────────────────────────────
socket.on("connect", () => {
  document.getElementById("conn-warning").classList.remove("visible");
  setStatus("Connected");
});
socket.on("disconnect", () => {
  document.getElementById("conn-warning").classList.add("visible");
  setStatus("Disconnected");
});
socket.on("viewer_count", (n) => {
  document.getElementById("viewer-count").textContent = n;
});
socket.on("status", (data) => setStatus(data.msg));

// ── State ─────────────────────────────────────────────────────────────────
socket.on("state", (s) => {
  _stateCache = s;
  const _prevCamId = _selectedCamId;
  window._selectedCamId = s.selected_cam_id || "";
  _selectedCamId = window._selectedCamId;
  if (_prevCamId !== _selectedCamId) {
    mobileStreamClear();
    if (_selectedCamId) _mobileCamSelectedAt[_selectedCamId] = Date.now();
    requestAnimationFrame(() => _emitStreamSize(_selectedCamId));
  }

  const newOpenIds = new Set(Object.keys(s.cameras || {}));

  newOpenIds.forEach(cam_id => {
    if (!_openCamIds.has(cam_id)) {
      const ci = (s.cameras[cam_id] || {}).cam_info || {};
      addMdiWindow(cam_id, ci.model || cam_id);
    }
  });
  _openCamIds.forEach(cam_id => {
    if (!newOpenIds.has(cam_id)) {
      removeMdiWindow(cam_id);
      cleanupLocalPluginsUI(cam_id);
    }
  });
  _openCamIds = newOpenIds;

  // Emit immediately after DOM is updated — eliminates the rAF 16ms delay that causes
  // late-joining viewers to receive full-resolution frames before set_stream_size arrives.
  if (_selectedCamId) _emitStreamSize(_selectedCamId);

  document.querySelectorAll(".mdi-win").forEach(w => {
    w.classList.toggle("selected", w.dataset.cam === _selectedCamId);
  });

  updateCamInfo(s);
  updateCameraDropdown(s);
  syncPluginArea(s);

  document.getElementById("viewer-count").textContent = s.viewer_count;
  if (s.pending_notifications !== undefined) {
    renderNotifications(s.pending_notifications);
  }
  _syncMobileStreamPause();
});

// ── Frame (rAF-buffered) ──────────────────────────────────────────────────
// _pendingFrames keeps only the latest frame per cam_id (overwrite on arrival);
// each rAF tick renders it.  Server-side backpressure (streaming.py) bounds how
// many frames can be in flight, so no stale backlog reaches us to begin with.
const _pendingFrames = {};
let   _rafPending    = false;

function _flushPendingFrames() {
  _rafPending = false;
  for (const cam_id in _pendingFrames) {
    const data = _pendingFrames[cam_id];
    delete _pendingFrames[cam_id];
    _applyFrame(data);
  }
}

// 6.3: Create a blob URL from binary JPEG, revoke the previous one
function _setImgBinary(img, imgData) {
  const prev = img._blobUrl;
  const url  = URL.createObjectURL(new Blob([imgData], { type: "image/jpeg" }));
  img.src      = url;
  img._blobUrl = url;
  if (prev) URL.revokeObjectURL(prev);
}

function _applyFrame(data) {
  // lastFrameTime is declared in health.js
  if (typeof lastFrameTime !== "undefined") lastFrameTime[data.cam_id] = Date.now();

  // data.img is ArrayBuffer (binary, socket.io 4.x) or string (base64 fallback)
  const isBinary = data.img instanceof ArrayBuffer || ArrayBuffer.isView(data.img);

  const win = document.querySelector(`.mdi-win[data-cam="${data.cam_id}"]`);
  if (win) {
    const img = win.querySelector(".mdi-stream");
    const ph  = win.querySelector(".mdi-placeholder");
    if (isBinary) _setImgBinary(img, data.img);
    else img.src = "data:image/jpeg;base64," + data.img;
    img.style.display = "block";
    ph.style.display  = "none";
    const cw = win.querySelector(".mdi-cam-warn");
    if (cw) cw.classList.remove("visible");
    win.querySelector(".mdi-fps").textContent = data.cap_fps + " fps";
    win.querySelector(".mdi-recbadge").style.display =
      (data.recording || data.buf_rec) ? "block" : "none";
  }

  if (data.cam_id === _selectedCamId) {
    document.getElementById("info-capfps").textContent = data.cap_fps + " fps";
    const msi = document.getElementById("mobile-stream-img");
    if (msi) {
      if (isBinary) _setImgBinary(msi, data.img);
      else msi.src = "data:image/jpeg;base64," + data.img;
      msi.style.display = "block";
      const ph = document.getElementById("mobile-stream-placeholder");
      if (ph) ph.style.display = "none";
    }
    const mfps = document.getElementById("mobile-stream-fps");
    if (mfps) mfps.textContent = data.cap_fps + " fps";
    const mrec = document.getElementById("mobile-recbadge");
    if (mrec) mrec.style.display = (data.recording || data.buf_rec) ? "block" : "none";
    const mw = document.getElementById("mobile-cam-warn");
    if (mw) mw.classList.remove("visible");

  }
}

socket.on("frame", (data) => {
  _pendingFrames[data.cam_id] = data;
  if (!_rafPending) {
    _rafPending = true;
    requestAnimationFrame(_flushPendingFrames);
  }
});

// ── Camera info panel ─────────────────────────────────────────────────────
function updateCamInfo(s) {
  const camData = s.selected_cam_id && s.cameras ? s.cameras[s.selected_cam_id] : null;
  const ci = camData ? (camData.cam_info || {}) : null;
  if (ci && ci.model) {
    document.getElementById("info-model").textContent  = ci.model  || "—";
    document.getElementById("info-serial").textContent = ci.serial || "—";
    document.getElementById("info-res").textContent    = (ci.width || "—") + " × " + (ci.height || "—");
    document.getElementById("info-capfps").textContent = camData.cap_fps ? camData.cap_fps + " fps" : "—";
  } else {
    ["info-model","info-serial","info-res","info-capfps"]
      .forEach(id => document.getElementById(id).textContent = "—");
  }
}

// ── Camera dropdown ───────────────────────────────────────────────────────
const DRIVER_BADGE = { "AravisDriver": "U3V", "UVCDriver": "UVC", "VirtualCameraDriver": "VIRT" };
let _suppressCamChange = false;

function updateCameraDropdown(s) {
  const sel   = document.getElementById("sel-camera");
  const avail = s.available_cameras || [];
  const openIds = new Set(Object.keys(s.cameras || {}));
  const selId   = s.selected_cam_id || "";

  _suppressCamChange = true;
  sel.innerHTML = "";
  if (avail.length === 0) {
    sel.innerHTML = '<option value="">No camera found</option>';
  } else {
    avail.forEach(c => {
      const opt    = document.createElement("option");
      opt.value    = c.device_id;
      const isOpen = openIds.has(c.device_id);
      const badge  = DRIVER_BADGE[c.driver] ? "[" + DRIVER_BADGE[c.driver] + "] " : "";
      opt.textContent = (isOpen ? "● " : "") + badge + c.label;
      if (isOpen) opt.style.color = "#3a7bd5";
      if (c.device_id === selId) opt.selected = true;
      sel.appendChild(opt);
    });
  }
  _suppressCamChange = false;
}

function onDropdownCameraChange(sel) {
  if (_suppressCamChange) return;
  if (sel.value) selectCamera(sel.value);
}

// ── Camera actions ────────────────────────────────────────────────────────
function scanCameras() {
  document.getElementById("btn-scan").classList.add("scanning");
  socket.emit("scan_cameras");
  setTimeout(() => document.getElementById("btn-scan").classList.remove("scanning"), 1500);
}

function onOpenCamera() {
  const cam_id = document.getElementById("sel-camera").value;
  if (cam_id) socket.emit("open_camera", { cam_id });
  else setStatus("Select a camera first");
}

function onCloseCamera() {
  const cam_id = document.getElementById("sel-camera").value || _selectedCamId;
  if (cam_id) socket.emit("close_camera", { cam_id });
}

function onCloseAll() {
  if (!_openCamIds.size) { setStatus("No cameras open"); return; }
  socket.emit("close_all_cameras");
}

function selectCamera(cam_id) {
  if (_selectedCamId === cam_id) return;
  socket.emit("select_camera", { cam_id });
  if (_isMobile()) {
    _openCamIds.forEach(id => {
      socket.emit("set_stream_paused", { cam_id: id, paused: id !== cam_id });
    });
  }
}

// ── Adaptive stream size ──────────────────────────────────────────────────
function _emitStreamSize(cam_id) {
  if (!cam_id) return;
  const win = document.querySelector(`.mdi-win[data-cam="${CSS.escape(cam_id)}"]`);
  if (win && win.offsetWidth > 0) {
    const content = win.querySelector(".mdi-content");
    if (content && content.offsetWidth > 0) {
      socket.emit("set_stream_size", { cam_id, w: content.offsetWidth, h: content.offsetHeight });
      return;
    }
  }
  if (cam_id === _selectedCamId) {
    const msa = document.getElementById("mobile-stream-area");
    if (msa && msa.offsetWidth > 0) {
      socket.emit("set_stream_size", { cam_id, w: msa.offsetWidth, h: msa.offsetHeight });
      return;
    }
    // DOM not yet laid out — use viewport as initial estimate for server-side scaling
    if (_isMobile()) {
      socket.emit("set_stream_size", { cam_id, w: window.innerWidth, h: window.innerHeight });
    }
  }
}

// ── Mobile helpers ────────────────────────────────────────────────────────
function _isMobile() {
  const ws = document.getElementById("workspace");
  return ws && ws.offsetParent === null;
}

function _syncMobileStreamPause() {
  if (!_isMobile()) {
    _openCamIds.forEach(id => {
      if (typeof _mdiMinState === "undefined" || !_mdiMinState[id]) {
        socket.emit("set_stream_paused", { cam_id: id, paused: false });
      }
    });
    return;
  }
  _openCamIds.forEach(id => {
    socket.emit("set_stream_paused", { cam_id: id, paused: id !== _selectedCamId });
  });
}

function mobileStreamClear() {
  const img = document.getElementById("mobile-stream-img");
  const ph  = document.getElementById("mobile-stream-placeholder");
  if (img) {
    if (img._blobUrl) { URL.revokeObjectURL(img._blobUrl); img._blobUrl = null; }
    img.src = ""; img.style.display = "none";
  }
  if (ph)  ph.style.display = "";
  const fps = document.getElementById("mobile-stream-fps");
  if (fps) fps.textContent = "";
  const rec = document.getElementById("mobile-recbadge");
  if (rec) rec.style.display = "none";
}

window.addEventListener("resize", () => {
  if (typeof _layoutMinimizedWindows === "function") _layoutMinimizedWindows();
  _openCamIds.forEach(cam_id => _emitStreamSize(cam_id));
  _syncMobileStreamPause();
});

// ── Utilities ─────────────────────────────────────────────────────────────
function setStatus(msg) {
  document.getElementById("status-bar").textContent = msg;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
