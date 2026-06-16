/* health.js — Camera stale detection and connection warning */

const CAMERA_STALE_MS = 5000;
const lastFrameTime   = {};   // cam_id → Date.now() at last frame (written by socket.js)

function _checkCameraHealth() {
  const now = Date.now();
  _openCamIds.forEach(cam_id => {
    // Skip minimized windows — they're intentionally paused
    if (typeof _mdiMinState !== "undefined" && _mdiMinState[cam_id]) return;
    const last  = lastFrameTime[cam_id];
    // Grace period: after selecting a camera, give it CAMERA_STALE_MS before judging.
    // Prevents false "Camera not responding" while server un-pauses and sends first frame.
    const selAt = (typeof _mobileCamSelectedAt !== "undefined")
      ? (_mobileCamSelectedAt[cam_id] || 0) : 0;
    const stale = (() => {
      if (last === undefined) return selAt > 0 && (now - selAt) > CAMERA_STALE_MS;
      if (selAt > 0 && last < selAt) return (now - selAt) > CAMERA_STALE_MS;
      return (now - last) > CAMERA_STALE_MS;
    })();

    const win = document.querySelector(`.mdi-win[data-cam="${CSS.escape(cam_id)}"]`);
    if (win) win.querySelector(".mdi-cam-warn")?.classList.toggle("visible", stale);

    if (cam_id === _selectedCamId) {
      document.getElementById("mobile-cam-warn")?.classList.toggle("visible", stale);
    }
  });
}

setInterval(_checkCameraHealth, 2000);
