// looprecord/ui.js — Loop Recording plugin frontend (1.0.0)
// Multi-camera safe: NO getElementById. All queries scoped to .plugin-ui-block.

function _lrBlk(el) { return el.closest('.plugin-ui-block'); }
function _lrQ(b, k)  { return b ? b.querySelector('[data-id="' + k + '"]') : null; }
function _lrCam(el)  {
  const b = _lrBlk(el);
  return (b && b.dataset.cam) ? b.dataset.cam : (window._selectedCamId || "");
}

// ── Actions ───────────────────────────────────────────────────────────────────

function lrToggle(el) {
  const cam_id = _lrCam(el);
  const action = el.classList.contains("btn-red") ? "stop_loop_record" : "start_loop_record";
  socket.emit("plugin_action", { cam_id, action });
}

function lrSetRes(el) {
  socket.emit("set_param", { cam_id: _lrCam(el), key: "loop_res_preset", value: el.dataset.lrRes });
}

function lrSetQuality(el) {
  socket.emit("set_param", { cam_id: _lrCam(el), key: "loop_quality", value: el.dataset.lrQ });
}

function lrSetParam(el, key) {
  const v = parseInt(el.value, 10);
  if (!isNaN(v)) socket.emit("set_param", { cam_id: _lrCam(el), key, value: v });
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function _lrFmtTime(sec) {
  const m = Math.floor(sec / 60), s = sec % 60;
  return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

function _lrEstimate(bk, chMin, maxC) {
  const chSec = chMin * 60;
  const chMB  = (bk * 1000 / 8 / 1024 / 1024 * chSec).toFixed(1);
  const totGB = (chMB * maxC / 1024).toFixed(2);
  const dayGB = (bk * 1000 / 8 / 1024 / 1024 / 1024 * 86400).toFixed(1);
  return `Chunk ≈ ${chMB} MB × ${maxC} = ${totGB} GB  |  Daily write ≈ ${dayGB} GB`;
}

// Per-camera meta for real-time frame-driven chunk bar updates
const _lrMeta = {};  // cam_id → { maxC, durSec, rec, windowStart }

function _lrSetChunkBar(block, chunks, elapsed, maxC, durSec) {
  const el = _lrQ(block, "lr-chunk-bar");
  if (!el) return;
  // chunks = completed count; current chunk = chunks+1; show "Looping" when ring is full
  if (chunks >= maxC) {
    el.textContent = `Looping  ${_lrFmtTime(elapsed)} / ${_lrFmtTime(durSec)}`;
  } else {
    el.textContent = `Chunk ${chunks + 1}/${maxC}  ${_lrFmtTime(elapsed)} / ${_lrFmtTime(durSec)}`;
  }
}

// ── Apply state ───────────────────────────────────────────────────────────────

function _applyLoopRecordState(s) {
  document.querySelectorAll('.plugin-ui-block[data-plugin="LoopRecord"]').forEach(block => {
    const cid = block.dataset.cam;
    const cs  = (s.cameras && cid) ? s.cameras[cid] : null;
    if (!cs) return;

    const rec      = !!cs.loop_recording;
    const chunks   = cs.loop_chunks_count   || 0;
    const maxC     = cs.loop_chunks_max     || 0;
    const elapsed  = cs.loop_chunk_elapsed  || 0;
    const durSec   = cs.loop_chunk_dur_sec  || 0;
    const freeGB   = cs.loop_disk_free_gb   || 0;
    const dayGB    = cs.loop_daily_write_gb || 0;
    const bk       = cs.loop_bitrate_kbps  || 1000;
    const chMin    = cs.loop_chunk_min      || 10;
    const lpMin    = cs.loop_loop_min       || 60;
    const qual     = cs.loop_quality        || "medium";
    const res      = cs.loop_res_preset     || "native";

    const ffmpegOk = cs.loop_ffmpeg_ok !== false;

    // Cache for frame-driven real-time updates
    _lrMeta[cid] = { maxC, durSec, rec };

    // Badge
    const badge = _lrQ(block, "lr-badge");
    if (badge) badge.style.display = rec ? "inline" : "none";

    // Status area
    const statusArea = _lrQ(block, "lr-status-area");
    if (statusArea) statusArea.style.display = (rec || chunks > 0) ? "block" : "none";

    // Chunk bar
    const chunkBar = _lrQ(block, "lr-chunk-bar");
    if (chunkBar) {
      if (rec) {
        _lrSetChunkBar(block, chunks, elapsed, maxC, durSec);
      } else if (chunks > 0) {
        chunkBar.textContent = `Saved ${chunks}/${maxC} chunks`;
      } else {
        chunkBar.textContent = "";
      }
    }

    const diskBar = _lrQ(block, "lr-disk-bar");
    if (diskBar) {
      diskBar.textContent = `Free ${freeGB.toFixed(1)} GB  |  Daily write ${dayGB.toFixed(1)} GB`;
    }

    // Settings lock while recording
    const settings = _lrQ(block, "lr-settings");
    if (settings) {
      settings.querySelectorAll("input, button").forEach(el => { el.disabled = rec; });
    }

    // Resolution buttons — selected = btn-blue, unselected = btn-gray
    block.querySelectorAll("[data-lr-res]").forEach(btn => {
      btn.className = "btn " + (btn.dataset.lrRes === res ? "btn-blue" : "btn-gray");
    });

    // Quality buttons — selected = btn-blue, unselected = btn-gray
    block.querySelectorAll("[data-lr-q]").forEach(btn => {
      btn.className = "btn " + (btn.dataset.lrQ === qual ? "btn-blue" : "btn-gray");
    });

    // Custom bitrate row
    const customRow = _lrQ(block, "lr-custom-bitrate-row");
    if (customRow) customRow.style.display = (qual === "custom") ? "flex" : "none";

    // Inputs (don't override while user is typing)
    const loopInp = _lrQ(block, "lr-loop-min");
    if (loopInp  && document.activeElement !== loopInp)  loopInp.value  = lpMin;
    const chunkInp = _lrQ(block, "lr-chunk-min");
    if (chunkInp && document.activeElement !== chunkInp) chunkInp.value = chMin;
    const bitrateInp = _lrQ(block, "lr-bitrate");
    if (bitrateInp && document.activeElement !== bitrateInp) bitrateInp.value = bk;

    // Storage estimate
    const est = _lrQ(block, "lr-estimate");
    if (est) {
      est.style.display = "block";
      est.textContent   = _lrEstimate(bk, chMin, maxC);
    }

    // Main button — blue=start, red=stop, disabled if ffmpeg missing
    const btn = _lrQ(block, "lr-btn");
    if (btn) {
      btn.textContent = rec ? "Stop Loop Recording" : "Start Loop Recording";
      btn.className   = "btn " + (rec ? "btn-red" : "btn-blue");
      btn.disabled    = !ffmpegOk && !rec;
    }
  });
}

// Real-time chunk bar update driven by frame events (updates elapsed every frame)
socket.on("frame", (data) => {
  if (!data.loop_rec) return;
  const cid  = data.cam_id;
  const meta = _lrMeta[cid];
  if (!meta || !meta.rec) return;
  document.querySelectorAll(
    `.plugin-ui-block[data-plugin="LoopRecord"][data-cam="${CSS.escape(cid)}"]`
  ).forEach(block => {
    _lrSetChunkBar(block, data.loop_rt_chunks || 0, data.loop_rt_elapsed || 0, meta.maxC, meta.durSec);
  });
});

socket.on("state", _applyLoopRecordState);
window.addEventListener("plugin-state-update", (e) => _applyLoopRecordState(e.detail));
