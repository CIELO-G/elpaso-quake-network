/* El Paso Seismic Monitor — overview.js
 * Overview tab: the landing page. Answers "is the network healthy and what
 * needs me" in one screen: attention tiles, station health board, review
 * queue, 30-day event rate, pipeline state.
 *
 * Data model: the existing polls (filters-polling.js) push every payload
 * through ovSet(key, value); nothing here fetches on its own. Rendering is
 * coalesced to one requestAnimationFrame and skipped while another tab is
 * active (switchTab calls renderOverview() when the tab is shown).
 *
 * Classic script sharing one global scope with its siblings; no build step.
 * Load order is fixed in index.html (definitions first, boot.js last).
 */

var ovData = {};          // stats, status, progress, health, errors, disk, queue, rate, apihealth
var _ovPending = false;

function ovSet(key, val) {
  ovData[key] = val;
  if (typeof activeTab !== 'undefined' && activeTab !== 'overview') return;
  if (_ovPending) return;
  _ovPending = true;
  requestAnimationFrame(function() { _ovPending = false; renderOverview(); });
}

// "6 d ago" / "3 h ago" / "12 min ago" / "in 8 h"
function fmtAgo(iso) {
  if (!iso) return '';
  var t = new Date(iso).getTime();
  if (isNaN(t)) return '';
  var s = Math.round((Date.now() - t) / 1000);
  var future = s < 0;
  s = Math.abs(s);
  var txt;
  if (s < 90) txt = s + ' s';
  else if (s < 5400) txt = Math.round(s / 60) + ' min';
  else if (s < 129600) txt = Math.round(s / 3600) + ' h';
  else if (s < 86400 * 400) txt = Math.round(s / 86400) + ' d';
  else txt = (s / (86400 * 365.25)).toFixed(1) + ' y';
  return future ? 'in ' + txt : txt + ' ago';
}

function _ovUtc(iso) {
  return iso ? String(iso).slice(0, 16).replace('T', ' ') + ' UTC' : '—';
}

var OV_SEV = ['sev-ok', 'sev-warn', 'sev-bad', 'sev-neutral'];
function _ovTile(id, sev, value, sub) {
  var el = document.getElementById('ov-tile-' + id);
  if (!el) return;
  OV_SEV.forEach(function(c) { el.classList.remove(c); });
  el.classList.add('sev-' + sev);
  el.querySelector('.ov-value').innerHTML = value;
  el.querySelector('.ov-sub').innerHTML = sub || '';
}

function _ovActiveHealth() {
  return (ovData.health || []).filter(function(h) { return h.status !== 'retired'; });
}

// Pipeline state summarised the same way the Pipeline tab's progress panel
// does (running / up to date / behind / failed / idle), so the two never
// disagree about what "healthy" means.
function _ovPipelineState() {
  var st = ovData.status && ovData.status.pipeline;
  var p = ovData.progress;
  if (!st && !p) return { key: 'neutral', label: '—', sub: '' };
  var status = (st && st.status) || (p && p.pipeline_status) || 'idle';
  if (status === 'failed') return { key: 'bad', label: 'Failed', sub: 'see the error log' };
  if (status === 'running') {
    var step = st && (st.steps || []).filter(function(s) { return s.status === 'running'; })[0];
    var stepName = step ? (STEP_SHORT[step.name] || step.name) : null;
    var cur = (st && st.continuous && st.continuous.current_day) || (p && p.current_day) || '';
    return { key: 'neutral', label: 'Running', sub: (cur ? 'day ' + esc(cur) : '') + (stepName ? ' · ' + esc(stepName) : '') };
  }
  if (p && p.target_date) {
    var ageDays = Math.floor((Date.now() - new Date(p.target_date + 'T00:00:00Z').getTime()) / 86400000);
    var next = p.next_run_at ? 'next run ' + fmtAgo(p.next_run_at).replace('in ', 'in ') : '';
    if ((p.percent || 0) >= 95) {
      if (ageDays > 2) return { key: 'warn', label: 'Behind', sub: 'data is ' + ageDays + ' days old' + (next ? ' · ' + next : '') };
      return { key: 'ok', label: 'Up to date', sub: next || 'through ' + esc(p.target_date) };
    }
    return { key: 'neutral', label: status === 'waiting' ? 'Waiting' : 'Idle', sub: (p.days_complete || 0) + ' / ' + (p.total_days || 0) + ' days' };
  }
  return { key: 'neutral', label: status.charAt(0).toUpperCase() + status.slice(1), sub: '' };
}

