/* El Paso Seismic Monitor — pipeline-panels.js
 * Pipeline tab panels: hero stats, progress, throughput, completeness calendar, disk, errors.
 *
 * Classic script sharing one global scope with its siblings; no build step.
 * Load order is fixed in index.html (definitions first, boot.js last):
 *   theme-chrome.js → map.js → util.js → monitor.js → pipeline-panels.js → filters-polling.js → tabs.js → helicorder.js → catalog.js → waveforms.js → review.js → waveviewer.js → pipeline-control.js → boot.js
 */

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
  // PhaseNO is optional: only drawn when it has run.
  var order = ['ingest', 'process', 'detect', 'associate', 'phaseno', 'catalog'].filter(function(n) {
    return n !== 'phaseno' || stepAvg[n];
  });
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
// Drawn on a <canvas>, not SVG: 60+ stations × 400+ days is ~25k cells,
// which as DOM nodes costs tens of ms per render and bloats the page.
// Retired / campaign stations collapse into one aggregate row (per-day max
// across them) unless the "Retired" toggle is on, so the nodal campaign's
// 49 one-week rows don't swamp the live network.
var lastCompletenessData = null;
var lastStationHealth = null;     // from /api/station_health; drives grouping
var cmpShowRetired = false;
var _cmpLayout = null;            // hit-test data for the hover tooltip
var _cmpTip = null;

function _cmpRows(data) {
  var retired = {};
  (lastStationHealth || []).forEach(function(h) { if (h.status === 'retired') retired[h.station] = true; });
  var rows = [], retiredIdx = [];
  data.stations.forEach(function(sta, si) {
    if (retired[sta]) retiredIdx.push(si);
    else rows.push({ label: sta, values: data.matrix[si], kind: 'active' });
  });
  if (retiredIdx.length) {
    if (cmpShowRetired) {
      retiredIdx.forEach(function(si) { rows.push({ label: data.stations[si], values: data.matrix[si], kind: 'retired' }); });
    } else {
      var agg = new Array(data.days.length).fill(0);
      retiredIdx.forEach(function(si) {
        var r = data.matrix[si];
        for (var d = 0; d < r.length; d++) if (r[d] > agg[d]) agg[d] = r[d];
      });
      rows.push({ label: 'retired (' + retiredIdx.length + ')', values: agg, kind: 'group', n: retiredIdx.length });
    }
  }
  return { rows: rows, nRetired: retiredIdx.length };
}

