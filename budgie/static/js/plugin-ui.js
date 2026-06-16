/* plugin-ui.js — Plugin UI management, drag-reorder, modals, notifications */

// ── Plugin area state ─────────────────────────────────────────────────────
const _pluginUIsLocal = {};   // cam_id → Set<instance_key>
let _syncRunning = false;
let _syncQueued  = null;

async function syncPluginArea(s) {
  if (!_isAdmin) return;
  if (_syncRunning) { _syncQueued = s; return; }
  _syncRunning = true;
  try {
    await _doSyncPluginArea(s);
    while (_syncQueued) {
      const qs = _syncQueued;
      _syncQueued = null;
      await _doSyncPluginArea(qs);
    }
  } finally {
    _syncRunning = false;
  }
}

async function _doSyncPluginArea(s) {
  const cam_id  = s.selected_cam_id || "";
  const ppState = s.plugin_pipeline || {};

  document.querySelectorAll(".local-plugins-container").forEach(c => {
    c.style.display = (c.dataset.cam === cam_id) ? "" : "none";
  });
  if (!cam_id) return;

  const pipeline     = ppState[cam_id] || [];
  const pipelineKeys = new Set(pipeline.map(e => e.instance_key || e.name));

  let container = document.querySelector(
    `.local-plugins-container[data-cam="${CSS.escape(cam_id)}"]`
  );
  if (!container) {
    container = document.createElement("div");
    container.className   = "local-plugins-container";
    container.dataset.cam = cam_id;
    document.getElementById("local-plugins-area").appendChild(container);
    _setupContainerDrop(container, cam_id);
  }
  if (!_pluginUIsLocal[cam_id]) _pluginUIsLocal[cam_id] = new Set();

  // Add missing plugins
  for (const entry of pipeline) {
    const ikey = entry.instance_key || entry.name;
    if (!_pluginUIsLocal[cam_id].has(ikey)) {
      await loadPluginUI(entry.name, ikey, cam_id, container);
      _pluginUIsLocal[cam_id].add(ikey);
    }
  }

  // Remove stale plugins
  for (const ikey of [..._pluginUIsLocal[cam_id]]) {
    if (!pipelineKeys.has(ikey)) {
      const el = container.querySelector(`.plugin-ui-block[data-instance="${CSS.escape(ikey)}"]`);
      if (el) el.remove();
      _pluginUIsLocal[cam_id].delete(ikey);
    }
  }

  // Sort DOM to match pipeline order
  pipeline.forEach(entry => {
    const ikey = entry.instance_key || entry.name;
    const el = container.querySelector(`.plugin-ui-block[data-instance="${CSS.escape(ikey)}"]`);
    if (el) container.appendChild(el);
  });

  // Update order badges and mode buttons
  pipeline.forEach((entry, idx) => {
    const ikey = entry.instance_key || entry.name;
    const el = container.querySelector(`.plugin-ui-block[data-instance="${CSS.escape(ikey)}"]`);
    if (!el) return;
    const badge = el.querySelector(".plugin-order-badge");
    if (badge) badge.textContent = idx;
    _applyModeBtn(el.querySelector(".plugin-mode-btn"), entry.mode);
  });

  if (_stateCache) {
    window.dispatchEvent(new CustomEvent("plugin-state-update", { detail: _stateCache }));
  }
}

async function loadPluginUI(plugin_name, instance_key, cam_id, container) {
  const slug = plugin_name.toLowerCase().replace(/\s+/g, "");
  try {
    const resp = await fetch("/plugin/" + slug + "/ui.html");
    if (!resp.ok) return;
    const html = await resp.text();
    const div  = document.createElement("div");
    div.className        = "plugin-ui-block";
    div.dataset.plugin   = plugin_name;
    div.dataset.instance = instance_key;
    div.dataset.cam      = cam_id;
    div.setAttribute("draggable", "true");
    div.innerHTML        = html;

    const hdr = div.querySelector(".collapsible-hdr, .section-title, h3, h4");
    if (hdr) {
      // Drag handle
      const dh = document.createElement("span");
      dh.className  = "plugin-drag-handle";
      dh.textContent = "⠿";
      dh.title = "Drag to reorder";
      hdr.insertBefore(dh, hdr.firstChild);

      // Order badge
      const ob = document.createElement("span");
      ob.className   = "plugin-order-badge";
      ob.textContent = "0";
      hdr.insertBefore(ob, hdr.children[1] || null);

      // Mode toggle button
      const mb = document.createElement("button");
      mb.className = "plugin-mode-btn";
      mb.dataset.mode = "pipeline";
      _applyModeBtn(mb, "pipeline");
      mb.onclick = (e) => {
        e.stopPropagation();
        const newMode = mb.dataset.mode === "pipeline" ? "display" : "pipeline";
        socket.emit("set_plugin_mode", { cam_id: div.dataset.cam, instance_key, plugin_name, mode: newMode });
      };
      hdr.appendChild(mb);

      // Remove button
      const rb = document.createElement("button");
      rb.className   = "plugin-remove-btn";
      rb.textContent = "✕";
      rb.title       = "Remove plugin";
      rb.onclick = (e) => {
        e.stopPropagation();
        onRemovePlugin(plugin_name, instance_key, cam_id);
      };
      hdr.appendChild(rb);
    }

    _setupBlockDrag(div);
    container.appendChild(div);
    if (_stateCache) {
      window.dispatchEvent(new CustomEvent("plugin-state-update", { detail: _stateCache }));
    }
  } catch (err) {
    console.error("Plugin UI load error:", err);
  }
}

