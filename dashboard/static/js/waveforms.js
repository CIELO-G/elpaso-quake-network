/* El Paso Seismic Monitor — waveforms.js
 * Event waveform overlay + shared canvas renderers (_plotGeom, _drawYAxis, _drawTimeBand).
 *
 * Classic script sharing one global scope with its siblings; no build step.
 * Load order is fixed in index.html (definitions first, boot.js last):
 *   theme-chrome.js → map.js → util.js → monitor.js → pipeline-panels.js → filters-polling.js → tabs.js → helicorder.js → catalog.js → waveforms.js → review.js → waveviewer.js → pipeline-control.js → boot.js
 */

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
    mono:     v('--font-mono', 'ui-monospace, Menlo, monospace'),
  };
}

// Plot geometry shared by every waveform canvas. A band at the bottom is
// reserved for the time axis so amplitude labels on the right edge can
// never collide with time labels, whatever the canvas height.
var AXIS_BAND_H = 16;
function _plotGeom(H) {
  var top = 2, bottom = H - AXIS_BAND_H;
  return { top: top, bottom: bottom, mid: (top + bottom) / 2, amp: ((bottom - top) / 2) * 0.9 };
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
  ctx.font = '9px ' + c.mono;
  ctx.textAlign = 'right';
  ctx.lineWidth = 0.5;

  // Ticks: +max, +half, 0, -half, -max — overlaid on the right edge, all
  // inside the plot area (the time band below is reserved by _plotGeom).
  // The unit rides on the top tick only, so it can't collide with anything.
  var ticks = [maxAbs, maxAbs * 0.5, 0, -maxAbs * 0.5, -maxAbs];
  ticks.forEach(function(val, i) {
    var y = mid - (val / maxAbs) * amp;
    ctx.strokeStyle = c.grid;
    ctx.setLineDash([2, 3]);
    ctx.beginPath();
    ctx.moveTo(W, y);
    ctx.lineTo(W - 6, y);
    ctx.stroke();
    ctx.setLineDash([]);
    var label = _formatSI(val) + (i === 0 ? ' m/s' : '');
    var tw = ctx.measureText(label).width;
    ctx.fillStyle = c.bg;
    ctx.globalAlpha = 0.75;
    ctx.fillRect(W - tw - 10, y - 5, tw + 5, 10);
    ctx.globalAlpha = 1.0;
    ctx.fillStyle = c.axisText;
    ctx.fillText(label, W - 7, y + 3);
  });
  ctx.textAlign = 'left';
}

// Time axis in the reserved bottom band: nice-interval ticks relative to
// refT, faint gridlines through the plot, labels centred on the tick and
// kept inside the canvas at both edges.
function _drawTimeBand(ctx, c, tStart, dur, refT, W, H, geom) {
  var interval = _niceTickInterval(dur, 8);
  var rel0 = tStart - refT;                       // offset of the left edge
  var first = Math.ceil(rel0 / interval - 1e-6) * interval;
  ctx.font = '10px ' + c.mono;
  ctx.fillStyle = c.axisText;
  ctx.strokeStyle = c.grid;
  ctx.lineWidth = 0.5;
  // band separator
  ctx.beginPath();
  ctx.moveTo(0, geom.bottom + 0.5);
  ctx.lineTo(W, geom.bottom + 0.5);
  ctx.stroke();
  for (var rel = first; rel <= rel0 + dur + 1e-9; rel += interval) {
    var x = ((rel - rel0) / dur) * W;
    ctx.setLineDash([1, 3]);
    ctx.beginPath();
    ctx.moveTo(x, geom.top);
    ctx.lineTo(x, geom.bottom);
    ctx.stroke();
    ctx.setLineDash([]);
    var label = _formatTimeOffset(rel, interval);
    var tw = ctx.measureText(label).width;
    var lx = x - tw / 2;
    if (lx < 2) lx = 2;
    if (lx + tw > W - 2) lx = W - 2 - tw;
    ctx.fillText(label, lx, H - 4);
  }
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
  var geom = _plotGeom(H);
  var mid = geom.mid;
  var amp = geom.amp;

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
        ctx.lineTo(plotLeft + otFrac * plotW, geom.bottom);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = c.ot;
        ctx.font = 'bold 12px ' + c.mono;
        ctx.fillText('OT', plotLeft + otFrac * plotW + 3, geom.bottom - 4);
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
        ctx.lineTo(px, geom.bottom);
        ctx.stroke();
        ctx.setLineDash([]);

        // Label
        ctx.fillStyle = color;
        ctx.font = 'bold 12px ' + c.mono;
        var label = pick.phase + (pick.probability != null ? ' ' + (pick.probability * 100).toFixed(0) + '%' : '');
        ctx.fillText(label, px + 3, 14);
      });
    }

    // Time axis (relative to origin time, or to trace start without one)
    var refT = eventTime ? new Date(eventTime.replace('T', ' ').replace('Z', '')).getTime() / 1000 : tStart;
    _drawTimeBand(ctx, c, tStart, dur, refT, W, H, geom);
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
