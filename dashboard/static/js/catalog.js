/* El Paso Seismic Monitor — catalog.js
 * Catalog tab: grid, filters, paging, keyboard; enhanced event detail overlay.
 *
 * Classic script sharing one global scope with its siblings; no build step.
 * Load order is fixed in index.html (definitions first, boot.js last):
 *   theme-chrome.js → map.js → util.js → monitor.js → pipeline-panels.js → filters-polling.js → tabs.js → helicorder.js → catalog.js → waveforms.js → review.js → waveviewer.js → pipeline-control.js → boot.js
 */

// ── Catalog Table ───────────────────────────────────────────────
var catPage = 1;
var catPerPage = 50;
try { catPerPage = parseInt(localStorage.getItem('cat.perPage'), 10) || 50; } catch (e) {}
if ([50, 100, 200].indexOf(catPerPage) === -1) catPerPage = 50;
var catSortBy = 'time';
var catSortOrder = 'desc';
var catMinMag = null;
var catMaxDepth = null;
var catMinPicks = null;
var catRows = [];        // events on the current page (row lookup by id)
var _catReqSeq = 0;      // drop stale responses when loads overlap

// num: right-aligned tabular figures. cls: extra td class.
var CAT_COLUMNS = [
  { key: 'reviewed',       label: 'Status',   sortable: false },
  { key: 'event_type',     label: 'Type',     sortable: false },
  { key: 'event_id',       label: 'ID',       sortable: false, cls: 'mono' },
  { key: 'time',           label: 'Time',     sortable: true,  cls: 'mono' },
  { key: 'magnitude',      label: 'Mag',      sortable: true,  num: true },
  { key: 'magnitude_type', label: 'Scale',    sortable: false, cls: 'dim' },
  { key: 'depth_km',       label: 'Depth km', sortable: true,  num: true },
  { key: 'latitude',       label: 'Lat',      sortable: false, num: true },
  { key: 'longitude',      label: 'Lon',      sortable: false, num: true },
  { key: 'num_picks',      label: 'Picks',    sortable: true,  num: true },
  { key: 'sigma_time',     label: '\u03c3t (s)', sortable: false, num: true, nocase: true },
  { key: 'sigma_amp',      label: '\u03c3a',     sortable: false, num: true, nocase: true },
];

// Header is built once; sort state is reflected by toggling classes, and
// one delegated click handler on <thead> replaces per-<th> listeners.
var _catHeaderBuilt = false;
function renderCatalogHeader() {
  var thead = document.getElementById('cat-thead');
  if (!_catHeaderBuilt) {
    thead.innerHTML = '<tr>' + CAT_COLUMNS.map(function(col) {
      var cls = (col.num ? 'num' : '') + (col.sortable ? ' sortable' : '') + (col.nocase ? ' nocase' : '');
      var arrow = col.sortable ? '<span class="sort-arrow">\u25bc</span>' : '';
      return '<th class="' + cls.trim() + '" data-sort="' + (col.sortable ? col.key : '') + '"' +
        (col.sortable ? ' title="Sort by ' + esc(col.label) + '"' : '') + '>' + esc(col.label) + arrow + '</th>';
    }).join('') + '</tr>';
    thead.addEventListener('click', function(ev) {
      var th = ev.target.closest('th[data-sort]');
      if (!th) return;
      var sortKey = th.getAttribute('data-sort');
      if (!sortKey) return;
      if (catSortBy === sortKey) catSortOrder = catSortOrder === 'asc' ? 'desc' : 'asc';
      else { catSortBy = sortKey; catSortOrder = 'desc'; }
      catPage = 1;
      loadCatalogTable();
    });
    _catHeaderBuilt = true;
  }
  thead.querySelectorAll('th[data-sort]').forEach(function(th) {
    var on = th.getAttribute('data-sort') === catSortBy;
    th.classList.toggle('sorted', on);
    var a = th.querySelector('.sort-arrow');
    if (a) a.textContent = (on && catSortOrder === 'asc') ? '\u25b2' : '\u25bc';
  });
}

