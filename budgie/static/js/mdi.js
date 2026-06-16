/* mdi.js — MDI window management: add/remove, drag, resize, minimize/maximize */

const MDI_W = 480, MDI_H = 360;
let _mdiCounter = 0;
const _mdiMaxState = {};
const _mdiMinState = {};   // cam_id → true when minimized
const _mdiMinSaved = {};   // cam_id → {left, top, w, h}
const _MDI_MIN_W   = 220;  // compact width of a minimized titlebar

function addMdiWindow(cam_id, label) {
  if (document.querySelector(`.mdi-win[data-cam="${CSS.escape(cam_id)}"]`)) return;

  const ws  = document.getElementById("workspace");
  const off = (_mdiCounter % 8) * 22;
  const x   = Math.min(16 + off, Math.max(0, ws.clientWidth  - MDI_W - 16));
  const y   = Math.min(16 + off, Math.max(0, ws.clientHeight - MDI_H - 16));
  _mdiCounter++;

  const win = document.createElement("div");
  win.className   = "mdi-win";
  win.dataset.cam = cam_id;
  win.style.left   = x + "px";
  win.style.top    = y + "px";
  win.style.width  = MDI_W + "px";
  win.style.height = MDI_H + "px";

  const safeId   = escapeHtml(cam_id);
  const closeBtn = _isAdmin
    ? `<button class="mdi-btn close-btn" title="Close" onclick="socket.emit('close_camera',{cam_id:'${safeId}'})">&#10005;</button>`
    : "";
  win.innerHTML =
    `<div class="mdi-titlebar">
       <span class="mdi-label">${escapeHtml(label)}</span>
       <div class="mdi-btns">
         <button class="mdi-btn" title="Reset"    onclick="mdiReset('${safeId}')">&#8633;</button>
         <button class="mdi-btn min-btn" title="Minimize" onclick="mdiToggleMin('${safeId}')">&#8722;</button>
         <button class="mdi-btn max-btn" title="Maximize" onclick="mdiToggleMax('${safeId}')">&#9723;</button>
         ${closeBtn}
       </div>
     </div>
     <div class="mdi-content">
       <div class="mdi-placeholder">Waiting for stream...</div>
       <img class="mdi-stream" alt="stream">
       <div class="mdi-recbadge">REC</div>
       <div class="mdi-fps"></div>
       <div class="mdi-cam-warn">Camera not responding</div>
     </div>
     <div class="mdi-resize"></div>`;

  const tb = win.querySelector(".mdi-titlebar");
  tb.addEventListener("mousedown", (e) => {
    if (e.target.closest(".mdi-btns")) return;
    mdiStartDrag(e, win);
    selectCamera(cam_id);
  });
  tb.addEventListener("touchstart", (e) => {
    if (e.target.closest(".mdi-btns")) return;
    mdiStartDragTouch(e.touches[0], win);
    selectCamera(cam_id);
  }, { passive: true });

  win.querySelector(".mdi-content").addEventListener("click", () => selectCamera(cam_id));

  win.querySelector(".mdi-resize").addEventListener("mousedown", (e) => {
    mdiStartResize(e, win); e.stopPropagation();
  });
  win.querySelector(".mdi-resize").addEventListener("touchstart", (e) => {
    mdiStartResizeTouch(e.touches[0], win); e.stopPropagation();
  }, { passive: true });

  ws.appendChild(win);
  selectCamera(cam_id);
  requestAnimationFrame(() => _emitStreamSize(cam_id));
}

function removeMdiWindow(cam_id) {
  const win = document.querySelector(`.mdi-win[data-cam="${CSS.escape(cam_id)}"]`);
  if (win) {
    // Revoke any outstanding blob URLs before tearing down the window
    win.querySelectorAll("img").forEach(img => {
      if (img._blobUrl) { URL.revokeObjectURL(img._blobUrl); img._blobUrl = null; }
    });
    win.remove();
  }
  delete _mdiMaxState[cam_id];
  delete _mdiMinState[cam_id];
  delete _mdiMinSaved[cam_id];
  if (typeof lastFrameTime !== "undefined") delete lastFrameTime[cam_id];
  _layoutMinimizedWindows();
}

function mdiReset(cam_id) {
  const win = document.querySelector(`.mdi-win[data-cam="${CSS.escape(cam_id)}"]`);
  if (!win) return;
  if (_mdiMinState[cam_id]) mdiToggleMin(cam_id);
  delete _mdiMaxState[cam_id];
  win.querySelector(".max-btn").textContent = "⧃";
  win.classList.remove("minimized");
  win.style.left = "16px"; win.style.top = "16px";
  win.style.width = MDI_W + "px"; win.style.height = MDI_H + "px";
  requestAnimationFrame(() => _emitStreamSize(cam_id));
}

function mdiToggleMax(cam_id) {
  const win = document.querySelector(`.mdi-win[data-cam="${CSS.escape(cam_id)}"]`);
  if (!win) return;
  if (_mdiMinState[cam_id]) mdiToggleMin(cam_id);
  const ws  = document.getElementById("workspace");
  const btn = win.querySelector(".max-btn");
  if (_mdiMaxState[cam_id]) {
    const s = _mdiMaxState[cam_id];
    win.style.left = s.left; win.style.top  = s.top;
    win.style.width = s.w;  win.style.height = s.h;
    delete _mdiMaxState[cam_id];
    btn.innerHTML = "&#9723;";
  } else {
    _mdiMaxState[cam_id] = {
      left: win.style.left, top: win.style.top,
      w: win.style.width,   h: win.style.height,
    };
    win.style.left = "0"; win.style.top = "0";
    win.style.width  = ws.clientWidth  + "px";
    win.style.height = ws.clientHeight + "px";
    btn.innerHTML = "&#9724;";
  }
  requestAnimationFrame(() => _emitStreamSize(cam_id));
}

