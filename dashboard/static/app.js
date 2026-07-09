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
  if (typeof mapTileLayers !== 'undefined' && typeof currentTileLayer !== 'undefined' && typeof map !== 'undefined') {
    var mapStyle = theme === 'light' ? 'light' : 'dark';
    if (mapTileLayers[mapStyle] !== currentTileLayer) {
      map.removeLayer(currentTileLayer);
      currentTileLayer = mapTileLayers[mapStyle];
      currentTileLayer.addTo(map);
      // Sync the map style buttons
      document.querySelectorAll('.map-style-btn').forEach(function(b) {
        b.classList.toggle('active', b.getAttribute('data-style') === mapStyle);
      });
    }
  }
  // Switch review map tiles if it exists
  if (typeof reviewMap !== 'undefined' && reviewMap && typeof _reviewTileLayer !== 'undefined') {
    reviewMap.removeLayer(_reviewTileLayer);
    var url = theme === 'light'
      ? 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png'
      : 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png';
    _reviewTileLayer = L.tileLayer(url, {
      attribution: '&copy; CARTO &copy; OSM', subdomains: 'abcd', maxZoom: 19
    }).addTo(reviewMap);
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

// ── Constants ───────────────────────────────────────────────────
const STEP_COLORS = {
  ingest: '#3b82f6', process: '#f59e0b', detect: '#22c55e',
  associate: '#a855f7', catalog: '#64748b'
};
const STEP_LABELS = {
  ingest: '1 Ingest', process: '2 Process', detect: '3 Detect',
  associate: '4 Associate', catalog: '5 Catalog'
};
const STEP_SHORT = {
  ingest: 'Ingest', process: 'Process', detect: 'Detect',
  associate: 'Assoc', catalog: 'Catalog'
};
const STATUS_ICON = {
  pending: '', running: '\u25b6', completed: '\u2713', failed: '\u2717', skipped: '\u2014'
};
const HEALTH_COLORS = { ok: '#10b981', warning: '#f59e0b', error: '#ef4444', unknown: '#64748b' };

// ── State ───────────────────────────────────────────────────────
let filterStartDate = '';
let filterEndDate = '';
let apiFailCount = 0;

// ── Map setup ───────────────────────────────────────────────────
const MAP_INIT_VIEW = [31.85, -106.40];
const MAP_INIT_ZOOM = 10;
const map = L.map('map', {
  zoomControl: false,        // we render a custom control top-right (see below)
  zoomSnap: 0,               // allow any fractional zoom level — smooth feel
  zoomDelta: 0.5,            // half-step per +/- click instead of full integer
  wheelPxPerZoomLevel: 60,   // scroll responsiveness — lower = faster zoom per scroll tick
  zoomAnimation: true,
}).setView(MAP_INIT_VIEW, MAP_INIT_ZOOM);

var mapTileLayers = {
  dark: L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; <a href="https://carto.com/">CARTO</a> &copy; <a href="https://www.openstreetmap.org/">OSM</a>',
    subdomains: 'abcd', maxZoom: 19
  }),
  light: L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; <a href="https://carto.com/">CARTO</a> &copy; <a href="https://www.openstreetmap.org/">OSM</a>',
    subdomains: 'abcd', maxZoom: 19
  }),
  satellite: L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
    attribution: '&copy; <a href="https://www.esri.com/">Esri</a> &mdash; Sources: Esri, Maxar, Earthstar',
    maxZoom: 19
  }),
  osm: L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19
  }),
};
var _initMapStyle = _currentAppTheme === 'light' ? 'light' : 'dark';
var currentTileLayer = mapTileLayers[_initMapStyle];
currentTileLayer.addTo(map);
// Sync map style buttons to match theme
document.querySelectorAll('.map-style-btn').forEach(function(b) {
  b.classList.toggle('active', b.getAttribute('data-style') === _initMapStyle);
});

document.querySelectorAll('.map-style-btn').forEach(function(btn) {
  btn.addEventListener('click', function() {
    var style = btn.getAttribute('data-style');
    if (mapTileLayers[style] === currentTileLayer) return;
    map.removeLayer(currentTileLayer);
    currentTileLayer = mapTileLayers[style];
    currentTileLayer.addTo(map);
    document.querySelectorAll('.map-style-btn').forEach(function(b) { b.classList.remove('active'); });
    btn.classList.add('active');
  });
});

// Study area bounds (center ±1.0° ≈ ±110 km) — togglable, hidden by default.
// Matches the NLLoc travel-time grid extent (GRID_X_HALF_KM / GRID_Y_HALF_KM
// in lib/location/nlloc_config.py), so the rectangle visually represents
// the boundary beyond which NLLoc cannot precisely locate events.
var studyAreaRect = L.rectangle(
  [[31.85 - 1.0, -106.40 - 1.0], [31.85 + 1.0, -106.40 + 1.0]],
  { color: '#ef4444', weight: 1.5, dashArray: '6 4', fill: false, interactive: false }
);
var studyAreaVisible = false;
document.getElementById('toggle-study-area').addEventListener('click', function() {
  var btn = this;
  if (studyAreaVisible) {
    map.removeLayer(studyAreaRect);
    studyAreaVisible = false;
    btn.style.background = '';
  } else {
    studyAreaRect.addTo(map);
    studyAreaVisible = true;
    btn.style.background = 'var(--blue)';
  }
});

// ── Fault layer (USGS Quaternary Faults, served locally) ────────
map.createPane('faults').style.zIndex = 350;
var faultLayer = L.layerGroup({ pane: 'faults' });
var faultsVisible = false;
var faultsLoaded = false;

document.getElementById('toggle-faults').addEventListener('click', function() {
  var btn = this;
  if (!faultsLoaded) {
    btn.textContent = 'Loading\u2026';
    fetch('/api/faults').then(function(r) { return r.json(); }).then(function(data) {
      data.features.forEach(function(feature) {
        if (!feature.geometry || !feature.geometry.coordinates) return;
        var coords = feature.geometry.coordinates.map(function(c) { return [c[1], c[0]]; });
        var p = feature.properties;
        var html = '<b>' + esc(p.fault_name || 'Unnamed') + '</b>';
        if (p.slip_sense) html += '<br>Slip: ' + esc(p.slip_sense);
        if (p.slip_rate) html += '<br>Rate: ' + esc(p.slip_rate);
        if (p.age) html += '<br>Age: ' + esc(p.age);
        // Glow layer (wider, semi-transparent)
        L.polyline(coords, {
          pane: 'faults', color: '#ff6600', weight: 5, opacity: 0.25, interactive: false
        }).addTo(faultLayer);
        // Core line
        L.polyline(coords, {
          pane: 'faults', color: '#ff4422', weight: 1.5, opacity: 0.9
        }).bindPopup(html).addTo(faultLayer);
      });
      faultLayer.addTo(map);
      faultsLoaded = true;
      faultsVisible = true;
      btn.classList.add('active');
      btn.textContent = 'Faults';
    }).catch(function() {
      btn.textContent = 'Faults';
    });
  } else {
    faultsVisible = !faultsVisible;
    if (faultsVisible) {
      faultLayer.addTo(map);
      btn.classList.add('active');
    } else {
      map.removeLayer(faultLayer);
      btn.classList.remove('active');
    }
  }
});

// ── Quarry layer (MSHA + OSM — lazy-load on toggle) ──
// Markers are diamonds (distinct from event circles + station triangles).
// MSHA features (authoritative + currently active) render bigger and in
// commodity colours; OSM features (community-tagged, may not be active)
// render smaller and muted so the visual hierarchy reflects data quality.
map.createPane('quarries').style.zIndex = 360;
var quarryLayer = L.layerGroup({ pane: 'quarries' });
var quarriesVisible = false;
var quarriesLoaded = false;

var QUARRY_COLORS = {
  'Stone':         '#a855f7',   // purple
  'SandAndGravel': '#eab308',   // mustard
  'Nonmetal':      '#06b6d4',   // cyan
  'Mine':          '#fb923c',   // orange
  'default':       '#94a3b8',   // slate
};

function _quarryColor(commodity) {
  return QUARRY_COLORS[commodity] || QUARRY_COLORS.default;
}

// Crossed hammer + pick — the standard USGS topo-map symbol for mines.
// Larger + saturated for MSHA (verified active); smaller + muted for OSM.
function _quarryIcon(commodity, source) {
  var size = source === 'MSHA' ? 20 : 16;
  var fill = source === 'MSHA' ? _quarryColor(commodity) : '#9ca3af';
  var handle = source === 'MSHA' ? '#451a03' : '#6b7280';  // dark wood / gray
  var opacity = source === 'MSHA' ? 1.0 : 0.7;
  // SVG coords in a 20-unit viewBox; scales to 'size' via width/height
  var svg =
    '<svg width="' + size + '" height="' + size + '" viewBox="0 0 20 20"' +
      ' style="overflow:visible;opacity:' + opacity +
      ';filter:drop-shadow(0 1px 1.5px rgba(0,0,0,0.45))">' +
      // Pickaxe handle: upper-left to lower-right diagonal
      '<line x1="5" y1="5" x2="16" y2="16" stroke="' + handle +
        '" stroke-width="2" stroke-linecap="round"/>' +
      // Hammer handle: upper-right to lower-left diagonal
      '<line x1="15" y1="5" x2="4" y2="16" stroke="' + handle +
        '" stroke-width="2" stroke-linecap="round"/>' +
      // Pickaxe head: narrow triangle, upper-left
      '<path d="M 0.5 3.5 L 6.5 0.5 L 6 7 Z" fill="' + fill +
        '" stroke="#fff" stroke-width="0.8" stroke-linejoin="round"/>' +
      // Hammer head: blocky rectangle, upper-right
      '<path d="M 12.5 0.5 L 19.5 0.5 L 19.5 5.5 L 13.5 5.5 Z" fill="' + fill +
        '" stroke="#fff" stroke-width="0.8" stroke-linejoin="round"/>' +
    '</svg>';
  return L.divIcon({
    className: '',
    iconSize: [size, size],
    // Anchor at the visual center of the crossed handles (~60% down)
    iconAnchor: [size / 2, Math.round(size * 0.6)],
    html: svg,
  });
}

document.getElementById('toggle-quarries').addEventListener('click', function() {
  var btn = this;
  if (!quarriesLoaded) {
    btn.textContent = 'Loading…';
    fetch('/api/quarries').then(function(r) { return r.json(); }).then(function(data) {
      (data.features || []).forEach(function(feature) {
        if (!feature.geometry || !feature.geometry.coordinates) return;
        var c = feature.geometry.coordinates;  // [lon, lat]
        var p = feature.properties || {};
        var srcBadge = p.source === 'MSHA'
          ? '<span style="background:var(--blue);color:#fff;padding:1px 5px;border-radius:3px;font-size:.55rem">MSHA · ' + esc(p.status || '') + '</span>'
          : '<span style="background:var(--border);color:var(--muted);padding:1px 5px;border-radius:3px;font-size:.55rem">OSM</span>';
        var html =
          '<b>' + esc(p.name || 'Unnamed quarry') + '</b> &nbsp;' + srcBadge +
          (p.operator ? '<br>Operator: ' + esc(p.operator) : '') +
          (p.type ? '<br>Type: ' + esc(p.type) : '') +
          '<br>Commodity: ' + esc(p.commodity || '—') +
          (p.county || p.state
            ? '<br>' + esc([p.county, p.state].filter(Boolean).join(', '))
            : '') +
          '<br><span style="color:var(--muted);font-size:.6rem">ID: ' + esc(p.mine_id || '') + '</span>';
        L.marker([c[1], c[0]], {
          pane: 'quarries',
          icon: _quarryIcon(p.commodity, p.source),
        }).bindPopup(html).addTo(quarryLayer);
      });
      quarryLayer.addTo(map);
      quarriesLoaded = true;
      quarriesVisible = true;
      btn.classList.add('active');
      btn.textContent = 'Quarries';
    }).catch(function() {
      btn.textContent = 'Quarries';
    });
  } else {
    quarriesVisible = !quarriesVisible;
    if (quarriesVisible) {
      quarryLayer.addTo(map);
      btn.classList.add('active');
    } else {
      map.removeLayer(quarryLayer);
      btn.classList.remove('active');
    }
  }
});

const stationLayer = L.layerGroup().addTo(map);
const eventLayer = L.layerGroup().addTo(map);
var _pulseMarker = null;

function clearPulseMarker() {
  if (_pulseMarker) { map.removeLayer(_pulseMarker); _pulseMarker = null; }
}

function showPulseMarker(lat, lon) {
  clearPulseMarker();
  _pulseMarker = L.marker([lat, lon], {
    icon: L.divIcon({
      className: '',
      iconSize: [0, 0],
      iconAnchor: [0, 0],
      html: '<div class="event-pulse-div"></div>',
    }),
    interactive: false,
  }).addTo(map);
}

// ── Station triangle icon (color-aware) ─────────────────────────
// Upward triangle is the standard seismic-station symbol (USGS/IRIS).
// Slight drop shadow for depth, white stroke for contrast on any base map,
// rounded line joins so the apex doesn't look spiky.
function stationIcon(color) {
  color = color || '#10b981';
  return L.divIcon({
    className: '',
    iconSize: [18, 18],
    iconAnchor: [9, 15],
    html:
      '<svg width="18" height="18" viewBox="0 0 18 18" style="overflow:visible">' +
        '<polygon points="9,2 16,15 2,15"' +
          ' fill="' + esc(color) + '"' +
          ' stroke="#ffffff"' +
          ' stroke-width="1.5"' +
          ' stroke-linejoin="round"' +
          ' style="filter:drop-shadow(0 1px 1.5px rgba(0,0,0,0.45))"/>' +
      '</svg>'
  });
}

// ── Event color interpolation (amber to red by index) ────────────
// Viridis colormap — maps magnitude to color
var _viridis = [
  [0.267004, 0.004874, 0.329415],
  [0.282327, 0.140926, 0.457517],
  [0.253935, 0.265254, 0.529983],
  [0.206756, 0.371758, 0.553117],
  [0.163625, 0.471133, 0.558148],
  [0.127568, 0.566949, 0.550556],
  [0.134692, 0.658636, 0.517649],
  [0.266941, 0.748751, 0.440573],
  [0.477504, 0.821444, 0.318195],
  [0.741388, 0.873449, 0.149561],
  [0.993248, 0.906157, 0.143936],
];
function eventColor(mag) {
  var minM = 0, maxM = 4;
  var t = Math.max(0, Math.min(1, ((mag || 0) - minM) / (maxM - minM)));
  var idx = t * (_viridis.length - 1);
  var lo = Math.floor(idx), hi = Math.min(lo + 1, _viridis.length - 1);
  var f = idx - lo;
  var r = Math.round((_viridis[lo][0] * (1 - f) + _viridis[hi][0] * f) * 255);
  var g = Math.round((_viridis[lo][1] * (1 - f) + _viridis[hi][1] * f) * 255);
  var b = Math.round((_viridis[lo][2] * (1 - f) + _viridis[hi][2] * f) * 255);
  return 'rgb(' + r + ',' + g + ',' + b + ')';
}

// ── Helpers ─────────────────────────────────────────────────────
function formatTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// Format an ISO UTC timestamp in the browser's local timezone, including
// the timezone abbreviation (e.g. "Oct 22, 2025, 12:43:14 PM MDT").
// Each user sees their own local time — Marc in El Paso sees MDT, a
// collaborator in California would see PDT, etc.
function formatLocalTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'long' });
  } catch (e) {
    return '';
  }
}

function fmtDur(sec) {
  if (sec == null) return '\u2014';
  sec = Math.round(sec);
  if (sec < 60) return sec + 's';
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return m + 'm ' + String(s).padStart(2, '0') + 's';
  const h = Math.floor(m / 60), rm = m % 60;
  return h + 'h ' + String(rm).padStart(2, '0') + 'm';
}

function esc(s) {
  if (s == null) return '';
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

function showLoading(id) {
  const el = document.getElementById(id);
  if (el) el.classList.add('active');
}

function hideLoading(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove('active');
}

function showErrorBanner(msg) {
  const banner = document.getElementById('error-banner');
  document.getElementById('error-banner-text').textContent = msg;
  banner.classList.add('visible');
}

function hideErrorBanner() {
  document.getElementById('error-banner').classList.remove('visible');
}

document.getElementById('error-banner-dismiss').addEventListener('click', hideErrorBanner);

// ── API fetch with error handling ───────────────────────────────
async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const ct = r.headers.get('content-type') || '';
  if (r.status === 304) return null;
  return r.json();
}

function onApiSuccess() {
  apiFailCount = 0;
  hideErrorBanner();
}

function onApiFailure(e) {
  apiFailCount++;
  if (apiFailCount >= 3) {
    showErrorBanner('Cannot reach API server. Retrying...');
    _sbConn('offline');
  }
}

// ── Render pipeline status + step strip ─────────────────────────
function renderStatus(data) {
  if (!data) return;
  const p = data.pipeline;
  const steps = data.steps || [];

  const mode = p.mode || 'single';
  _sbSet('sb-pipeline', p.status + (mode === 'continuous' ? ' · continuous' : ''));

  // Status badge, subheader, and step-strip elements were removed from the
  // top of the page in favor of the Start/Stop button as the single state
  // indicator. Guard against their absence so older index.html versions
  // (and any test pages) keep working.
  const badge = document.getElementById('pipeline-badge');
  if (badge) {
    const label = p.status + (mode === 'continuous' ? ' \u00b7 continuous' : '');
    badge.textContent = label;
    badge.className = 'badge badge-' + esc(p.status);
  }

  const sub = document.getElementById('subheader');
  if (sub) {
    let parts = [];
    if (p.started_at) parts.push('Started: ' + formatTime(p.started_at));
    if (mode === 'continuous' && p.continuous) {
      parts.push('Day: ' + (p.continuous.current_day || '?'));
    } else if (p.args && (p.args.start || p.args.end)) {
      parts.push('Date range: ' + (p.args.start || '?') + ' to ' + (p.args.end || '?'));
    }
    sub.textContent = parts.join(' \u00b7 ');
  }

  const strip = document.getElementById('step-strip');
  if (strip) {
    strip.innerHTML = steps.map(function(s, i) {
      var icon = STATUS_ICON[s.status] || '';
      var chip = '<span class="step-chip st-' + esc(s.status) + '">' + (icon ? icon + ' ' : '') + esc(STEP_LABELS[s.name] || s.name) + '</span>';
      return (i > 0 ? '<span class="arrow">\u2192</span>' : '') + chip;
    }).join('');
  }

  // Sync Start/Stop buttons with pipeline state
  if (typeof updateControlButtons === 'function') {
    updateControlButtons(p.status === 'running' || p.status === 'waiting');
  }
}

// ── Render stations on map ──────────────────────────────────────
let stationsLoaded = false;
const stationMarkers = {};

function renderStations(stations) {
  if (stationsLoaded) return;
  stationsLoaded = true;
  stations.forEach(function(s) {
    var m = L.marker([s.latitude, s.longitude], { icon: stationIcon() })
      .bindTooltip('<b>' + esc(s.station) + '</b><br>' + esc(s.network) + ' \u00b7 ' + esc(s.model) + '<br>' + esc(s.channels), { className: '' })
      .addTo(stationLayer);
    m.on('click', function() { openStationDetail(s.station); });
    stationMarkers[s.station] = { marker: m, data: s };
  });
}

