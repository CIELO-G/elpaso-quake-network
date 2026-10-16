/* El Paso Seismic Monitor — tabs.js
 * Map zoom control, completeness resize observer, tab navigation, stat-card links, error modal, QuakeML export.
 *
 * Classic script sharing one global scope with its siblings; no build step.
 * Load order is fixed in index.html (definitions first, boot.js last):
 *   theme-chrome.js → map.js → util.js → monitor.js → pipeline-panels.js → filters-polling.js → tabs.js → helicorder.js → catalog.js → waveforms.js → review.js → waveviewer.js → pipeline-control.js → boot.js
 */

// ── Custom zoom control (top-right, replaces Leaflet's default) ─────
(function wireMapZoomControl() {
  var slider = document.getElementById('mz-slider');
  var btnIn = document.getElementById('mz-in');
  var btnOut = document.getElementById('mz-out');
  var btnFit = document.getElementById('mz-fit');
  if (!slider || !btnIn || !btnOut || !btnFit) return;

  // Initialize slider to current zoom
  slider.value = map.getZoom();

  // Slider → map (smooth: setZoom respects zoomAnimation + fractional via zoomSnap=0)
  slider.addEventListener('input', function() {
    var z = parseFloat(slider.value);
    if (!isNaN(z)) map.setZoom(z);
  });

  // Buttons → map (zoomIn/zoomOut step by zoomDelta = 0.5)
  btnIn.addEventListener('click', function() { map.zoomIn(); });
  btnOut.addEventListener('click', function() { map.zoomOut(); });

  // Reset → initial view (smooth pan + zoom)
  btnFit.addEventListener('click', function() {
    map.flyTo(MAP_INIT_VIEW, MAP_INIT_ZOOM, { duration: 0.6 });
  });

  // Map → slider (keep in sync when user scrolls, pinches, double-clicks, etc.)
  map.on('zoom', function() {
    slider.value = map.getZoom();
  });
})();

// Re-render completeness calendar when its container resizes (window resize, tab show)
if (typeof ResizeObserver !== 'undefined') {
  var calEl = document.getElementById('completeness-calendar');
  if (calEl) {
    var lastCalW = 0;
    new ResizeObserver(function(entries) {
      var w = entries[0].contentRect.width;
      if (w && Math.abs(w - lastCalW) > 4) {
        lastCalW = w;
        renderCompleteness();
      }
    }).observe(calEl);
  }
}

// ── Tab Navigation ──────────────────────────────────────────────
var activeTab = 'monitor';
var tabDataLoaded = { monitor: true, pipeline: true, catalog: false, waveviewer: false, helicorder: false };

function switchTab(tab) {
  if (tab === activeTab) return;
  document.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.remove('active'); });
  var targetBtn = document.querySelector('.tab-btn[data-tab="' + tab + '"]');
  if (targetBtn) targetBtn.classList.add('active');
  document.getElementById('tab-' + activeTab).classList.remove('active');
  document.getElementById('tab-' + tab).classList.add('active');
  activeTab = tab;
  if (tab === 'monitor') {
    setTimeout(function() { map.invalidateSize(); }, 50);
  }
  if (tab === 'pipeline') {
    setTimeout(function() { renderCompleteness(); }, 50);
  }
  if (!tabDataLoaded[tab]) {
    tabDataLoaded[tab] = true;
    if (tab === 'catalog') loadCatalogTable();
    if (tab === 'helicorder') initHelicorderTab();
  }
}

// ── Pipeline stat-card click handlers ───────────────────────────
document.getElementById('stat-card-events').addEventListener('click', function() {
  document.getElementById('cat-review-filter').value = '';
  catPage = 1;
  switchTab('catalog');
  if (tabDataLoaded.catalog) loadCatalogTable();
});
document.getElementById('stat-card-picks').addEventListener('click', function() {
  document.getElementById('cat-review-filter').value = '';
  catPage = 1;
  switchTab('catalog');
  if (tabDataLoaded.catalog) loadCatalogTable();
});
document.getElementById('stat-card-reviewed').addEventListener('click', function() {
  document.getElementById('cat-review-filter').value = 'confirmed';
  catPage = 1;
  switchTab('catalog');
  if (tabDataLoaded.catalog) loadCatalogTable();
});

// ── Error log expand modal ──────────────────────────────────────
var errorModal = document.getElementById('error-modal');
document.getElementById('error-log-expand').addEventListener('click', function() {
  errorModal.classList.add('open');
});
document.getElementById('error-modal-close').addEventListener('click', function() {
  errorModal.classList.remove('open');
});
errorModal.addEventListener('click', function(e) {
  if (e.target === errorModal) errorModal.classList.remove('open');
});

// ── QuakeML Export ──────────────────────────────────────────────
document.getElementById('export-quakeml').addEventListener('click', function() {
  var url = '/api/catalog/export/quakeml';
  var params = [];
  if (filterStartDate) params.push('start_date=' + encodeURIComponent(filterStartDate));
  if (filterEndDate) params.push('end_date=' + encodeURIComponent(filterEndDate));
  if (params.length > 0) url += '?' + params.join('&');
  downloadFromUrl(url, 'catalog_export.xml');
});
