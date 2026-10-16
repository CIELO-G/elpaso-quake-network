/* El Paso Seismic Monitor — util.js
 * Formatting helpers, fetchJSON, status/type vocabulary (statusPill, typePill).
 *
 * Classic script sharing one global scope with its siblings; no build step.
 * Load order is fixed in index.html (definitions first, boot.js last):
 *   theme-chrome.js → map.js → util.js → monitor.js → pipeline-panels.js → filters-polling.js → tabs.js → helicorder.js → catalog.js → waveforms.js → review.js → waveviewer.js → pipeline-control.js → boot.js
 */

// ── Helpers ─────────────────────────────────────────────────────
function formatTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

// Format an ISO UTC timestamp in the browser's local timezone, including
// the timezone abbreviation (e.g. "Oct 22, 2025, 12:43:14 PM MDT").
// Each user sees their own local time — Marc in El Paso sees MDT, a
// collaborator in California would see PDT, etc.
function formatLocalTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'long' });
  } catch (e) {
    return '';
  }
}

function fmtDur(sec) {
  if (sec == null) return '\u2014';
  sec = Math.round(sec);
  if (sec < 60) return sec + 's';
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return m + 'm ' + String(s).padStart(2, '0') + 's';
  const h = Math.floor(m / 60), rm = m % 60;
  return h + 'h ' + String(rm).padStart(2, '0') + 'm';
}

var _ESC_RE = /[&<>"']/g;
var _ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
function esc(s) {
  if (s == null) return '';
  return String(s).replace(_ESC_RE, function(ch) { return _ESC_MAP[ch]; });
}

function showLoading(id) {
  const el = document.getElementById(id);
  if (el) el.classList.add('active');
}

function hideLoading(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove('active');
}

function showErrorBanner(msg) {
  const banner = document.getElementById('error-banner');
  document.getElementById('error-banner-text').textContent = msg;
  banner.classList.add('visible');
}

function hideErrorBanner() {
  document.getElementById('error-banner').classList.remove('visible');
}

document.getElementById('error-banner-dismiss').addEventListener('click', hideErrorBanner);

// ── API fetch with error handling ───────────────────────────────
async function fetchJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const ct = r.headers.get('content-type') || '';
  if (r.status === 304) return null;
  return r.json();
}

function onApiSuccess() {
  apiFailCount = 0;
  hideErrorBanner();
}

function onApiFailure(e) {
  apiFailCount++;
  if (apiFailCount >= 3) {
    showErrorBanner('Cannot reach API server. Retrying...');
    _sbConn('offline');
  }
}


// ── Status semantics ────────────────────────────────────────────
// One vocabulary for "where is this event in the review workflow":
//   confirmed · rejected · queue (needs a human) · auto (triage dismissed it)
// Colours live in styles.css (--st-*). Every surface calls these helpers
// so the catalog, the detail overlay and the review header cannot drift.
var STATUS_LABEL = { confirmed: 'Confirmed', rejected: 'Rejected', queue: 'Unreviewed', auto: 'Auto-rejected' };

function eventStatusKey(e) {
  if (e.review_status === 'confirmed') return 'confirmed';
  if (e.review_status === 'rejected') return 'rejected';
  return _isAutoRejected(e) ? 'auto' : 'queue';
}

function statusPill(e) {
  var k = eventStatusKey(e);
  var title = (k === 'auto' && e.triage_reason) ? ' title="' + esc(e.triage_reason) + '"' : '';
  return '<span class="pill pill-' + k + '"' + title + '>' + STATUS_LABEL[k] + '</span>';
}

var TYPE_PILL = {
  earthquake:   ['eq', 'EQ', 'Earthquake'],
  quarry_blast: ['qb', 'QB', 'Quarry blast'],
  noise:        ['noise', 'Noise', 'Noise'],
};

function typePill(t) {
  var d = TYPE_PILL[t];
  if (!d) return '<span class="pill pill-type pill-type-none" title="Type not set">\u2014</span>';
  return '<span class="pill pill-type pill-type-' + d[0] + '" title="' + d[2] + '">' + d[1] + '</span>';
}

// Catalog time display: UTC is canonical; local matters for blasts (they
// keep office hours). Persisted per browser.
var catTimeMode = 'utc';
try { catTimeMode = localStorage.getItem('cat.timeMode') === 'local' ? 'local' : 'utc'; } catch (e) {}

var _localFmt = null;
function fmtLocalCompact(iso) {
  // "2026-10-08 20:51:09 MDT" — same shape as the UTC column so the eye
  // doesn't have to re-parse when toggling.
  if (!iso) return '';
  var d = new Date(iso);
  if (isNaN(d.getTime())) return '';
  if (!_localFmt) {
    _localFmt = new Intl.DateTimeFormat(undefined, {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
      hourCycle: 'h23', timeZoneName: 'short',
    });
  }
  var p = {};
  _localFmt.formatToParts(d).forEach(function(x) { p[x.type] = x.value; });
  return p.year + '-' + p.month + '-' + p.day + ' ' + p.hour + ':' + p.minute + ':' + p.second +
    ' <span class="cat-time-local">' + esc(p.timeZoneName || '') + '</span>';
}

function fmtCatTime(iso) {
  if (!iso) return '';
  if (catTimeMode === 'local') return fmtLocalCompact(iso);
  // "2026-10-09T02:51:09.299" → "2026-10-09 02:51:09"
  return esc(iso.slice(0, 19).replace('T', ' '));
}
