/* El Paso Seismic Monitor — theme-chrome.js
 * Theme, clocks, status bar, in-window menubar.
 *
 * Classic script sharing one global scope with its siblings; no build step.
 * Load order is fixed in index.html (definitions first, boot.js last):
 *   theme-chrome.js → map.js → util.js → monitor.js → pipeline-panels.js → filters-polling.js → tabs.js → helicorder.js → catalog.js → waveforms.js → review.js → waveviewer.js → pipeline-control.js → boot.js
 */

// ── Theme toggle ────────────────────────────────────────────────
// Light is the implicit default (matches :root in styles.css). Dark is
// applied via data-theme="dark" so the empty/initial state never flashes
// the wrong palette.
var _currentAppTheme = localStorage.getItem('theme') || 'light';
if (_currentAppTheme === 'dark') document.documentElement.setAttribute('data-theme', 'dark');

// Inline SVGs (no extra HTTP request, currentColor-tinted so they pick
// up --text automatically across themes). Path data adapted from Feather
// icon set — single-stroke, geometric, fits "licensed software" tone.
var _SVG_SUN  = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>';
var _SVG_MOON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';

function _applyThemeUI(theme) {
  var icon = document.getElementById('theme-icon');
  var label = document.getElementById('theme-label');
  // Button shows the OPPOSITE of the current theme (the action you'd take)
  if (theme === 'light') {
    if (icon) icon.innerHTML = _SVG_MOON;
    if (label) label.textContent = 'Dark';
  } else {
    if (icon) icon.innerHTML = _SVG_SUN;
    if (label) label.textContent = 'Light';
  }
}

function switchAppTheme(theme) {
  _currentAppTheme = theme;
  if (theme === 'dark') {
    document.documentElement.setAttribute('data-theme', 'dark');
  } else {
    document.documentElement.removeAttribute('data-theme');
  }
  localStorage.setItem('theme', theme);
  _applyThemeUI(theme);
  // Switch main map tiles if map is ready
  if (typeof _setMainMapStyle === 'function' && typeof map !== 'undefined') {
    var mapStyle = theme === 'light' ? 'light' : 'dark';
    if (_setMainMapStyle(mapStyle)) {
      // Sync the map style buttons
      document.querySelectorAll('.map-style-btn').forEach(function(b) {
        b.classList.toggle('active', b.getAttribute('data-style') === mapStyle);
      });
    }
  }
  // Switch review map tiles if it exists
  if (typeof reviewMap !== 'undefined' && reviewMap && typeof _reviewTileLayer !== 'undefined') {
    reviewMap.removeLayer(_reviewTileLayer);
    _reviewTileLayer = makeBasemapLayer(theme).addTo(reviewMap);
  }
  // Redraw any visible waveform canvases so they pick up the new theme colors
  if (typeof reviewState !== 'undefined' && reviewState.traces && reviewState.traces.length > 0) {
    document.querySelectorAll('.review-trace canvas:not(.spectrogram-canvas)').forEach(function(canvas) {
      var net = canvas.dataset.network, sta = canvas.dataset.station, chan = canvas.dataset.channel;
      var tr = reviewState.traces.find(function(t) {
        return t.network === net && t.station === sta && t.channel === chan;
      });
      if (tr && tr.data && tr.data.length > 0) drawReviewCanvas(canvas, tr);
    });
  }
  // Helicorder resolves its colors from CSS vars at draw time
  if (typeof _heliData !== 'undefined' && _heliData) drawHelicorder();
}

document.addEventListener('DOMContentLoaded', function() {
  _applyThemeUI(_currentAppTheme);
  // Legacy floating toggle (removed from index.html; View > Toggle Theme
  // is the control now). Guarded so old cached pages keep working.
  var legacyToggle = document.getElementById('theme-toggle');
  if (legacyToggle) legacyToggle.addEventListener('click', function() {
    switchAppTheme(_currentAppTheme === 'light' ? 'dark' : 'light');
  });
  _initMenubar();
  _startClocks();
});

// ── Application chrome: clocks + status bar ─────────────────────
// The status bar is fed opportunistically from the same polls that fill
// the panels; every setter is null-guarded so partial data never throws.
function _sbSet(id, text) {
  var el = document.getElementById(id);
  if (el) el.textContent = text;
}

function _startClocks() {
  function tick() {
    var now = new Date();
    var utc = now.toISOString().slice(11, 19);
    _sbSet('top-clock', utc + ' UTC');
    _sbSet('sb-clock-utc', now.toISOString().slice(0, 10) + ' ' + utc + ' UTC');
    _sbSet('sb-clock-local', now.toTimeString().slice(0, 8) + ' local');
  }
  tick();
  setInterval(tick, 1000);
}

function _sbConn(state) {  // 'live' | 'polling' | 'offline'
  _sbSet('sb-conn', state);
  var led = document.getElementById('sb-conn-led');
  if (led) led.className = 'led ' +
    (state === 'live' ? 'led-green' : state === 'polling' ? 'led-amber' : 'led-red');
}

// ── In-window menubar ───────────────────────────────────────────
// Replaces the previous pywebview-native menubar. Works identically in
// both the native app window and a regular browser, so we keep a single
// control surface. Menu actions either click() existing in-page buttons
// (export, pipeline start/stop) or POST to /api/admin/* endpoints
// (backup, validate, open folder).

function _menubarCloseAll() {
  document.querySelectorAll('.menu-panel.open').forEach(function(p) {
    p.classList.remove('open');
  });
  document.querySelectorAll('.menu-label.active').forEach(function(l) {
    l.classList.remove('active');
  });
}

