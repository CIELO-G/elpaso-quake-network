/* El Paso Seismic Monitor — map.js
 * Constants, shared state, Leaflet/MapLibre map, fault + quarry layers, icons.
 *
 * Classic script sharing one global scope with its siblings; no build step.
 * Load order is fixed in index.html (definitions first, boot.js last):
 *   theme-chrome.js → map.js → util.js → monitor.js → pipeline-panels.js → filters-polling.js → tabs.js → helicorder.js → catalog.js → waveforms.js → review.js → waveviewer.js → pipeline-control.js → boot.js
 */

// ── Constants ───────────────────────────────────────────────────
const STEP_COLORS = {
  ingest: '#3b82f6', process: '#f59e0b', detect: '#22c55e',
  associate: '#a855f7', phaseno: '#ec4899', catalog: '#64748b'
};
const STEP_LABELS = {
  ingest: '1 Ingest', process: '2 Process', detect: '3 Detect',
  associate: '4 Associate', phaseno: '4b PhaseNO', catalog: '5 Catalog'
};
const STEP_SHORT = {
  ingest: 'Ingest', process: 'Process', detect: 'Detect',
  associate: 'Assoc', phaseno: 'PhaseNO', catalog: 'Catalog'
};
const STATUS_ICON = {
  pending: '', running: '\u25b6', completed: '\u2713', failed: '\u2717', skipped: '\u2014'
};
const HEALTH_COLORS = { ok: '#10b981', warning: '#f59e0b', error: '#ef4444', unknown: '#64748b', retired: '#94a3b8' };

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

// CARTO's raster basemaps require an API key since 2026-08. The key is
// injected at runtime by /api/mapconfig.js (window.CARTO_BASEMAPS_KEY,
// sourced from the gitignored deploy/app/local.env — never committed).
// With a key: the original CARTO Positron / Dark Matter raster tiles.
// Without one (e.g. a fresh clone): keyless OpenFreeMap vector tiles of
// the same styles, rendered via MapLibre GL (see index.html includes).
// GL layers cannot be shared across maps or reliably re-added after
// removal, so every use site builds a fresh layer via this factory.
function makeBasemapLayer(theme) {
  var style = theme === 'light' ? 'light' : 'dark';
  var key = window.CARTO_BASEMAPS_KEY || '';
  if (key) {
    return L.tileLayer(
      'https://basemaps.cartocdn.com/' + style + '_all/{z}/{x}/{y}{r}.png?key=' + key, {
        attribution: '&copy; <a href="https://carto.com/attributions">CARTO</a> ' +
          '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>',
        maxZoom: 19
      });
  }
  return L.maplibreGL({
    style: 'https://tiles.openfreemap.org/styles/' + (style === 'light' ? 'positron' : 'dark'),
    attribution: '&copy; <a href="https://openfreemap.org">OpenFreeMap</a> ' +
      '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a>, ' +
      'style &copy; <a href="https://carto.com/">CARTO</a>'
  });
}

// satellite/osm are shared raster layers (safe to re-add); dark/light GL
// layers are built fresh on each activation via makeBasemapLayer().
var mapTileLayers = {
  satellite: L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
    attribution: '&copy; <a href="https://www.esri.com/">Esri</a> &mdash; Sources: Esri, Maxar, Earthstar',
    maxZoom: 19
  }),
  osm: L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    maxZoom: 19
  }),
};
var currentMapStyle = _currentAppTheme === 'light' ? 'light' : 'dark';
var currentTileLayer = makeBasemapLayer(currentMapStyle);
currentTileLayer.addTo(map);
// Sync map style buttons to match theme
document.querySelectorAll('.map-style-btn').forEach(function(b) {
  b.classList.toggle('active', b.getAttribute('data-style') === currentMapStyle);
});

// Swap the main map basemap; returns true if the style actually changed.
function _setMainMapStyle(style) {
  if (style === currentMapStyle) return false;
  map.removeLayer(currentTileLayer);
  currentTileLayer = mapTileLayers[style] || makeBasemapLayer(style);
  currentTileLayer.addTo(map);
  currentMapStyle = style;
  return true;
}

document.querySelectorAll('.map-style-btn').forEach(function(btn) {
  btn.addEventListener('click', function() {
    if (!_setMainMapStyle(btn.getAttribute('data-style'))) return;
    document.querySelectorAll('.map-style-btn').forEach(function(b) { b.classList.remove('active'); });
    btn.classList.add('active');
  });
});

// Study area — togglable, hidden by default. Bounds come from /api/study_area
// (lib/constants.py), the same definition GaMMA and the NLLoc grid use, so
// the rectangle is a 1:1 visual of where events can be associated and located.
var studyAreaRect = L.rectangle(
  [[MAP_INIT_VIEW[0] - 1.0, MAP_INIT_VIEW[1] - 1.0], [MAP_INIT_VIEW[0] + 1.0, MAP_INIT_VIEW[1] + 1.0]],
  { color: '#ef4444', weight: 1.5, dashArray: '6 4', fill: false, interactive: false }
);
fetch('/api/study_area').then(function(r) { return r.ok ? r.json() : null; }).then(function(a) {
  if (a) studyAreaRect.setBounds([[a.lat_min, a.lon_min], [a.lat_max, a.lon_max]]);
}).catch(function() {});
var studyAreaVisible = false;
document.getElementById('toggle-study-area').addEventListener('click', function() {
  var btn = this;
  if (studyAreaVisible) {
    map.removeLayer(studyAreaRect);
    studyAreaVisible = false;
  } else {
    studyAreaRect.addTo(map);
    studyAreaVisible = true;
  }
  btn.classList.toggle('active', studyAreaVisible);
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