function _applyModeBtn(btn, mode) {
  if (!btn) return;
  const isPipeline = mode === "pipeline";
  btn.dataset.mode = mode;
  btn.textContent  = isPipeline ? "P" : "D";
  btn.title = isPipeline
    ? "Pipeline mode — processed before display"
    : "Display mode — processed after pipeline";
  btn.classList.toggle("mode-display", !isPipeline);
}

// ── Drag-to-reorder ───────────────────────────────────────────────────────
let _dragSrc = null;

function _setupBlockDrag(block) {
  block.addEventListener("dragstart", (e) => {
    _dragSrc = block;
    e.dataTransfer.effectAllowed = "move";
    setTimeout(() => block.classList.add("dragging"), 0);
  });
  block.addEventListener("dragend", () => {
    block.classList.remove("dragging");
    _dragSrc = null;
    document.querySelectorAll(".drag-over-top,.drag-over-bottom").forEach(el => {
      el.classList.remove("drag-over-top", "drag-over-bottom");
    });
  });
  block.addEventListener("dragover", (e) => {
    if (!_dragSrc || _dragSrc === block) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
    block.classList.remove("drag-over-top", "drag-over-bottom");
    const r = block.getBoundingClientRect();
    block.classList.add(e.clientY < r.top + r.height / 2 ? "drag-over-top" : "drag-over-bottom");
  });
  block.addEventListener("dragleave", () => {
    block.classList.remove("drag-over-top", "drag-over-bottom");
  });
  block.addEventListener("drop", (e) => {
    e.preventDefault();
    if (!_dragSrc || _dragSrc === block) return;
    const r = block.getBoundingClientRect();
    if (e.clientY < r.top + r.height / 2) block.before(_dragSrc);
    else block.after(_dragSrc);
    block.classList.remove("drag-over-top", "drag-over-bottom");
  });
}

function _setupContainerDrop(container, cam_id) {
  container.addEventListener("drop", (e) => {
    e.preventDefault();
    if (!_dragSrc) return;
    const names = [...container.querySelectorAll(".plugin-ui-block")]
      .map(b => b.dataset.instance || b.dataset.plugin);
    socket.emit("reorder_plugins", { cam_id, names });
  });
  container.addEventListener("dragover", (e) => {
    e.preventDefault(); e.dataTransfer.dropEffect = "move";
  });
}

function cleanupLocalPluginsUI(cam_id) {
  const c = document.querySelector(`.local-plugins-container[data-cam="${CSS.escape(cam_id)}"]`);
  if (c) c.remove();
  delete _pluginUIsLocal[cam_id];
}

function onRemovePlugin(plugin_name, instance_key, cam_id) {
  socket.emit("remove_plugin", { plugin_name, instance_key, cam_id: cam_id || "" });
}

// ── Add-plugin modal ──────────────────────────────────────────────────────
function openAddPluginModal() {
  if (!_stateCache) return;
  const s         = _stateCache;
  const cam_id    = (s.selected_cam_id && s.cameras && s.cameras[s.selected_cam_id])
                    ? s.selected_cam_id : "";
  const pipeline   = ((s.plugin_pipeline || {})[cam_id] || []);
  const inPipeline = new Set(pipeline.map(e => e.name));
  const available  = s.available_plugins || [];
  const avail      = available.filter(p => !inPipeline.has(p.name) || p.allow_multiple);

  const body = document.getElementById("add-plugin-list");
  body.innerHTML = "";
  if (!cam_id) {
    body.innerHTML = '<p class="rec-empty">Select an open camera to add plugins.</p>';
  } else if (avail.length === 0) {
    body.innerHTML = '<p class="rec-empty">No additional plugins available.</p>';
  } else {
    avail.forEach(p => body.appendChild(makePluginItem(p, cam_id)));
  }
  document.getElementById("add-plugin-overlay").classList.add("open");
}

function makePluginItem(p, cam_id) {
  const div = document.createElement("div");
  div.className = "plugin-modal-item";
  div.innerHTML =
    `<div class="plugin-modal-name">${escapeHtml(p.name)}</div>` +
    (p.description ? `<div class="plugin-modal-desc">${escapeHtml(p.description)}</div>` : "");
  div.onclick = () => {
    socket.emit("add_plugin", { plugin_name: p.name, cam_id });
    closeAddPluginModal();
  };
  return div;
}

