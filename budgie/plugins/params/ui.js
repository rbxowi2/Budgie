// params/ui.js — Parameters plugin frontend (6.1.0)
// Multi-camera safe: NO getElementById. All queries scoped to .plugin-ui-block.
// cam_id is always read from block.dataset.cam, never from a global variable.

// ── Scoped query helpers ──────────────────────────────────────────────────────

function _blk(el) { return el.closest('.plugin-ui-block'); }
function _q(b, k) { return b ? b.querySelector('[data-id="' + k + '"]') : null; }

// ── Per-block log bounds (stored on DOM element to survive camera switches) ───

function _lb(block, key) {
  if (!block._logBounds) {
    block._logBounds = {
      exposure: { lo: 100, hi: 200000 },
      fps:      { lo: 1,   hi: 1000   },
    };
  }
  return block._logBounds[key];
}
function _setLogBounds(block, key, lo, hi) {
  const b = _lb(block, key);
  b.lo = Math.max(lo, 1);  // lo=0 causes log(0)=NaN in _logToPhys
  b.hi = hi;
}
function _logToPhys(block, key, pos) {
  const { lo, hi } = _lb(block, key);
  return lo * Math.pow(hi / lo, pos / 1000);
}
function _physToLog(block, key, v) {
  const { lo, hi } = _lb(block, key);
  return Math.log(v / lo) / Math.log(hi / lo) * 1000;
}

// ── Display / slider sync (within one block) ──────────────────────────────────

function _syncDisplay(block, key, physVal) {
  const rounded = (key === "gain")
    ? parseFloat(physVal).toFixed(1)
    : Math.round(physVal);
  const ids = {
    exposure: ["exp-display",  "exp-display-m"],
    gain:     ["gain-display", "gain-display-m"],
    fps:      ["fps-display",  "fps-display-m"],
  };
  (ids[key] || []).forEach(id => {
    const el = _q(block, id);
    if (el) el.textContent = rounded;
  });
}

function _syncSliderPos(block, key, physVal) {
  const pos = (key === "gain") ? physVal : _physToLog(block, key, physVal);
  const ids = {
    exposure: ["sl-exp", "sl-exp-m"],
    gain:     ["sl-gain", "sl-gain-m"],
    fps:      ["sl-fps", "sl-fps-m"],
  };
  (ids[key] || []).forEach(id => {
    const el = _q(block, id);
    if (el) el.value = pos;
  });
}

// ── Slider event handlers ─────────────────────────────────────────────────────

function onLogSlider(key, el, isMirror) {
  const block   = _blk(el);
  const cam_id  = block ? block.dataset.cam : "";
  const phys    = _logToPhys(block, key, parseFloat(el.value));
  const rounded = Math.round(phys);
  _syncDisplay(block, key, rounded);
  // Sync desktop ↔ mobile mirror slider within same block
  const mirrorId = isMirror
    ? (key === "exposure" ? "sl-exp"   : "sl-fps")
    : (key === "exposure" ? "sl-exp-m" : "sl-fps-m");
  const mirror = _q(block, mirrorId);
  if (mirror) mirror.value = el.value;
  socket.emit("set_param", { cam_id, key, value: rounded });
}

function onLinearSlider(key, el, isMirror) {
  const block  = _blk(el);
  const cam_id = block ? block.dataset.cam : "";
  const v      = parseFloat(el.value);
  _syncDisplay(block, key, v);
  const mirror = _q(block, isMirror ? "sl-gain" : "sl-gain-m");
  if (mirror) mirror.value = v;
  socket.emit("set_param", { cam_id, key, value: v });
}

function onExpAutoUpper(el) {
  const block  = _blk(el);
  const cam_id = block ? block.dataset.cam : "";
  const expMin = parseFloat(el.min) || 100;
  const expMax = parseFloat(el.max) || 200000;
  let v = Math.round(parseFloat(el.value));
  if (isNaN(v)) return;
  v = Math.max(expMin, Math.min(expMax, v));
  el.value = v;
  // Sync the other input (desktop ↔ mobile)
  const myId     = el.dataset.id;
  const mirrorId = (myId === "inp-exp-auto-upper") ? "inp-exp-auto-upper-m" : "inp-exp-auto-upper";
  const mirror   = _q(block, mirrorId);
  if (mirror) mirror.value = v;
  socket.emit("set_param", { cam_id, key: "exp_auto_upper", value: v });
}

function onExposureAuto(el) {
  const block   = _blk(el);
  const cam_id  = block ? block.dataset.cam : "";
  const checked = el.checked;
  ["chk-exp-auto", "chk-exp-auto-m"].forEach(id => {
    const e = _q(block, id); if (e) e.checked = checked;
  });
  socket.emit("set_param", { cam_id, key: "exposure_auto", value: checked });
}

function onGainAuto(el) {
  const block   = _blk(el);
  const cam_id  = block ? block.dataset.cam : "";
  const checked = el.checked;
  ["chk-gain-auto", "chk-gain-auto-m"].forEach(id => {
    const e = _q(block, id); if (e) e.checked = checked;
  });
  ["sl-gain", "sl-gain-m"].forEach(id => {
    const e = _q(block, id); if (e) e.disabled = checked;
  });
  socket.emit("set_param", { cam_id, key: "gain_auto", value: checked });
}

function applyNativeMode(el) {
  const block  = _blk(el);
  const cam_id = block ? block.dataset.cam : "";
  const sel    = _q(block, "sel-native-mode");
  if (!sel) return;
  socket.emit("apply_native_mode", { cam_id, index: parseInt(sel.value) });
}