function _ovTiles() {
  // Review queue
  var q = ovData.queue;
  if (q) {
    var n = q.total || 0;
    var oldest = (q.events || []).length ? q.events[q.events.length - 1] : null;
    _ovTile('queue', n === 0 ? 'ok' : 'warn', n === 0 ? 'Clear' : String(n),
      n === 0 ? 'nothing waiting for review' : (oldest ? 'oldest ' + esc(fmtAgo(oldest.time)) : ''));
  }

  // Stations
  var act = _ovActiveHealth();
  if (ovData.health) {
    var up = act.filter(function(h) { return h.status === 'ok'; }).length;
    var warn = act.filter(function(h) { return h.status === 'warning'; }).length;
    var bad = act.filter(function(h) { return h.status === 'error' || h.status === 'unknown'; }).length;
    var parts = [];
    if (warn) parts.push(warn + ' intermittent');
    if (bad) parts.push(bad + ' offline');
    _ovTile('stations', bad ? 'bad' : warn ? 'warn' : 'ok',
      up + '<span class="ov-dim">/' + act.length + '</span>',
      parts.length ? parts.join(' · ') : 'all stations reporting');
  }

  // Pipeline
  var ps = _ovPipelineState();
  _ovTile('pipeline', ps.key, ps.label, ps.sub);

  // Data freshness + partial days
  var p = ovData.progress, st = ovData.status && ovData.status.pipeline;
  if (p) {
    var through = p.last_associated || p.target_date;
    var partial = (st && st.continuous && st.continuous.days_partial) || [];
    _ovTile('data', partial.length ? 'warn' : 'ok',
      through ? 'through ' + esc(through.slice(5)) : '—',
      partial.length ? partial.length + ' partial day' + (partial.length === 1 ? '' : 's') + ' re-fetching' : 'all days complete');
  }

  // Last event
  var s = ovData.stats;
  if (s) {
    if (s.latest_event_time) {
      _ovTile('last', 'neutral',
        (s.latest_event_mag != null ? 'M' + Number(s.latest_event_mag).toFixed(1) + ' · ' : '') + esc(fmtAgo(s.latest_event_time)),
        esc(_ovUtc(s.latest_event_time)));
    } else {
      _ovTile('last', 'neutral', '—', 'no events yet');
    }
  }

  // Errors + alert channels
  var errs = ovData.errors ? (ovData.errors.errors || []) : null;
  var ah = ovData.apihealth && ovData.apihealth.alerting;
  var chan = ah ? ('alerts: email ' + (ah.email ? '✓' : '—') + ' · webhook ' + (ah.webhook ? '✓' : '—')) : '';
  if (errs) {
    _ovTile('errors', errs.length ? 'bad' : 'ok', errs.length ? String(errs.length) : 'None', chan);
  }
}

var OV_HEALTH_LABEL = { ok: 'Online', warning: 'Intermittent', error: 'Offline', unknown: 'Unknown' };
var OV_HEALTH_RANK = { error: 0, unknown: 1, warning: 2, ok: 3 };

function _ovStations() {
  var body = document.getElementById('ov-sta-body');
  var foot = document.getElementById('ov-sta-foot');
  var count = document.getElementById('ov-sta-count');
  if (!body || !ovData.health) return;
  var act = _ovActiveHealth().slice().sort(function(a, b) {
    var r = (OV_HEALTH_RANK[a.status] || 0) - (OV_HEALTH_RANK[b.status] || 0);
    return r || (a.network + a.station).localeCompare(b.network + b.station);
  });
  var retired = ovData.health.length - act.length;
  count.textContent = act.length ? act.length + ' active' : '';
  body.innerHTML = act.map(function(h) {
    var k = h.status === 'ok' ? 'ok' : h.status === 'warning' ? 'warn' : 'bad';
    var sq = '';
    for (var i = 0; i < 3; i++) sq += '<i class="' + (i < (h.files_recent || 0) ? 'on' : '') + '"></i>';
    return '<tr tabindex="0" data-sta="' + esc(h.station) + '">' +
      '<td class="mono"><span class="ov-dim">' + esc(h.network) + '.</span>' + esc(h.station) + '</td>' +
      '<td><span class="pill pill-h-' + k + '">' + OV_HEALTH_LABEL[h.status] + '</span></td>' +
      '<td class="num"><span class="cov3" title="' + (h.files_recent || 0) + ' of the last 3 days have data">' + sq + '</span></td>' +
      '</tr>';
  }).join('') || '<tr><td colspan="3" class="ov-empty">No station health yet.</td></tr>';
  foot.textContent = retired ? retired + ' retired / campaign station' + (retired === 1 ? '' : 's') + ' not shown' : '';
}