function _layoutMinimizedWindows() {
  const ws = document.getElementById("workspace");
  if (!ws) return;
  const wsH = ws.clientHeight;
  let x = 4;
  document.querySelectorAll(".mdi-win.minimized").forEach(win => {
    win.style.left  = x + "px";
    win.style.top   = (wsH - 32) + "px";
    win.style.width = _MDI_MIN_W + "px";
    x += _MDI_MIN_W + 4;
  });
}

function mdiToggleMin(cam_id) {
  const win = document.querySelector(`.mdi-win[data-cam="${CSS.escape(cam_id)}"]`);
  if (!win) return;
  const btn = win.querySelector(".min-btn");
  if (_mdiMinState[cam_id]) {
    const saved = _mdiMinSaved[cam_id];
    if (saved) {
      win.style.left   = saved.left;
      win.style.top    = saved.top;
      win.style.width  = saved.w;
      win.style.height = saved.h;
    }
    delete _mdiMinState[cam_id];
    delete _mdiMinSaved[cam_id];
    win.classList.remove("minimized");
    if (btn) btn.innerHTML = "&#8722;";
    socket.emit("set_stream_paused", { cam_id, paused: false });
    requestAnimationFrame(() => _emitStreamSize(cam_id));
    _layoutMinimizedWindows();
  } else {
    _mdiMinSaved[cam_id] = {
      left: win.style.left, top: win.style.top,
      w: win.style.width,   h: win.style.height,
    };
    _mdiMinState[cam_id] = true;
    win.classList.add("minimized");
    if (btn) btn.innerHTML = "&#9723;";
    socket.emit("set_stream_paused", { cam_id, paused: true });
    _layoutMinimizedWindows();
  }
}

// ── Mouse drag / resize ───────────────────────────────────────────────────
let _mdiDrag = null, _mdiResize = null;

function mdiStartDrag(e, win) {
  e.preventDefault();
  const r  = win.getBoundingClientRect();
  const wr = document.getElementById("workspace").getBoundingClientRect();
  _mdiDrag = { win, dx: e.clientX - r.left, dy: e.clientY - r.top, wr };
}

function mdiStartResize(e, win) {
  e.preventDefault();
  const r = win.getBoundingClientRect();
  _mdiResize = { win, sx: e.clientX, sy: e.clientY, sw: r.width, sh: r.height };
}

document.addEventListener("mousemove", (e) => {
  if (_mdiDrag) {
    const {win, dx, dy, wr} = _mdiDrag;
    win.style.left = Math.max(0, Math.min(e.clientX - dx, wr.width  - 40)) + "px";
    win.style.top  = Math.max(0, Math.min(e.clientY - dy, wr.height - 40)) + "px";
  }
  if (_mdiResize) {
    const {win, sx, sy, sw, sh} = _mdiResize;
    win.style.width  = Math.max(220, sw + e.clientX - sx) + "px";
    win.style.height = Math.max(160, sh + e.clientY - sy) + "px";
  }
});

document.addEventListener("mouseup", () => {
  if (_mdiResize) _emitStreamSize(_mdiResize.win.dataset.cam);
  _mdiDrag = null; _mdiResize = null;
});

// ── Touch drag / resize ───────────────────────────────────────────────────
function mdiStartDragTouch(touch, win) {
  const r  = win.getBoundingClientRect();
  const wr = document.getElementById("workspace").getBoundingClientRect();
  _mdiDrag = { win, dx: touch.clientX - r.left, dy: touch.clientY - r.top, wr };
}

function mdiStartResizeTouch(touch, win) {
  const r = win.getBoundingClientRect();
  _mdiResize = { win, sx: touch.clientX, sy: touch.clientY, sw: r.width, sh: r.height };
}

document.addEventListener("touchmove", (e) => {
  if (!_mdiDrag && !_mdiResize) return;
  e.preventDefault();
  const t = e.touches[0];
  if (_mdiDrag) {
    const {win, dx, dy, wr} = _mdiDrag;
    win.style.left = Math.max(0, Math.min(t.clientX - dx, wr.width  - 40)) + "px";
    win.style.top  = Math.max(0, Math.min(t.clientY - dy, wr.height - 40)) + "px";
  }
  if (_mdiResize) {
    const {win, sx, sy, sw, sh} = _mdiResize;
    win.style.width  = Math.max(220, sw + t.clientX - sx) + "px";
    win.style.height = Math.max(160, sh + t.clientY - sy) + "px";
  }
}, { passive: false });

document.addEventListener("touchend",    () => {
  if (_mdiResize) _emitStreamSize(_mdiResize.win.dataset.cam);
  _mdiDrag = null; _mdiResize = null;
});
document.addEventListener("touchcancel", () => { _mdiDrag = null; _mdiResize = null; });

// ── Mobile sidebar drawer ─────────────────────────────────────────────────
function toggleMobileSidebar() {
  const sb   = document.getElementById("sidebar");
  const bd   = document.getElementById("mobile-sidebar-backdrop");
  const open = sb.classList.toggle("mobile-open");
  if (bd) bd.classList.toggle("visible", open);
}

window.addEventListener("orientationchange", () => {
  document.getElementById("sidebar").classList.remove("mobile-open");
  const bd = document.getElementById("mobile-sidebar-backdrop");
  if (bd) bd.classList.remove("visible");
  if (typeof _syncMobileStreamPause === "function") _syncMobileStreamPause();
});