function loadCatalogTable() {
  updateReviewQueueCount();
  var params = ['page=' + catPage, 'per_page=' + catPerPage, 'sort_by=' + catSortBy, 'sort_order=' + catSortOrder];
  if (filterStartDate) params.push('start_date=' + encodeURIComponent(filterStartDate));
  if (filterEndDate) params.push('end_date=' + encodeURIComponent(filterEndDate));
  var searchVal = document.getElementById('cat-search').value.trim();
  if (searchVal) params.push('search=' + encodeURIComponent(searchVal));
  var reviewFilter = document.getElementById('cat-review-filter').value;
  if (reviewFilter) params.push('review_status=' + encodeURIComponent(reviewFilter));
  if (catMinMag != null) params.push('min_magnitude=' + catMinMag);
  if (catMaxDepth != null) params.push('max_depth=' + catMaxDepth);
  if (catMinPicks != null) params.push('min_picks=' + catMinPicks);

  var seq = ++_catReqSeq;
  fetchJSON('/api/catalog?' + params.join('&')).then(function(data) {
    if (seq !== _catReqSeq) return;   // a newer load already landed
    renderCatalogHeader();
    var tbody = document.getElementById('cat-tbody');
    catRows = data.events || [];
    if (catRows.length === 0) {
      tbody.innerHTML = '<tr><td colspan="' + CAT_COLUMNS.length + '" style="text-align:center;color:var(--muted);padding:2rem">No events found.</td></tr>';
    } else {
      tbody.innerHTML = catRows.map(_catRowHTML).join('');
    }

    var start = catRows.length ? (data.page - 1) * data.per_page + 1 : 0;
    var end = start ? start + catRows.length - 1 : 0;
    document.getElementById('cat-page-info').textContent =
      (start ? start + '\u2013' + end : '0') + ' of ' + data.total + ' \u00b7 page ' + data.page + '/' + data.total_pages;
    document.getElementById('cat-summary').textContent = data.total + ' event' + (data.total !== 1 ? 's' : '');
    document.getElementById('cat-prev').disabled = data.page <= 1;
    document.getElementById('cat-next').disabled = data.page >= data.total_pages;
  }).catch(function() {
    if (seq !== _catReqSeq) return;
    document.getElementById('cat-tbody').innerHTML = '<tr><td colspan="' + CAT_COLUMNS.length + '" style="text-align:center;color:var(--red);padding:2rem">Failed to load catalog.</td></tr>';
  });
}

function _num(v, digits) { return v != null ? v.toFixed(digits) : '\u2014'; }

function _catRowHTML(e) {
  var auto = eventStatusKey(e) === 'auto';
  return '<tr tabindex="0" data-eid="' + esc(e.event_id) + '"' + (auto ? ' class="is-auto"' : '') + '>' +
    '<td>' + statusPill(e) + triageChip(e) + detectorChip(e) + '</td>' +
    '<td>' + typePill(e.event_type) + '</td>' +
    '<td class="mono">' + esc(e.event_id) + '</td>' +
    '<td class="mono">' + fmtCatTime(e.time) + '</td>' +
    '<td class="num">' + _num(e.magnitude, 2) + '</td>' +
    '<td class="dim">' + esc(e.magnitude_type || '') + '</td>' +
    '<td class="num">' + _num(e.depth_km, 1) + '</td>' +
    '<td class="num">' + _num(e.latitude, 4) + '</td>' +
    '<td class="num">' + _num(e.longitude, 4) + '</td>' +
    '<td class="num">' + (e.num_picks || 0) + '</td>' +
    '<td class="num">' + _num(e.sigma_time, 3) + '</td>' +
    '<td class="num">' + _num(e.sigma_amp, 3) + '</td>' +
    '</tr>';
}

// Row actions. 'detail' = map + detail panel (the click default),
// 'review' = straight into review mode, 'waves' = waveform overlay.
function _catRowAction(eid, mode) {
  if (mode === 'review') { openReviewMode(eid); return; }
  if (mode === 'waves') { showEventWaveforms(eid); return; }
  var ev = null;
  for (var i = 0; i < catRows.length; i++) if (catRows[i].event_id === eid) { ev = catRows[i]; break; }
  switchTab('monitor');
  if (ev && ev.latitude != null && ev.longitude != null) map.flyTo([ev.latitude, ev.longitude], 13);
  showEventDetail(eid);
}