function _ovQueue() {
  var list = document.getElementById('ov-queue-list');
  var count = document.getElementById('ov-queue-count');
  var next = document.getElementById('ov-review-next');
  if (!list || !ovData.queue) return;
  var q = ovData.queue;
  var ev = q.events || [];
  count.textContent = q.total ? q.total + ' waiting' : '';
  next.hidden = !q.total;
  if (!ev.length) {
    list.innerHTML = '<div class="ov-empty ov-clear"><span class="pill pill-confirmed">Queue clear</span> every candidate has been reviewed</div>';
    return;
  }
  list.innerHTML = ev.slice(0, 8).map(function(e) {
    return '<button class="ov-row" data-eid="' + esc(e.event_id) + '">' +
      '<span class="ov-row-time mono">' + esc(String(e.time || '').slice(0, 16).replace('T', ' ')) + '</span>' +
      '<span class="ov-row-mag num">' + (e.magnitude != null ? 'M' + e.magnitude.toFixed(1) : 'M?') + '</span>' +
      '<span class="ov-row-picks ov-dim">' + (e.num_picks || 0) + ' picks</span>' +
      '<span class="ov-row-id mono ov-dim">' + esc(e.event_id) + '</span>' +
      (e.triage === 'flag' ? '<span class="triage-chip triage-flag" title="' + esc(e.triage_reason || '') + '">flag</span>' : '') +
      '<span class="ov-row-go" aria-hidden="true">›</span>' +
      '</button>';
  }).join('') + (q.total > 8 ? '<div class="ov-more">+ ' + (q.total - 8) + ' more in the catalog</div>' : '');
}

// 30-day event rate as a small bar chart. One bar per day, today at the
// right, zero-filled from /api/event_rate.
function _ovRate() {
  var canvas = document.getElementById('ov-rate');
  var totalEl = document.getElementById('ov-rate-total');
  if (!canvas || !ovData.rate) return;
  var byDay = {};
  (ovData.rate.days || []).forEach(function(d) { byDay[d.date] = d.count; });
  var DAYS = 30, counts = [], labels = [], total = 0, max = 1;
  var today = new Date(); today.setUTCHours(0, 0, 0, 0);
  for (var i = DAYS - 1; i >= 0; i--) {
    var d = new Date(today.getTime() - i * 86400000);
    var key = d.toISOString().slice(0, 10);
    var c = byDay[key] || 0;
    counts.push(c); labels.push(key); total += c; if (c > max) max = c;
  }
  totalEl.textContent = total + ' event' + (total === 1 ? '' : 's');

  var W = canvas.parentElement.clientWidth - 2 || 400, H = 72;
  var dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
  canvas.style.height = H + 'px';
  var ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  var css = getComputedStyle(document.documentElement);
  var bar = css.getPropertyValue('--blue').trim() || '#2563eb';
  var muted = css.getPropertyValue('--muted').trim() || '#64748b';
  var border = css.getPropertyValue('--border').trim() || '#cbd5e1';
  var mono = css.getPropertyValue('--font-mono').trim() || 'ui-monospace, Menlo, monospace';
  var padL = 18, padB = 16, padT = 10;
  var plotW = W - padL - 4, plotH = H - padB - padT;
  var slot = plotW / DAYS, bw = Math.max(2, slot - 2);
  // baseline + y max
  ctx.strokeStyle = border; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(padL, padT + plotH + 0.5); ctx.lineTo(W - 4, padT + plotH + 0.5); ctx.stroke();
  ctx.font = '9px ' + mono; ctx.fillStyle = muted; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
  ctx.fillText(String(max), padL - 4, padT);
  ctx.fillText('0', padL - 4, padT + plotH);
  ctx.fillStyle = bar;
  counts.forEach(function(c, i) {
    if (!c) return;
    var h = Math.max(2, (c / max) * plotH);
    ctx.fillRect(padL + i * slot + (slot - bw) / 2, padT + plotH - h, bw, h);
  });
  ctx.fillStyle = muted; ctx.textBaseline = 'alphabetic';
  ctx.textAlign = 'left';  ctx.fillText(labels[0].slice(5), padL, H - 4);
  ctx.textAlign = 'right'; ctx.fillText('today', W - 4, H - 4);
  ctx.textAlign = 'left';
}