// ── Station state-of-health panel (marker click) ────────────────
// Latency thresholds account for the ~30 h day-batch ingest lag:
// data ending "yesterday" is the healthy steady state.
function _latencyClass(h) {
  if (h == null) return { cls: 'sd-gray', label: 'no data' };
  if (h <= 36) return { cls: 'sd-ok', label: 'nominal' };
  if (h <= 60) return { cls: 'sd-warn', label: 'lagging' };
  return { cls: 'sd-bad', label: 'stale' };
}

function openStationDetail(station) {
  var overlay = document.getElementById('event-detail');
  var body = document.getElementById('detail-body');
  var title = document.getElementById('detail-title');
  title.textContent = 'Station ' + station;
  body.innerHTML = '<div class="empty-state">Loading…</div>';
  overlay.classList.add('visible');

  fetchJSON('/api/station/' + encodeURIComponent(station) + '/detail')
    .then(function(d) {
      var lat = _latencyClass(d.latency_hours);
      var maxFiles = Math.max.apply(null, d.days.map(function(x) { return x.files; }).concat([1]));
      var bars = d.days.map(function(x) {
        var hpx = x.files > 0 ? Math.max(4, Math.round(22 * x.files / maxFiles)) : 2;
        return '<span class="sd-bar' + (x.files === 0 ? ' sd-bar-empty' : '') +
          '" style="height:' + hpx + 'px" title="' + esc(x.date) + ': ' +
          x.files + ' file(s)"></span>';
      }).join('');
      body.innerHTML =
        '<div class="detail-row"><span>Network / model</span><b>' + esc(d.network) + ' · ' + esc(d.model || '?') + '</b></div>' +
        '<div class="detail-row"><span>Channels</span><b>' + esc(d.channels || '?') + '</b></div>' +
        '<div class="detail-row"><span>Elevation</span><b>' + (d.elevation_m != null ? d.elevation_m + ' m' : '—') + '</b></div>' +
        '<div class="detail-row"><span>Data latency</span><b class="' + lat.cls + '">' +
          (d.latency_hours != null ? d.latency_hours + ' h (' + lat.label + ')' : lat.label) + '</b></div>' +
        '<div class="detail-row"><span>Newest data</span><b>' + esc(d.last_data_utc ? d.last_data_utc.slice(0, 19).replace('T', ' ') + ' UTC' : '—') + '</b></div>' +
        '<div class="detail-row"><span>Coverage (' + d.days_window + ' d)</span><b>' + d.days_covered + '/' + d.days_window + ' days</b></div>' +
        '<div class="sd-days">' + bars + '</div>' +
        '<div class="sd-days-label">raw files per day · last ' + d.days_window + ' days</div>';
    })
    .catch(function(err) {
      body.innerHTML = '<div class="empty-state">Failed to load: ' + esc(err.message) + '</div>';
    });
}

// ── Update station health on map ────────────────────────────────
function renderStationHealth(healthData) {
  var HEALTH_LABEL = { ok: 'OK', warning: 'Intermittent', error: 'Offline', unknown: 'Unknown' };
  var up = healthData.filter(function(h) { return h.status === 'ok'; }).length;
  _sbSet('sb-stations', up + '/' + healthData.length + ' up');
  healthData.forEach(function(h) {
    var entry = stationMarkers[h.station];
    if (!entry) return;
    var color = HEALTH_COLORS[h.status] || HEALTH_COLORS.unknown;
    entry.marker.setIcon(stationIcon(color));
    var s = entry.data;
    entry.marker.setTooltipContent(
      '<b>' + esc(s.station) + '</b><br>' + esc(s.network) + ' \u00b7 ' + esc(s.model) + '<br>' + esc(s.channels) + '<br>Health: <b>' + esc(HEALTH_LABEL[h.status] || h.status) + '</b>'
    );
  });
}

// ── Render events on map (click for detail) ─────────────────────
let allEvents = [];

// Fixed palette per event category. One solid colour per type; magnitude
// is encoded in dot radius (see _eventRadius), not in colour.
var EVENT_TYPE_COLORS = {
  earthquake:   '#3b82f6',   // blue
  quarry_blast: '#f59e0b',   // amber
  unreviewed:   '#94a3b8',   // muted gray
};

function eventMapStyle(e) {
  // Fill colour + stroke colour keyed to review status and event type.
  var status = e.review_status || '';
  var type = e.event_type || '';
  if (status === 'confirmed') {
    if (type === 'quarry_blast') {
      return { fill: EVENT_TYPE_COLORS.quarry_blast, stroke: '#78350f' };
    }
    // Earthquake (and undetermined-but-confirmed): solid blue, size = magnitude.
    return { fill: EVENT_TYPE_COLORS.earthquake, stroke: '#1e3a8a' };
  }
  // Unreviewed (or any other non-rejected state).
  return { fill: EVENT_TYPE_COLORS.unreviewed, stroke: '#334155' };
}

// Map marker radius from magnitude. Min ~3px so M~0 events stay visible;
// climbs ~2px per magnitude unit so M2 ≈ 7px, M4 ≈ 11px.
function _eventRadius(mag) {
  return 3 + Math.max(0, (mag || 0)) * 2;
}

// Build an event marker icon based on type (sized by magnitude). Three shapes:
//   earthquake   → shockwave: centre dot + 8 radiating spokes (propagating wavefront)
//   quarry_blast → explosion burst: 8-point jagged star (sudden energy release)
//   unreviewed   → simple filled circle (unclassified)
// All shapes use a 20×20 SVG viewBox so the same coords scale to any pixel size.
function _eventIconSVG(type, sizePx, fill, stroke) {
  var inner = '';
  if (type === 'quarry_blast') {
    // 16 alternating outer/inner vertices → 8-point burst star
    var pts = [];
    for (var i = 0; i < 16; i++) {
      var ang = (i / 16) * Math.PI * 2 - Math.PI / 2;
      var r = (i % 2 === 0) ? 9.0 : 3.6;
      pts.push((10 + r * Math.cos(ang)).toFixed(2) + ',' + (10 + r * Math.sin(ang)).toFixed(2));
    }
    inner =
      '<polygon points="' + pts.join(' ') + '" fill="' + fill +
        '" stroke="' + stroke + '" stroke-width="1" stroke-linejoin="round"/>';
  } else if (type === 'earthquake') {
    // 8 spokes (cardinal + diagonals) + filled centre dot
    inner =
      '<g stroke="' + fill + '" stroke-width="1.6" stroke-linecap="round" fill="none">' +
        '<line x1="10" y1="0.5" x2="10" y2="4.5"/>' +
        '<line x1="10" y1="19.5" x2="10" y2="15.5"/>' +
        '<line x1="0.5" y1="10" x2="4.5" y2="10"/>' +
        '<line x1="19.5" y1="10" x2="15.5" y2="10"/>' +
        '<line x1="3.2" y1="3.2" x2="6.0" y2="6.0"/>' +
        '<line x1="16.8" y1="3.2" x2="14.0" y2="6.0"/>' +
        '<line x1="3.2" y1="16.8" x2="6.0" y2="14.0"/>' +
        '<line x1="16.8" y1="16.8" x2="14.0" y2="14.0"/>' +
      '</g>' +
      '<circle cx="10" cy="10" r="3.6" fill="' + fill +
        '" stroke="' + stroke + '" stroke-width="1"/>';
  } else {
    // Unreviewed / default — simple disc
    inner =
      '<circle cx="10" cy="10" r="6" fill="' + fill +
        '" stroke="' + stroke + '" stroke-width="1.2"/>';
  }
  return '<svg width="' + sizePx + '" height="' + sizePx + '"' +
    ' viewBox="0 0 20 20" style="overflow:visible;' +
    'filter:drop-shadow(0 1px 1px rgba(0,0,0,0.35))">' +
    inner + '</svg>';
}

function _eventIcon(e) {
  var style = eventMapStyle(e);
  // Pick shape: confirmed earthquake / quarry_blast / everything else = unreviewed
  var type;
  if (e.review_status === 'confirmed') {
    type = (e.event_type === 'quarry_blast') ? 'quarry_blast' : 'earthquake';
  } else {
    type = 'unreviewed';
  }
  // Detail icons (spokes / points) need a bit more space than a filled circle
  // to stay readable; bump min size + use radius*2.5 instead of *2.
  var minSize = (type === 'unreviewed') ? 8 : 14;
  var sizePx = Math.max(minSize, _eventRadius(e.magnitude) * 2.5);
  return L.divIcon({
    className: '',
    iconSize: [sizePx, sizePx],
    iconAnchor: [sizePx / 2, sizePx / 2],
    html: _eventIconSVG(type, sizePx, style.fill, style.stroke),
  });
}

function shouldRenderEventOnMap(e) {
  // Hide rejected events entirely. Hide confirmed noise (not an arrival
  // worth showing on a geographic map).
  if (e.review_status === 'rejected') return false;
  if (e.review_status === 'confirmed' && e.event_type === 'noise') return false;
  return true;
}

// ── New-event notifications ─────────────────────────────────────
// Toast when the catalog grows while the app is open. The known-ID set
// only ever grows, so filter changes can never resurrect a "new" event;
// the first fetch seeds silently.
var _knownEventIds = null;

function _notifyNewEvents(events) {
  if (_knownEventIds === null) {
    _knownEventIds = new Set(events.map(function(e) { return e.event_id; }));
    return;
  }
  events.forEach(function(e) {
    if (_knownEventIds.has(e.event_id)) return;
    _knownEventIds.add(e.event_id);
    _showToast(e);
  });
}

function _showToast(e) {
  var stack = document.getElementById('toast-stack');
  if (!stack) {
    stack = document.createElement('div');
    stack.id = 'toast-stack';
    document.body.appendChild(stack);
  }
  var t = document.createElement('div');
  t.className = 'toast';
  var mag = e.magnitude != null ? 'M' + Number(e.magnitude).toFixed(1) : 'M?';
  var when = (e.time || '').slice(11, 16) + ' UTC';
  t.innerHTML = '<span class="toast-dot"></span><b>New event</b> ' +
    esc(mag) + ' · ' + esc(when) + ' · ' + esc(e.event_id) +
    '<button class="toast-x" aria-label="Dismiss">&times;</button>';
  t.addEventListener('click', function(ev) {
    if (ev.target.classList.contains('toast-x')) { t.remove(); return; }
    if (activeTab !== 'monitor') switchTab('monitor');
    if (e.latitude != null && e.longitude != null) map.flyTo([e.latitude, e.longitude], 13);
    showEventDetail(e.event_id);
    t.remove();
  });
  stack.appendChild(t);
  setTimeout(function() { if (t.parentNode) t.remove(); }, 12000);
}

function renderEvents(events) {
  _notifyNewEvents(events);
  allEvents = events;
  eventLayer.clearLayers();
  if (!events || events.length === 0) return;
  events.forEach(function(e) {
    if (e.latitude == null || e.longitude == null) return;
    if (!shouldRenderEventOnMap(e)) return;
    L.marker([e.latitude, e.longitude], { icon: _eventIcon(e) })
    .bindTooltip(
      '<b>' + esc(e.event_id) + '</b><br>' + esc(e.time) + '<br><span style="color:#94a3b8;font-size:.7rem">' + esc(formatLocalTime(e.time)) + '</span><br>M ' + (e.magnitude != null ? e.magnitude.toFixed(1) : '?') + ' \u00b7 ' + (e.depth_km != null ? e.depth_km.toFixed(1) : '?') + ' km<br>' + (e.num_picks || 0) + ' picks',
      { className: '' }
    )
    .on('click', function() { showEventDetail(e.event_id); })
    .addTo(eventLayer);
  });
}

// ── Event detail overlay ────────────────────────────────────────
function showEventDetail(eventId) {
  var overlay = document.getElementById('event-detail');
  var body = document.getElementById('detail-body');
  var title = document.getElementById('detail-title');
  title.textContent = 'Loading...';
  body.innerHTML = '<div class="spinner" style="margin:.5rem auto;display:block"></div>';
  overlay.classList.add('visible');

  fetchJSON('/api/event/' + encodeURIComponent(eventId))
    .then(function(data) {
      if (!data) return;
      var ev = data.event;
      title.textContent = esc(ev.event_id);
      var html = '';
      html += '<div class="detail-row"><span class="detail-label">Time</span><span class="detail-value">' + esc(ev.time) + '<div class="local-time">' + esc(formatLocalTime(ev.time)) + '</div></span></div>';
      html += '<div class="detail-row"><span class="detail-label">Magnitude</span><span class="detail-value">' + (ev.magnitude != null ? ev.magnitude.toFixed(2) : '-') + '</span></div>';
      html += '<div class="detail-row"><span class="detail-label">Depth</span><span class="detail-value">' + (ev.depth_km != null ? ev.depth_km.toFixed(1) + ' km' : '-') + '</span></div>';
      html += '<div class="detail-row"><span class="detail-label">Location</span><span class="detail-value">' + (ev.latitude != null ? ev.latitude.toFixed(4) + ', ' + ev.longitude.toFixed(4) : '-') + '</span></div>';
      html += '<div class="detail-row"><span class="detail-label">Picks</span><span class="detail-value">' + (ev.num_picks || 0) + '</span></div>';

      var contribs = data.station_contributions || {};
      var staKeys = Object.keys(contribs);
      if (staKeys.length > 0) {
        html += '<div class="detail-section"><h4>Station Contributions</h4><div class="station-contrib">';
        staKeys.forEach(function(sta) {
          var c = contribs[sta];
          html += '<span class="sc-chip">' + esc(sta) + ' P:' + c.P + ' S:' + c.S + '</span>';
        });
        html += '</div></div>';
      }

      body.innerHTML = html;
    })
    .catch(function() {
      body.innerHTML = '<div class="empty-state">Failed to load event details.</div>';
    });
}

document.getElementById('detail-close').addEventListener('click', function() {
  document.getElementById('event-detail').classList.remove('visible');
  clearPulseMarker();
});

// ── Render stats panel ──────────────────────────────────────────
function renderStats(s) {
  document.getElementById('s-events').textContent = s.catalog_events || '0';
  document.getElementById('s-picks').textContent = s.total_picks != null ? s.total_picks.toLocaleString() : '\u2014';
  document.getElementById('s-days').textContent = s.days_with_raw || '0';
  document.getElementById('s-stations').textContent = s.station_count || '0';

  if (s.magnitude_min != null && s.magnitude_max != null) {
    var mn = s.magnitude_min.toFixed(1);
    var mx = s.magnitude_max.toFixed(1);
    document.getElementById('s-mag').textContent = mn === mx ? mn : mn + ' \u2013 ' + mx;
  } else {
    document.getElementById('s-mag').textContent = '\u2014';
  }

  var latestEl = document.getElementById('s-latest');
  var latestCard = document.getElementById('stat-card-latest');
  if (s.latest_event_time) {
    var d = new Date(s.latest_event_time);
    var timeStr = d.toISOString().slice(0, 16).replace('T', ' ');
    latestEl.textContent = timeStr;
    _sbSet('sb-last-event', timeStr + ' UTC');
    latestCard.classList.add('clickable');
    latestCard.onclick = function() {
      if (activeTab !== 'monitor') switchTab('monitor');
      if (s.latest_event_lat != null && s.latest_event_lon != null) {
        map.flyTo([s.latest_event_lat, s.latest_event_lon], 13);
      }
      showEventDetail(s.latest_event_id);
    };
  } else {
    latestEl.textContent = '\u2014';
    latestCard.classList.remove('clickable');
    latestCard.onclick = null;
  }

  // Review progress
  var total = s.catalog_events || 0;
  var reviewed = s.reviewed_count || 0;
  var revEl = document.getElementById('s-reviewed');
  if (total > 0) {
    var revPct = Math.round((reviewed / total) * 100);
    var revColor = revPct === 100 ? 'var(--green)' : revPct > 0 ? 'var(--amber)' : 'var(--muted)';
    revEl.innerHTML = reviewed + '<span style="color:var(--muted);font-weight:400;font-size:.75rem"> / ' + total + '</span>' +
      ' <span style="font-size:.6rem;color:' + revColor + '">' + revPct + '%</span>';
  } else {
    revEl.textContent = '\u2014';
  }
}

// ── Render progress panel ───────────────────────────────────────
function renderProgress(p) {
  var bar = document.getElementById('progress-bar');
  var badge = document.getElementById('progress-badge');
  var count = document.getElementById('progress-count');
  var txt = document.getElementById('progress-text');
  var panel = document.getElementById('progress-panel');
  var pct = p.percent || 0;
  var isCaughtUp = pct >= 95 && p.pipeline_status !== 'running';

  // Status accent on panel
  var statusClass = 'status-idle';
  if (p.pipeline_status === 'running') statusClass = 'status-running';
  else if (isCaughtUp) {
    var target = new Date(p.target_date + 'T00:00:00Z');
    var ageDays = Math.floor((Date.now() - target.getTime()) / 86400000);
    statusClass = ageDays > 2 ? 'status-behind' : 'status-caught-up';
  }
  panel.classList.remove('status-running', 'status-caught-up', 'status-behind', 'status-idle', 'status-failed');
  panel.classList.add(statusClass);

  // Bar
  bar.style.width = Math.min(pct, 100) + '%';

  var months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

  function fmtDate(dateStr) {
    var d = new Date(dateStr + 'T00:00:00Z');
    return months[d.getUTCMonth()] + ' ' + d.getUTCDate();
  }

  if (p.pipeline_status === 'running') {
    // Running
    bar.style.background = 'var(--blue)';
    badge.innerHTML = '\u25cf Running';
    badge.style.color = 'var(--blue)';
    count.textContent = p.days_complete + ' / ' + p.total_days;
    txt.innerHTML = p.current_day
      ? 'Processing <b>' + esc(p.current_day) + '</b>'
      : '';
  } else if (isCaughtUp) {
    // Caught up — check freshness
    var targetDate = new Date(p.target_date + 'T00:00:00Z');
    var dataAgeDays = Math.floor((Date.now() - targetDate.getTime()) / 86400000);
    count.textContent = '';

    if (dataAgeDays > 2) {
      // Stale
      bar.style.background = 'var(--amber)';
      badge.innerHTML = '\u25cf Behind';
      badge.style.color = 'var(--amber)';
      txt.innerHTML = 'Data is <b>' + dataAgeDays + '</b> days old';
    } else {
      // Fresh
      bar.style.background = 'var(--green)';
      badge.innerHTML = '\u25cf Up to date';
      badge.style.color = 'var(--green)';
      txt.innerHTML = 'Data through <b>' + fmtDate(p.target_date) + '</b>';
    }
  } else {
    // Not yet caught up, idle
    bar.style.background = 'var(--blue)';
    badge.innerHTML = '\u25cf Idle';
    badge.style.color = 'var(--muted)';
    count.textContent = p.days_complete + ' / ' + p.total_days;
    txt.innerHTML = '';
  }

  var steps = document.getElementById('progress-steps');
  steps.innerHTML = [
    ['Ingested', p.last_ingested], ['Processed', p.last_processed],
    ['Detected', p.last_detected], ['Associated', p.last_associated],
  ].map(function(pair) {
    return '<span class="ps-item">' + pair[0] + ': <span class="ps-val">' + (pair[1] ? fmtDate(pair[1]) : '\u2014') + '</span></span>';
  }).join('');
}