// ── Parameter visibility (within one block) ───────────────────────────────────

function _applyParamVisibility(block, supported) {
  const all = supported.length === 0;
  const has = (k) => all || supported.includes(k);

  const gainVisible = has("gain") || has("gain_auto");
  block.querySelectorAll(".section-title").forEach(el => {
    if (el.textContent.trim().startsWith("Gain"))
      el.style.display = gainVisible ? "" : "none";
  });
  block.querySelectorAll(".check-row").forEach(el => {
    if (el.querySelector('[data-id="chk-gain-auto"]'))
      el.style.display = gainVisible ? "" : "none";
  });
  block.querySelectorAll(".param-row").forEach(el => {
    if (el.querySelector('[data-id="sl-gain"]'))
      el.style.display = gainVisible ? "" : "none";
  });
  const gainMobile = _q(block, "sl-group-gain");
  if (gainMobile) gainMobile.style.display = gainVisible ? "" : "none";

  const autoExpVisible = has("exposure_auto");
  block.querySelectorAll(".check-row").forEach(el => {
    if (el.querySelector('[data-id="chk-exp-auto"]'))
      el.style.display = autoExpVisible ? "" : "none";
  });
}

// ── Per-block state apply ─────────────────────────────────────────────────────

function _applyParamsBlock(block, cs, s) {
  const ci = cs.cam_info || {};

  // Update log-scale bounds from hardware limits
  if (ci.exp_min != null) _setLogBounds(block, "exposure", ci.exp_min, ci.exp_max);
  if (ci.fps_min != null) _setLogBounds(block, "fps",      ci.fps_min, ci.fps_max);
  ["sl-gain", "sl-gain-m"].forEach(id => {
    const el = _q(block, id);
    if (el) { el.min = ci.gain_min; el.max = ci.gain_max; }
  });

  // Slider positions
  if (cs.exposure != null) { _syncDisplay(block, "exposure", cs.exposure); _syncSliderPos(block, "exposure", cs.exposure); }
  if (cs.gain     != null) { _syncDisplay(block, "gain",     cs.gain);     _syncSliderPos(block, "gain",     cs.gain); }
  if (cs.fps      != null) { _syncDisplay(block, "fps",      cs.fps);      _syncSliderPos(block, "fps",      cs.fps); }

  // Auto exposure
  ["chk-exp-auto", "chk-exp-auto-m"].forEach(id => {
    const el = _q(block, id); if (el) el.checked = cs.exposure_auto;
  });
  ["inp-exp-auto-upper", "inp-exp-auto-upper-m"].forEach(id => {
    const el = _q(block, id);
    if (!el) return;
    el.value = cs.exp_auto_upper;
    if (ci.exp_min != null) { el.min = Math.ceil(ci.exp_min); el.max = Math.floor(ci.exp_max); }
  });

  // Auto gain
  ["chk-gain-auto", "chk-gain-auto-m"].forEach(id => {
    const el = _q(block, id); if (el) el.checked = cs.gain_auto;
  });
  ["sl-gain", "sl-gain-m"].forEach(id => {
    const el = _q(block, id); if (el) el.disabled = cs.gain_auto;
  });

  // Native resolution selector
  const nmWrap = _q(block, "native-mode-wrap");
  const nmSel  = _q(block, "sel-native-mode");
  if (nmWrap && nmSel) {
    const modes   = cs.native_modes || [];
    const cid     = block.dataset.cam;
    const camOpen = cid && s.cameras && cid in s.cameras;
    if (modes.length > 0 && camOpen) {
      nmWrap.style.display = "block";
      const selIdx = cs.selected_native_mode || 0;
      nmSel.innerHTML = "";
      modes.forEach((m, i) => {
        const opt = document.createElement("option");
        opt.value = i;
        opt.textContent = m.width + " x " + m.height + "  @  " + m.fps + " fps";
        if (i === selIdx) opt.selected = true;
        nmSel.appendChild(opt);
      });
    } else {
      nmWrap.style.display = "none";
    }
  }

  // Parameter visibility
  if (cs.cam_supported_params !== undefined) {
    _applyParamVisibility(block, cs.cam_supported_params);
  }
}

// ── State sync: iterate ALL BasicParams blocks ────────────────────────────────

function _applyParamsState(s) {
  document.querySelectorAll('.plugin-ui-block[data-plugin="BasicParams"]').forEach(block => {
    const cid = block.dataset.cam;
    const cs  = (s.cameras && cid) ? s.cameras[cid] : null;
    if (!cs) return;
    _applyParamsBlock(block, cs, s);
  });
}

socket.on("state", _applyParamsState);
window.addEventListener("plugin-state-update", (e) => _applyParamsState(e.detail));

// ── Frame handler: live readback (gain / exposure auto-track) ─────────────────

socket.on("frame", (data) => {
  document.querySelectorAll('.plugin-ui-block[data-plugin="BasicParams"]').forEach(block => {
    if (block.dataset.cam !== data.cam_id) return;
    if (data.current_gain     !== undefined) {
      _syncDisplay(block, "gain",     data.current_gain);
      _syncSliderPos(block, "gain",   data.current_gain);
    }
    if (data.current_exposure !== undefined) {
      _syncDisplay(block, "exposure",   data.current_exposure);
      _syncSliderPos(block, "exposure", data.current_exposure);
    }
  });
});

