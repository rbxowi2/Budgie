// photo/ui.js — Photo plugin frontend (1.0.0)
// Multi-camera safe: NO getElementById. All queries scoped to .plugin-ui-block.

function _photoBlk(el) { return el.closest('.plugin-ui-block'); }

function photoTakePhoto(el) {
  const cam_id = (_photoBlk(el) || {}).dataset?.cam || window._selectedCamId || "";
  socket.emit("plugin_action", { cam_id, action: "take_photo" });
}

function photoOnFmt(el) {
  const cam_id = (_photoBlk(el) || {}).dataset?.cam || window._selectedCamId || "";
  socket.emit("set_param", { cam_id, key: "photo_fmt", value: el.value });
}

function _applyPhotoState(s) {
  document.querySelectorAll('.plugin-ui-block[data-plugin="BasicPhoto"]').forEach(block => {
    const cid = block.dataset.cam;
    const cs  = (s.cameras && cid) ? s.cameras[cid] : null;
    if (!cs) return;
    block.querySelectorAll("input[name='photo_fmt']").forEach(r => {
      r.checked = (r.value === cs.photo_fmt);
    });
  });
}

socket.on("state", _applyPhotoState);
window.addEventListener("plugin-state-update", (e) => _applyPhotoState(e.detail));

// Keyboard shortcut: 's' takes a photo for the selected camera
document.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
  if (e.key === "s") {
    const sel = window._selectedCamId;
    const blk = document.querySelector(`.plugin-ui-block[data-plugin="BasicPhoto"][data-cam="${CSS.escape(sel)}"]`);
    if (blk) photoTakePhoto(blk);
  }
});
