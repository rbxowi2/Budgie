// motiondetect/ui.js — MotionDetect plugin frontend (1.1.1)
// Multi-camera safe: all DOM queries scoped to .plugin-ui-block.
// Modal is created once and reused across all cameras.

(function () {
  'use strict';

  // ── Modal state ────────────────────────────────────────────────────────────
  let _modal = null, _img = null, _canvas = null, _ctx = null;
  let _tool       = 'detect';
  let _zones      = [];       // completed: [{type, color, points:[[nx,ny],...]}]
  let _drawing    = null;     // in-progress: {type, color, points:[[px,py],...]} natural coords
  let _selected   = -1;
  let _mouseNat   = { x: 0, y: 0 };
  let _snapActive = false;
  let _camId      = '';
  let _flashOn    = false;
  let _flashTimer = null;
  let _lastZones  = {};       // cam_id → zones cache from last state event

  const SNAP_DISPLAY_PX = 14;
  const ZONE_COLOR = { detect: '#e63030' };

  // ── Stream overlay (sensitivity circle on camera view) ─────────────────────
  const _overlayTimers = new Map();   // cam_id → timerID

  function _getStreamContent(cam_id) {
    const win = document.querySelector(`.mdi-win[data-cam="${CSS.escape(cam_id)}"]`);
    return win ? win.querySelector('.mdi-content') : null;
  }

  function _getOrCreateOverlay(cam_id) {
    const content = _getStreamContent(cam_id);
    if (!content) return null;
    let ov = content.querySelector('.motdet-stream-overlay');
    if (!ov) {
      ov = document.createElement('canvas');
      ov.className = 'motdet-stream-overlay';
      Object.assign(ov.style, {
        position: 'absolute', inset: '0',
        width: '100%', height: '100%',
        pointerEvents: 'none', opacity: '0',
        transition: 'opacity 0.4s ease',
      });
      content.appendChild(ov);
    }
    return ov;
  }

  function _drawStreamCircle(cam_id, varT, count) {
    const ov = _getOrCreateOverlay(cam_id);
    if (!ov) return;

    const W = ov.offsetWidth || 1;
    const H = ov.offsetHeight || 1;
    ov.width  = W;
    ov.height = H;

    const minDim = Math.min(W, H);
    // radius: count 50→5000 maps to minDim*0.04 → minDim*0.38
    const r = minDim * 0.04 + (count - 50) / (5000 - 50) * (minDim * 0.34);
    // opacity: varT 5→100 maps to 0.18→0.88
    const opacity = 0.18 + (varT - 5) / (100 - 5) * 0.70;

    const c = ov.getContext('2d');
    c.clearRect(0, 0, W, H);
    c.beginPath();
    c.arc(W / 2, H / 2, r, 0, Math.PI * 2);
    c.fillStyle = `rgba(200,40,40,${opacity.toFixed(3)})`;
    c.fill();

    ov.style.transition = 'none';
    ov.style.opacity    = '1';
  }

  function _showStreamCircle(cam_id, block) {
    const vS = block.querySelector('.motdet-var-slider');
    const cS = block.querySelector('.motdet-count-slider');
    if (!vS || !cS) return;
    if (_overlayTimers.has(cam_id)) clearTimeout(_overlayTimers.get(cam_id));
    _drawStreamCircle(cam_id, parseFloat(vS.value), parseFloat(cS.value));
  }

  function _scheduleHideStreamCircle(cam_id) {
    if (_overlayTimers.has(cam_id)) clearTimeout(_overlayTimers.get(cam_id));
    const id = setTimeout(() => {
      const ov = _getOrCreateOverlay(cam_id);
      if (ov) {
        ov.style.transition = 'opacity 0.5s ease';
        ov.style.opacity    = '0';
      }
      _overlayTimers.delete(cam_id);
    }, 2000);
    _overlayTimers.set(cam_id, id);
  }

  // ── Modal builder (called once) ────────────────────────────────────────────
  function _buildModal() {
    if (_modal) return;

    _modal = document.createElement('div');
    Object.assign(_modal.style, {
      display: 'none', position: 'fixed', inset: '0',
      background: 'rgba(0,0,0,0.93)', zIndex: '9000',
      flexDirection: 'column', alignItems: 'center',
      justifyContent: 'flex-start', userSelect: 'none',
    });

    // ── Toolbar ──────────────────────────────────────────────────────────────
    const tb = document.createElement('div');
    Object.assign(tb.style, {
      display: 'flex', gap: '8px', padding: '10px 16px',
      background: '#181818', width: '100%', boxSizing: 'border-box',
      alignItems: 'center', flexShrink: '0',
    });

    function _mkBtn(text, bg, fg, border, id, onClick) {
      const b = document.createElement('button');
      b.textContent = text;
      if (id) b.id = id;
      Object.assign(b.style, {
        background: bg, color: fg,
        border: `1px solid ${border}`, borderRadius: '4px',
        padding: '6px 16px', fontSize: '13px',
        cursor: 'pointer', fontWeight: '600',
      });
      b.onclick = onClick;
      return b;
    }

    const btnArrow  = _mkBtn('↖ Select',     '#3a3a3a', '#d4d4d4', '#555',    'motdet-tb-arrow',  () => motdetSetTool('arrow'));
    const btnDetect = _mkBtn('Detect Zone', '#c0392b', '#fff',    '#e74c3c', 'motdet-tb-detect', () => motdetSetTool('detect'));
    const btnClear  = _mkBtn('Clear',       '#444',    '#d4d4d4', '#666',    '',                 motdetClearSelected);
    const btnExit   = _mkBtn('Exit',        '#1e4d2b', '#7dcea0', '#2e7d47', '',                 motdetExit);
    btnExit.style.marginLeft = 'auto';

    tb.append(btnArrow, btnDetect, btnClear, btnExit);
    _modal.appendChild(tb);

    // ── Image + canvas ────────────────────────────────────────────────────────
    const content = document.createElement('div');
    Object.assign(content.style, {
      flex: '1', display: 'flex', alignItems: 'center',
      justifyContent: 'center', overflow: 'hidden', width: '100%',
    });

    const wrap = document.createElement('div');
    Object.assign(wrap.style, { position: 'relative', display: 'inline-block' });

    _img = document.createElement('img');
    Object.assign(_img.style, {
      display: 'block',
      maxWidth: '95vw',
      maxHeight: 'calc(100vh - 60px)',
    });
    _img.onload = _onImgLoad;

    _canvas = document.createElement('canvas');
    Object.assign(_canvas.style, {
      position: 'absolute', top: '0', left: '0',
      width: '100%', height: '100%', cursor: 'crosshair',
    });
    _ctx = _canvas.getContext('2d');

    wrap.append(_img, _canvas);
    content.appendChild(wrap);
    _modal.appendChild(content);
    document.body.appendChild(_modal);

    // ── Events ────────────────────────────────────────────────────────────────
    _canvas.addEventListener('click',     _onCanvasClick);
    _canvas.addEventListener('mousemove', _onCanvasMouseMove);
    _canvas.addEventListener('dblclick',  _onCanvasDblClick);
    window.addEventListener('resize',     () => _redraw());

    document.addEventListener('keydown', e => {
      if (e.key === 'Escape' && _modal.style.display !== 'none') motdetExit();
    });
  }

  // ── Image load ─────────────────────────────────────────────────────────────
  function _onImgLoad() {
    _canvas.width  = _img.naturalWidth;
    _canvas.height = _img.naturalHeight;
    _redraw();
  }

  // ── Coordinate helpers ─────────────────────────────────────────────────────
  function _scaleX() {
    const r = _canvas.getBoundingClientRect();
    return r.width / (_canvas.width || 1);
  }

  function _toNat(e) {
    const r  = _canvas.getBoundingClientRect();
    const sx = r.width  / (_canvas.width  || 1);
    const sy = r.height / (_canvas.height || 1);
    return { x: (e.clientX - r.left) / sx, y: (e.clientY - r.top) / sy };
  }

  function _snapRadius() {
    return SNAP_DISPLAY_PX / (_scaleX() || 1);
  }

  // ── Tool selection ─────────────────────────────────────────────────────────
  function motdetSetTool(tool) {
    _tool       = tool;
    _drawing    = null;
    _snapActive = false;

    const btnA = document.getElementById('motdet-tb-arrow');
    const btnD = document.getElementById('motdet-tb-detect');
    if (btnA) btnA.style.background = tool === 'arrow'  ? '#3a7bd5' : '#3a3a3a';
    if (btnD) btnD.style.background = tool === 'detect' ? '#922b21' : '#c0392b';

    _canvas.style.cursor = tool === 'arrow' ? 'default' : 'crosshair';
    if (tool !== 'arrow') _selected = -1;
    _redraw();
  }

  // ── Canvas events ──────────────────────────────────────────────────────────
  function _onCanvasMouseMove(e) {
    const pos = _toNat(e);
    _mouseNat   = pos;
    _snapActive = false;

    if (_tool !== 'arrow' && _drawing && _drawing.points.length >= 3) {
      const fp = _drawing.points[0];
      const r  = _snapRadius();
      const dx = pos.x - fp[0], dy = pos.y - fp[1];
      _snapActive = Math.sqrt(dx * dx + dy * dy) < r;
    }
    _redraw();
  }

  function _onCanvasClick(e) {
    const pos   = _toNat(e);
    const color = ZONE_COLOR[_tool] || ZONE_COLOR.detect;

    if (_tool === 'arrow') {
      _selected = -1;
      for (let i = _zones.length - 1; i >= 0; i--) {
        if (_pointInZone(pos, _zones[i])) { _selected = i; break; }
      }
      _redraw();
      return;
    }

    // Close polygon via snap
    if (_snapActive && _drawing && _drawing.points.length >= 3) {
      _zones.push(_finishDrawing());
      _drawing    = null;
      _snapActive = false;
      _redraw();
      return;
    }

    if (!_drawing) _drawing = { type: _tool, color, points: [] };
    _drawing.points.push([pos.x, pos.y]);
    _redraw();
  }

  function _onCanvasDblClick(e) {
    if (_tool !== 'arrow' && _drawing && _drawing.points.length >= 3) {
      _zones.push(_finishDrawing());
      _drawing    = null;
      _snapActive = false;
      _redraw();
    }
  }

  function _finishDrawing() {
    const W = _canvas.width, H = _canvas.height;
    return {
      type:   _drawing.type,
      color:  _drawing.color,
      points: _drawing.points.map(p => [p[0] / W, p[1] / H]),
    };
  }

  function _pointInZone(pos, zone) {
    const W = _canvas.width, H = _canvas.height;
    const pts = zone.points.map(p => [p[0] * W, p[1] * H]);
    let inside = false, j = pts.length - 1;
    for (let i = 0; i < pts.length; i++) {
      const [xi, yi] = pts[i], [xj, yj] = pts[j];
      if ((yi > pos.y) !== (yj > pos.y) &&
          pos.x < (xj - xi) * (pos.y - yi) / (yj - yi) + xi)
        inside = !inside;
      j = i;
    }
    return inside;
  }

  // ── Rendering ──────────────────────────────────────────────────────────────
  function _redraw() {
    if (!_ctx) return;
    const W = _canvas.width, H = _canvas.height;
    _ctx.clearRect(0, 0, W, H);

    _zones.forEach((zone, idx) => {
      const pts = zone.points.map(p => [p[0] * W, p[1] * H]);
      _drawZone(pts, zone.color, idx === _selected && _flashOn);
    });

    if (_drawing && _drawing.points.length > 0) {
      const pts   = _drawing.points;
      const color = _drawing.color;
      const snap  = _snapActive ? pts[0] : [_mouseNat.x, _mouseNat.y];

      _ctx.strokeStyle = color;
      _ctx.lineWidth   = 2;
      _ctx.setLineDash([]);
      _ctx.beginPath();
      _ctx.moveTo(pts[0][0], pts[0][1]);
      for (let i = 1; i < pts.length; i++) _ctx.lineTo(pts[i][0], pts[i][1]);
      _ctx.lineTo(snap[0], snap[1]);
      _ctx.stroke();

      pts.forEach((p, i) => {
        _ctx.beginPath();
        _ctx.arc(p[0], p[1], 4, 0, Math.PI * 2);
        _ctx.fillStyle = i === 0 ? '#ffffff' : color;
        _ctx.fill();
      });

      if (_snapActive) {
        const r = _snapRadius();
        _ctx.strokeStyle = '#ffffff';
        _ctx.lineWidth   = 1.5;
        _ctx.setLineDash([4, 3]);
        _ctx.beginPath();
        _ctx.arc(pts[0][0], pts[0][1], r, 0, Math.PI * 2);
        _ctx.stroke();
        _ctx.setLineDash([]);
      }
    }
  }

  function _drawZone(pts, color, flash) {
    if (pts.length < 2) return;
    const [r, g, b] = _hexRgb(color);

    _ctx.beginPath();
    _ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) _ctx.lineTo(pts[i][0], pts[i][1]);
    _ctx.closePath();
    _ctx.fillStyle = `rgba(${r},${g},${b},0.22)`;
    _ctx.fill();

    _ctx.beginPath();
    _ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) _ctx.lineTo(pts[i][0], pts[i][1]);
    _ctx.closePath();
    if (flash) {
      _ctx.strokeStyle = '#ffffff';
      _ctx.lineWidth   = 3;
      _ctx.setLineDash([8, 4]);
    } else {
      _ctx.strokeStyle = color;
      _ctx.lineWidth   = 2;
      _ctx.setLineDash([]);
    }
    _ctx.stroke();
    _ctx.setLineDash([]);
  }

  function _hexRgb(hex) {
    return [
      parseInt(hex.slice(1, 3), 16),
      parseInt(hex.slice(3, 5), 16),
      parseInt(hex.slice(5, 7), 16),
    ];
  }

  function _startFlash() {
    if (_flashTimer) return;
    _flashTimer = setInterval(() => {
      _flashOn = !_flashOn;
      if (_selected >= 0) _redraw();
    }, 400);
  }

  function _stopFlash() {
    clearInterval(_flashTimer);
    _flashTimer = null;
    _flashOn    = false;
  }

  // ── Zone actions ───────────────────────────────────────────────────────────
  function motdetClearSelected() {
    if (_drawing) {
      _drawing    = null;
      _snapActive = false;
    } else if (_selected >= 0 && _selected < _zones.length) {
      _zones.splice(_selected, 1);
      _selected = -1;
    }
    _redraw();
  }

  function motdetExit() {
    if (_camId) {
      socket.emit('plugin_action', {
        cam_id: _camId,
        action: 'motdet_save_zones',
        zones:  _zones,
      });
    }
    _modal.style.display = 'none';
    _stopFlash();
    _drawing    = null;
    _selected   = -1;
    _snapActive = false;
  }

  // ── Open modal ─────────────────────────────────────────────────────────────
  window.motdetOpenDraw = function (el) {
    _buildModal();
    const block  = el.closest('.plugin-ui-block');
    const cam_id = block ? block.dataset.cam : (window._selectedCamId || '');
    _camId      = cam_id;
    _zones      = JSON.parse(JSON.stringify(_lastZones[cam_id] || []));
    _drawing    = null;
    _selected   = -1;
    _snapActive = false;

    _img.src = `/plugin/motiondetect/snapshot?cam_id=${encodeURIComponent(cam_id)}&t=${Date.now()}`;
    _modal.style.display = 'flex';
    motdetSetTool('detect');
    _startFlash();
  };

  window.motdetSetTool       = motdetSetTool;
  window.motdetClearSelected = motdetClearSelected;
  window.motdetExit          = motdetExit;

  // ── Sidebar param handlers ─────────────────────────────────────────────────
  function _blk(el)    { return el.closest('.plugin-ui-block'); }
  function _blkCam(el) { const b = _blk(el); return b ? b.dataset.cam : ''; }

  window.motdetOnEnable = function (el) {
    const cam_id = _blkCam(el);
    if (cam_id) socket.emit('set_param', { cam_id, key: 'motdet_enabled', value: el.checked });
  };

  window.motdetOnVarInput = function (el) {
    const block  = _blk(el);
    if (!block) return;
    const lbl = block.querySelector('.motdet-var-label');
    if (lbl) lbl.textContent = el.value;
    _showStreamCircle(block.dataset.cam, block);
  };

  window.motdetOnVarChange = function (el) {
    const block  = _blk(el);
    const cam_id = block ? block.dataset.cam : '';
    if (cam_id) socket.emit('set_param', { cam_id, key: 'motdet_var_threshold', value: parseInt(el.value) });
    if (cam_id) _scheduleHideStreamCircle(cam_id);
  };

  window.motdetOnCountInput = function (el) {
    const block = _blk(el);
    if (!block) return;
    const lbl = block.querySelector('.motdet-count-label');
    if (lbl) lbl.textContent = el.value;
    _showStreamCircle(block.dataset.cam, block);
  };

  window.motdetOnCountChange = function (el) {
    const block  = _blk(el);
    const cam_id = block ? block.dataset.cam : '';
    if (cam_id) socket.emit('set_param', { cam_id, key: 'motdet_min_pixel_count', value: parseInt(el.value) });
    if (cam_id) _scheduleHideStreamCircle(cam_id);
  };

  window.motdetOnCooldownInput = function (el) {
    const block = _blk(el);
    if (!block) return;
    const lbl = block.querySelector('.motdet-cooldown-label');
    if (lbl) lbl.textContent = parseFloat(el.value).toFixed(1);
  };

  window.motdetOnCooldownChange = function (el) {
    const cam_id = _blkCam(el);
    if (cam_id) socket.emit('set_param', { cam_id, key: 'motdet_cooldown_sec', value: parseFloat(el.value) });
  };

  // ── State sync ─────────────────────────────────────────────────────────────
  function _applyMotdetState(s) {
    document.querySelectorAll('.plugin-ui-block[data-plugin="MotionDetect"]').forEach(block => {
      const cid = block.dataset.cam;
      const cs  = (s.cameras && cid) ? s.cameras[cid] : null;
      if (!cs) return;

      const tog = block.querySelector('.motdet-enable');
      const vS  = block.querySelector('.motdet-var-slider');
      const vL  = block.querySelector('.motdet-var-label');
      const cS  = block.querySelector('.motdet-count-slider');
      const cL  = block.querySelector('.motdet-count-label');
      const dS  = block.querySelector('.motdet-cooldown-slider');
      const dL  = block.querySelector('.motdet-cooldown-label');

      if (tog) tog.checked    = !!cs.motdet_enabled;
      if (vS)  vS.value       = cs.motdet_var_threshold   ?? 25;
      if (vL)  vL.textContent = cs.motdet_var_threshold   ?? 25;
      if (cS)  cS.value       = cs.motdet_min_pixel_count ?? 500;
      if (cL)  cL.textContent = cs.motdet_min_pixel_count ?? 500;
      if (dS)  dS.value       = cs.motdet_cooldown_sec    ?? 3.0;
      if (dL)  dL.textContent = (cs.motdet_cooldown_sec   ?? 3.0).toFixed(1);

      if (cs.motdet_zones) _lastZones[cid] = cs.motdet_zones;
    });
  }

  socket.on('state', _applyMotdetState);
  window.addEventListener('plugin-state-update', e => _applyMotdetState(e.detail));

}());