(function wireCatalogGrid() {
  var tbody = document.getElementById('cat-tbody');
  tbody.addEventListener('click', function(ev) {
    var tr = ev.target.closest('tr[data-eid]');
    if (tr) _catRowAction(tr.getAttribute('data-eid'), 'detail');
  });
  tbody.addEventListener('keydown', function(ev) {
    var tr = ev.target.closest('tr[data-eid]');
    if (!tr) return;
    var k = ev.key;
    var next = null;
    if (k === 'ArrowDown') next = tr.nextElementSibling;
    else if (k === 'ArrowUp') next = tr.previousElementSibling;
    else if (k === 'Home') next = tbody.firstElementChild;
    else if (k === 'End') next = tbody.lastElementChild;
    // stopPropagation matters: openReviewMode() shows the overlay synchronously,
    // and the document-level review shortcuts (R = relocate, W, …) would
    // otherwise see this same keydown with the overlay already visible.
    else if (k === 'Enter') { ev.preventDefault(); ev.stopPropagation(); _catRowAction(tr.getAttribute('data-eid'), 'detail'); return; }
    else if (k === 'r' || k === 'R') { ev.preventDefault(); ev.stopPropagation(); _catRowAction(tr.getAttribute('data-eid'), 'review'); return; }
    else if (k === 'w' || k === 'W') { ev.preventDefault(); ev.stopPropagation(); _catRowAction(tr.getAttribute('data-eid'), 'waves'); return; }
    else if (k === 'PageDown' && !document.getElementById('cat-next').disabled) { catPage++; loadCatalogTable(); ev.preventDefault(); return; }
    else if (k === 'PageUp' && catPage > 1) { catPage--; loadCatalogTable(); ev.preventDefault(); return; }
    if (next && next.hasAttribute('data-eid')) {
      next.focus();
      next.scrollIntoView({ block: 'nearest' });
      ev.preventDefault();
    }
  });

  // UTC / Local time display
  var seg = document.getElementById('cat-time-mode');
  if (seg) {
    seg.querySelectorAll('.seg-btn').forEach(function(b) {
      b.classList.toggle('active', b.getAttribute('data-tz') === catTimeMode);
    });
    seg.addEventListener('click', function(ev) {
      var b = ev.target.closest('.seg-btn');
      if (!b) return;
      catTimeMode = b.getAttribute('data-tz') === 'local' ? 'local' : 'utc';
      try { localStorage.setItem('cat.timeMode', catTimeMode); } catch (e) {}
      seg.querySelectorAll('.seg-btn').forEach(function(x) { x.classList.toggle('active', x === b); });
      // Re-render from the rows in hand; no refetch needed.
      if (catRows.length) document.getElementById('cat-tbody').innerHTML = catRows.map(_catRowHTML).join('');
    });
  }

  // Rows per page
  var pp = document.getElementById('cat-per-page');
  if (pp) {
    pp.value = String(catPerPage);
    pp.addEventListener('change', function() {
      catPerPage = parseInt(pp.value, 10) || 50;
      try { localStorage.setItem('cat.perPage', String(catPerPage)); } catch (e) {}
      catPage = 1;
      loadCatalogTable();
    });
  }
})();

document.getElementById('cat-prev').addEventListener('click', function() { if (catPage > 1) { catPage--; loadCatalogTable(); } });
document.getElementById('cat-next').addEventListener('click', function() { catPage++; loadCatalogTable(); });
var _catSearchTimer = null;
document.getElementById('cat-search').addEventListener('input', function() {
  clearTimeout(_catSearchTimer);
  _catSearchTimer = setTimeout(function() { catPage = 1; loadCatalogTable(); }, 300);
});
document.getElementById('cat-search').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') { clearTimeout(_catSearchTimer); catPage = 1; loadCatalogTable(); }
});
['cat-min-mag', 'cat-max-depth', 'cat-min-picks'].forEach(function(id) {
  document.getElementById(id).addEventListener('keydown', function(e) {
    if (e.key === 'Enter') document.getElementById('cat-filter-apply').click();
  });
});
document.getElementById('cat-review-next').addEventListener('click', function() {
  advanceToNextUnreviewed();
});

