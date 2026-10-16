/* El Paso Seismic Monitor — monitor.js
 * Monitor tab: pipeline status strip, stations + health, events on the map, toasts, event detail.
 *
 * Classic script sharing one global scope with its siblings; no build step.
 * Load order is fixed in index.html (definitions first, boot.js last):
 *   theme-chrome.js → map.js → util.js → monitor.js → pipeline-panels.js → filters-polling.js → tabs.js → helicorder.js → catalog.js → waveforms.js → review.js → waveviewer.js → pipeline-control.js → boot.js
 */

// ── Render pipeline status + step strip ─────────────────────────
function renderStatus(data) {
  if (!data) return;
  if (typeof ovSet === 'function') ovSet('status', data);
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

// Retired stations (end_date set: temporary deployments, dead Shakes) live
// in their own layer, hidden by default, behind the "Retired" map button —
// 49 nodes from a 2021 deployment would otherwise bury the live network.
const retiredLayer = L.layerGroup();
var retiredVisible = false;

function renderStations(stations) {
  if (stationsLoaded) return;
  stationsLoaded = true;
  var nRetired = 0;
  stations.forEach(function(s) {
    var retired = !!s.end_date;
    var tip = '<b>' + esc(s.station) + '</b><br>' + esc(s.network) + ' \u00b7 ' + esc(s.model) +
      '<br>' + esc(s.channels) +
      (retired ? '<br><i>' + esc(s.start_date || '?') + ' \u2192 ' + esc(s.end_date) +
        (s.deployment ? ' \u00b7 ' + esc(s.deployment) : '') + '</i>' : '');
    var m = L.marker([s.latitude, s.longitude], { icon: stationIcon(retired ? HEALTH_COLORS.retired : undefined) })
      .bindTooltip(tip, { className: '' })
      .addTo(retired ? retiredLayer : stationLayer);
    m.on('click', function() { openStationDetail(s.station); });
    stationMarkers[s.network + '.' + s.station] = { marker: m, data: s };
    if (retired) nRetired++;
  });
  var btn = document.getElementById('toggle-retired');
  if (btn) {
    btn.hidden = nRetired === 0;
    btn.textContent = 'Retired (' + nRetired + ')';
    btn.addEventListener('click', function() {
      if (retiredVisible) map.removeLayer(retiredLayer);
      else retiredLayer.addTo(map);
      retiredVisible = !retiredVisible;
      btn.classList.toggle('active', retiredVisible);
    });
  }
}

// ── Station state-of-health panel (marker click) ────────────────
// Latency thresholds account for the day-batch ingest lag (a day is fetched
// 2 h after it ends, so data ending "yesterday" is the healthy steady state).
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
  var HEALTH_LABEL = { ok: 'OK', warning: 'Intermittent', error: 'Offline', unknown: 'Unknown', retired: 'Retired' };
  var active = healthData.filter(function(h) { return h.status !== 'retired'; });
  var up = active.filter(function(h) { return h.status === 'ok'; }).length;
  _sbSet('sb-stations', up + '/' + active.length + ' up');
  healthData.forEach(function(h) {
    var entry = stationMarkers[h.network + '.' + h.station];
    if (!entry || h.status === 'retired') return;
    var color = HEALTH_COLORS[h.status] || HEALTH_COLORS.unknown;
    entry.marker.setIcon(stationIcon(color));
    var s = entry.data;
    entry.marker.setTooltipContent(
      '<b>' + esc(s.station) + '</b><br>' + esc(s.network) + ' \u00b7 ' + esc(s.model) + '<br>' + esc(s.channels) + '<br>Health: <b>' + esc(HEALTH_LABEL[h.status] || h.status) + (h.end_date ? ' (since ' + esc(h.end_date) + ')' : '') + '</b>'
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
  } else if (type === 'auto_reject') {
    // Auto-rejected (only when that layer is on) — hollow dashed ring so it
    // can't be mistaken for an unreviewed candidate
    inner =
      '<circle cx="10" cy="10" r="6" fill="none" stroke="' + stroke +
        '" stroke-width="1.4" stroke-dasharray="2.2 1.6"/>';
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

function _isAutoRejected(e) {
  // Triage verdict that no human has overridden yet.
  return !e.review_status && e.triage === 'auto_reject';
}

function _eventIcon(e) {
  var style = eventMapStyle(e);
  // Pick shape: confirmed earthquake / quarry_blast / auto-rejected / unreviewed
  var type;
  if (e.review_status === 'confirmed') {
    type = (e.event_type === 'quarry_blast') ? 'quarry_blast' : 'earthquake';
  } else if (_isAutoRejected(e)) {
    type = 'auto_reject';
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

// Auto-rejected events (triage verdict, not yet human-reviewed) clutter the
// map, so they stay off unless the "Auto-rejected" layer toggle is on.
var showAutoRejected = false;

function shouldRenderEventOnMap(e) {
  // Hide rejected events entirely. Hide confirmed noise (not an arrival
  // worth showing on a geographic map).
  if (e.review_status === 'rejected') return false;
  if (e.review_status === 'confirmed' && e.event_type === 'noise') return false;
  if (_isAutoRejected(e) && !showAutoRejected) return false;
  return true;
}

document.getElementById('toggle-auto-rejected').addEventListener('click', function() {
  showAutoRejected = !showAutoRejected;
  this.classList.toggle('active', showAutoRejected);
  document.getElementById('ml-auto-rejected').hidden = !showAutoRejected;
  document.getElementById('ml-hidden-note').textContent = showAutoRejected
    ? 'Noise & rejected events hidden'
    : 'Noise, rejected & auto-rejected hidden';
  renderEvents(allEvents || []);
});

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
    if (_isAutoRejected(e)) return;   // triage already dismissed it; catalog still lists it
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
      if (ev.legacy_id) html += '<div class="detail-row"><span class="detail-label">Former ID</span><span class="detail-value mono">' + esc(ev.legacy_id) + '</span></div>';
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