function _ovPipeline() {
  var body = document.getElementById('ov-pipe-body');
  if (!body) return;
  var st = ovData.status && ovData.status.pipeline;
  var steps = (ovData.status && ovData.status.steps) || [];
  var p = ovData.progress;
  if (!st && !p) { body.innerHTML = '<div class="ov-empty">Waiting for pipeline status…</div>'; return; }
  var ps = _ovPipelineState();
  function row(k, v) { return '<div class="detail-row"><span class="detail-label">' + k + '</span><span class="detail-value">' + v + '</span></div>'; }
  var html = '';
  html += row('State', '<span class="pill pill-' + ps.key + '">' + ps.label + '</span>');
  if (st) html += row('Mode', esc(st.mode || '—') + (st.started_at ? ' <span class="ov-dim">since ' + esc(fmtAgo(st.started_at)) + '</span>' : ''));
  if (p) {
    html += row('Current day', '<span class="mono">' + esc(p.current_day || '—') + '</span>');
    if (p.next_run_at) html += row('Next run', '<span class="mono">' + esc(_ovUtc(p.next_run_at)) + '</span> <span class="ov-dim">' + esc(fmtAgo(p.next_run_at)) + '</span>');
    html += row('Last complete', '<span class="mono">' +
      [['ingest', p.last_ingested], ['process', p.last_processed], ['detect', p.last_detected], ['assoc', p.last_associated]]
        .map(function(x) { return '<span class="ov-dim">' + x[0] + '</span> ' + esc(x[1] ? x[1].slice(5) : '—'); }).join(' · ') + '</span>');
  }
  var partial = (st && st.continuous && st.continuous.days_partial) || [];
  if (partial.length) {
    var shown = partial.slice(0, 4).map(function(d) { return d.slice(5); }).join(', ');
    html += row('Partial days', '<span class="mono">' + esc(shown) + (partial.length > 4 ? ' <span class="ov-dim">+' + (partial.length - 4) + '</span>' : '') + '</span>');
  }
  if (steps.length) {
    html += '<div class="ov-steps">' + steps.map(function(s) {
      var icon = STATUS_ICON[s.status] || '';
      return '<span class="ov-step st-' + esc(s.status) + '" title="' + esc(s.name) + ': ' + esc(s.status) + '">' + (icon ? icon + ' ' : '') + esc(STEP_SHORT[s.name] || s.name) + '</span>';
    }).join('') + '</div>';
  }
  body.innerHTML = html;
}

function renderOverview() {
  _ovTiles();
  _ovStations();
  _ovQueue();
  _ovRate();
  _ovPipeline();
}

// ── Wiring ──────────────────────────────────────────────────────
(function wireOverview() {
  var root = document.getElementById('tab-overview');
  if (!root) return;

  // Tiles navigate
  root.querySelectorAll('.ov-tile').forEach(function(t) {
    t.addEventListener('click', function() {
      var go = t.getAttribute('data-go');
      if (go === 'queue') {
        var f = document.getElementById('cat-review-filter');
        if (f) f.value = 'queue';
        if (typeof catPage !== 'undefined') catPage = 1;
        switchTab('catalog');
        if (tabDataLoaded.catalog) loadCatalogTable();
      } else if (go === 'stations') {
        switchTab('monitor');
      } else if (go === 'last') {
        var s = ovData.stats;
        switchTab('monitor');
        if (s && s.latest_event_lat != null && s.latest_event_lon != null) map.flyTo([s.latest_event_lat, s.latest_event_lon], 13);
        if (s && s.latest_event_id) showEventDetail(s.latest_event_id);
      } else {
        switchTab('pipeline');
      }
    });
  });

  // Station rows → monitor tab, station panel, pan to it
  var staBody = document.getElementById('ov-sta-body');
  function goStation(tr) {
    var sta = tr.getAttribute('data-sta');
    switchTab('monitor');
    var entry = null;
    Object.keys(stationMarkers).forEach(function(k) { if (k.split('.')[1] === sta) entry = stationMarkers[k]; });
    if (entry) map.flyTo([entry.data.latitude, entry.data.longitude], 12);
    openStationDetail(sta);
  }
  staBody.addEventListener('click', function(ev) { var tr = ev.target.closest('tr[data-sta]'); if (tr) goStation(tr); });
  staBody.addEventListener('keydown', function(ev) {
    var tr = ev.target.closest('tr[data-sta]'); if (!tr) return;
    if (ev.key === 'Enter') { goStation(tr); ev.preventDefault(); }
    else if (ev.key === 'ArrowDown' && tr.nextElementSibling) { tr.nextElementSibling.focus(); ev.preventDefault(); }
    else if (ev.key === 'ArrowUp' && tr.previousElementSibling) { tr.previousElementSibling.focus(); ev.preventDefault(); }
  });

  // Queue rows → review mode
  document.getElementById('ov-queue-list').addEventListener('click', function(ev) {
    var b = ev.target.closest('.ov-row[data-eid]');
    if (b) openReviewMode(b.getAttribute('data-eid'));
  });
  document.getElementById('ov-review-next').addEventListener('click', function() { advanceToNextUnreviewed(); });
  document.getElementById('ov-open-catalog').addEventListener('click', function() { switchTab('catalog'); });
  document.getElementById('ov-open-pipeline').addEventListener('click', function() { switchTab('pipeline'); });

  // Re-draw the sparkline when the panel resizes
  if (typeof ResizeObserver !== 'undefined') {
    var rateEl = document.getElementById('ov-rate');
    var lastW = 0;
    new ResizeObserver(function(entries) {
      var w = entries[0].contentRect.width;
      if (w && Math.abs(w - lastW) > 4) { lastW = w; if (activeTab === 'overview') _ovRate(); }
    }).observe(rateEl.parentElement);
  }
})();