function renderCompleteness(data) {
  if (data) lastCompletenessData = data;
  else data = lastCompletenessData;
  if (!data) return;
  var stations = data.stations, days = data.days, matrix = data.matrix;
  var el = document.getElementById('completeness-calendar');
  var rangeEl = document.getElementById('completeness-range');
  var maxEl = document.getElementById('completeness-max');
  var toggle = document.getElementById('cmp-toggle-retired');

  if (!days || days.length === 0 || !stations || stations.length === 0) {
    el.innerHTML = '<div class="empty-state">No raw data yet.</div>';
    rangeEl.textContent = '';
    maxEl.textContent = 'max';
    if (toggle) toggle.hidden = true;
    _cmpLayout = null;
    return;
  }

  var globalMax = 1;
  for (var si = 0; si < matrix.length; si++) {
    var r0 = matrix[si];
    for (var di = 0; di < r0.length; di++) if (r0[di] > globalMax) globalMax = r0[di];
  }
  maxEl.textContent = globalMax;
  rangeEl.textContent = days.length + ' days';

  var grouped = _cmpRows(data);
  var rows = grouped.rows;
  if (toggle) {
    toggle.hidden = grouped.nRetired === 0;
    toggle.textContent = (cmpShowRetired ? 'Hide' : 'Show') + ' retired (' + grouped.nRetired + ')';
    toggle.classList.toggle('active', cmpShowRetired);
  }

  var canvas = el.querySelector('canvas');
  if (!canvas) {
    el.innerHTML = '';
    canvas = document.createElement('canvas');
    el.appendChild(canvas);
    canvas.addEventListener('mousemove', _cmpHover);
    canvas.addEventListener('mouseleave', _cmpHideTip);
  }

  var ROW_H = 16, PAD_BOTTOM = 18;
  var W = el.clientWidth || 600;
  var H = ROW_H * rows.length + PAD_BOTTOM;
  var dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(W * dpr);
  canvas.height = Math.round(H * dpr);
  canvas.style.height = H + 'px';
  var ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  var css = getComputedStyle(document.documentElement);
  var muted = css.getPropertyValue('--muted').trim() || '#64748b';
  var text = css.getPropertyValue('--text').trim() || '#0f172a';
  var border = css.getPropertyValue('--border').trim() || '#cbd5e1';
  var mono = css.getPropertyValue('--font-mono').trim() || 'ui-monospace, Menlo, monospace';

  // Label gutter fits the longest label (the aggregate row is the widest).
  ctx.font = 'italic 10px ' + mono;
  var LABEL_W = 64;
  rows.forEach(function(r) { LABEL_W = Math.max(LABEL_W, Math.ceil(ctx.measureText(r.label).width) + 12); });
  var chartW = W - LABEL_W;
  var cellW = chartW / days.length;

  // Pre-compute one fill per count so the hot loop does no string work.
  var palette = new Array(globalMax + 1);
  for (var v = 1; v <= globalMax; v++) palette[v] = completenessColor(v, globalMax);

  var cw = Math.max(cellW - 0.5, 0.5);
  ctx.font = '10px ' + mono;
  ctx.textAlign = 'right';
  ctx.textBaseline = 'middle';
  rows.forEach(function(row, ri) {
    var y = ri * ROW_H;
    // row guide
    ctx.fillStyle = border;
    ctx.globalAlpha = 0.35;
    ctx.fillRect(LABEL_W, y + ROW_H - 1, chartW, 0.5);
    ctx.globalAlpha = 1;
    ctx.fillStyle = row.kind === 'active' ? text : muted;
    if (row.kind === 'group') ctx.font = 'italic 10px ' + mono; else ctx.font = '10px ' + mono;
    ctx.fillText(row.label, LABEL_W - 6, y + ROW_H / 2);
    var vals = row.values;
    for (var d = 0; d < vals.length; d++) {
      var n = vals[d];
      if (n <= 0) continue;
      ctx.fillStyle = palette[Math.min(n, globalMax)];
      ctx.fillRect(LABEL_W + d * cellW, y + 1.5, cw, ROW_H - 3);
    }
  });

  // Month ticks
  var baseY = ROW_H * rows.length;
  ctx.font = '9px ' + mono;
  ctx.fillStyle = muted;
  ctx.textAlign = 'center';
  ctx.textBaseline = 'alphabetic';
  var pm = days[0].slice(0, 7);
  var ticks = [{ i: 0, label: days[0].slice(5) }];
  for (var k = 1; k < days.length; k++) {
    var mo = days[k].slice(0, 7);
    if (mo !== pm) { ticks.push({ i: k, label: days[k].slice(5) }); pm = mo; }
  }
  if (days.length > 1) ticks.push({ i: days.length - 1, label: days[days.length - 1].slice(5) });
  var lastX = -1e9;
  ticks.forEach(function(item) {
    var x = LABEL_W + item.i * cellW + cellW / 2;
    if (x - lastX < 34) return;          // don't let labels overprint
    lastX = x;
    ctx.fillText(item.label, x, baseY + PAD_BOTTOM - 4);
  });
  ctx.textAlign = 'left';

  _cmpLayout = { rows: rows, days: days, LABEL_W: LABEL_W, ROW_H: ROW_H, cellW: cellW };
}

function _cmpHover(ev) {
  if (!_cmpLayout) return;
  var L = _cmpLayout;
  var rect = ev.currentTarget.getBoundingClientRect();
  var x = ev.clientX - rect.left, y = ev.clientY - rect.top;
  var ri = Math.floor(y / L.ROW_H), di = Math.floor((x - L.LABEL_W) / L.cellW);
  if (ri < 0 || ri >= L.rows.length || di < 0 || di >= L.days.length) { _cmpHideTip(); return; }
  var row = L.rows[ri];
  var n = row.values[di];
  if (!_cmpTip) {
    _cmpTip = document.createElement('div');
    _cmpTip.className = 'cmp-tip';
    document.body.appendChild(_cmpTip);
  }
  _cmpTip.textContent = row.label + ' \u00b7 ' + L.days[di] + ' \u00b7 ' +
    (row.kind === 'group' ? 'max ' : '') + n + ' file' + (n === 1 ? '' : 's');
  _cmpTip.style.display = 'block';
  var tw = _cmpTip.offsetWidth, th = _cmpTip.offsetHeight;
  var left = ev.clientX + 12, top = ev.clientY - th - 8;
  if (left + tw > window.innerWidth - 8) left = ev.clientX - tw - 12;
  if (top < 8) top = ev.clientY + 14;
  _cmpTip.style.left = left + 'px';
  _cmpTip.style.top = top + 'px';
}

function _cmpHideTip() {
  if (_cmpTip) _cmpTip.style.display = 'none';
}

(function wireCompletenessToggle() {
  var t = document.getElementById('cmp-toggle-retired');
  if (!t) return;
  t.addEventListener('click', function() {
    cmpShowRetired = !cmpShowRetired;
    renderCompleteness();
  });
})();

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