function closeAddPluginModal() {
  document.getElementById("add-plugin-overlay").classList.remove("open");
}
function closeAddPluginOutside(e) {
  if (e.target === document.getElementById("add-plugin-overlay")) closeAddPluginModal();
}

// ── Notifications ─────────────────────────────────────────────────────────
socket.on("security_notification", (notif) => {
  if (!_isAdmin) return;
  const list = document.getElementById("notif-list");
  list.insertBefore(buildNotifItem(notif), list.firstChild);
  updateBadge();
});
socket.on("notifications_update", (data) => renderNotifications(data.notifications));
socket.on("security_records",     (data) => renderRecords(data));

function renderNotifications(notifications) {
  if (!_isAdmin) return;
  const list = document.getElementById("notif-list");
  list.innerHTML = "";
  notifications.forEach(n => list.appendChild(buildNotifItem(n)));
  updateBadge();
}

function buildNotifItem(n) {
  const div = document.createElement("div");
  div.className = "notif-item";
  div.innerHTML =
    `<div class="notif-text">
       <div>${escapeHtml(n.msg)}</div>
       <div class="notif-time">${escapeHtml(n.time)}</div>
     </div>
     <button class="notif-confirm" onclick="confirmNotification('${escapeHtml(n.id)}')">OK</button>`;
  return div;
}

function updateBadge() {
  if (!_isAdmin) return;
  const cnt   = document.getElementById("notif-list").children.length;
  const badge = document.getElementById("notif-badge");
  document.getElementById("notif-count").textContent = cnt;
  badge.classList.toggle("has-notif", cnt > 0);
  badge.classList.toggle("flashing",  cnt > 0);
  if (cnt === 0) document.getElementById("notif-dropdown").classList.remove("open");
}

function toggleNotifDropdown(e) {
  e.stopPropagation();
  document.getElementById("notif-dropdown").classList.toggle("open");
}

document.addEventListener("click", (e) => {
  const wrap = document.getElementById("notif-wrap");
  if (wrap && !wrap.contains(e.target))
    document.getElementById("notif-dropdown").classList.remove("open");
});

function confirmNotification(id) { socket.emit("confirm_notification", { id }); }
function confirmAllNotifications() {
  socket.emit("confirm_all_notifications");
  document.getElementById("notif-dropdown").classList.remove("open");
}

// ── Records modal ─────────────────────────────────────────────────────────
function openRecords() {
  socket.emit("get_security_records");
  document.getElementById("records-overlay").classList.add("open");
}
function closeRecords() { document.getElementById("records-overlay").classList.remove("open"); }
function closeRecordsOutside(e) {
  if (e.target === document.getElementById("records-overlay")) closeRecords();
}
function clearBlacklist() {
  if (!confirm("Clear all blocked IPs?")) return;
  socket.emit("clear_blacklist");
}
function clearNotifications() {
  if (!confirm("Clear all pending notifications?")) return;
  socket.emit("clear_notifications");
  document.getElementById("notif-dropdown").classList.remove("open");
}

function renderRecords(data) {
  const blWrap = document.getElementById("rec-blacklist-wrap");
  const bl  = data.blacklist || {};
  const ips = Object.keys(bl);
  if (!ips.length) {
    blWrap.innerHTML = '<p class="rec-empty">No blocked IPs.</p>';
  } else {
    let h = '<table class="rec-table"><thead><tr><th>IP</th><th>Blocked At</th><th>Reason</th></tr></thead><tbody>';
    ips.forEach(ip => {
      const info = bl[ip];
      h += `<tr><td>${escapeHtml(ip)}</td><td>${escapeHtml(info.blocked_at||"")}</td><td>${escapeHtml(info.reason||"")}</td></tr>`;
    });
    blWrap.innerHTML = h + "</tbody></table>";
  }
  const logs    = (data.log || []).slice().reverse();
  const logWrap = document.getElementById("rec-log-wrap");
  if (!logs.length) {
    logWrap.innerHTML = '<p class="rec-empty">No log entries.</p>';
  } else {
    logWrap.innerHTML = logs.map(e =>
      `<div class="rec-log-entry">`
      + `<span style="color:#666;">${escapeHtml(e.time)}</span> `
      + `<span style="color:#3a7bd5;">${escapeHtml(e.event)}</span> `
      + `IP: ${escapeHtml(e.ip)}`
      + (e.detail ? ` — <span style="color:#888;">${escapeHtml(e.detail)}</span>` : "")
      + `</div>`
    ).join("");
  }
}

// ── Misc ──────────────────────────────────────────────────────────────────
function toggleSection(bodyId, hdrEl) {
  const body  = document.getElementById(bodyId);
  const arrow = hdrEl.querySelector(".collapse-arrow");
  const open  = body.classList.contains("is-open");
  body.classList.toggle("is-open", !open);
  if (arrow) arrow.textContent = open ? "▶" : "▼";
}
