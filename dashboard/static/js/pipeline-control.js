/* El Paso Seismic Monitor — pipeline-control.js
 * Start / stop / backfill controls.
 *
 * Classic script sharing one global scope with its siblings; no build step.
 * Load order is fixed in index.html (definitions first, boot.js last):
 *   theme-chrome.js → map.js → util.js → monitor.js → pipeline-panels.js → filters-polling.js → tabs.js → helicorder.js → catalog.js → waveforms.js → review.js → waveviewer.js → pipeline-control.js → boot.js
 */

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
  document.getElementById('export-menu').classList.remove('open');
});

// Export dropdown (filter bar). The CSV / QuakeML items keep their IDs, so
// the File menu actions and the existing click handlers are unchanged.
document.getElementById('btn-export').addEventListener('click', function(e) {
  e.stopPropagation();
  document.getElementById('start-menu').classList.remove('open');
  document.getElementById('export-menu').classList.toggle('open');
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
