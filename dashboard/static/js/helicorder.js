/* El Paso Seismic Monitor — helicorder.js
 * Helicorder tab.
 *
 * Classic script sharing one global scope with its siblings; no build step.
 * Load order is fixed in index.html (definitions first, boot.js last):
 *   theme-chrome.js → map.js → util.js → monitor.js → pipeline-panels.js → filters-polling.js → tabs.js → helicorder.js → catalog.js → waveforms.js → review.js → waveviewer.js → pipeline-control.js → boot.js
 */

// ── Helicorder tab ──────────────────────────────────────────────
var _heliData = null;
var _heliEvents = [];

function initHelicorderTab() {
  fetchJSON('/api/stations').then(function(sts) {
    var sel = document.getElementById('heli-station');
    var opt = function(s) {
      return '<option value="' + esc(s.station) + '">' + esc(s.station) +
        ' (' + esc(s.network) + ')</option>';
    };
    var live = sts.filter(function(s) { return !s.end_date; });
    var retired = sts.filter(function(s) { return !!s.end_date; });
    sel.innerHTML = live.map(opt).join('') +
      (retired.length ? '<optgroup label="Retired">' + retired.map(opt).join('') + '</optgroup>' : '');
    // Default to the first active broadband station (quietest helicorder),
    // else the first active station — nothing hard-coded (B7).
    var active = sts.filter(function(s) { return !s.end_date; });
    var pref = active.filter(function(s) { return s.channels === 'HH?'; })[0] || active[0];
    if (pref) sel.value = pref.station;
  }).catch(function() {});
  // Latest complete day: a day is processed 26 h after it starts + 2 h buffer.
  var d = new Date(Date.now() - 26 * 3600000);
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
    fetchJSON('/api/catalog?per_page=0&start_date=' + encodeURIComponent(date) +
      '&end_date=' + encodeURIComponent(date)).catch(function() { return { events: [] }; }),
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