// ── Render throughput + step timing ─────────────────────────────
function renderThroughput(t) {
  var el = document.getElementById('throughput-text');
  var barEl = document.getElementById('step-timing-bar');
  var legEl = document.getElementById('step-timing-legend');

  if (!t.avg_day_sec) {
    el.innerHTML = '';
    barEl.innerHTML = '';
    legEl.innerHTML = '';
    return;
  }

  var line = 'Avg/day: <b>' + fmtDur(t.avg_day_sec) + '</b>';
  line += ' &nbsp;\u00b7&nbsp; Last: <b>' + fmtDur(t.last_day_sec) + '</b>';
  if (t.eta_sec != null && t.remaining_days > 0) {
    line += ' &nbsp;\u00b7&nbsp; ETA: <b>~' + fmtDur(t.eta_sec) + '</b> (' + t.remaining_days + 'd)';
  }
  if (t.sample_size) line += ' &nbsp;\u00b7&nbsp; n=' + t.sample_size;
  el.innerHTML = line;

  var stepAvg = t.step_avg_sec || {};
  var total = Object.values(stepAvg).reduce(function(a, b) { return a + b; }, 0) || 1;
  var order = ['ingest', 'process', 'detect', 'associate', 'catalog'];
  barEl.innerHTML = order.map(function(name) {
    var sec = stepAvg[name] || 0;
    var pct = (sec / total) * 100;
    return '<div style="flex:' + Math.max(pct, 0.5) + ';background:' + STEP_COLORS[name] + '" title="' + esc(STEP_SHORT[name]) + ': ' + fmtDur(sec) + ' (' + Math.round(pct) + '%)"></div>';
  }).join('');

  legEl.innerHTML = order.filter(function(n) { return stepAvg[n]; }).map(function(name) {
    return '<span><span class="stl-dot" style="background:' + STEP_COLORS[name] + '"></span>' + esc(STEP_SHORT[name]) + ' ' + fmtDur(stepAvg[name]) + '</span>';
  }).join('');
}

// ── Completeness color (dark to green) ────────────────────────────
function completenessColor(val, max) {
  if (val === 0) return getComputedStyle(document.documentElement).getPropertyValue('--surface').trim();
  var t = Math.min(val / max, 1);
  var r = Math.round(22 + (34 - 22) * t);
  var g = Math.round(101 + (197 - 101) * t);
  var b = Math.round(52 + (94 - 52) * t);
  return 'rgb(' + r + ',' + g + ',' + b + ')';
}

// ── Render data completeness calendar ────────────────────────────
var lastCompletenessData = null;
function renderCompleteness(data) {
  if (data) lastCompletenessData = data;
  else data = lastCompletenessData;
  if (!data) return;
  var stations = data.stations, days = data.days, matrix = data.matrix;
  var el = document.getElementById('completeness-calendar');
  var rangeEl = document.getElementById('completeness-range');
  var maxEl = document.getElementById('completeness-max');

  if (!days || days.length === 0 || !stations || stations.length === 0) {
    el.innerHTML = '<div class="empty-state">No raw data yet.</div>';
    rangeEl.textContent = '';
    maxEl.textContent = 'max';
    return;
  }

  var globalMax = Math.max.apply(null, [1].concat(matrix.reduce(function(a, b) { return a.concat(b); }, [])));
  maxEl.textContent = globalMax;
  rangeEl.textContent = days.length + ' days';

  var LABEL_W = 60;
  var W = el.clientWidth || 280;
  var chartW = W - LABEL_W;
  var ROW_H = 24;
  var PAD_BOTTOM = 20;
  var cellW = Math.max(1, chartW / days.length);
  var svgW = LABEL_W + cellW * days.length;
  var svgH = ROW_H * stations.length + PAD_BOTTOM;

  var svg = '<svg width="' + svgW + '" height="' + svgH + '" viewBox="0 0 ' + svgW + ' ' + svgH + '">';
  stations.forEach(function(sta, si) {
    var y = si * ROW_H;
    svg += '<text x="' + (LABEL_W - 4) + '" y="' + (y + ROW_H / 2 + 3) + '" fill="var(--muted)" font-size="10" font-family="monospace" text-anchor="end">' + esc(sta) + '</text>';
    var row = matrix[si];
    row.forEach(function(count, di) {
      var x = LABEL_W + di * cellW;
      var color = completenessColor(count, globalMax);
      svg += '<rect x="' + x + '" y="' + (y + 1) + '" width="' + Math.max(cellW - 0.5, 0.5) + '" height="' + (ROW_H - 2) + '" fill="' + color + '" rx="1"><title>' + esc(sta) + ' ' + esc(days[di]) + ': ' + count + ' files</title></rect>';
    });
  });
  var baseY = ROW_H * stations.length;
  var pm2 = days[0].slice(0, 7);
  var ticks2 = [{ i: 0, label: days[0].slice(5) }];
  for (var k = 1; k < days.length; k++) {
    var mo3 = days[k].slice(0, 7);
    if (mo3 !== pm2) {
      ticks2.push({ i: k, label: days[k].slice(5) });
      pm2 = mo3;
    }
  }
  if (days.length > 1) ticks2.push({ i: days.length - 1, label: days[days.length - 1].slice(5) });
  ticks2.forEach(function(item) {
    var x = LABEL_W + item.i * cellW + cellW / 2;
    svg += '<text x="' + x + '" y="' + (baseY + PAD_BOTTOM - 4) + '" fill="var(--muted)" font-size="9" font-family="monospace" text-anchor="middle">' + esc(item.label) + '</text>';
  });
  svg += '</svg>';
  el.innerHTML = svg;
}

// ── Render disk panel ───────────────────────────────────────────
function renderDisk(d) {
  var bar = document.getElementById('disk-bar');
  var pct = d.usage_percent || 0;
  _sbSet('sb-disk', pct.toFixed(0) + '% · ' + d.free_gb + ' GB free');
  bar.style.width = pct + '%';
  bar.style.background = pct >= 85 ? 'var(--red)' : pct >= 70 ? 'var(--amber)' : 'var(--green)';

  document.getElementById('disk-text').innerHTML =
    '<b>' + d.used_gb + ' GB</b> / ' + d.total_gb + ' GB used \u00b7 ' + d.free_gb + ' GB free \u00b7 Pipeline: <b>' + d.output_size_gb + ' GB</b>';

  var bd = d.breakdown || {};
  var parts = Object.keys(bd).map(function(k) {
    var v = bd[k];
    return esc(k) + ': <span>' + (v >= 0.01 ? v.toFixed(2) : '&lt;0.01') + ' GB</span>';
  });
  document.getElementById('disk-breakdown').innerHTML = parts.join(' &nbsp;\u00b7&nbsp; ');
}

// ── Render error log panel ──────────────────────────────────────
function renderErrors(data) {
  var errs = data.errors || [];
  var el = document.getElementById('error-log');
  var modalEl = document.getElementById('error-log-modal');
  var countEl = document.getElementById('error-count');
  var countModalEl = document.getElementById('error-count-modal');

  if (errs.length === 0) {
    var empty = '<div style="color:var(--muted)">No errors.</div>';
    el.innerHTML = empty;
    modalEl.innerHTML = empty;
    countEl.textContent = '';
    countModalEl.textContent = '';
    return;
  }

  var countText = '(' + errs.length + ')';
  countEl.textContent = countText;
  countModalEl.textContent = countText;
  var rowsHtml = errs.map(function(e) {
    return '<div class="el-row"><span class="el-time">' + esc(e.timestamp) + '</span><span class="el-step">[' + esc(e.step) + ']</span><span class="el-' + esc(e.level) + '">' + esc(e.message) + '</span></div>';
  }).join('');
  el.innerHTML = rowsHtml;
  modalEl.innerHTML = rowsHtml;
}

// ── Catalog date filter ─────────────────────────────────────────
// Preset row + optional Custom range + dismissible active chip.

function _filterIso(d) {
  // ISO YYYY-MM-DD in local time (matches what the date input emits)
  var y = d.getFullYear();
  var m = String(d.getMonth() + 1).padStart(2, '0');
  var dd = String(d.getDate()).padStart(2, '0');
  return y + '-' + m + '-' + dd;
}

// Resolve a preset name to [start, end] (empty strings = unbounded).
function _filterRangeForPreset(preset) {
  var today = new Date();
  var end = _filterIso(today);
  if (preset === 'all')    return ['', ''];
  if (preset === '7')      { var d = new Date(); d.setDate(d.getDate() - 7);  return [_filterIso(d), end]; }
  if (preset === '30')     { var d = new Date(); d.setDate(d.getDate() - 30); return [_filterIso(d), end]; }
  if (preset === '90')     { var d = new Date(); d.setDate(d.getDate() - 90); return [_filterIso(d), end]; }
  if (preset === 'year')   return [today.getFullYear() + '-01-01', end];
  return ['', ''];
}

function _updateFilterChip(label) {
  var chip = document.getElementById('filter-chip');
  var lbl = document.getElementById('filter-chip-label');
  if (!chip || !lbl) return;
  if (!label) {
    chip.hidden = true;
    return;
  }
  lbl.textContent = label;
  chip.hidden = false;
}

function _setActivePreset(preset) {
  document.querySelectorAll('.filter-preset').forEach(function(b) {
    b.classList.toggle('active', b.dataset.preset === preset);
  });
}

function _applyPreset(preset) {
  var range = _filterRangeForPreset(preset);
  filterStartDate = range[0];
  filterEndDate = range[1];
  _setActivePreset(preset);
  document.getElementById('filter-custom').hidden = true;

  // Chip text. "All" hides the chip; everything else shows the range.
  var label = null;
  if (preset === '7')        label = 'Last 7 days';
  else if (preset === '30')  label = 'Last 30 days';
  else if (preset === '90')  label = 'Last 90 days';
  else if (preset === 'year')label = 'This year';
  _updateFilterChip(label);

  pollData();
  pollSlow();
}

function _applyCustomRange() {
  var s = document.getElementById('filter-start').value || '';
  var e = document.getElementById('filter-end').value || '';
  if (!s && !e) return;  // nothing to apply
  filterStartDate = s;
  filterEndDate = e;
  _setActivePreset('custom');
  var label = (s || '…') + ' → ' + (e || '…');
  _updateFilterChip(label);
  pollData();
  pollSlow();
}

document.querySelectorAll('.filter-preset').forEach(function(btn) {
  btn.addEventListener('click', function() {
    var preset = btn.dataset.preset;
    if (preset === 'custom') {
      _setActivePreset('custom');
      var custom = document.getElementById('filter-custom');
      custom.hidden = false;
      document.getElementById('filter-start').focus();
    } else {
      _applyPreset(preset);
    }
  });
});

document.getElementById('filter-apply').addEventListener('click', _applyCustomRange);

// Pressing Enter in either date input also applies
document.querySelectorAll('.filter-date').forEach(function(input) {
  input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') _applyCustomRange();
  });
});

document.getElementById('filter-chip-x').addEventListener('click', function() {
  document.getElementById('filter-start').value = '';
  document.getElementById('filter-end').value = '';
  _applyPreset('all');
});

// Fetch a URL and save the response body as a download. Works in pywebview
// (where window.open() is unreliable) by routing through a blob + <a download>.
function downloadFromUrl(url, filename) {
  fetch(url).then(function(resp) {
    if (!resp.ok) {
      return resp.text().then(function(body) {
        throw new Error('HTTP ' + resp.status + ': ' + body.slice(0, 200));
      });
    }
    return resp.blob();
  }).then(function(blob) {
    var objUrl = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = objUrl;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(function() {
      document.body.removeChild(a);
      URL.revokeObjectURL(objUrl);
    }, 100);
  }).catch(function(err) {
    alert('Download failed: ' + err.message);
  });
}

document.getElementById('export-csv').addEventListener('click', function() {
  var url = '/api/catalog/export';
  var params = [];
  if (filterStartDate) params.push('start_date=' + encodeURIComponent(filterStartDate));
  if (filterEndDate) params.push('end_date=' + encodeURIComponent(filterEndDate));
  if (params.length > 0) url += '?' + params.join('&');
  downloadFromUrl(url, 'catalog_export.csv');
});

// ── Build query string from date filters ────────────────────────
function dateParams() {
  var params = [];
  if (filterStartDate) params.push('start_date=' + encodeURIComponent(filterStartDate));
  if (filterEndDate) params.push('end_date=' + encodeURIComponent(filterEndDate));
  return params.length > 0 ? '?' + params.join('&') : '';
}

function catalogParams() {
  var params = ['per_page=500'];
  if (filterStartDate) params.push('start_date=' + encodeURIComponent(filterStartDate));
  if (filterEndDate) params.push('end_date=' + encodeURIComponent(filterEndDate));
  return '?' + params.join('&');
}

// ── WebSocket for real-time status ──────────────────────────────
let wsConn = null;
let wsRetryTimer = null;
let useWebSocket = true;

function connectWebSocket() {
  if (!useWebSocket) return;
  try {
    var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    wsConn = new WebSocket(proto + '//' + location.host + '/ws/status');

    wsConn.onopen = function() {
      var ind = document.getElementById('ws-indicator');
      if (ind) { ind.textContent = 'live'; ind.classList.add('ws-connected'); }
      _sbConn('live');
    };

    wsConn.onmessage = function(evt) {
      try {
        var data = JSON.parse(evt.data);
        renderStatus(data);
        onApiSuccess();
      } catch(e) {}
    };

    wsConn.onclose = function() {
      var ind = document.getElementById('ws-indicator');
      if (ind) { ind.textContent = 'polling'; ind.classList.remove('ws-connected'); }
      _sbConn('polling');
      wsConn = null;
      // Retry after 5s
      if (useWebSocket) {
        wsRetryTimer = setTimeout(connectWebSocket, 5000);
      }
    };

    wsConn.onerror = function() {
      // Will trigger onclose
    };
  } catch(e) {
    useWebSocket = false;
    var ind = document.getElementById('ws-indicator');
    if (ind) { ind.textContent = 'polling'; ind.classList.remove('ws-connected'); }
  }
}

// ── Polling ─────────────────────────────────────────────────────
async function pollStatus() {
  // Skip HTTP polling if WebSocket is connected
  if (wsConn && wsConn.readyState === WebSocket.OPEN) return;
  try {
    renderStatus(await fetchJSON('/api/status'));
    onApiSuccess();
  } catch(e) { onApiFailure(e); }
}

async function pollData() {
  showLoading('loading-progress');
  showLoading('loading-stats');
  try {
    var results = await Promise.all([
      fetchJSON('/api/stats'),
      fetchJSON('/api/catalog' + catalogParams()),
      fetchJSON('/api/progress'),
      fetchJSON('/api/throughput')
    ]);
    renderStats(results[0]);
    renderEvents((results[1] && results[1].events) || []);
    renderProgress(results[2]);
    renderThroughput(results[3]);
    onApiSuccess();
  } catch(e) { onApiFailure(e); }
  hideLoading('loading-progress');
  hideLoading('loading-stats');
}

async function pollDisk() {
  showLoading('loading-disk');
  try {
    renderDisk(await fetchJSON('/api/disk'));
    onApiSuccess();
  } catch(e) { onApiFailure(e); }
  hideLoading('loading-disk');
}

async function pollErrors() {
  showLoading('loading-errors');
  try {
    renderErrors(await fetchJSON('/api/errors'));
    onApiSuccess();
  } catch(e) { onApiFailure(e); }
  hideLoading('loading-errors');
}

async function pollSlow() {
  showLoading('loading-completeness');
  try {
    var results = await Promise.all([
      fetchJSON('/api/station_health'),
      fetchJSON('/api/data_completeness'),
    ]);
    renderStationHealth(results[0]);
    renderCompleteness(results[1]);
    onApiSuccess();
  } catch(e) { onApiFailure(e); }
  hideLoading('loading-completeness');
}

// Initial loads
connectWebSocket();
fetchJSON('/api/stations').then(renderStations).catch(function() {});
pollStatus();
pollData();
pollDisk();
pollErrors();
pollSlow();

setInterval(pollStatus, 2500);
setInterval(pollData, 10000);
setInterval(pollDisk, 30000);
setInterval(pollErrors, 10000);
setInterval(pollSlow, 30000);

// Fix map size after layout settles
setTimeout(function() { map.invalidateSize(); }, 200);
window.addEventListener('resize', function() { map.invalidateSize(); });

// ── Custom zoom control (top-right, replaces Leaflet's default) ─────
(function wireMapZoomControl() {
  var slider = document.getElementById('mz-slider');
  var btnIn = document.getElementById('mz-in');
  var btnOut = document.getElementById('mz-out');
  var btnFit = document.getElementById('mz-fit');
  if (!slider || !btnIn || !btnOut || !btnFit) return;

  // Initialize slider to current zoom
  slider.value = map.getZoom();

  // Slider → map (smooth: setZoom respects zoomAnimation + fractional via zoomSnap=0)
  slider.addEventListener('input', function() {
    var z = parseFloat(slider.value);
    if (!isNaN(z)) map.setZoom(z);
  });

  // Buttons → map (zoomIn/zoomOut step by zoomDelta = 0.5)
  btnIn.addEventListener('click', function() { map.zoomIn(); });
  btnOut.addEventListener('click', function() { map.zoomOut(); });

  // Reset → initial view (smooth pan + zoom)
  btnFit.addEventListener('click', function() {
    map.flyTo(MAP_INIT_VIEW, MAP_INIT_ZOOM, { duration: 0.6 });
  });

  // Map → slider (keep in sync when user scrolls, pinches, double-clicks, etc.)
  map.on('zoom', function() {
    slider.value = map.getZoom();
  });
})();

// Re-render completeness calendar when its container resizes (window resize, tab show)
if (typeof ResizeObserver !== 'undefined') {
  var calEl = document.getElementById('completeness-calendar');
  if (calEl) {
    var lastCalW = 0;
    new ResizeObserver(function(entries) {
      var w = entries[0].contentRect.width;
      if (w && Math.abs(w - lastCalW) > 4) {
        lastCalW = w;
        renderCompleteness();
      }
    }).observe(calEl);
  }
}

// ── Tab Navigation ──────────────────────────────────────────────
var activeTab = 'monitor';
var tabDataLoaded = { monitor: true, pipeline: true, catalog: false, waveviewer: false, helicorder: false };

function switchTab(tab) {
  if (tab === activeTab) return;
  document.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.remove('active'); });
  var targetBtn = document.querySelector('.tab-btn[data-tab="' + tab + '"]');
  if (targetBtn) targetBtn.classList.add('active');
  document.getElementById('tab-' + activeTab).classList.remove('active');
  document.getElementById('tab-' + tab).classList.add('active');
  activeTab = tab;
  if (tab === 'monitor') {
    setTimeout(function() { map.invalidateSize(); }, 50);
  }
  if (tab === 'pipeline') {
    setTimeout(function() { renderCompleteness(); }, 50);
  }
  if (!tabDataLoaded[tab]) {
    tabDataLoaded[tab] = true;
    if (tab === 'catalog') loadCatalogTable();
    if (tab === 'helicorder') initHelicorderTab();
  }
}

// ── Helicorder tab ──────────────────────────────────────────────
var _heliData = null;
var _heliEvents = [];

function initHelicorderTab() {
  fetchJSON('/api/stations').then(function(sts) {
    var sel = document.getElementById('heli-station');
    sel.innerHTML = sts.map(function(s) {
      return '<option value="' + esc(s.station) + '">' + esc(s.station) +
        ' (' + esc(s.network) + ')</option>';
    }).join('');
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].value === 'KIDD') { sel.value = 'KIDD'; break; }
    }
  }).catch(function() {});
  // Latest complete day under the ~30 h ingest lag is two days back.
  var d = new Date(Date.now() - 2 * 86400000);
  document.getElementById('heli-date').value = d.toISOString().slice(0, 10);
}

