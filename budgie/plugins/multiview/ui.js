// multiview/ui.js — MultiView plugin frontend (1.1.0)

// Label sets per layout: [slot1, slot2]
const _MV_LABELS = {
  "2h": ["Left",  "Right"],
  "2v": ["Top",   "Bottom"],
  "4":  ["TL",    "TR"],
};

function _mvBlock(el)  { return el.closest(".plugin-ui-block"); }
function _mvCamId(el)  { const b = _mvBlock(el); return b ? b.dataset.cam : ""; }

function mvOnLayout(el) {
  const cam_id = _mvCamId(el);
  if (!cam_id) return;
  socket.emit("set_param", { cam_id, key: "multiview_layout", value: el.value });
  _mvApplyLayout(_mvBlock(el), el.value);
}

function mvOnCam(el, slot) {
  const cam_id = _mvCamId(el);
  if (!cam_id) return;
  socket.emit("set_param", { cam_id, key: `multiview_cam_${slot}`, value: el.value });
}

function mvOnSrc(el, slot) {
  const cam_id = _mvCamId(el);
  if (!cam_id) return;
  socket.emit("set_param", { cam_id, key: `multiview_src_${slot}`, value: el.value });
}

function mvOnRes(el) {
  const cam_id = _mvCamId(el);
  if (!cam_id) return;
  socket.emit("set_param", { cam_id, key: "multiview_res", value: el.value });
}

// ── Layout UI helpers ─────────────────────────────────────────────────────────

function _mvApplyLayout(block, layout) {
  if (!block) return;
  // Show/hide 4-grid slots
  const extra = block.querySelector(".mv-extra");
  if (extra) extra.style.display = layout === "4" ? "flex" : "none";
  // Update slot 1/2 labels
  const lbls = _MV_LABELS[layout] || ["1", "2"];
  const l1 = block.querySelector(".mv-lbl-1");
  const l2 = block.querySelector(".mv-lbl-2");
  if (l1) l1.textContent = lbls[0];
  if (l2) l2.textContent = lbls[1];
}

// ── Camera dropdown population ────────────────────────────────────────────────

function _mvPopulateCams(block, cams, values) {
  const selfId = block.dataset.cam;
  for (let slot = 1; slot <= 4; slot++) {
    const sel = block.querySelector(`.mv-cam-${slot}`);
    if (!sel) continue;
    const cur = values[slot - 1] ?? sel.value;
    sel.innerHTML = '<option value="">-- none --</option>' +
      cams
        .filter(c => c !== selfId)
        .map(c => `<option value="${escapeHtml(c)}">${escapeHtml(c)}</option>`).join("");
    sel.value = cur;
  }
}

// ── State sync ────────────────────────────────────────────────────────────────

function _applyMultiViewState(s) {
  document.querySelectorAll('.plugin-ui-block[data-plugin="MultiView"]').forEach(block => {
    const cam_id = block.dataset.cam;
    const cs     = (s.cameras || {})[cam_id] || {};
    const cams   = Object.keys(s.cameras || {});

    // Layout selector
    const layoutSel = block.querySelector(".mv-layout");
    const layout = cs.multiview_layout ?? (layoutSel ? layoutSel.value : "2h");
    if (layoutSel && cs.multiview_layout != null) layoutSel.value = cs.multiview_layout;
    _mvApplyLayout(block, layout);

    // Populate camera dropdowns
    const camVals = [
      cs.multiview_cam_1 ?? "",
      cs.multiview_cam_2 ?? "",
      cs.multiview_cam_3 ?? "",
      cs.multiview_cam_4 ?? "",
    ];
    _mvPopulateCams(block, cams, camVals);

    // Source selectors
    [1, 2, 3, 4].forEach(slot => {
      const key = `multiview_src_${slot}`;
      if (cs[key] != null) {
        const sel = block.querySelector(`.mv-src-${slot}`);
        if (sel) sel.value = cs[key];
      }
    });

    // Resolution selector
    const resSel = block.querySelector(".mv-res");
    if (resSel && cs.multiview_res != null) resSel.value = cs.multiview_res;
  });
}

socket.on("state", _applyMultiViewState);
window.addEventListener("plugin-state-update", e => _applyMultiViewState(e.detail));
