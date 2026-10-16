/* El Paso Seismic Monitor — filters-polling.js
 * Date filter bar, export, query-string builders, WebSocket + polling functions (no boot here).
 *
 * Classic script sharing one global scope with its siblings; no build step.
 * Load order is fixed in index.html (definitions first, boot.js last):
 *   theme-chrome.js → map.js → util.js → monitor.js → pipeline-panels.js → filters-polling.js → tabs.js → helicorder.js → catalog.js → waveforms.js → review.js → waveviewer.js → pipeline-control.js → boot.js
 */

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
  // per_page=0 = every event in the date range (B8): the map and the
  // notification list must never drop events once the catalog passes 500.
  var params = ['per_page=0'];
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
    lastStationHealth = results[0];
    renderStationHealth(results[0]);
    renderCompleteness(results[1]);
    onApiSuccess();
  } catch(e) { onApiFailure(e); }
  hideLoading('loading-completeness');
}