function loadHelicorder() {
  var sta = document.getElementById('heli-station').value;
  var date = document.getElementById('heli-date').value;
  if (!sta || !date) return;
  var fmin = document.getElementById('heli-fmin').value || 0;
  var fmax = document.getElementById('heli-fmax').value || 0;
  var body = document.getElementById('heli-body');
  body.innerHTML = '<div class="empty-state"><span class="spinner"></span> Loading full day…</div>';

  Promise.all([
    fetchJSON('/api/helicorder?station=' + encodeURIComponent(sta) +
      '&date=' + encodeURIComponent(date) + '&fmin=' + fmin + '&fmax=' + fmax),
    fetchJSON('/api/catalog?per_page=500').catch(function() { return { events: [] }; }),
  ]).then(function(res) {
    _heliData = res[0];
    _heliEvents = ((res[1] && res[1].events) || []).filter(function(e) {
      return (e.time || '').slice(0, 10) === date;
    });
    body.innerHTML = '<canvas id="heli-canvas"></canvas>';
    drawHelicorder();
    var pk = (_heliData.abs_max * 1e6).toPrecision(3);
    document.getElementById('heli-summary').textContent =
      _heliData.network + '.' + _heliData.station + ' ' + _heliData.channel +
      ' · ' + date + ' · ' + _heliData.fmin + '–' + _heliData.fmax +
      ' Hz · peak ' + pk + ' µm/s · ' + _heliEvents.length + ' cataloged event(s)';
  }).catch(function(err) {
    _heliData = null;
    body.innerHTML = '<div class="empty-state">' + esc(err.message) + '</div>';
  });
}

function drawHelicorder() {
  if (!_heliData) return;
  var canvas = document.getElementById('heli-canvas');
  var wrap = document.getElementById('heli-body');
  if (!canvas || !wrap) return;

  // Same palette as review mode (_waveColors) so traces look identical
  // across the app: blue trace on canvas-bg, border-grey grid.
  var c = _waveColors();
  var css = getComputedStyle(document.documentElement);
  var cAmber = css.getPropertyValue('--amber').trim() || '#f59e0b';

  var mL = 56, mR = 10, mT = 10, mB = 24, rowH = 30;
  var W = Math.max(640, wrap.clientWidth - 20);
  var H = mT + 24 * rowH + mB;
  var dpr = window.devicePixelRatio || 1;
  canvas.width = W * dpr;
  canvas.height = H * dpr;
  canvas.style.width = W + 'px';
  canvas.style.height = H + 'px';
  var ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.fillStyle = c.bg;
  ctx.fillRect(0, 0, W, H);

  var plotW = W - mL - mR;
  var gain = parseFloat(document.getElementById('heli-gain').value) || 1;
  var half = rowH / 2;
  var clip = rowH * 1.45;  // allow gentle overlap into neighbor rows, like a real drum

  ctx.font = '9px ui-monospace, Menlo, monospace';
  ctx.strokeStyle = c.grid;
  ctx.lineWidth = 0.5;
  ctx.globalAlpha = 0.5;
  for (var m = 0; m <= 60; m += 10) {
    var gx = mL + (m / 60) * plotW;
    ctx.beginPath(); ctx.moveTo(gx, mT); ctx.lineTo(gx, H - mB); ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillStyle = c.axisText;
    ctx.textAlign = 'center';
    ctx.fillText('+' + m + 'm', gx, H - mB + 12);
    ctx.globalAlpha = 0.5;
  }
  ctx.globalAlpha = 1;

  _heliData.rows.forEach(function(row) {
    var y0 = mT + row.hour * rowH + half;
    ctx.fillStyle = c.axisText;
    ctx.textAlign = 'right';
    ctx.fillText(String(row.hour).padStart(2, '0') + ':00', mL - 8, y0 + 3);
    var n = row.max.length;
    if (!n) return;
    ctx.strokeStyle = c.trace;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (var i = 0; i < n; i++) {
      var x = mL + (i / n) * plotW;
      var top = Math.min(Math.max(row.max[i] * gain * half, -clip), clip);
      var bot = Math.min(Math.max(row.min[i] * gain * half, -clip), clip);
      ctx.moveTo(x, y0 - top);
      ctx.lineTo(x, y0 - bot + 0.5);
    }
    ctx.stroke();
  });

  _heliEvents.forEach(function(e) {
    var t = new Date(e.time);
    var hour = t.getUTCHours();
    var frac = (t.getUTCMinutes() * 60 + t.getUTCSeconds()) / 3600;
    var x = mL + frac * plotW;
    var y = mT + hour * rowH + 4;
    ctx.fillStyle = e.event_type === 'earthquake' ? c.trace : cAmber;
    ctx.beginPath();
    ctx.moveTo(x, y + 6);
    ctx.lineTo(x - 4, y);
    ctx.lineTo(x + 4, y);
    ctx.closePath();
    ctx.fill();
  });
}

document.getElementById('heli-load').addEventListener('click', loadHelicorder);
document.getElementById('heli-gain').addEventListener('change', drawHelicorder);
window.addEventListener('resize', function() {
  if (activeTab === 'helicorder' && _heliData) drawHelicorder();
});

document.querySelectorAll('.tab-btn').forEach(function(btn) {
  btn.addEventListener('click', function() {
    switchTab(btn.getAttribute('data-tab'));
  });
});

// ── Pipeline stat-card click handlers ───────────────────────────
document.getElementById('stat-card-events').addEventListener('click', function() {
  document.getElementById('cat-review-filter').value = '';
  catPage = 1;
  switchTab('catalog');
  if (tabDataLoaded.catalog) loadCatalogTable();
});
document.getElementById('stat-card-picks').addEventListener('click', function() {
  document.getElementById('cat-review-filter').value = '';
  catPage = 1;
  switchTab('catalog');
  if (tabDataLoaded.catalog) loadCatalogTable();
});
document.getElementById('stat-card-reviewed').addEventListener('click', function() {
  document.getElementById('cat-review-filter').value = 'confirmed';
  catPage = 1;
  switchTab('catalog');
  if (tabDataLoaded.catalog) loadCatalogTable();
});

// ── Error log expand modal ──────────────────────────────────────
var errorModal = document.getElementById('error-modal');
document.getElementById('error-log-expand').addEventListener('click', function() {
  errorModal.classList.add('open');
});
document.getElementById('error-modal-close').addEventListener('click', function() {
  errorModal.classList.remove('open');
});
errorModal.addEventListener('click', function(e) {
  if (e.target === errorModal) errorModal.classList.remove('open');
});

// ── QuakeML Export ──────────────────────────────────────────────
document.getElementById('export-quakeml').addEventListener('click', function() {
  var url = '/api/catalog/export/quakeml';
  var params = [];
  if (filterStartDate) params.push('start_date=' + encodeURIComponent(filterStartDate));
  if (filterEndDate) params.push('end_date=' + encodeURIComponent(filterEndDate));
  if (params.length > 0) url += '?' + params.join('&');
  downloadFromUrl(url, 'catalog_export.xml');
});

// ── Catalog Table ───────────────────────────────────────────────
var catPage = 1;
var catPerPage = 50;
var catSortBy = 'time';
var catSortOrder = 'desc';
var catMinMag = null;
var catMaxDepth = null;
var catMinPicks = null;

var CAT_COLUMNS = [
  { key: 'reviewed', label: 'Review', sortable: false },
  { key: 'event_type', label: 'Type', sortable: false },
  { key: 'event_id', label: 'ID', sortable: false },
  { key: 'time', label: 'Time', sortable: true },
  { key: 'magnitude', label: 'Mag', sortable: true },
  { key: 'magnitude_type', label: 'Mag Type', sortable: false },
  { key: 'depth_km', label: 'Depth (km)', sortable: true },
  { key: 'latitude', label: 'Lat', sortable: false },
  { key: 'longitude', label: 'Lon', sortable: false },
  { key: 'num_picks', label: 'Picks', sortable: true },
  { key: 'sigma_time', label: '\u03c3 T', sortable: false },
  { key: 'sigma_amp', label: '\u03c3 A', sortable: false },
];

function renderCatalogHeader() {
  var thead = document.getElementById('cat-thead');
  var html = '<tr>';
  CAT_COLUMNS.forEach(function(col) {
    var arrow = '';
    if (col.sortable) {
      if (catSortBy === col.key) {
        arrow = '<span class="sort-arrow">' + (catSortOrder === 'asc' ? '\u25b2' : '\u25bc') + '</span>';
      } else {
        arrow = '<span class="sort-arrow" style="opacity:.3">\u25bc</span>';
      }
    }
    html += '<th data-sort="' + (col.sortable ? col.key : '') + '">' + esc(col.label) + arrow + '</th>';
  });
  html += '</tr>';
  thead.innerHTML = html;

  thead.querySelectorAll('th[data-sort]').forEach(function(th) {
    th.addEventListener('click', function() {
      var sortKey = th.getAttribute('data-sort');
      if (!sortKey) return;
      if (catSortBy === sortKey) {
        catSortOrder = catSortOrder === 'asc' ? 'desc' : 'asc';
      } else {
        catSortBy = sortKey;
        catSortOrder = 'desc';
      }
      catPage = 1;
      loadCatalogTable();
    });
  });
}

function magClass(m) {
  if (m == null) return '';
  if (m < 1.0) return 'mag-green';
  if (m < 2.0) return 'mag-amber';
  return 'mag-red';
}

function eventTypeBadge(t) {
  var colors = {
    earthquake: 'var(--blue)',
    quarry_blast: 'var(--amber)',
    noise: 'var(--gray)',
    undetermined: 'var(--muted)',
  };
  var labels = {
    earthquake: 'EQ',
    quarry_blast: 'QB',
    noise: 'Noise',
    undetermined: '?',
  };
  var c = colors[t] || 'var(--muted)';
  var l = labels[t] || esc(t || '?');
  return '<span style="color:' + c + ';font-weight:700;font-size:.6rem;text-transform:uppercase">' + l + '</span>';
}

function loadCatalogTable() {
  updateReviewQueueCount();
  var params = ['page=' + catPage, 'per_page=' + catPerPage, 'sort_by=' + catSortBy, 'sort_order=' + catSortOrder];
  if (filterStartDate) params.push('start_date=' + encodeURIComponent(filterStartDate));
  if (filterEndDate) params.push('end_date=' + encodeURIComponent(filterEndDate));
  var searchVal = document.getElementById('cat-search').value.trim();
  if (searchVal) params.push('search=' + encodeURIComponent(searchVal));
  var reviewFilter = document.getElementById('cat-review-filter').value;
  if (reviewFilter) params.push('review_status=' + encodeURIComponent(reviewFilter));
  if (catMinMag != null) params.push('min_magnitude=' + catMinMag);
  if (catMaxDepth != null) params.push('max_depth=' + catMaxDepth);
  if (catMinPicks != null) params.push('min_picks=' + catMinPicks);

  fetchJSON('/api/catalog?' + params.join('&')).then(function(data) {
    renderCatalogHeader();
    var tbody = document.getElementById('cat-tbody');
    if (!data.events || data.events.length === 0) {
      tbody.innerHTML = '<tr><td colspan="' + CAT_COLUMNS.length + '" style="text-align:center;color:var(--muted);padding:2rem">No events found.</td></tr>';
    } else {
      tbody.innerHTML = data.events.map(function(e) {
        return '<tr data-eid="' + esc(e.event_id) + '">' +
          '<td>' + (e.review_status === 'confirmed' ? '<span style="color:var(--green);font-weight:700;font-size:.7rem">\u25cf Confirmed</span>' : e.review_status === 'rejected' ? '<span style="color:var(--red);font-weight:700;font-size:.7rem">\u25cf Rejected</span>' : '<span style="color:var(--amber);font-weight:700;font-size:.7rem">\u25cf Unreviewed</span>') + '</td>' +
          '<td>' + eventTypeBadge(e.event_type) + '</td>' +
          '<td>' + esc(e.event_id) + '</td>' +
          '<td>' + esc(e.time || '') + '<div class="local-time">' + esc(formatLocalTime(e.time)) + '</div></td>' +
          '<td class="' + magClass(e.magnitude) + '">' + (e.magnitude != null ? e.magnitude.toFixed(2) : '-') + '</td>' +
          '<td>' + esc(e.magnitude_type || '') + '</td>' +
          '<td>' + (e.depth_km != null ? e.depth_km.toFixed(1) : '-') + '</td>' +
          '<td>' + (e.latitude != null ? e.latitude.toFixed(4) : '-') + '</td>' +
          '<td>' + (e.longitude != null ? e.longitude.toFixed(4) : '-') + '</td>' +
          '<td>' + (e.num_picks || 0) + '</td>' +
          '<td>' + (e.sigma_time != null ? e.sigma_time.toFixed(3) : '-') + '</td>' +
          '<td>' + (e.sigma_amp != null ? e.sigma_amp.toFixed(3) : '-') + '</td>' +
          '</tr>';
      }).join('');

      tbody.querySelectorAll('tr').forEach(function(row) {
        row.addEventListener('click', function() {
          var eid = row.getAttribute('data-eid');
          switchTab('monitor');
          var ev = data.events.find(function(x) { return x.event_id === eid; });
          if (ev && ev.latitude != null && ev.longitude != null) {
            map.flyTo([ev.latitude, ev.longitude], 13);
          }
          showEventDetail(eid);
        });
      });
    }

    document.getElementById('cat-page-info').textContent = 'Page ' + data.page + ' of ' + data.total_pages;
    document.getElementById('cat-summary').textContent = data.total + ' event' + (data.total !== 1 ? 's' : '');
    document.getElementById('cat-prev').disabled = data.page <= 1;
    document.getElementById('cat-next').disabled = data.page >= data.total_pages;
  }).catch(function() {
    document.getElementById('cat-tbody').innerHTML = '<tr><td colspan="' + CAT_COLUMNS.length + '" style="text-align:center;color:var(--red);padding:2rem">Failed to load catalog.</td></tr>';
  });
}

document.getElementById('cat-prev').addEventListener('click', function() { if (catPage > 1) { catPage--; loadCatalogTable(); } });
document.getElementById('cat-next').addEventListener('click', function() { catPage++; loadCatalogTable(); });
document.getElementById('cat-search').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') { catPage = 1; loadCatalogTable(); }
});
document.getElementById('cat-review-next').addEventListener('click', function() {
  advanceToNextUnreviewed();
});

document.getElementById('cat-filter-apply').addEventListener('click', function() {
  var v = document.getElementById('cat-min-mag').value;
  catMinMag = v ? parseFloat(v) : null;
  v = document.getElementById('cat-max-depth').value;
  catMaxDepth = v ? parseFloat(v) : null;
  v = document.getElementById('cat-min-picks').value;
  catMinPicks = v ? parseInt(v, 10) : null;
  catPage = 1;
  loadCatalogTable();
});
document.getElementById('cat-filter-clear').addEventListener('click', function() {
  document.getElementById('cat-search').value = '';
  document.getElementById('cat-min-mag').value = '';
  document.getElementById('cat-max-depth').value = '';
  document.getElementById('cat-min-picks').value = '';
  document.getElementById('cat-review-filter').value = '';
  catMinMag = null; catMaxDepth = null; catMinPicks = null;
  catPage = 1;
  loadCatalogTable();
});
document.getElementById('cat-review-filter').addEventListener('change', function() {
  catPage = 1;
  loadCatalogTable();
});

// ── Enhanced Event Detail (with waveform button + extra fields) ──
var _origShowEventDetail = showEventDetail;
showEventDetail = function(eventId) {
  var overlay = document.getElementById('event-detail');
  var body = document.getElementById('detail-body');
  var title = document.getElementById('detail-title');
  title.textContent = 'Loading...';
  body.innerHTML = '<div class="spinner" style="margin:.5rem auto;display:block"></div>';
  overlay.classList.add('visible');

  fetchJSON('/api/event/' + encodeURIComponent(eventId))
    .then(function(data) {
      if (!data) return;
      var ev = data.event;
      // Pulsating ring on selected event
      if (ev.latitude != null && ev.longitude != null) {
        showPulseMarker(ev.latitude, ev.longitude);
      }
      title.textContent = esc(ev.event_id);
      var html = '';
      html += '<div class="detail-row"><span class="detail-label">Time</span><span class="detail-value">' + esc(ev.time) + '<div class="local-time">' + esc(formatLocalTime(ev.time)) + '</div></span></div>';
      html += '<div class="detail-row"><span class="detail-label">Magnitude</span><span class="detail-value">' + (ev.magnitude != null ? ev.magnitude.toFixed(2) : '-') + (ev.magnitude_type ? ' (' + esc(ev.magnitude_type) + ')' : '') + '</span></div>';
      if (ev.ml_err != null) {
        html += '<div class="detail-row"><span class="detail-label">ML Error</span><span class="detail-value">\u00b1' + ev.ml_err.toFixed(3) + '</span></div>';
      }
      html += '<div class="detail-row"><span class="detail-label">Depth</span><span class="detail-value">' + (ev.depth_km != null ? ev.depth_km.toFixed(1) + ' km' : '-') + '</span></div>';
      html += '<div class="detail-row"><span class="detail-label">Location</span><span class="detail-value">' + (ev.latitude != null ? ev.latitude.toFixed(4) + ', ' + ev.longitude.toFixed(4) : '-') + '</span></div>';
      html += '<div class="detail-row"><span class="detail-label">Picks</span><span class="detail-value">' + (ev.num_picks || 0) + '</span></div>';
      if (ev.sigma_time != null) {
        html += '<div class="detail-row"><span class="detail-label">\u03c3 Time</span><span class="detail-value">' + ev.sigma_time.toFixed(3) + ' s</span></div>';
      }
      if (ev.sigma_amp != null) {
        html += '<div class="detail-row"><span class="detail-label">\u03c3 Amp</span><span class="detail-value">' + ev.sigma_amp.toFixed(3) + '</span></div>';
      }
      if (ev.num_ml_sta) {
        html += '<div class="detail-row"><span class="detail-label">ML Stations</span><span class="detail-value">' + ev.num_ml_sta + '</span></div>';
      }

      var contribs = data.station_contributions || {};
      var staKeys = Object.keys(contribs);
      if (staKeys.length > 0) {
        html += '<div class="detail-section"><h4>Station Contributions</h4><div class="station-contrib">';
        staKeys.forEach(function(sta) {
          var c = contribs[sta];
          html += '<span class="sc-chip">' + esc(sta) + ' P:' + c.P + ' S:' + c.S + '</span>';
        });
        html += '</div></div>';
      }

      // Reviewed status
      if (ev.reviewed) {
        html += '<div class="detail-row"><span class="detail-label">Reviewed</span><span class="detail-value reviewed-check">\u2713 ' + esc(ev.reviewed) + '</span></div>';
      }

      // Waveform + Review buttons
      html += '<div style="margin-top:.5rem;display:flex;gap:.3rem">';
      html += '<button class="btn-sm btn-primary" id="btn-view-waveforms" style="flex:1">View Waveforms</button>';
      html += '<button class="btn-sm" id="btn-review-event" style="flex:1;background:var(--green);border-color:var(--green);color:#fff">Review</button>';
      html += '</div>';

      body.innerHTML = html;

      document.getElementById('btn-view-waveforms').addEventListener('click', function() {
        showEventWaveforms(ev.event_id);
      });
      document.getElementById('btn-review-event').addEventListener('click', function() {
        openReviewMode(ev.event_id);
      });
    })
    .catch(function() {
      body.innerHTML = '<div class="empty-state">Failed to load event details.</div>';
    });
};

