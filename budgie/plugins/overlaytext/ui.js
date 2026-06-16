// overlaytext/ui.js — OverlayText plugin frontend (1.1.0)
// Depends on: socket (global, defined in index.html)
//
// Multi-camera + multi-instance safety:
//   cam_id      read from closest('.plugin-ui-block').dataset.cam
//   instance_key read from closest('.plugin-ui-block').dataset.instance
// Each block is fully independent.

const _overlayTextTimers = {};   // instance_key → timerHandle

// ── Helpers ───────────────────────────────────────────────────────────────────

function _otBlock(el)      { return el.closest(".plugin-ui-block"); }
function _otCam(el)        { const b = _otBlock(el); return b ? b.dataset.cam      : ""; }
function _otInstance(el)   { const b = _otBlock(el); return b ? (b.dataset.instance || "OverlayText") : "OverlayText"; }

function _otSuffix(instanceKey) {
  return instanceKey === "OverlayText" ? "" : "_" + instanceKey;
}

function _hexToRgb(hex) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return [r, g, b];
}

function _rgbToHex(rgb) {
  return "#" + rgb.map(c => ("0" + Math.max(0, Math.min(255, c)).toString(16)).slice(-2)).join("");
}

// ── Input handlers ────────────────────────────────────────────────────────────

function overlayTextOnInput(el) {
  const cam_id = _otCam(el);
  const ik     = _otInstance(el);
  if (!cam_id) return;
  clearTimeout(_overlayTextTimers[ik]);
  _overlayTextTimers[ik] = setTimeout(() => {
    socket.emit("set_param", { cam_id, key: `overlay_text${_otSuffix(ik)}`, value: el.value });
  }, 300);
}

function overlayTextOnColor(el) {
  const cam_id = _otCam(el);
  const ik     = _otInstance(el);
  if (!cam_id) return;
  socket.emit("set_param", { cam_id, key: `overlay_color${_otSuffix(ik)}`, value: _hexToRgb(el.value) });
}

function overlayTextOnScale(el) {
  const cam_id = _otCam(el);
  const ik     = _otInstance(el);
  if (!cam_id) return;
  const v = parseFloat(el.value);
  const lbl = el.closest(".collapsible-body")?.querySelector(".overlaytext-scale-label");
  if (lbl) lbl.textContent = v === 0 ? "Auto" : v.toFixed(1);
  socket.emit("set_param", { cam_id, key: `overlay_font_scale${_otSuffix(ik)}`, value: v });
}

function overlayTextOnPos(el) {
  const cam_id = _otCam(el);
  const ik     = _otInstance(el);
  if (!cam_id) return;
  socket.emit("set_param", { cam_id, key: `overlay_position${_otSuffix(ik)}`, value: el.value });
}

function overlayTextClear(el) {
  const block  = _otBlock(el);
  if (!block) return;
  const cam_id = block.dataset.cam;
  const ik     = block.dataset.instance || "OverlayText";
  clearTimeout(_overlayTextTimers[ik]);
  const ta = block.querySelector(".overlaytext-input");
  if (ta) ta.value = "";
  if (cam_id) socket.emit("set_param", { cam_id, key: `overlay_text${_otSuffix(ik)}`, value: "" });
}

// ── State sync ────────────────────────────────────────────────────────────────

function _applyOverlayTextState(s) {
  document.querySelectorAll('.plugin-ui-block[data-plugin="OverlayText"]').forEach(block => {
    const cid = block.dataset.cam;
    const ik  = block.dataset.instance || "OverlayText";
    const cs  = (s.cameras && cid) ? s.cameras[cid] : null;
    if (!cs) return;
    const sfx = _otSuffix(ik);

    const ta = block.querySelector(".overlaytext-input");
    if (ta && document.activeElement !== ta)
      ta.value = cs[`overlay_text${sfx}`] ?? "";

    const colorEl = block.querySelector(".overlaytext-color");
    if (colorEl) {
      const c = cs[`overlay_color${sfx}`];
      if (Array.isArray(c)) colorEl.value = _rgbToHex(c);
    }

    const scaleEl = block.querySelector(".overlaytext-scale");
    if (scaleEl && document.activeElement !== scaleEl) {
      const v = cs[`overlay_font_scale${sfx}`] ?? 0;
      scaleEl.value = v;
      const lbl = block.querySelector(".overlaytext-scale-label");
      if (lbl) lbl.textContent = v === 0 ? "Auto" : parseFloat(v).toFixed(1);
    }

    const posEl = block.querySelector(".overlaytext-pos");
    if (posEl) posEl.value = cs[`overlay_position${sfx}`] ?? "center";
  });
}

socket.on("state", _applyOverlayTextState);
window.addEventListener("plugin-state-update", (e) => _applyOverlayTextState(e.detail));
