/* El Paso Seismic Monitor — boot.js
 * Initial loads + polling intervals. Must stay last.
 *
 * Classic script sharing one global scope with its siblings; no build step.
 * Load order is fixed in index.html (definitions first, boot.js last):
 *   theme-chrome.js → map.js → util.js → monitor.js → pipeline-panels.js → filters-polling.js → tabs.js → helicorder.js → catalog.js → waveforms.js → review.js → waveviewer.js → pipeline-control.js → boot.js
 */

// Initial loads
connectWebSocket();
fetchJSON('/api/stations').then(renderStations).catch(function() {});
pollStatus();
pollData();
pollDisk();
pollErrors();
pollSlow();

setInterval(pollStatus, 2500);
setInterval(pollData, 10000);
setInterval(pollDisk, 30000);
setInterval(pollErrors, 10000);
setInterval(pollSlow, 30000);

// Fix map size after layout settles
setTimeout(function() { map.invalidateSize(); }, 200);
window.addEventListener('resize', function() { map.invalidateSize(); });