// ── Waveform Viewer ─────────────────────────────────────────────
var currentWaveformEventId = null;

function _renderWfTrace(tr, eventTime, container) {
  var div = document.createElement('div');
  div.className = 'waveform-trace';
  var label = document.createElement('div');
  label.className = 'waveform-trace-label';
  label.textContent = tr.network + '.' + tr.station + '.' + tr.channel + (tr.error ? ' \u2014 ' + tr.error : '');
  div.appendChild(label);

  if (tr.data && tr.data.length > 0) {
    var canvas = document.createElement('canvas');
    div.appendChild(canvas);
    if (tr.spectrogram_b64) {
      var specCanvas = document.createElement('canvas');
      specCanvas.className = 'spectrogram-canvas';
      div.appendChild(specCanvas);
    }
    container.appendChild(div);
    var dpr = window.devicePixelRatio || 1;
    var dw = canvas.clientWidth || 800;
    var dh = canvas.clientHeight || 80;
    canvas.width = dw * dpr;
    canvas.height = dh * dpr;
    renderWaveformCanvas(canvas, tr, eventTime, dw, dh, dpr);
    if (tr.spectrogram_b64) {
      drawSpectrogramCanvas(specCanvas, tr, eventTime);
    }
  } else {
    var nodata = document.createElement('div');
    nodata.className = 'empty-state';
    nodata.textContent = tr.error || 'No data';
    nodata.style.height = '80px';
    nodata.style.lineHeight = '80px';
    div.appendChild(nodata);
    container.appendChild(div);
  }
}

function showEventWaveforms(eventId) {
  currentWaveformEventId = eventId;
  var overlay = document.getElementById('waveform-overlay');
  var body = document.getElementById('wf-body');
  var title = document.getElementById('wf-title');
  title.textContent = 'Waveforms \u2014 ' + eventId;
  body.innerHTML = '<div class="empty-state">Loading waveforms...</div>';
  overlay.classList.add('visible');

  var channel = document.getElementById('wf-channel').value;
  var before = document.getElementById('wf-before').value;
  var after = document.getElementById('wf-after').value;
  var showSpec = document.getElementById('wf-spectrogram').checked;

  if (channel === '3C') {
    // Fetch all 3 components and group by station
    var channels3 = ['EHZ', 'EHN', 'EHE'];
    var baseUrl = '/api/event/' + encodeURIComponent(eventId) + '/waveforms?window_before=' + before + '&window_after=' + after + '&spectrogram=' + showSpec;
    Promise.all(channels3.map(function(ch) {
      return fetchJSON(baseUrl + '&channel=' + ch);
    })).then(function(results) {
      body.innerHTML = '';
      // Build station order from the first (Z) result
      var eventTime = results[0].event_time;
      var stationOrder = (results[0].traces || []).map(function(t) { return t.network + '.' + t.station; });
      // Index traces by station+channel
      var traceMap = {};
      results.forEach(function(data, ci) {
        (data.traces || []).forEach(function(tr) {
          var key = tr.network + '.' + tr.station;
          if (!traceMap[key]) traceMap[key] = {};
          traceMap[key][channels3[ci]] = tr;
          if (stationOrder.indexOf(key) === -1) stationOrder.push(key);
        });
      });
      if (stationOrder.length === 0) {
        body.innerHTML = '<div class="empty-state">No waveform data available.</div>';
        return;
      }
      stationOrder.forEach(function(staKey) {
        var group = document.createElement('div');
        group.className = 'review-trace-group';
        var groupLabel = document.createElement('div');
        groupLabel.className = 'group-label';
        groupLabel.textContent = staKey;
        group.appendChild(groupLabel);
        channels3.forEach(function(ch) {
          var tr = traceMap[staKey] && traceMap[staKey][ch];
          if (tr) {
            _renderWfTrace(tr, eventTime, group);
          }
        });
        body.appendChild(group);
      });
    }).catch(function() {
      body.innerHTML = '<div class="empty-state">Failed to load waveforms.</div>';
    });
  } else {
    fetchJSON('/api/event/' + encodeURIComponent(eventId) + '/waveforms?channel=' + channel + '&window_before=' + before + '&window_after=' + after + '&spectrogram=' + showSpec)
      .then(function(data) {
        if (!data.traces || data.traces.length === 0) {
          body.innerHTML = '<div class="empty-state">No waveform data available.</div>';
          return;
        }
        body.innerHTML = '';
        data.traces.forEach(function(tr) {
          _renderWfTrace(tr, data.event_time, body);
        });
      })
      .catch(function() {
        body.innerHTML = '<div class="empty-state">Failed to load waveforms.</div>';
      });
  }
}

// Light is the implicit default (no attribute on <html>) per styles.css;
// dark is opted in via data-theme="dark". Centralised so the rest of the
// JS doesn't re-litigate the convention.
function _isLightTheme() {
  return document.documentElement.getAttribute('data-theme') !== 'dark';
}

function _waveColors() {
  // Read from the design tokens so the canvas tracks whatever the active
  // theme is (light, dark, or a future theme that adds new --canvas-bg
  // etc. values).
  var s = getComputedStyle(document.documentElement);
  function v(name, fallback) {
    return (s.getPropertyValue(name).trim()) || fallback;
  }
  return {
    bg:       v('--canvas-bg', '#ffffff'),
    trace:    v('--blue',      '#2563eb'),
    ot:       v('--text',      '#334155'),
    axisText: v('--muted',     '#64748b'),
    grid:     v('--border',    '#e2e8f0'),
  };
}

function _formatSI(v) {
  var a = Math.abs(v);
  if (a === 0) return '0';
  if (a >= 1)    return v.toFixed(1);
  if (a >= 1e-3) return (v * 1e3).toFixed(1) + 'e-3';
  if (a >= 1e-6) return (v * 1e6).toFixed(1) + 'e-6';
  return v.toExponential(1);
}

function _drawYAxis(ctx, c, maxAbs, mid, amp, W, H) {
  ctx.font = '9px monospace';
  ctx.textAlign = 'right';
  ctx.lineWidth = 0.5;

  // Ticks: +max, +half, 0, -half, -max — overlaid on right edge
  var ticks = [maxAbs, maxAbs * 0.5, 0, -maxAbs * 0.5, -maxAbs];
  ticks.forEach(function(val) {
    var y = mid - (val / maxAbs) * amp;
    // Tick dash
    ctx.strokeStyle = c.grid;
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    ctx.moveTo(W, y);
    ctx.lineTo(W - 6, y);
    ctx.stroke();
    ctx.setLineDash([]);
    // Label with background for readability
    var label = _formatSI(val);
    var tw = ctx.measureText(label).width;
    ctx.fillStyle = c.bg;
    ctx.globalAlpha = 0.7;
    ctx.fillRect(W - tw - 10, y - 5, tw + 4, 10);
    ctx.globalAlpha = 1.0;
    ctx.fillStyle = c.axisText;
    ctx.fillText(label, W - 7, y + 3);
  });

  // Unit label top-right
  var ulw = ctx.measureText('m/s').width;
  ctx.fillStyle = c.bg;
  ctx.globalAlpha = 0.7;
  ctx.fillRect(W - ulw - 6, 1, ulw + 4, 11);
  ctx.globalAlpha = 1.0;
  ctx.fillStyle = c.axisText;
  ctx.fillText('m/s', W - 4, 10);
  ctx.textAlign = 'left';
}

