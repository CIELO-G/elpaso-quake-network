/* El Paso Seismic Monitor — review.js
 * Review mode: traces, picking, undo/redo, relocation, review map, queue.
 *
 * Classic script sharing one global scope with its siblings; no build step.
 * Load order is fixed in index.html (definitions first, boot.js last):
 *   theme-chrome.js → map.js → util.js → monitor.js → pipeline-panels.js → filters-polling.js → tabs.js → helicorder.js → catalog.js → waveforms.js → review.js → waveviewer.js → pipeline-control.js → boot.js
 */

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

  // Vertical guide line (stops at the time band)
  ctx.strokeStyle = '#3b82f6';
  ctx.lineWidth = 1;
  ctx.setLineDash([2, 3]);
  ctx.beginPath();
  ctx.moveTo(cx, 0);
  ctx.lineTo(cx, H - AXIS_BAND_H);
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

function reviewStatusBadge(ev) {
  return ' ' + statusPill(ev || {});
}

function updateReviewTitleBadge() {
  // Fetch event detail to get review_status for badge
  fetchJSON('/api/event/' + encodeURIComponent(reviewState.eventId)).then(function(data) {
    if (data && data.event) {
      document.getElementById('rv-title').innerHTML = 'Review \u2014 ' + esc(reviewState.eventId) + reviewStatusBadge(data.event);
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
  var geom = _plotGeom(H);
  var mid = geom.mid;
  var amp = geom.amp;

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
      ctx.moveTo(ox, 0); ctx.lineTo(ox, geom.bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = c.ot;
      ctx.font = 'bold 13px ' + c.mono;
      ctx.fillText('OT', ox + 3, geom.bottom - 4);
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
    ctx.moveTo(px, 0); ctx.lineTo(px, geom.bottom);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = color;
    ctx.font = 'bold 14px ' + c.mono;
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
        ctx.moveTo(px, 0); ctx.lineTo(px, geom.bottom);
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = color;
        ctx.font = '11px ' + c.mono;
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
  // Ticks are placed on offsets from origin time (so "+0.0" is exactly OT),
  // not on absolute epoch seconds.
  var firstMajor = eventOriginT + Math.ceil((viewStart - eventOriginT) / interval - 1e-6) * interval;
  var firstMinor = eventOriginT + Math.ceil((viewStart - eventOriginT) / minorInterval - 1e-6) * minorInterval;
  ctx.fillStyle = c.axisText;
  ctx.strokeStyle = c.grid;
  ctx.font = '10px ' + c.mono;
  ctx.textAlign = 'left';
  ctx.lineWidth = 0.5;

  // Band separator
  ctx.beginPath();
  ctx.moveTo(0, geom.bottom + 0.5); ctx.lineTo(W, geom.bottom + 0.5);
  ctx.stroke();

  // Minor ticks first (so majors overlay cleanly where they coincide).
  ctx.globalAlpha = 0.55;
  for (var minor = firstMinor; minor <= viewEnd; minor += minorInterval) {
    var xm = tx(minor);
    ctx.beginPath();
    ctx.moveTo(xm, geom.bottom);
    ctx.lineTo(xm, geom.bottom + 4);
    ctx.stroke();
  }
  ctx.globalAlpha = 1.0;

  // Major ticks: dashed through the plot + centred label in the band
  for (var tick = firstMajor; tick <= viewEnd; tick += interval) {
    var xT = tx(tick);
    ctx.setLineDash([1, 3]);
    ctx.beginPath();
    ctx.moveTo(xT, geom.top); ctx.lineTo(xT, geom.bottom);
    ctx.stroke();
    ctx.setLineDash([]);
    var label = _formatTimeOffset(tick - eventOriginT, interval);
    var tw = ctx.measureText(label).width;
    var lx = xT - tw / 2;
    if (lx < 2) lx = 2;
    if (lx + tw > W - 2) lx = W - 2 - tw;
    ctx.fillText(label, lx, H - 4);
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
        ctx.strokeStyle = _isLightTheme() ? '#334155' : '#ffffff';
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
  _reviewTileLayer = makeBasemapLayer(_currentAppTheme).addTo(reviewMap);
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

// Triage chip: shown on unreviewed rows only (reviewed rows carry a human
// decision that supersedes the automatic label). Hover for the reason.
function triageChip(e) {
  // auto_reject is carried by the status pill (statusPill → "Auto-rejected");
  // only a soft flag still needs its own chip.
  if (e.review_status || !e.triage || e.triage === 'ok' || e.triage === 'auto_reject') return '';
  return '<span class="triage-chip triage-flag" title="' + esc(e.triage_reason || '') + '">flag</span>';
}

// Detector chip: the event was found by the second detector only (PhaseNO),
// which PhaseNet's picks corroborate. Shown on every row, reviewed or not.
function detectorChip(e) {
  if (!e.detector) return '';
  var why = 'Found by ' + e.detector + ' only';
  if (e.corroborating_picks != null) why += '; ' + e.corroborating_picks + ' of its picks match PhaseNet picks';
  return '<span class="triage-chip detector-chip" title="' + esc(why) + '">' + esc(e.detector) + '</span>';
}

// ── Review queue ────────────────────────────────────────────────
// Count of unreviewed events; drives the "Review next" button in the
// catalog toolbar and the counter chip in the review header.
function updateReviewQueueCount() {
  fetchJSON('/api/catalog?review_status=queue&per_page=1')
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
  fetchJSON('/api/catalog?review_status=queue&per_page=1&sort_by=time&sort_order=asc')
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
