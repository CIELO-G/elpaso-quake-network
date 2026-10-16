/* El Paso Seismic Monitor — waveviewer.js
 * Waveviewer tab (arbitrary time window across all stations).
 *
 * Classic script sharing one global scope with its siblings; no build step.
 * Load order is fixed in index.html (definitions first, boot.js last):
 *   theme-chrome.js → map.js → util.js → monitor.js → pipeline-panels.js → filters-polling.js → tabs.js → helicorder.js → catalog.js → waveforms.js → review.js → waveviewer.js → pipeline-control.js → boot.js
 */

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