function renderWaveformCanvas(canvas, trace, eventTime, cssW, cssH, dpr) {
  var ctx = canvas.getContext('2d');
  dpr = dpr || 1;
  var W = cssW || canvas.width;
  var H = cssH || canvas.height;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  var data = trace.data;
  var n = data.length;
  var c = _waveColors();

  // Clear
  ctx.fillStyle = c.bg;
  ctx.fillRect(0, 0, W, H);
  if (n === 0) return;

  // Normalize data
  var maxAbs = 0;
  for (var i = 0; i < n; i++) {
    var a = Math.abs(data[i]);
    if (a > maxAbs) maxAbs = a;
  }
  if (maxAbs === 0) maxAbs = 1;

  var plotLeft = 0;
  var plotW = W;
  var mid = H / 2;
  var amp = (H / 2) * 0.85;

  // Draw waveform
  ctx.strokeStyle = c.trace;
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (var j = 0; j < n; j++) {
    var x = plotLeft + (j / (n - 1)) * plotW;
    var y = mid - (data[j] / maxAbs) * amp;
    if (j === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();

  // Draw origin time + pick markers
  if (trace.starttime) {
    var tStart = new Date(trace.starttime.replace('T', ' ').replace('Z', '')).getTime() / 1000;
    var tEnd = tStart + n / (trace.sampling_rate || 100);
    var dur = tEnd - tStart;

    // Origin time line
    if (eventTime) {
      var originT = new Date(eventTime.replace('T', ' ').replace('Z', '')).getTime() / 1000;
      var otFrac = (originT - tStart) / dur;
      if (otFrac >= 0 && otFrac <= 1) {
        ctx.strokeStyle = c.ot;
        ctx.lineWidth = 1.5;
        ctx.setLineDash([6, 4]);
        ctx.beginPath();
        ctx.moveTo(plotLeft + otFrac * plotW, 0);
        ctx.lineTo(plotLeft + otFrac * plotW, H);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = c.ot;
        ctx.font = 'bold 12px monospace';
        ctx.fillText('OT', plotLeft + otFrac * plotW + 3, H - 4);
      }
    }

    // Pick markers
    if (trace.picks) {
      trace.picks.forEach(function(pick) {
        if (!pick.time) return;
        var pickT = new Date(pick.time.replace('T', ' ').replace('Z', '')).getTime() / 1000;
        var frac = (pickT - tStart) / dur;
        if (frac < 0 || frac > 1) return;
        var px = plotLeft + frac * plotW;

        var color = pick.phase === 'P' ? '#ef4444' : '#22c55e';
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.setLineDash([4, 3]);
        ctx.beginPath();
        ctx.moveTo(px, 0);
        ctx.lineTo(px, H);
        ctx.stroke();
        ctx.setLineDash([]);

        // Label
        ctx.fillStyle = color;
        ctx.font = 'bold 12px monospace';
        var label = pick.phase + (pick.probability != null ? ' ' + (pick.probability * 100).toFixed(0) + '%' : '');
        ctx.fillText(label, px + 3, 14);
      });
    }

    // Time axis labels (relative to origin time or start)
    ctx.fillStyle = c.axisText;
    ctx.font = '10px monospace';
    for (var t = 0; t <= 10; t++) {
      var relSec = (t / 10) * dur;
      var xPos = plotLeft + (t / 10) * plotW;
      var refT = eventTime ? new Date(eventTime.replace('T', ' ').replace('Z', '')).getTime() / 1000 : tStart;
      var relToRef = (tStart + relSec) - refT;
      ctx.fillText((relToRef >= 0 ? '+' : '') + relToRef.toFixed(1) + 's', xPos + 2, H - 2);
    }
  }

  // Y-axis overlay (drawn last, on top of waveform)
  _drawYAxis(ctx, c, maxAbs, mid, amp, W, H);
}

document.getElementById('wf-close').addEventListener('click', function() {
  document.getElementById('waveform-overlay').classList.remove('visible');
});
document.getElementById('wf-reload').addEventListener('click', function() {
  if (currentWaveformEventId) showEventWaveforms(currentWaveformEventId);
});

// ── Review Mode ─────────────────────────────────────────────────
var reviewState = {
  eventId: null,
  eventTime: null,
  eventLat: null,
  eventLon: null,
  traces: [],
  picks: [],
  relocated: false,
  locationResult: null,
  magnitudeResult: null,
  dirty: true,
  pickMode: 'addP',
  picksModified: false,
  dragPick: null,
  dragCanvas: null,
  undoStack: [],
  redoStack: [],
};

// ── Cursor crosshair + live time readout ────────────────────────
// One throttled redraw per animation frame. Tracks the active canvas +
// last clientX so the crosshair re-renders on top of the wave without
// blocking the main event loop.
var _activeCrosshair = null;  // { canvas, trace, clientX }
var _crosshairPending = false;

function _scheduleCrosshair(canvas, trace, clientX) {
  // Spectrogram canvases hold a pre-rendered PNG that's drawn asynchronously
  // (Image.onload). Calling drawReviewCanvas on them paints a waveform over
  // the spectrogram, and the obvious "repaint the spectrogram first" fix
  // races the async load. Cleaner: just don't crosshair-overlay on them.
  // Click handlers (add pick / delete / drag) keep working since they bind
  // on mousedown, not mousemove.
  if (canvas.classList && canvas.classList.contains('spectrogram-canvas')) return;
  _activeCrosshair = { canvas: canvas, trace: trace, clientX: clientX };
  if (_crosshairPending) return;
  _crosshairPending = true;
  requestAnimationFrame(function() {
    _crosshairPending = false;
    if (!_activeCrosshair) return;
    var a = _activeCrosshair;
    drawReviewCanvas(a.canvas, a.trace);
    _drawCursorCrosshair(a.canvas, a.trace, a.clientX);
  });
}

function _drawCursorCrosshair(canvas, trace, clientX) {
  var ctx = canvas.getContext('2d');
  var dpr = window.devicePixelRatio || 1;
  var W = canvas.width / dpr;
  var H = canvas.height / dpr;
  var rect = canvas.getBoundingClientRect();
  var pxX = clientX - rect.left;
  if (pxX < 0 || pxX > rect.width) return;
  // Translate to canvas-CSS pixels (we already setTransform'd to dpr).
  var cx = (pxX / rect.width) * W;

  // Vertical guide line
  ctx.strokeStyle = '#3b82f6';
  ctx.lineWidth = 1;
  ctx.setLineDash([2, 3]);
  ctx.beginPath();
  ctx.moveTo(cx, 0);
  ctx.lineTo(cx, H);
  ctx.stroke();
  ctx.setLineDash([]);

  // Time readout — relative to event origin, ms precision regardless of zoom
  var view = _getTraceView(trace);
  var tCursor = view.start + (pxX / rect.width) * view.dur;
  var originT = parseTimeStr(reviewState.eventTime || trace.starttime);
  var offset = tCursor - originT;
  var label = (offset >= 0 ? '+' : '') + offset.toFixed(3) + 's';

  ctx.font = 'bold 11px monospace';
  var tw = ctx.measureText(label).width;
  // Background pill so the label reads against any waveform colour
  var bw = tw + 8, bh = 14;
  // Position: prefer to the right of the cursor; flip left if near right edge
  var bx = cx + 4;
  if (bx + bw > W - 2) bx = cx - bw - 4;
  ctx.fillStyle = '#3b82f6';
  ctx.fillRect(bx, 2, bw, bh);
  ctx.fillStyle = '#fff';
  ctx.textBaseline = 'middle';
  ctx.fillText(label, bx + 4, 2 + bh / 2);
  ctx.textBaseline = 'alphabetic';  // restore default for any later drawing
}

// Time-window helper used by draw + click-to-time conversion. Always the
// trace's natural extent — wheel zoom was tried and pulled (it desyncs the
// pre-rendered spectrograms below the waveforms). Kept as a function so
// the draw / pick code paths have one definition of "the window" if zoom
// is ever revisited via a different mechanism.
function _getTraceView(trace) {
  var tStart = parseTimeStr(trace.starttime);
  var sr = trace.sampling_rate || 100;
  var tEnd = tStart + (trace.data ? trace.data.length / sr : 0);
  return { start: tStart, end: tEnd, dur: tEnd - tStart, fullStart: tStart, fullEnd: tEnd };
}

// Choose a "nice" tick interval (1/2/5 × 10^n) targeting ~6-8 ticks.
function _niceTickInterval(durSec, targetTicks) {
  targetTicks = targetTicks || 7;
  var rough = durSec / targetTicks;
  var exp = Math.floor(Math.log10(rough));
  var base = rough / Math.pow(10, exp);
  var nice;
  if      (base < 1.5) nice = 1;
  else if (base < 3)   nice = 2;
  else if (base < 7)   nice = 5;
  else                 nice = 10;
  return nice * Math.pow(10, exp);
}

// Format a time offset (seconds) relative to a reference time, choosing
// precision based on the magnitude of the tick interval.
function _formatTimeOffset(offsetSec, interval) {
  // Decimal places driven by tick interval — 1 ms ticks need 3 decimals, etc.
  var decimals;
  if      (interval >= 1)    decimals = 1;
  else if (interval >= 0.1)  decimals = 1;
  else if (interval >= 0.01) decimals = 2;
  else                       decimals = 3;
  var s = offsetSec.toFixed(decimals);
  if (offsetSec > 0 && s[0] !== '-') s = '+' + s;
  return s + 's';
}

function openReviewMode(eventId) {
  updateReviewQueueCount();
  reviewState.eventId = eventId;
  reviewState.picks = [];
  reviewState.relocated = false;
  reviewState.locationResult = null;
  reviewState.magnitudeResult = null;
  reviewState.dirty = true;
  reviewState.picksModified = false;
  reviewState.undoStack = [];
  reviewState.redoStack = [];
  document.getElementById('rv-save').disabled = true;
  document.getElementById('rv-results').style.display = 'none';
  document.getElementById('rv-unsaved-dot').classList.remove('active');
  document.getElementById('rv-title').innerHTML = 'Review \u2014 ' + esc(eventId);
  document.getElementById('review-overlay').classList.add('visible');
  // Reset map state on open and initialize
  if (reviewMapLayers) reviewMapLayers.clearLayers();
  if (!reviewMap) initReviewMap();
  setTimeout(function() { if (reviewMap) reviewMap.invalidateSize(); }, 100);
  // Load event type into dropdown
  document.getElementById('rv-event-type').value = 'undetermined';
  fetchJSON('/api/event/' + encodeURIComponent(eventId)).then(function(data) {
    if (data && data.event) {
      document.getElementById('rv-event-type').value = data.event.event_type || 'undetermined';
    }
  }).catch(function() {});
  loadReviewWaveforms();
}

function reviewStatusBadge(status) {
  if (status === 'confirmed') return ' <span style="color:var(--green);font-size:.7rem;font-weight:700">\u25cf Confirmed</span>';
  if (status === 'rejected') return ' <span style="color:var(--red);font-size:.7rem;font-weight:700">\u25cf Rejected</span>';
  return ' <span style="color:var(--amber);font-size:.7rem;font-weight:700">\u25cf Unreviewed</span>';
}

function updateReviewTitleBadge() {
  // Fetch event detail to get review_status for badge
  fetchJSON('/api/event/' + encodeURIComponent(reviewState.eventId)).then(function(data) {
    if (data && data.event) {
      document.getElementById('rv-title').innerHTML = 'Review \u2014 ' + esc(reviewState.eventId) + reviewStatusBadge(data.event.review_status);
    }
  }).catch(function() {});
}

function loadReviewWaveforms() {
  var body = document.getElementById('rv-body');
  body.innerHTML = '<div class="empty-state">Loading all station waveforms...</div>';
  updateReviewTitleBadge();

  var channel = document.getElementById('rv-channel').value;
  var before = document.getElementById('rv-before').value || 10;
  var after = document.getElementById('rv-after').value || 60;
  var fmin = document.getElementById('rv-fmin').value;
  var fmax = document.getElementById('rv-fmax').value;

  var showSpec = document.getElementById('rv-spectrogram').checked;
  var url = '/api/event/' + encodeURIComponent(reviewState.eventId) + '/waveforms/all?channel=' + channel + '&window_before=' + before + '&window_after=' + after + '&max_samples=10000&spectrogram=' + showSpec;
  if (fmin) url += '&freqmin=' + fmin;
  if (fmax) url += '&freqmax=' + fmax;

  fetchJSON(url).then(function(data) {
    reviewState.eventTime = data.event_time;
    reviewState.eventLat = data.event_latitude;
    reviewState.eventLon = data.event_longitude;
    reviewState.traces = data.traces || [];

    // Sort traces by distance from event origin (closest first)
    var evLat = reviewState.locationResult ? reviewState.locationResult.latitude : reviewState.eventLat;
    var evLon = reviewState.locationResult ? reviewState.locationResult.longitude : reviewState.eventLon;
    if (evLat != null && evLon != null) {
      reviewState.traces.sort(function(a, b) {
        var dA = (a.latitude != null && a.longitude != null) ? haversine_km(evLat, evLon, a.latitude, a.longitude) : 99999;
        var dB = (b.latitude != null && b.longitude != null) ? haversine_km(evLat, evLon, b.latitude, b.longitude) : 99999;
        return dA - dB;
      });
    }

    // Import existing picks if we haven't edited yet
    if (reviewState.picks.length === 0) {
      var seenPicks = {};
      data.traces.forEach(function(tr) {
        if (tr.picks) {
          tr.picks.forEach(function(p) {
            if (p.time) {
              var dedupKey = tr.network + '.' + tr.station + '.' + p.phase + '.' + p.time;
              if (seenPicks[dedupKey]) return;
              seenPicks[dedupKey] = true;
              reviewState.picks.push({
                network: tr.network,
                station: tr.station,
                channel: p.channel || tr.channel,
                phase: p.phase,
                time: p.time,
                probability: p.probability || 0.8,
                amplitude: p.amplitude || 0,
              });
            }
          });
        }
      });
    }

    renderReviewTraces();
    updatePickStatus();
    // Ensure map container is sized before updating
    if (reviewMap) reviewMap.invalidateSize();
    updateReviewMap();
  }).catch(function(err) {
    body.innerHTML = '<div class="empty-state">Failed to load waveforms: ' + esc(err.message || err) + '</div>';
  });
}

function traceDistLabel(tr) {
  var evLat = reviewState.locationResult ? reviewState.locationResult.latitude : reviewState.eventLat;
  var evLon = reviewState.locationResult ? reviewState.locationResult.longitude : reviewState.eventLon;
  if (evLat != null && evLon != null && tr.latitude != null && tr.longitude != null) {
    var d = haversine_km(evLat, evLon, tr.latitude, tr.longitude);
    return ' <span style="color:var(--muted);font-size:.55rem;opacity:.8">' + d.toFixed(1) + ' km</span>';
  }
  return '';
}

function renderReviewTraces() {
  var body = document.getElementById('rv-body');
  body.innerHTML = '';

  var is3C = document.getElementById('rv-channel').value === '3C';

  if (is3C) {
    // Group traces by network.station
    var groups = {};
    var groupOrder = [];
    reviewState.traces.forEach(function(tr) {
      var key = tr.network + '.' + tr.station;
      if (!groups[key]) {
        groups[key] = [];
        groupOrder.push(key);
      }
      groups[key].push(tr);
    });

    groupOrder.forEach(function(key) {
      var trList = groups[key];
      var groupDiv = document.createElement('div');
      groupDiv.className = 'review-trace-group';
      var groupLabel = document.createElement('div');
      groupLabel.className = 'group-label';
      groupLabel.innerHTML = esc(key) + traceDistLabel(trList[0]);
      groupDiv.appendChild(groupLabel);

      var zTrace = null;
      var lastDiv = null;

      trList.forEach(function(tr) {
        var div = document.createElement('div');
        div.className = 'review-trace';
        var label = document.createElement('div');
        label.className = 'review-trace-label';
        var chanSuffix = tr.channel ? tr.channel.slice(-1) : '?';
        label.innerHTML = esc(chanSuffix);
        if (!tr.has_data && tr.error) {
          label.innerHTML += ' <span class="no-data-badge">' + esc(tr.error) + '</span>';
        }
        div.appendChild(label);

        if (tr.data && tr.data.length > 0) {
          var canvas = document.createElement('canvas');
          canvas.dataset.network = tr.network;
          canvas.dataset.station = tr.station;
          canvas.dataset.channel = tr.channel;
          div.appendChild(canvas);
          // Remember Z trace for spectrogram (rendered after last component)
          if (chanSuffix === 'Z' && tr.spectrogram_b64) {
            zTrace = tr;
          }
          lastDiv = div;
          groupDiv.appendChild(div);
          var dpr2 = window.devicePixelRatio || 1;
          var cw2 = canvas.clientWidth || 1600;
          var ch2 = canvas.clientHeight || 80;
          canvas.width = cw2 * dpr2;
          canvas.height = ch2 * dpr2;
          drawReviewCanvas(canvas, tr);
          attachCanvasEvents(canvas, tr);
        } else {
          var nodata = document.createElement('div');
          nodata.className = 'empty-state';
          nodata.textContent = tr.error || 'No data';
          nodata.style.height = '80px';
          nodata.style.lineHeight = '80px';
          div.appendChild(nodata);
          lastDiv = div;
          groupDiv.appendChild(div);
        }
      });

      // Append spectrogram under the last component (not Z)
      if (zTrace && lastDiv) {
        var specCanvas = document.createElement('canvas');
        specCanvas.className = 'spectrogram-canvas';
        specCanvas.dataset.network = zTrace.network;
        specCanvas.dataset.station = zTrace.station;
        specCanvas.dataset.channel = zTrace.channel;
        specCanvas.dataset.role = 'spectrogram';
        lastDiv.appendChild(specCanvas);
        zTrace._specCanvas = specCanvas;
        var staPicks = reviewState.picks.filter(function(p) {
          return p.station === zTrace.station && p.network === zTrace.network;
        });
        drawSpectrogramCanvas(zTrace._specCanvas, zTrace, reviewState.eventTime, staPicks);
        attachCanvasEvents(zTrace._specCanvas, zTrace);
      }

      body.appendChild(groupDiv);
    });
  } else {
    // Single-channel mode — existing behavior
    reviewState.traces.forEach(function(tr) {
      var div = document.createElement('div');
      div.className = 'review-trace';
      var label = document.createElement('div');
      label.className = 'review-trace-label';
      label.innerHTML = esc(tr.network + '.' + tr.station + '.' + tr.channel) + traceDistLabel(tr);
      if (!tr.has_data && tr.error) {
        label.innerHTML += ' <span class="no-data-badge">' + esc(tr.error) + '</span>';
      }
      div.appendChild(label);

      if (tr.data && tr.data.length > 0) {
        var canvas = document.createElement('canvas');
        canvas.dataset.network = tr.network;
        canvas.dataset.station = tr.station;
        canvas.dataset.channel = tr.channel;
        div.appendChild(canvas);
        if (tr.spectrogram_b64) {
          var specCanvas = document.createElement('canvas');
          specCanvas.className = 'spectrogram-canvas';
          specCanvas.dataset.network = tr.network;
          specCanvas.dataset.station = tr.station;
          specCanvas.dataset.channel = tr.channel;
          specCanvas.dataset.role = 'spectrogram';
          div.appendChild(specCanvas);
          tr._specCanvas = specCanvas;
        }
        body.appendChild(div);
        var dpr2 = window.devicePixelRatio || 1;
        var cw2 = canvas.clientWidth || 1600;
        var ch2 = canvas.clientHeight || 120;
        canvas.width = cw2 * dpr2;
        canvas.height = ch2 * dpr2;
        drawReviewCanvas(canvas, tr);
        attachCanvasEvents(canvas, tr);
        if (tr._specCanvas) {
          var staPicks = reviewState.picks.filter(function(p) {
            return p.station === tr.station && p.network === tr.network;
          });
          drawSpectrogramCanvas(tr._specCanvas, tr, reviewState.eventTime, staPicks);
          attachCanvasEvents(tr._specCanvas, tr);
        }
      } else {
        var nodata = document.createElement('div');
        nodata.className = 'empty-state';
        nodata.textContent = tr.error || 'No data';
        nodata.style.height = '120px';
        nodata.style.lineHeight = '120px';
        div.appendChild(nodata);
        body.appendChild(div);
      }
    });
  }
}

function drawReviewCanvas(canvas, trace) {
  var ctx = canvas.getContext('2d');
  var dpr = window.devicePixelRatio || 1;
  var W = canvas.width / dpr;
  var H = canvas.height / dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  var data = trace.data;
  var n = data.length;
  var c = _waveColors();

  ctx.fillStyle = c.bg;
  ctx.fillRect(0, 0, W, H);
  if (n === 0) return;

  // Visible time window (synced across all traces via reviewState).
  // When unzoomed, view spans the whole trace.
  var view = _getTraceView(trace);
  var tStart = view.fullStart;
  var sr = trace.sampling_rate || 100;
  var viewStart = view.start, viewEnd = view.end, viewDur = view.dur;
  if (viewDur <= 0) return;

  var plotLeft = 0;
  var plotW = W;
  var mid = H / 2;
  var amp = (H / 2) * 0.85;

  // Sample indices that intersect the visible window. Clamp to trace bounds
  // so panning past the edges just shows blank instead of crashing.
  var iFirst = Math.max(0, Math.floor((viewStart - tStart) * sr));
  var iLast  = Math.min(n - 1, Math.ceil((viewEnd - tStart) * sr));

  // Normalise to the VISIBLE peak — zooming into a quiet patch reveals
  // detail that the global peak would otherwise flatten. This is what
  // makes the canvas useful for ms-accurate picking.
  var maxAbs = 0;
  for (var i = iFirst; i <= iLast; i++) {
    var a = Math.abs(data[i]);
    if (a > maxAbs) maxAbs = a;
  }
  if (maxAbs === 0) maxAbs = 1;

  // Helper: time → canvas x.
  function tx(timeSec) {
    return plotLeft + ((timeSec - viewStart) / viewDur) * plotW;
  }

  // Draw waveform (visible samples only).
  ctx.strokeStyle = c.trace;
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (var j = iFirst; j <= iLast; j++) {
    var sampleTime = tStart + j / sr;
    var x = tx(sampleTime);
    var y = mid - (data[j] / maxAbs) * amp;
    if (j === iFirst) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  }
  ctx.stroke();

  // Event origin time
  var eventOriginT = reviewState.eventTime ? parseTimeStr(reviewState.eventTime) : tStart;
  if (reviewState.eventTime) {
    if (eventOriginT >= viewStart && eventOriginT <= viewEnd) {
      var ox = tx(eventOriginT);
      ctx.strokeStyle = c.ot;
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      ctx.moveTo(ox, 0); ctx.lineTo(ox, H);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = c.ot;
      ctx.font = 'bold 13px monospace';
      ctx.fillText('OT', ox + 3, H - 4);
    }
  }

  // Picks for this station
  var staPicks = reviewState.picks.filter(function(p) {
    return p.station === trace.station && p.network === trace.network;
  });
  staPicks.forEach(function(pick) {
    var pickT = parseTimeStr(pick.time);
    if (pickT < viewStart || pickT > viewEnd) return;
    var px = tx(pickT);
    var color = pick.phase === 'P' ? '#ef4444' : '#22c55e';
    ctx.strokeStyle = color;
    ctx.lineWidth = 2.5;
    ctx.setLineDash([5, 3]);
    ctx.beginPath();
    ctx.moveTo(px, 0); ctx.lineTo(px, H);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = color;
    ctx.font = 'bold 14px monospace';
    ctx.fillText(pick.phase, px + 3, 16);
  });

  // Theoretical travel time lines after relocation
  if (reviewState.locationResult && trace.latitude != null && trace.longitude != null) {
    var loc = reviewState.locationResult;
    if (loc.latitude != null && loc.longitude != null) {
      var horizDist = haversine_km(trace.latitude, trace.longitude, loc.latitude, loc.longitude);
      var depthKm = loc.depth_km || 0;
      var hypoDist = Math.sqrt(horizDist * horizDist + depthKm * depthKm);
      var refT = parseTimeStr(loc.time || reviewState.eventTime);
      var tpPred = refT + hypoDist / 5.0;
      var tsPred = refT + hypoDist / 2.89;
      function _drawPred(timeSec, label, color) {
        if (timeSec < viewStart || timeSec > viewEnd) return;
        var px = tx(timeSec);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.2;
        ctx.setLineDash([3, 4]);
        ctx.beginPath();
        ctx.moveTo(px, 0); ctx.lineTo(px, H);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = color;
        ctx.font = '11px monospace';
        ctx.fillText(label, px + 3, 30);
      }
      _drawPred(tpPred, 'Tp', '#00e5ff');
      _drawPred(tsPred, 'Ts', '#ff9800');
    }
  }

  // Time axis: major + minor ticks. Major ticks get dashed full-height
  // gridlines + a label; minor ticks (1/5 of major) get short marks
  // above the label band as visual reference for sub-major intervals.
  // Both labelled as offsets from event origin so picks align visually
  // with the same +0.012s style across zoom levels.
  var interval = _niceTickInterval(viewDur, 7);
  var minorInterval = interval / 5;
  var firstMajor = Math.ceil(viewStart / interval) * interval;
  var firstMinor = Math.ceil(viewStart / minorInterval) * minorInterval;
  ctx.fillStyle = c.axisText;
  ctx.strokeStyle = c.grid;
  ctx.font = '10px monospace';
  ctx.textAlign = 'left';
  ctx.lineWidth = 0.5;

  // Minor ticks first (so majors overlay cleanly where they coincide).
  ctx.globalAlpha = 0.55;
  for (var minor = firstMinor; minor <= viewEnd; minor += minorInterval) {
    var xm = tx(minor);
    ctx.beginPath();
    ctx.moveTo(xm, H - 18);
    ctx.lineTo(xm, H - 14);
    ctx.stroke();
  }
  ctx.globalAlpha = 1.0;

  // Major ticks: dashed full-height + label
  for (var tick = firstMajor; tick <= viewEnd; tick += interval) {
    var xT = tx(tick);
    ctx.setLineDash([1, 3]);
    ctx.beginPath();
    ctx.moveTo(xT, 0); ctx.lineTo(xT, H - 14);
    ctx.stroke();
    ctx.setLineDash([]);
    var label = _formatTimeOffset(tick - eventOriginT, interval);
    ctx.fillText(label, xT + 2, H - 2);
  }

  // Y-axis overlay (drawn last, on top of waveform)
  _drawYAxis(ctx, c, maxAbs, mid, amp, W, H);
}

function parseTimeStr(s) {
  if (!s) return 0;
  var str = String(s);
  // If the string already carries an explicit timezone (Z, +HH:MM, -HH:MM),
  // parse it as-is; otherwise assume UTC and append Z.
  var hasTz = /(Z|[+-]\d{2}:?\d{2})$/.test(str);
  var t = Date.parse(hasTz ? str : (str.replace(' ', 'T') + 'Z'));
  return isNaN(t) ? 0 : t / 1000;
}

function haversine_km(lat1, lon1, lat2, lon2) {
  var R = 6371.0;
  var rlat1 = lat1 * Math.PI / 180, rlat2 = lat2 * Math.PI / 180;
  var dlat = (lat2 - lat1) * Math.PI / 180, dlon = (lon2 - lon1) * Math.PI / 180;
  var a = Math.sin(dlat/2)**2 + Math.cos(rlat1)*Math.cos(rlat2)*Math.sin(dlon/2)**2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function greatCirclePoints(lat1, lon1, lat2, lon2, npts) {
  npts = npts || 50;
  var toRad = Math.PI / 180, toDeg = 180 / Math.PI;
  var phi1 = lat1 * toRad, lam1 = lon1 * toRad;
  var phi2 = lat2 * toRad, lam2 = lon2 * toRad;
  var d = 2 * Math.asin(Math.sqrt(
    Math.sin((phi2 - phi1)/2)**2 + Math.cos(phi1)*Math.cos(phi2)*Math.sin((lam2 - lam1)/2)**2
  ));
  if (d < 1e-10) return [[lat1, lon1], [lat2, lon2]];
  var pts = [];
  for (var i = 0; i <= npts; i++) {
    var f = i / npts;
    var A = Math.sin((1 - f) * d) / Math.sin(d);
    var B = Math.sin(f * d) / Math.sin(d);
    var x = A * Math.cos(phi1) * Math.cos(lam1) + B * Math.cos(phi2) * Math.cos(lam2);
    var y = A * Math.cos(phi1) * Math.sin(lam1) + B * Math.cos(phi2) * Math.sin(lam2);
    var z = A * Math.sin(phi1) + B * Math.sin(phi2);
    pts.push([Math.atan2(z, Math.sqrt(x*x + y*y)) * toDeg, Math.atan2(y, x) * toDeg]);
  }
  return pts;
}

function redrawAllReviewCanvases() {
  var body = document.getElementById('rv-body');
  if (!body) return;
  var canvases = body.querySelectorAll('canvas:not(.spectrogram-canvas)');
  canvases.forEach(function(canvas) {
    var net = canvas.dataset.network;
    var sta = canvas.dataset.station;
    var chan = canvas.dataset.channel;
    var tr = reviewState.traces.find(function(t) {
      return t.network === net && t.station === sta && t.channel === chan;
    });
    if (tr && tr.data && tr.data.length > 0) {
      drawReviewCanvas(canvas, tr);
    }
  });
}

function drawSpectrogramCanvas(canvas, trace, eventTime, picks) {
  var dpr = window.devicePixelRatio || 1;
  var cssW = canvas.clientWidth || 800;
  var cssH = canvas.clientHeight || 50;
  canvas.width = cssW * dpr;
  canvas.height = cssH * dpr;
  var ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  if (!trace.spectrogram_b64) return;

  var img = new Image();
  img.onload = function() {
    // Shift spectrogram left by ~2s to align STFT centers with waveform
    var dur = trace.data.length / (trace.sampling_rate || 100);
    var shiftPx = (0.5 / dur) * cssW;
    ctx.drawImage(img, shiftPx, 0, cssW, cssH);

    // Frequency axis labels (right edge, with padding to avoid clipping).
    // Translucent black on light, translucent white on dark so the labels
    // read against the spectrogram regardless of the underlying colormap
    // density at the right edge.
    ctx.fillStyle = _isLightTheme()
      ? 'rgba(0,0,0,0.6)'
      : 'rgba(255,255,255,0.7)';
    ctx.font = '9px monospace';
    ctx.textAlign = 'right';
    var nyquist = (trace.sampling_rate || 100) / 2;
    var labels = [0, nyquist * 0.25, nyquist * 0.5, nyquist * 0.75, nyquist];
    var padTop = 10;
    var padBot = 4;
    labels.forEach(function(freq, i) {
      var frac = i / (labels.length - 1);
      var y = (cssH - padBot) - frac * (cssH - padTop - padBot);
      ctx.fillText(freq.toFixed(0) + ' Hz', cssW - 3, y);
    });
    ctx.textAlign = 'left';

    // Draw pick lines on spectrogram (same time mapping as waveform)
    var picksToShow = picks || (trace.picks || []);
    if (picksToShow.length > 0 && trace.starttime) {
      var tStart = parseTimeStr(trace.starttime);
      var n = trace.data.length;
      var dur = n / (trace.sampling_rate || 100);
      picksToShow.forEach(function(pick) {
        if (!pick.time) return;
        var pickT = parseTimeStr(pick.time);
        var frac = (pickT - tStart) / dur;
        if (frac < 0 || frac > 1) return;
        var px = frac * cssW;
        var color = pick.phase === 'P' ? '#ef4444' : '#22c55e';
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.setLineDash([4, 3]);
        ctx.beginPath();
        ctx.moveTo(px, 0);
        ctx.lineTo(px, cssH);
        ctx.stroke();
        ctx.setLineDash([]);
      });
    }

    // Draw origin time line
    if (eventTime) {
      var tStart2 = parseTimeStr(trace.starttime);
      var n2 = trace.data.length;
      var dur2 = n2 / (trace.sampling_rate || 100);
      var originT = parseTimeStr(eventTime);
      var frac2 = (originT - tStart2) / dur2;
      if (frac2 >= 0 && frac2 <= 1) {
        ctx.strokeStyle = _light ? '#334155' : '#ffffff';
        ctx.lineWidth = 1.5;
        ctx.setLineDash([6, 4]);
        ctx.beginPath();
        ctx.moveTo(frac2 * cssW, 0);
        ctx.lineTo(frac2 * cssW, cssH);
        ctx.stroke();
        ctx.setLineDash([]);
      }
    }
  };
  img.src = 'data:image/png;base64,' + trace.spectrogram_b64;
}

function redrawTraceCanvases(canvas, trace) {
  // Find the waveform canvas (first canvas sibling without spectrogram-canvas class)
  var parent = canvas.parentElement;
  if (parent) {
    var canvases = parent.querySelectorAll('canvas:not(.spectrogram-canvas)');
    if (canvases.length > 0) {
      drawReviewCanvas(canvases[0], trace);
    }
  } else {
    drawReviewCanvas(canvas, trace);
  }
  // Redraw spectrogram: either on this trace or the Z trace of the same station (3C mode)
  var specTrace = trace;
  if (!specTrace._specCanvas) {
    specTrace = reviewState.traces.find(function(t) {
      return t.network === trace.network && t.station === trace.station && t._specCanvas;
    }) || null;
  }
  if (specTrace && specTrace._specCanvas) {
    var staPicks = reviewState.picks.filter(function(p) {
      return p.station === specTrace.station && p.network === specTrace.network;
    });
    drawSpectrogramCanvas(specTrace._specCanvas, specTrace, reviewState.eventTime, staPicks);
  }
}

function canvasToTime(canvas, clientX, trace) {
  // Uses the (possibly-zoomed) visible window so pixel-to-time conversion
  // gives sub-pixel accuracy when zoomed in. Clicking at pixel 400 in an
  // 800-pixel-wide canvas over a 1s window yields exactly 0.500 s into
  // the window, regardless of the underlying trace duration.
  var rect = canvas.getBoundingClientRect();
  var frac = (clientX - rect.left) / rect.width;
  frac = Math.max(0, Math.min(1, frac));
  var view = _getTraceView(trace);
  return view.start + frac * view.dur;
}

function timeToIso(t) {
  return new Date(t * 1000).toISOString();
}

function findNearestPick(canvas, clientX, trace) {
  // Same fractional-tolerance approach as before, but in the visible
  // window's coordinate system. Picks outside the current view are
  // unreachable by this nearest-search (you have to zoom out / pan first).
  var rect = canvas.getBoundingClientRect();
  var clickFrac = (clientX - rect.left) / rect.width;
  var view = _getTraceView(trace);
  var tolerance = 10 / rect.width; // 10px tolerance

  var best = null;
  var bestDist = Infinity;
  reviewState.picks.forEach(function(p, idx) {
    if (p.station !== trace.station || p.network !== trace.network) return;
    var pickT = parseTimeStr(p.time);
    var pickFrac = (pickT - view.start) / view.dur;
    if (pickFrac < 0 || pickFrac > 1) return;  // off-screen pick
    var dist = Math.abs(pickFrac - clickFrac);
    if (dist < tolerance && dist < bestDist) {
      bestDist = dist;
      best = { pick: p, index: idx };
    }
  });
  return best;
}

// Measure peak absolute amplitude (m/s velocity) from waveform data
// between P and S picks (or P + 5s window if no S).
function measureAmplitude(trace, network, station) {
  if (!trace || !trace.data || trace.data.length === 0) return 0;
  var sr = trace.sampling_rate || 100;
  var tStart = parseTimeStr(trace.starttime);

  // Find P and S picks for this station
  var pTime = null, sTime = null;
  reviewState.picks.forEach(function(pk) {
    if (pk.station !== station || pk.network !== network) return;
    var t = parseTimeStr(pk.time);
    if (pk.phase === 'P' && (pTime === null || t < pTime)) pTime = t;
    if (pk.phase === 'S' && (sTime === null || t > (pTime || 0))) sTime = t;
  });

  if (pTime === null) return 0;

  // Measurement window: P to S, or P to P+5s
  var winStart = pTime;
  var winEnd = sTime ? sTime : pTime + 5.0;
  // Ensure at least 0.5s window
  if (winEnd - winStart < 0.5) winEnd = winStart + 0.5;

  var i0 = Math.max(0, Math.round((winStart - tStart) * sr));
  var i1 = Math.min(trace.data.length - 1, Math.round((winEnd - tStart) * sr));
  if (i1 <= i0) return 0;

  var maxAbs = 0;
  for (var i = i0; i <= i1; i++) {
    var v = Math.abs(trace.data[i]);
    if (v > maxAbs) maxAbs = v;
  }
  return maxAbs;
}

// Update amplitude on all picks for a given station using available trace data
function updateStationAmplitudes(network, station) {
  // Find the best trace for amplitude measurement (prefer Z component)
  var bestTrace = null;
  reviewState.traces.forEach(function(tr) {
    if (tr.network !== network || tr.station !== station) return;
    if (!tr.data || tr.data.length === 0) return;
    if (!bestTrace || tr.channel.slice(-1) === 'Z') bestTrace = tr;
  });
  if (!bestTrace) return;

  var amp = measureAmplitude(bestTrace, network, station);
  reviewState.picks.forEach(function(pk) {
    if (pk.station === station && pk.network === network) {
      pk.amplitude = amp;
    }
  });
}

function attachCanvasEvents(canvas, trace) {
  canvas.addEventListener('mousedown', function(e) {
    var mode = reviewState.pickMode;

    if (mode === 'addP' || mode === 'addS') {
      pushUndo();
      var t = canvasToTime(canvas, e.clientX, trace);
      var phase = mode === 'addP' ? 'P' : 'S';
      reviewState.picks.push({
        network: trace.network,
        station: trace.station,
        channel: trace.channel,
        phase: phase,
        time: timeToIso(t),
        probability: 0.8,
        amplitude: 0,
      });
      reviewState.dirty = true;
      reviewState.picksModified = true;
      document.getElementById('rv-save').disabled = true;
      document.getElementById('rv-unsaved-dot').classList.add('active');
      updateStationAmplitudes(trace.network, trace.station);
      redrawTraceCanvases(canvas, trace);
      updatePickStatus();
    } else if (mode === 'delete') {
      var found = findNearestPick(canvas, e.clientX, trace);
      if (found) {
        pushUndo();
        reviewState.picks.splice(found.index, 1);
        reviewState.dirty = true;
        reviewState.picksModified = true;
        document.getElementById('rv-save').disabled = true;
        document.getElementById('rv-unsaved-dot').classList.add('active');
        updateStationAmplitudes(trace.network, trace.station);
        redrawTraceCanvases(canvas, trace);
        updatePickStatus();
      }
    } else if (mode === 'drag') {
      var found2 = findNearestPick(canvas, e.clientX, trace);
      if (found2) {
        pushUndo();
        reviewState.dragPick = found2;
        reviewState.dragCanvas = canvas;
        canvas.style.cursor = 'grabbing';
      }
    }
  });

  canvas.addEventListener('mousemove', function(e) {
    if (reviewState.pickMode === 'drag' && reviewState.dragPick && reviewState.dragCanvas === canvas) {
      var t = canvasToTime(canvas, e.clientX, trace);
      reviewState.dragPick.pick.time = timeToIso(t);
      redrawTraceCanvases(canvas, trace);
      return;
    }
    // Live cursor crosshair + time readout. Throttled to one redraw per
    // animation frame so dragging a 1080p mouse across the canvas at high
    // dpi doesn't queue dozens of redraws per second.
    _scheduleCrosshair(canvas, trace, e.clientX);
  });

  canvas.addEventListener('mouseleave', function() {
    // Clear crosshair when leaving the canvas, and release any in-progress
    // pick-drag (the existing behavior — cancel the drag if the cursor
    // leaves the canvas, since we can't tell where they're letting go).
    _activeCrosshair = null;
    if (reviewState.dragPick && reviewState.dragCanvas === canvas) {
      reviewState.dragPick = null;
      reviewState.dragCanvas = null;
      canvas.style.cursor = 'crosshair';
    }
    // Same skip as _scheduleCrosshair — don't paint a waveform over a
    // spectrogram canvas when the cursor leaves it.
    if (canvas.classList && canvas.classList.contains('spectrogram-canvas')) return;
    drawReviewCanvas(canvas, trace);
  });

  canvas.addEventListener('mouseup', function(e) {
    if (reviewState.dragPick && reviewState.dragCanvas === canvas) {
      var t = canvasToTime(canvas, e.clientX, trace);
      reviewState.dragPick.pick.time = timeToIso(t);
      reviewState.dirty = true;
      reviewState.picksModified = true;
      document.getElementById('rv-save').disabled = true;
      document.getElementById('rv-unsaved-dot').classList.add('active');
      reviewState.dragPick = null;
      reviewState.dragCanvas = null;
      canvas.style.cursor = 'crosshair';
      updateStationAmplitudes(trace.network, trace.station);
      redrawTraceCanvases(canvas, trace);
      updatePickStatus();
    }
  });

}

function updatePickStatus() {
  var pCount = reviewState.picks.filter(function(p) { return p.phase === 'P'; }).length;
  var sCount = reviewState.picks.filter(function(p) { return p.phase === 'S'; }).length;
  document.getElementById('rv-pick-status').textContent = pCount + 'P + ' + sCount + 'S picks';
}

// ── Undo / Redo ─────────────────────────────────────────────────
function pushUndo() {
  reviewState.undoStack.push(JSON.stringify(reviewState.picks));
  reviewState.redoStack = [];
  if (reviewState.undoStack.length > 50) reviewState.undoStack.shift();
  updateUndoButtons();
}

function undo() {
  if (reviewState.undoStack.length === 0) return;
  reviewState.redoStack.push(JSON.stringify(reviewState.picks));
  reviewState.picks = JSON.parse(reviewState.undoStack.pop());
  reviewState.dirty = true;
  reviewState.picksModified = true;
  document.getElementById('rv-save').disabled = true;
  document.getElementById('rv-unsaved-dot').classList.add('active');
  redrawAllReviewCanvases();
  // Redraw all spectrograms
  reviewState.traces.forEach(function(tr) {
    if (tr._specCanvas) {
      var staPicks = reviewState.picks.filter(function(p) {
        return p.station === tr.station && p.network === tr.network;
      });
      drawSpectrogramCanvas(tr._specCanvas, tr, reviewState.eventTime, staPicks);
    }
  });
  updatePickStatus();
  updateUndoButtons();
}

function redo() {
  if (reviewState.redoStack.length === 0) return;
  reviewState.undoStack.push(JSON.stringify(reviewState.picks));
  reviewState.picks = JSON.parse(reviewState.redoStack.pop());
  reviewState.dirty = true;
  reviewState.picksModified = true;
  document.getElementById('rv-save').disabled = true;
  document.getElementById('rv-unsaved-dot').classList.add('active');
  redrawAllReviewCanvases();
  reviewState.traces.forEach(function(tr) {
    if (tr._specCanvas) {
      var staPicks = reviewState.picks.filter(function(p) {
        return p.station === tr.station && p.network === tr.network;
      });
      drawSpectrogramCanvas(tr._specCanvas, tr, reviewState.eventTime, staPicks);
    }
  });
  updatePickStatus();
  updateUndoButtons();
}

function updateUndoButtons() {
  var undoBtn = document.getElementById('rv-undo');
  var redoBtn = document.getElementById('rv-redo');
  if (undoBtn) undoBtn.disabled = reviewState.undoStack.length === 0;
  if (redoBtn) redoBtn.disabled = reviewState.redoStack.length === 0;
}

// Pick mode buttons
document.querySelectorAll('.pick-mode-btn').forEach(function(btn) {
  btn.addEventListener('click', function() {
    document.querySelectorAll('.pick-mode-btn').forEach(function(b) { b.classList.remove('active'); });
    btn.classList.add('active');
    reviewState.pickMode = btn.getAttribute('data-mode');
  });
});


// Relocate
document.getElementById('rv-relocate').addEventListener('click', function() {
  var btn = document.getElementById('rv-relocate');
  btn.disabled = true;
  btn.textContent = 'Relocating...';

  fetch('/api/event/' + encodeURIComponent(reviewState.eventId) + '/relocate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ picks: reviewState.picks }),
  })
  .then(function(r) {
    if (!r.ok) return r.json().then(function(d) { throw new Error(d.detail || 'Relocation failed'); });
    return r.json();
  })
  .then(function(result) {
    reviewState.relocated = true;
    reviewState.locationResult = result.location;
    reviewState.magnitudeResult = result.magnitude;
    reviewState.dirty = false;
    document.getElementById('rv-save').disabled = false;
    showRelocationResults(result);
    redrawAllReviewCanvases();
    updateReviewMap();
    updateMainMapEvent();
  })
  .catch(function(err) {
    alert('Relocation failed: ' + err.message);
  })
  .finally(function() {
    btn.disabled = false;
    btn.textContent = 'Relocate';
  });
});

function showRelocationResults(result) {
  var panel = document.getElementById('rv-results');
  panel.style.display = 'block';

  var loc = result.location || {};
  var mag = result.magnitude || {};
  var grid = document.getElementById('rv-results-grid');
  grid.innerHTML = [
    { label: 'Latitude', value: loc.latitude != null ? loc.latitude.toFixed(4) : '-' },
    { label: 'Longitude', value: loc.longitude != null ? loc.longitude.toFixed(4) : '-' },
    { label: 'Depth', value: loc.depth_km != null ? loc.depth_km.toFixed(2) + ' km' : '-' },
    { label: 'Time', value: loc.time ? loc.time.slice(0, 23) : '-' },
    { label: 'ML', value: mag.ml != null ? mag.ml.toFixed(2) : '-' },
    { label: 'ML err', value: mag.ml_err != null ? '\u00b1' + mag.ml_err.toFixed(2) : '-' },
    { label: 'Picks used', value: loc.num_picks || '-' },
    { label: 'ML stations', value: mag.num_ml_sta || '-' },
  ].map(function(item) {
    return '<div class="rr-item"><div class="rr-value">' + esc(item.value) + '</div><div class="rr-label">' + esc(item.label) + '</div></div>';
  }).join('');

  var tbody = document.getElementById('rv-residual-tbody');
  var residuals = result.residuals || [];
  tbody.innerHTML = residuals.map(function(r) {
    var absR = Math.abs(r.residual_s);
    var cls = absR < 0.5 ? 'residual-good' : absR < 1.0 ? 'residual-warn' : 'residual-bad';
    return '<tr><td>' + esc(r.station) + '</td><td>' + esc(r.phase) + '</td>' +
      '<td>' + r.observed_s.toFixed(3) + '</td><td>' + r.predicted_s.toFixed(3) + '</td>' +
      '<td class="' + cls + '">' + r.residual_s.toFixed(3) + '</td></tr>';
  }).join('');
}

// Layer group for great circle arcs on the main map (cleared on close)
var mainMapArcsLayer = L.layerGroup().addTo(map);

function updateMainMapEvent() {
  // Move the event dot on the main dashboard map to the relocated position
  if (!reviewState.locationResult || !reviewState.eventId) return;
  var loc = reviewState.locationResult;
  if (loc.latitude == null || loc.longitude == null) return;
  var mag = reviewState.magnitudeResult;
  var mlVal = (mag && mag.ml != null) ? mag.ml : 0;

  // Collect layers to remove first (avoid modifying during iteration)
  var toRemove = [];
  eventLayer.eachLayer(function(layer) {
    var tip = layer.getTooltip && layer.getTooltip();
    if (tip) {
      var content = tip.getContent();
      if (content && content.indexOf(reviewState.eventId) !== -1) {
        toRemove.push(layer);
      }
    }
  });
  toRemove.forEach(function(layer) { eventLayer.removeLayer(layer); });

  var radius = 2 + mlVal * 1.5;
  L.circleMarker([loc.latitude, loc.longitude], {
    radius: radius,
    fillColor: eventColor(mlVal),
    color: '#000',
    weight: 1,
    fillOpacity: 0.8
  })
  .bindTooltip(
    '<b>' + esc(reviewState.eventId) + '</b><br>' + esc(loc.time || '') + '<br>M ' + mlVal.toFixed(1) + ' \u00b7 ' + (loc.depth_km != null ? loc.depth_km.toFixed(1) : '?') + ' km',
    { className: '' }
  )
  .on('click', function() { showEventDetail(reviewState.eventId); })
  .addTo(eventLayer);

  // Draw concentric distance circles on the main map
  mainMapArcsLayer.clearLayers();
  var maxDist = 0;
  var seenDist = {};
  reviewState.traces.forEach(function(tr) {
    if (tr.latitude == null || tr.longitude == null) return;
    var dk = tr.network + '.' + tr.station;
    if (seenDist[dk]) return;
    seenDist[dk] = true;
    var d = haversine_km(loc.latitude, loc.longitude, tr.latitude, tr.longitude);
    if (d > maxDist) maxDist = d;
  });
  var ringStep = 10;
  if (maxDist > 100) ringStep = 25;
  if (maxDist > 250) ringStep = 50;
  var numRings = Math.ceil(maxDist / ringStep);
  for (var ri = 1; ri <= numRings; ri++) {
    var ringKm = ri * ringStep;
    mainMapArcsLayer.addLayer(L.circle([loc.latitude, loc.longitude], {
      radius: ringKm * 1000,
      color: '#64748b',
      weight: 0.8,
      opacity: 0.5,
      fill: false,
      dashArray: '4 4',
      interactive: false,
    }));
    var labelLat = loc.latitude + (ringKm / 111.12);
    mainMapArcsLayer.addLayer(L.marker([labelLat, loc.longitude], {
      icon: L.divIcon({
        className: '',
        iconSize: [0, 0],
        html: '<span style="font-size:9px;color:var(--muted);white-space:nowrap;position:relative;left:-12px;top:-6px">' + ringKm + 'km</span>',
      }),
      interactive: false,
    }));
  }
}

// ── Review Map ──────────────────────────────────────────────────
var reviewMap = null;
var reviewMapLayers = L.layerGroup();

var _reviewTileLayer = null;
function initReviewMap() {
  if (reviewMap) return;
  reviewMap = L.map('rv-map-container', { zoomControl: true }).setView([31.85, -106.40], 10);
  var rvUrl = _currentAppTheme === 'light'
    ? 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png'
    : 'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png';
  _reviewTileLayer = L.tileLayer(rvUrl, {
    attribution: '&copy; CARTO &copy; OSM',
    subdomains: 'abcd', maxZoom: 19
  }).addTo(reviewMap);
  reviewMapLayers.addTo(reviewMap);
}

function updateReviewMap() {
  if (!reviewMap) initReviewMap();

  reviewMapLayers.clearLayers();

  // Determine event location: relocated or catalog
  var evLat = null, evLon = null;
  if (reviewState.locationResult && reviewState.locationResult.latitude != null) {
    evLat = reviewState.locationResult.latitude;
    evLon = reviewState.locationResult.longitude;
  } else if (reviewState.eventLat != null) {
    evLat = reviewState.eventLat;
    evLon = reviewState.eventLon;
  }

  // Collect unique stations from traces
  var seenSta = {};
  var bounds = [];
  reviewState.traces.forEach(function(tr) {
    if (tr.latitude == null || tr.longitude == null) return;
    var key = tr.network + '.' + tr.station;
    if (seenSta[key]) return;
    seenSta[key] = true;
    var marker = L.marker([tr.latitude, tr.longitude], { icon: stationIcon('#3b82f6') });
    marker.bindTooltip(key, { permanent: false, direction: 'top', className: 'station-tooltip' });
    reviewMapLayers.addLayer(marker);
    bounds.push([tr.latitude, tr.longitude]);
  });

  // Event marker
  if (evLat != null && evLon != null) {
    var evLatLng = [evLat, evLon];
    bounds.push(evLatLng);

    var evIcon = L.divIcon({
      className: '',
      iconSize: [18, 18],
      iconAnchor: [9, 9],
      html: '<svg width="18" height="18" viewBox="0 0 18 18"><polygon points="9,1 11.5,6.5 17,7.2 13,11 14,16.5 9,13.8 4,16.5 5,11 1,7.2 6.5,6.5" fill="#fbbf24" stroke="#fff" stroke-width="0.8"/></svg>'
    });
    reviewMapLayers.addLayer(L.marker(evLatLng, { icon: evIcon }).bindTooltip('Event', { direction: 'top' }));

    // Concentric distance circles around epicenter
    // Find max station distance to determine ring range
    var maxDist = 0;
    var seenDist = {};
    reviewState.traces.forEach(function(tr) {
      if (tr.latitude == null || tr.longitude == null) return;
      var dk = tr.network + '.' + tr.station;
      if (seenDist[dk]) return;
      seenDist[dk] = true;
      var d = haversine_km(evLat, evLon, tr.latitude, tr.longitude);
      if (d > maxDist) maxDist = d;
    });

    // Draw rings at regular intervals
    var ringStep = 10; // km
    if (maxDist > 100) ringStep = 25;
    if (maxDist > 250) ringStep = 50;
    var numRings = Math.ceil(maxDist / ringStep);
    for (var ri = 1; ri <= numRings; ri++) {
      var ringKm = ri * ringStep;
      var circle = L.circle(evLatLng, {
        radius: ringKm * 1000, // meters
        color: '#64748b',
        weight: 0.8,
        opacity: 0.5,
        fill: false,
        dashArray: '4 4',
        interactive: false,
      });
      reviewMapLayers.addLayer(circle);
      // Label at top of circle
      var labelLat = evLat + (ringKm / 111.12);
      reviewMapLayers.addLayer(L.marker([labelLat, evLon], {
        icon: L.divIcon({
          className: '',
          iconSize: [0, 0],
          html: '<span style="font-size:9px;color:var(--muted);white-space:nowrap;position:relative;left:-12px;top:-6px">' + ringKm + 'km</span>',
        }),
        interactive: false,
      }));
    }
  }

  if (bounds.length > 1) {
    reviewMap.fitBounds(bounds, { padding: [20, 20] });
  } else if (bounds.length === 1) {
    reviewMap.setView(bounds[0], 10);
  }
}


// Save
document.getElementById('rv-save').addEventListener('click', function() {
  if (!reviewState.relocated) {
    alert('Please relocate first before saving.');
    return;
  }
  var btn = document.getElementById('rv-save');
  btn.disabled = true;
  btn.textContent = 'Saving...';

  fetch('/api/event/' + encodeURIComponent(reviewState.eventId) + '/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      picks: reviewState.picks,
      location: reviewState.locationResult,
      magnitude: reviewState.magnitudeResult,
      review_status: 'confirmed',
      event_type: document.getElementById('rv-event-type').value,
    }),
  })
  .then(function(r) {
    if (!r.ok) return r.json().then(function(d) { throw new Error(d.detail || 'Save failed'); });
    return r.json();
  })
  .then(function(data) {
    reviewState.picksModified = false;
    document.getElementById('rv-unsaved-dot').classList.remove('active');
    btn.textContent = 'Saved!';
    setTimeout(function() { btn.textContent = 'Save'; }, 2000);
    if (data && data.duplicate_warning) {
      alert('Saved, but: ' + data.duplicate_warning);
    }
    // Refresh dashboard data
    pollData();
    pollSlow();
    // Refresh catalog if loaded
    if (tabDataLoaded.catalog) loadCatalogTable();
  })
  .catch(function(err) {
    alert('Save failed: ' + err.message);
    btn.textContent = 'Save';
    btn.disabled = false;
  });
});