function _menubarOpen(label) {
  _menubarCloseAll();
  var panel = document.getElementById('menu-' + label.dataset.menu);
  if (!panel) return;
  var rect = label.getBoundingClientRect();
  panel.style.left = rect.left + 'px';
  panel.style.top = (rect.bottom + 1) + 'px';
  panel.classList.add('open');
  label.classList.add('active');
}

function _initMenubar() {
  var anyOpen = function() {
    return !!document.querySelector('.menu-panel.open');
  };

  document.querySelectorAll('.menu-label').forEach(function(label) {
    label.addEventListener('click', function(e) {
      e.stopPropagation();
      if (label.classList.contains('active')) {
        _menubarCloseAll();
      } else {
        _menubarOpen(label);
      }
    });
    // Once any menu is open, hovering siblings flips to theirs (native feel)
    label.addEventListener('mouseenter', function() {
      if (anyOpen() && !label.classList.contains('active')) _menubarOpen(label);
    });
  });

  document.querySelectorAll('.menu-item').forEach(function(item) {
    item.addEventListener('click', function(e) {
      e.stopPropagation();
      _menubarCloseAll();
      _menubarAction(item.dataset.action);
    });
  });

  // Click anywhere else, or Escape, closes the open menu.
  document.addEventListener('click', _menubarCloseAll);
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') _menubarCloseAll();
  });
}

function _menubarAction(action) {
  switch (action) {
    // File
    case 'export-csv':       document.getElementById('export-csv').click(); break;
    case 'export-quakeml':   document.getElementById('export-quakeml').click(); break;
    case 'backup':           _menubarAdminPost('/api/admin/backup', 'Backup'); break;
    case 'quit':             _menubarQuit(); break;
    // Pipeline
    case 'start-continuous': document.getElementById('start-continuous').click(); break;
    case 'start-backfill':   document.getElementById('start-backfill').click(); break;
    case 'stop':             document.getElementById('btn-stop-pipeline').click(); break;
    case 'validate':         _menubarAdminPost('/api/admin/validate', 'Validate environment'); break;
    case 'open-logs':        _openExternal('logs',   'logs folder'); break;
    case 'open-output':      _openExternal('output', 'output folder'); break;
    // View
    case 'toggle-theme':     switchAppTheme(_currentAppTheme === 'light' ? 'dark' : 'light'); break;
    case 'reload':           location.reload(); break;
    case 'tab-overview':     switchTab('overview'); break;
    case 'tab-monitor':      switchTab('monitor'); break;
    case 'tab-pipeline':     switchTab('pipeline'); break;
    case 'tab-catalog':      switchTab('catalog'); break;
    case 'tab-waveviewer':   switchTab('waveviewer'); break;
    case 'tab-helicorder':   switchTab('helicorder'); break;
    // Help
    case 'about':            _menubarAbout(); break;
    case 'open-readme':      _openExternal('readme', 'README'); break;
    case 'shortcuts':        _menubarShortcuts(); break;
  }
}

function _openExternal(which, label) {
  fetch('/api/admin/open-folder?which=' + encodeURIComponent(which), {method: 'POST'})
    .then(function(r) {
      if (!r.ok) return r.json().then(function(d) {
        var msg = (typeof d.detail === 'string') ? d.detail : ('HTTP ' + r.status);
        throw new Error(msg);
      });
    })
    .catch(function(e) { alert('Could not open ' + label + ': ' + (e && e.message ? e.message : e)); });
}

function _menubarQuit() {
  if (!confirm('Quit the dashboard?')) return;
  fetch('/api/admin/quit', {method: 'POST'}).catch(function() {});
}

function _menubarAdminPost(url, label) {
  _showCmdOutput(label + '…', 'Running…');
  fetch(url, {method: 'POST'})
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var title = label + (d.ok ? ' — OK' : ' — FAILED (exit ' + d.returncode + ')');
      var out = (d.stdout || '').trim();
      var err = (d.stderr || '').trim();
      var body = '';
      if (out) body += out;
      if (err) body += (body ? '\n\n--- stderr ---\n' : '') + err;
      if (!body) body = '(no output)';
      _showCmdOutput(title, body);
    })
    .catch(function(e) { _showCmdOutput(label + ' — request failed', String(e)); });
}

function _showCmdOutput(title, body) {
  var modal = document.getElementById('cmd-output-modal');
  document.getElementById('cmd-output-title').textContent = title;
  document.getElementById('cmd-output-body').textContent = body;
  modal.classList.add('open');
}
(function() {
  var modal = document.getElementById('cmd-output-modal');
  var closeBtn = document.getElementById('cmd-output-close');
  if (closeBtn) closeBtn.addEventListener('click', function() { modal.classList.remove('open'); });
  if (modal) modal.addEventListener('click', function(e) {
    if (e.target === modal) modal.classList.remove('open');
  });
})();

function _menubarAbout() {
  alert(
    'El Paso Seismic Monitor\n\n' +
    'Local seismic detection, association, and cataloging for the\n' +
    'El Paso / southern Rio Grande Rift area.\n\n' +
    '  Detection         PhaseNet (SeisBench)\n' +
    '  Association       GaMMA (BGMM)\n' +
    '  Bulk location     GaMMA\n' +
    '  Review relocation NonLinLoc\n\n' +
    'by Marc Garcia'
  );
}

function _menubarShortcuts() {
  alert(
    'Review mode shortcuts\n\n' +
    '  P / S      Add P or S pick at cursor\n' +
    '  D          Delete-pick mode\n' +
    '  G          Drag-pick mode\n' +
    '  R          Relocate\n' +
    '  Ctrl+Z     Undo\n' +
    '  Ctrl+Y     Redo\n' +
    '  c          Confirm event\n' +
    '  x          Reject event\n' +
    '  n          Next unreviewed\n' +
    '  Esc        Close review panel'
  );
}