document.getElementById('cat-filter-apply').addEventListener('click', function() {
  var v = document.getElementById('cat-min-mag').value;
  catMinMag = v ? parseFloat(v) : null;
  v = document.getElementById('cat-max-depth').value;
  catMaxDepth = v ? parseFloat(v) : null;
  v = document.getElementById('cat-min-picks').value;
  catMinPicks = v ? parseInt(v, 10) : null;
  catPage = 1;
  loadCatalogTable();
});
document.getElementById('cat-filter-clear').addEventListener('click', function() {
  document.getElementById('cat-search').value = '';
  document.getElementById('cat-min-mag').value = '';
  document.getElementById('cat-max-depth').value = '';
  document.getElementById('cat-min-picks').value = '';
  document.getElementById('cat-review-filter').value = '';
  catMinMag = null; catMaxDepth = null; catMinPicks = null;
  catPage = 1;
  loadCatalogTable();
});
document.getElementById('cat-review-filter').addEventListener('change', function() {
  catPage = 1;
  loadCatalogTable();
});

// ── Enhanced Event Detail (with waveform button + extra fields) ──
var _origShowEventDetail = showEventDetail;
showEventDetail = function(eventId) {
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
      // Pulsating ring on selected event
      if (ev.latitude != null && ev.longitude != null) {
        showPulseMarker(ev.latitude, ev.longitude);
      }
      title.textContent = esc(ev.event_id);
      var html = '';
      html += '<div class="detail-row"><span class="detail-label">Status</span><span class="detail-value">' + statusPill(ev) + ' &nbsp;' + typePill(ev.event_type) + '</span></div>';
      html += '<div class="detail-row"><span class="detail-label">Time</span><span class="detail-value">' + esc(ev.time) + '<div class="local-time">' + esc(formatLocalTime(ev.time)) + '</div></span></div>';
      if (ev.legacy_id) html += '<div class="detail-row"><span class="detail-label">Former ID</span><span class="detail-value mono">' + esc(ev.legacy_id) + '</span></div>';
      html += '<div class="detail-row"><span class="detail-label">Magnitude</span><span class="detail-value">' + (ev.magnitude != null ? ev.magnitude.toFixed(2) : '-') + (ev.magnitude_type ? ' (' + esc(ev.magnitude_type) + ')' : '') + '</span></div>';
      if (ev.ml_err != null) {
        html += '<div class="detail-row"><span class="detail-label">ML Error</span><span class="detail-value">\u00b1' + ev.ml_err.toFixed(3) + '</span></div>';
      }
      html += '<div class="detail-row"><span class="detail-label">Depth</span><span class="detail-value">' + (ev.depth_km != null ? ev.depth_km.toFixed(1) + ' km' : '-') + '</span></div>';
      html += '<div class="detail-row"><span class="detail-label">Location</span><span class="detail-value">' + (ev.latitude != null ? ev.latitude.toFixed(4) + ', ' + ev.longitude.toFixed(4) : '-') + '</span></div>';
      html += '<div class="detail-row"><span class="detail-label">Picks</span><span class="detail-value">' + (ev.num_picks || 0) + '</span></div>';
      if (ev.sigma_time != null) {
        html += '<div class="detail-row"><span class="detail-label">\u03c3 Time</span><span class="detail-value">' + ev.sigma_time.toFixed(3) + ' s</span></div>';
      }
      if (ev.sigma_amp != null) {
        html += '<div class="detail-row"><span class="detail-label">\u03c3 Amp</span><span class="detail-value">' + ev.sigma_amp.toFixed(3) + '</span></div>';
      }
      if (ev.num_ml_sta) {
        html += '<div class="detail-row"><span class="detail-label">ML Stations</span><span class="detail-value">' + ev.num_ml_sta + '</span></div>';
      }

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

      // Reviewed status
      if (ev.reviewed) {
        html += '<div class="detail-row"><span class="detail-label">Reviewed</span><span class="detail-value reviewed-check">\u2713 ' + esc(ev.reviewed) + '</span></div>';
      }

      // Waveform + Review buttons
      html += '<div style="margin-top:.5rem;display:flex;gap:.3rem">';
      html += '<button class="btn-sm btn-primary" id="btn-view-waveforms" style="flex:1">View Waveforms</button>';
      html += '<button class="btn-sm" id="btn-review-event" style="flex:1;background:var(--green);border-color:var(--green);color:#fff">Review</button>';
      html += '</div>';

      body.innerHTML = html;

      document.getElementById('btn-view-waveforms').addEventListener('click', function() {
        showEventWaveforms(ev.event_id);
      });
      document.getElementById('btn-review-event').addEventListener('click', function() {
        openReviewMode(ev.event_id);
      });
    })
    .catch(function() {
      body.innerHTML = '<div class="empty-state">Failed to load event details.</div>';
    });
};