// Close
document.getElementById('rv-close').addEventListener('click', function() {
  if (reviewState.picksModified) {
    if (!confirm('You have unsaved pick changes. Close anyway?')) return;
  }
  reviewState.picksModified = false;
  document.getElementById('rv-unsaved-dot').classList.remove('active');
  document.getElementById('review-overlay').classList.remove('visible');
  // Clear map layers on close
  if (reviewMapLayers) reviewMapLayers.clearLayers();
  if (mainMapArcsLayer) mainMapArcsLayer.clearLayers();
});

// Undo/Redo button click handlers
document.getElementById('rv-undo').addEventListener('click', function() { undo(); });
document.getElementById('rv-redo').addEventListener('click', function() { redo(); });

// Confirm handler — same as Save but sends review_status: "confirmed"
document.getElementById('rv-confirm').addEventListener('click', function() {
  if (!reviewState.relocated) {
    alert('Please relocate first before confirming.');
    return;
  }
  var btn = document.getElementById('rv-confirm');
  btn.disabled = true;
  btn.textContent = 'Confirming...';

  fetch('/api/event/' + encodeURIComponent(reviewState.eventId) + '/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      picks: reviewState.picks,
      location: reviewState.locationResult,
      magnitude: reviewState.magnitudeResult,
      review_status: 'confirmed',
      event_type: document.getElementById('rv-event-type').value,
    }),
  })
  .then(function(r) {
    if (!r.ok) return r.json().then(function(d) { throw new Error(d.detail || 'Confirm failed'); });
    return r.json();
  })
  .then(function() {
    reviewState.picksModified = false;
    document.getElementById('rv-unsaved-dot').classList.remove('active');
    btn.textContent = '\u2713 Confirmed!';
    setTimeout(function() { btn.textContent = '\u2713 Confirm'; btn.disabled = false; }, 1500);
    pollData();
    if (tabDataLoaded.catalog) loadCatalogTable();
    advanceToNextUnreviewed();
  })
  .catch(function(err) {
    alert('Confirm failed: ' + err.message);
    btn.textContent = '\u2713 Confirm';
    btn.disabled = false;
  });
});

// Reject handler — quick status update, no relocation required
document.getElementById('rv-reject').addEventListener('click', function() {
  var btn = document.getElementById('rv-reject');
  btn.disabled = true;
  btn.textContent = 'Rejecting...';

  var rejectBody = { status: 'rejected' };
  var evTypeVal = document.getElementById('rv-event-type').value;
  if (evTypeVal !== 'undetermined') rejectBody.event_type = evTypeVal;

  fetch('/api/event/' + encodeURIComponent(reviewState.eventId) + '/review-status', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(rejectBody),
  })
  .then(function(r) {
    if (!r.ok) return r.json().then(function(d) { throw new Error(d.detail || 'Reject failed'); });
    return r.json();
  })
  .then(function() {
    reviewState.picksModified = false;
    document.getElementById('rv-unsaved-dot').classList.remove('active');
    btn.textContent = '\u2717 Rejected!';
    setTimeout(function() { btn.textContent = '\u2717 Reject'; btn.disabled = false; }, 1500);
    pollData();
    if (tabDataLoaded.catalog) loadCatalogTable();
    advanceToNextUnreviewed();
  })
  .catch(function(err) {
    alert('Reject failed: ' + err.message);
    btn.textContent = '\u2717 Reject';
    btn.disabled = false;
  });
});

// ── Review queue ────────────────────────────────────────────────
// Count of unreviewed events; drives the "Review next" button in the
// catalog toolbar and the counter chip in the review header.
function updateReviewQueueCount() {
  fetchJSON('/api/catalog?review_status=unreviewed&per_page=1')
    .then(function(data) {
      var n = (data && data.total) || 0;
      var btn = document.getElementById('cat-review-next');
      if (btn) {
        btn.hidden = n === 0;
        var c = document.getElementById('cat-queue-count');
        if (c) c.textContent = '(' + n + ')';
      }
      var chip = document.getElementById('rv-queue');
      if (chip) {
        chip.textContent = n > 0 ? n + ' in queue' : 'queue clear';
        chip.classList.toggle('rv-queue-clear', n === 0);
      }
    })
    .catch(function() {});
}

// Auto-advance to next unreviewed event
function advanceToNextUnreviewed() {
  updateReviewQueueCount();
  fetchJSON('/api/catalog?review_status=unreviewed&per_page=1&sort_by=time&sort_order=asc')
    .then(function(data) {
      if (data && data.events && data.events.length > 0) {
        var nextId = data.events[0].event_id;
        openReviewMode(nextId);
      } else {
        alert('All events reviewed!');
        document.getElementById('review-overlay').classList.remove('visible');
        if (reviewMapLayers) reviewMapLayers.clearLayers();
        if (mainMapArcsLayer) mainMapArcsLayer.clearLayers();
      }
    })
    .catch(function() {});
}

// Keyboard shortcuts in review mode
document.addEventListener('keydown', function(e) {
  if (!document.getElementById('review-overlay').classList.contains('visible')) return;
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;

  var key = e.key.toLowerCase();

  // Undo: Ctrl+Z
  if ((e.ctrlKey || e.metaKey) && key === 'z' && !e.shiftKey) {
    undo();
    e.preventDefault();
    return;
  }
  // Redo: Ctrl+Y or Ctrl+Shift+Z
  if ((e.ctrlKey || e.metaKey) && (key === 'y' || (key === 'z' && e.shiftKey))) {
    redo();
    e.preventDefault();
    return;
  }

  // Pick mode keyboard shortcuts
  var modeMap = { p: 'addP', s: 'addS', d: 'delete', g: 'drag' };
  if (modeMap[key]) {
    reviewState.pickMode = modeMap[key];
    document.querySelectorAll('.pick-mode-btn').forEach(function(b) {
      b.classList.toggle('active', b.getAttribute('data-mode') === modeMap[key]);
    });
    e.preventDefault();
  } else if (key === 'r') {
    document.getElementById('rv-relocate').click();
    e.preventDefault();
  } else if (key === 'c') {
    document.getElementById('rv-confirm').click();
    e.preventDefault();
  } else if (key === 'x') {
    document.getElementById('rv-reject').click();
    e.preventDefault();
  } else if (key === 'n') {
    advanceToNextUnreviewed();
    e.preventDefault();
  } else if (key === 'escape') {
    document.getElementById('rv-close').click();
  }
});

// Reload waveforms
document.getElementById('rv-reload').addEventListener('click', function() {
  if (reviewState.eventId) loadReviewWaveforms();
});

// ── Waveviewer Tab ──────────────────────────────────────────────

function loadWaveviewerTraces() {
  var dateVal = document.getElementById('wv-date').value;
  var hh = document.getElementById('wv-hh').value;
  var mm = document.getElementById('wv-mm').value;
  if (!dateVal || hh === '' || mm === '') {
    alert('Please enter a date, hour, and minute.');
    return;
  }
  var ss = document.getElementById('wv-ss').value || '0';
  var windowSec = parseInt(document.getElementById('wv-window').value, 10);
  // Build UTC ISO string: pad h/m/s to 2 digits
  var pad2 = function(v) { var s = String(parseInt(v, 10)); return s.length < 2 ? '0' + s : s; };
  var startISO = dateVal + 'T' + pad2(hh) + ':' + pad2(mm) + ':' + pad2(ss) + 'Z';
  var startD = new Date(startISO);
  var endD = new Date(startD.getTime() + windowSec * 1000);
  var endISO = endD.toISOString();

  var channel = document.getElementById('wv-channel').value;
  var fmin = document.getElementById('wv-fmin').value;
  var fmax = document.getElementById('wv-fmax').value;
  var showSpec = document.getElementById('wv-spectrogram').checked;

  var body = document.getElementById('wv-trace-body');
  body.innerHTML = '<div class="empty-state">Loading waveforms...</div>';
  document.getElementById('wv-summary').textContent = '';

  var url = '/api/waveforms/all?start=' + encodeURIComponent(startISO) +
    '&end=' + encodeURIComponent(endISO) +
    '&channel=' + encodeURIComponent(channel) +
    '&max_samples=10000&spectrogram=' + showSpec;
  if (fmin) url += '&freqmin=' + fmin;
  if (fmax) url += '&freqmax=' + fmax;

  fetchJSON(url).then(function(data) {
    if (!data.traces || data.traces.length === 0) {
      body.innerHTML = '<div class="empty-state">No waveform data available for this time window.</div>';
      return;
    }
    var withData = data.traces.filter(function(t) { return t.has_data; });
    var totalPicks = 0;
    data.traces.forEach(function(t) { totalPicks += (t.picks ? t.picks.length : 0); });
    document.getElementById('wv-summary').textContent = withData.length + ' / ' + data.traces.length + ' traces with data, ' + totalPicks + ' picks';

    body.innerHTML = '';
    data.traces.forEach(function(tr) {
      var div = document.createElement('div');
      div.className = 'waveform-trace';
      var label = document.createElement('div');
      label.className = 'waveform-trace-label';
      var pickInfo = '';
      if (tr.picks && tr.picks.length > 0) {
        var pCount = tr.picks.filter(function(p) { return p.phase === 'P'; }).length;
        var sCount = tr.picks.filter(function(p) { return p.phase === 'S'; }).length;
        pickInfo = ' \u2014 ' + pCount + 'P + ' + sCount + 'S picks';
      }
      label.textContent = tr.network + '.' + tr.station + '.' + tr.channel + pickInfo + (tr.error && !tr.has_data ? ' \u2014 ' + tr.error : '');
      div.appendChild(label);

      if (tr.data && tr.data.length > 0) {
        var canvas = document.createElement('canvas');
        div.appendChild(canvas);
        if (tr.spectrogram_b64) {
          var specCanvas = document.createElement('canvas');
          specCanvas.className = 'spectrogram-canvas';
          div.appendChild(specCanvas);
        }
        body.appendChild(div);
        var dpr = window.devicePixelRatio || 1;
        var dw = canvas.clientWidth || 800;
        var dh = canvas.clientHeight || 100;
        canvas.width = dw * dpr;
        canvas.height = dh * dpr;
        renderWaveformCanvas(canvas, tr, null, dw, dh, dpr);
        if (tr.spectrogram_b64) {
          drawSpectrogramCanvas(specCanvas, tr, null);
        }
      } else {
        var nodata = document.createElement('div');
        nodata.className = 'empty-state';
        nodata.textContent = tr.error || 'No data';
        nodata.style.height = '60px';
        nodata.style.lineHeight = '60px';
        div.appendChild(nodata);
        body.appendChild(div);
      }
    });
  }).catch(function(err) {
    body.innerHTML = '<div class="empty-state" style="color:var(--red)">Failed to load waveforms: ' + esc(err.message || 'unknown error') + '</div>';
  });
}

document.getElementById('wv-load').addEventListener('click', function() {
  loadWaveviewerTraces();
});


// ── Pipeline Control Logic ──────────────────────────────────────

// Backfill modal HTML
(function() {
  var modal = document.createElement('div');
  modal.className = 'modal-overlay';
  modal.id = 'backfill-modal';
  modal.innerHTML =
    '<div class="modal-box">' +
      '<h3>Backfill Date Range</h3>' +
      '<label>Start date</label>' +
      '<input type="date" id="bf-start" />' +
      '<label>End date</label>' +
      '<input type="date" id="bf-end" />' +
      '<div class="modal-check"><input type="checkbox" id="bf-force" /> <span>Force reprocessing</span></div>' +
      '<div class="modal-actions">' +
        '<button class="btn-modal-cancel" id="bf-cancel">Cancel</button>' +
        '<button class="btn-modal-start" id="bf-submit">Start Backfill</button>' +
      '</div>' +
    '</div>';
  document.body.appendChild(modal);
})();

function updateControlButtons(isRunning) {
  var startBtn = document.getElementById('btn-start-pipeline');
  var stopBtn = document.getElementById('btn-stop-pipeline');
  startBtn.disabled = isRunning;
  stopBtn.style.display = isRunning ? '' : 'none';
}

// Toggle start dropdown
document.getElementById('btn-start-pipeline').addEventListener('click', function(e) {
  if (this.disabled) return;
  e.stopPropagation();
  document.getElementById('start-menu').classList.toggle('open');
});
document.addEventListener('click', function() {
  document.getElementById('start-menu').classList.remove('open');
});

// Start continuous
document.getElementById('start-continuous').addEventListener('click', function() {
  document.getElementById('start-menu').classList.remove('open');
  if (!confirm('Start the pipeline in continuous mode?')) return;
  fetch('/api/pipeline/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mode: 'continuous' })
  }).then(function(r) {
    if (r.status === 409) { alert('Pipeline is already running.'); return; }
    if (!r.ok) return r.json().then(function(d) { alert(d.detail || 'Failed to start'); });
    updateControlButtons(true);
  }).catch(function() { alert('Failed to reach server.'); });
});

// Open backfill modal
document.getElementById('start-backfill').addEventListener('click', function() {
  document.getElementById('start-menu').classList.remove('open');
  document.getElementById('backfill-modal').classList.add('open');
});
document.getElementById('bf-cancel').addEventListener('click', function() {
  document.getElementById('backfill-modal').classList.remove('open');
});

// Submit backfill
document.getElementById('bf-submit').addEventListener('click', function() {
  var start = document.getElementById('bf-start').value;
  var end = document.getElementById('bf-end').value;
  if (!start || !end) { alert('Please select both start and end dates.'); return; }
  if (start > end) { alert('Start date must be before end date.'); return; }
  document.getElementById('backfill-modal').classList.remove('open');
  if (!confirm('Start backfill from ' + start + ' to ' + end + '?')) return;
  fetch('/api/pipeline/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      mode: 'backfill',
      start: start,
      end: end,
      force: document.getElementById('bf-force').checked
    })
  }).then(function(r) {
    if (r.status === 409) { alert('Pipeline is already running.'); return; }
    if (!r.ok) return r.json().then(function(d) { alert(d.detail || 'Failed to start'); });
    updateControlButtons(true);
  }).catch(function() { alert('Failed to reach server.'); });
});

// Stop pipeline
document.getElementById('btn-stop-pipeline').addEventListener('click', function() {
  if (!confirm('Stop the running pipeline?')) return;
  fetch('/api/pipeline/stop', { method: 'POST' }).then(function(r) {
    if (r.status === 404) { alert('No pipeline is running.'); return; }
    if (!r.ok) return r.json().then(function(d) { alert(d.detail || 'Failed to stop'); });
    updateControlButtons(false);
  }).catch(function() { alert('Failed to reach server.'); });
});

// Initial check
fetch('/api/pipeline/running').then(function(r) { return r.json(); }).then(function(d) {
  updateControlButtons(d.running);
}).catch(function() {});
