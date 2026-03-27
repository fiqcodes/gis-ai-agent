/* ============================================================
   GIS Agent WebApp — app.js
   Handles: Leaflet map, chat, polling, Plotly charts, ROI drawing
   ============================================================ */

'use strict';

// ── State ─────────────────────────────────────────────────────────────────────
let map, drawnItems, drawControl;
let activeROI      = null;   // { name, layer, geojson }
let mapLayers      = [];     // [{ name, leafletLayer, visible, type }]
let currentJobId   = null;
let pollingTimer   = null;
let isAnalyzing    = false;
let planExpanded   = true;
let assetCount     = 0;

// ── Init ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initMap();
  checkHealth();
  setInterval(checkHealth, 30000);
});

// ════════════════════════════════════════════════════════
// MAP SETUP
// ════════════════════════════════════════════════════════
function initMap() {
  map = L.map('map', {
    center: [20, 0],
    zoom: 3,
    zoomControl: false,
    attributionControl: true,
  });

  // Satellite basemap (ESRI World Imagery)
  L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    { attribution: 'Tiles © Esri', maxZoom: 19 }
  ).addTo(map);

  // Drawn items layer group
  drawnItems = new L.FeatureGroup();
  map.addLayer(drawnItems);

  // Leaflet Draw
  drawControl = new L.Control.Draw({
    edit: { featureGroup: drawnItems },
    draw: {
      polygon  : { shapeOptions: { color: '#ff4757', fillOpacity: 0.18, weight: 2 } },
      rectangle: { shapeOptions: { color: '#ff4757', fillOpacity: 0.18, weight: 2 } },
      circle   : false,
      circlemarker: false,
      marker   : false,
      polyline : false,
    },
  });

  map.on(L.Draw.Event.CREATED, onROIDrawn);
  map.on(L.Draw.Event.DELETED, onROIDeleted);
}

function onROIDrawn(e) {
  // Remove previous drawn ROI
  drawnItems.clearLayers();

  const layer   = e.layer;
  const geojson = layer.toGeoJSON();
  drawnItems.addLayer(layer);

  // Show naming modal
  document.getElementById('roiModal').style.display = 'block';
  const input = document.getElementById('roiNameInput');
  input.value = 'custom_region_' + Date.now().toString().slice(-4);
  input.focus();
  input.select();

  // Temp store
  window._pendingROI = { layer, geojson };

  // Reset draw button
  setDrawMode(null);
}

function onROIDeleted() {
  clearROI();
}

function confirmROI() {
  if (!window._pendingROI) return;
  const name = document.getElementById('roiNameInput').value.trim()
                || 'custom_region';

  activeROI = {
    name   : name,
    layer  : window._pendingROI.layer,
    geojson: window._pendingROI.geojson,
  };
  window._pendingROI = null;

  document.getElementById('roiModal').style.display = 'none';
  showROIChip(name);
  assetCount++;
  updateAssetsBadge();

  // Add drawn polygon as subtle outline only (no fill confusion)
  activeROI.layer.setStyle({
    color      : '#00d4b8',
    fillColor  : '#00d4b8',
    fillOpacity: 0.05,
    weight     : 1.5,
    dashArray  : '4,4',
  });
  addMapLayer({
    name   : name,
    layer  : activeROI.layer,
    type   : 'roi',
    visible: true,
  });

  appendSystemMessage(`Region <strong>${name}</strong> added. Reference it with @${name} in your message.`);
}

function cancelROIDraw() {
  drawnItems.clearLayers();
  window._pendingROI = null;
  document.getElementById('roiModal').style.display = 'none';
  setDrawMode(null);
}

function clearROI() {
  activeROI = null;
  drawnItems.clearLayers();
  document.getElementById('roiChips').style.display = 'none';
}

function showROIChip(name) {
  document.getElementById('roiChipName').textContent = name;
  document.getElementById('roiChips').style.display = 'flex';
}

let currentDrawMode = null;
function setDrawMode(mode) {
  // Disable any active draw
  if (map._drawn) { map._drawn.disable(); map._drawn = null; }
  document.getElementById('drawPolyBtn').classList.remove('drawing');
  document.getElementById('drawRectBtn').classList.remove('drawing');

  if (mode === 'polygon') {
    map._drawn = new L.Draw.Polygon(map, drawControl.options.draw.polygon);
    map._drawn.enable();
    document.getElementById('drawPolyBtn').classList.add('drawing');
  } else if (mode === 'rectangle') {
    map._drawn = new L.Draw.Rectangle(map, drawControl.options.draw.rectangle);
    map._drawn.enable();
    document.getElementById('drawRectBtn').classList.add('drawing');
  }
  currentDrawMode = mode;
}

function startDrawPolygon() {
  if (currentDrawMode === 'polygon') { setDrawMode(null); return; }
  setDrawMode('polygon');
}

function startDrawRect() {
  if (currentDrawMode === 'rectangle') { setDrawMode(null); return; }
  setDrawMode('rectangle');
}

function toggleROIMode() {
  document.getElementById('roiModeBtn').classList.toggle('active');
}

// ════════════════════════════════════════════════════════
// MAP LAYERS MANAGEMENT
// ════════════════════════════════════════════════════════
function addMapLayer({ name, layer, type, visible = true, bbox = null }) {
  const id = 'layer_' + Date.now();

  if (layer) {
    if (visible) map.addLayer(layer);
    mapLayers.push({ id, name, layer, type, visible });
  }

  renderLayersList();
  updateLayerBadge(name);
}

function addImageOverlay(name, base64Img, bbox) {
  if (!bbox) {
    console.error('addImageOverlay: no bbox for', name);
    return;
  }
  const [w, s, e, n] = bbox;
  const bounds = [[s, w], [n, e]];
  console.log('Adding overlay:', name, 'bounds:', bounds);

  const overlay = L.imageOverlay(base64Img, bounds, {
    opacity    : 0.85,
    interactive: false,
    className  : 'gis-overlay',
  });
  overlay.addTo(map);

  // Small delay before fitting so map is ready
  setTimeout(() => {
    map.fitBounds(bounds, { padding: [30, 30] });
  }, 100);

  const layerId = 'layer_' + Date.now() + '_' + Math.random().toString(36).slice(2,6);
  mapLayers.push({
    id     : layerId,
    name   : name,
    layer  : overlay,
    type   : 'raster',
    visible: true,
  });

  renderLayersList();
  updateLayerBadge(name);
  // Show layers panel automatically
  document.getElementById('layersPanel').style.display = 'block';
}

function addROIOverlayFromBbox(regionName, bbox) {
  // No zoom during analysis — zoom happens when first tile layer arrives
  // Just store bbox globally for reference
  window._currentBbox = bbox;
}

function addTileLayer(name, tileUrl, bbox) {
  // GEE tile layer — interactive, pans/zooms correctly
  const tileLayer = L.tileLayer(tileUrl, {
    opacity    : 0.85,
    maxZoom    : 18,
    tileSize   : 256,
    attribution: 'Google Earth Engine',
  });
  tileLayer.addTo(map);

  // Zoom to this region's bbox — always zoom to first tile layer in each batch
  const existingTiles = mapLayers.filter(l => l.type === 'tile').length;
  if (bbox && existingTiles === 0) {
    const [w, s, e, n] = bbox;
    // Wait for tile layer to actually render before zooming
    tileLayer.once('load', () => {
      map.fitBounds([[s, w], [n, e]], { padding: [40, 40] });
    });
    // Fallback timeout in case 'load' doesn't fire
    setTimeout(() => {
      map.fitBounds([[s, w], [n, e]], { padding: [40, 40] });
    }, 2000);
  }

  const layerId = 'layer_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6);
  mapLayers.push({
    id     : layerId,
    name   : name,
    layer  : tileLayer,
    type   : 'tile',
    visible: true,
  });

  renderLayersList();
  updateLayerBadge(name);
  document.getElementById('layersPanel').style.display = 'block';
  console.log('✓ Tile layer added:', name);
}

function toggleLayerVisibility(id) {
  const item = mapLayers.find(l => l.id === id);
  if (!item) return;
  if (item.visible) {
    map.removeLayer(item.layer);
    item.visible = false;
  } else {
    map.addLayer(item.layer);
    item.visible = true;
  }
  renderLayersList();
}

function zoomToLayer(id) {
  const item = mapLayers.find(l => l.id === id);
  if (!item || !item.layer.getBounds) return;
  try { map.fitBounds(item.layer.getBounds(), { padding: [40, 40] }); } catch(e){}
}

function removeLayerById(id) {
  const idx = mapLayers.findIndex(l => l.id === id);
  if (idx < 0) return;
  map.removeLayer(mapLayers[idx].layer);
  mapLayers.splice(idx, 1);
  renderLayersList();
}

function clearAllLayers() {
  mapLayers.forEach(l => { try { map.removeLayer(l.layer); } catch(e){} });
  mapLayers = [];
  renderLayersList();
  document.getElementById('layerBadge').style.display = 'none';
}

function renderLayersList() {
  const list = document.getElementById('layersList');
  if (!list) return;
  list.innerHTML = '';

  if (mapLayers.length === 0) {
    list.innerHTML = '<div style="padding:12px;color:var(--text3);font-size:12px;text-align:center">No layers yet</div>';
    return;
  }

  [...mapLayers].reverse().forEach(item => {
    const div = document.createElement('div');
    div.className = 'layer-item';
    div.innerHTML = `
      <div class="layer-item-header">
        <span class="layer-drag">⠿</span>
        <button class="layer-eye ${item.visible ? '' : 'hidden'}" onclick="toggleLayerVisibility('${item.id}')">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            ${item.visible
              ? '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>'
              : '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>'}
          </svg>
        </button>
        <span class="layer-name" title="${item.name}">${item.name}</span>
        <div class="layer-actions">
          <button class="layer-action-btn" onclick="zoomToLayer('${item.id}')" title="Zoom to">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
            </svg>
          </button>
          <button class="layer-action-btn" onclick="removeLayerById('${item.id}')" title="Remove" style="color:var(--red)">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
      </div>
    `;
    list.appendChild(div);
  });
}

function updateLayerBadge(name) {
  const badge = document.getElementById('layerBadge');
  badge.style.display = 'block';
  document.getElementById('layerBadgeText').textContent = name;
}

function toggleLayersPanel() {
  const panel = document.getElementById('layersPanel');
  if (panel.style.display === 'none') {
    panel.style.display = 'block';
    renderLayersList();
  } else {
    panel.style.display = 'none';
  }
}

function toggleMapPanel() {
  const mp  = document.getElementById('mapPanel');
  const btn = document.getElementById('collapseMapBtn');
  mp.classList.toggle('collapsed');
  btn.style.transform = mp.classList.contains('collapsed') ? 'rotate(180deg)' : '';
}

// ════════════════════════════════════════════════════════
// CHAT
// ════════════════════════════════════════════════════════
function setNavActive(btn) {
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

function handleKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 150) + 'px';
}

function handleInputChange(el) {
  const val = el.value;
  const qa  = document.getElementById('quickActions');

  // Show quick actions on '/'
  if (val.endsWith('/') || val.match(/\/\w*$/)) {
    qa.style.display = 'block';
  } else {
    qa.style.display = 'none';
  }
}

function insertQuickAction(cmd) {
  const input = document.getElementById('chatInput');
  input.value = input.value.replace(/\/\w*$/, '') + cmd + ' ';
  document.getElementById('quickActions').style.display = 'none';
  input.focus();
}

function sendMessage() {
  const input = document.getElementById('chatInput');
  let text    = input.value.trim();
  if (!text || isAnalyzing) return;

  // Inject active ROI name if '@' not present but ROI is active
  if (activeROI && !text.includes('@')) {
    text += ` @${activeROI.name}`;
  }

  appendUserMessage(text);
  input.value = '';
  input.style.height = 'auto';
  document.getElementById('quickActions').style.display = 'none';

  startAnalysis(text);
}

function appendUserMessage(text) {
  const msgs = document.getElementById('messages');
  const div  = document.createElement('div');
  div.className = 'msg-row user';
  div.innerHTML = `
    <div class="msg-avatar">YOU</div>
    <div class="msg-bubble user">${escapeHtml(text)}</div>
  `;
  msgs.appendChild(div);
  scrollToBottom();
}

function appendAIMessage(html) {
  const msgs = document.getElementById('messages');
  const div  = document.createElement('div');
  div.className = 'msg-row ai';
  div.innerHTML = `
    <div class="msg-avatar">AI</div>
    <div class="msg-bubble ai">${html}</div>
  `;
  msgs.appendChild(div);
  scrollToBottom();
  return div.querySelector('.msg-bubble');
}

function appendTypingIndicator() {
  const msgs = document.getElementById('messages');
  const div  = document.createElement('div');
  div.className = 'msg-row ai';
  div.id = 'typingIndicator';
  div.innerHTML = `
    <div class="msg-avatar">AI</div>
    <div class="msg-bubble ai">
      <div class="typing-indicator">
        <div class="typing-dot"></div>
        <div class="typing-dot"></div>
        <div class="typing-dot"></div>
      </div>
    </div>
  `;
  msgs.appendChild(div);
  scrollToBottom();
}

function removeTypingIndicator() {
  const el = document.getElementById('typingIndicator');
  if (el) el.remove();
}

function appendSystemMessage(html) {
  const msgs = document.getElementById('messages');
  const div  = document.createElement('div');
  div.style.cssText = 'text-align:center;padding:6px 0;';
  div.innerHTML = `<span style="font-size:11.5px;color:var(--text3)">${html}</span>`;
  msgs.appendChild(div);
  scrollToBottom();
}

function scrollToBottom() {
  const msgs = document.getElementById('messages');
  setTimeout(() => { msgs.scrollTop = msgs.scrollHeight; }, 50);
}

function clearChat() {
  document.getElementById('messages').innerHTML = '';
  clearAllLayers();
  hidePlanWidget();
  stopPolling();
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ════════════════════════════════════════════════════════
// ANALYSIS — POST + POLL
// ════════════════════════════════════════════════════════
function startAnalysis(text) {
  isAnalyzing = true;
  setSendBtnStop();
  appendTypingIndicator();
  showPlanWidget();

  const body = { message: text };
  if (activeROI) body.roi = activeROI.geojson;

  fetch('/api/analyze', {
    method : 'POST',
    headers: { 'Content-Type': 'application/json' },
    body   : JSON.stringify(body),
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) { handleError(data.error); return; }
    currentJobId = data.job_id;
    startPolling(data.job_id);
  })
  .catch(err => handleError(err.toString()));
}

function startPolling(jobId) {
  pollingTimer = setInterval(() => pollJob(jobId), 1500);
}

function stopPolling() {
  if (pollingTimer) { clearInterval(pollingTimer); pollingTimer = null; }
  currentJobId = null;
  isAnalyzing  = false;
  setSendBtnSend();
}

function pollJob(jobId) {
  fetch(`/api/job/${jobId}`)
  .then(r => r.json())
  .then(data => {
    updatePlanSteps(data.steps);

    // Show geocode result on map as soon as we have it
    if (data.geo && data.geo.bbox && !window._geoShown) {
      window._geoShown = true;
      if (data.parsed && data.parsed.region) {
        addROIOverlayFromBbox(data.parsed.region, data.geo.bbox);
      }
    }

    if (data.status === 'complete') {
      stopPolling();
      window._geoShown = false;
      removeTypingIndicator();
      hidePlanWidget();
      handleResult(data.result);
    } else if (data.status === 'error') {
      stopPolling();
      window._geoShown = false;
      removeTypingIndicator();
      hidePlanWidget();
      handleError(data.error || 'Unknown error');
    }
  })
  .catch(() => {});
}

function handleError(msg) {
  stopPolling();
  removeTypingIndicator();
  hidePlanWidget();
  appendAIMessage(`<p style="color:var(--red)">⚠️ Error: ${escapeHtml(msg)}</p>`);
}

// ════════════════════════════════════════════════════════
// RESULT RENDERING
// ════════════════════════════════════════════════════════
function handleResult(result) {
  if (!result) { appendAIMessage('<p>No result returned.</p>'); return; }

  if (result.type === 'qa') {
    appendAIMessage(parseMarkdown(result.answer));
    return;
  }

  // Analysis result
  const { region, start_date, end_date, variables, stats, layers, geo, insight, figures } = result;

  // 1. Clear previous layers and load new GEE tile layers onto map
  clearAllLayers();  // Remove old region's layers before adding new ones
  if (layers && layers.length > 0) {
    console.log('Loading', layers.length, 'tile layers onto map');
    layers.forEach((lyr, i) => {
      console.log('Layer', i, lyr.name, 'type:', lyr.type, 'has tile_url:', !!lyr.tile_url);
      if (lyr.tile_url && lyr.type === 'tile') {
        addTileLayer(lyr.name, lyr.tile_url, lyr.bbox);
      } else if ((lyr.url || lyr.image) && lyr.bbox) {
        addImageOverlay(lyr.name, lyr.url || lyr.image, lyr.bbox);
      } else {
        console.warn('Layer missing tile_url or bbox:', lyr.name, lyr);
      }
    });
  } else {
    console.warn('No layers returned from analysis');
    if (result.geo && result.geo.bbox) {
      const [w, s, e, n] = result.geo.bbox;
      map.fitBounds([[s, w], [n, e]], { padding: [40, 40] });
    }
  }

  // 2. Build chat message
  let html = buildResultHTML(region, start_date, end_date, variables, stats, layers, insight, figures);
  appendAIMessage(html);
}

function buildResultHTML(region, startDate, endDate, variables, stats, layers, insight, figures) {
  const varList = (variables || []).map(v => v.toUpperCase()).join(', ');
  const dateStr = `${startDate} → ${endDate}`;
  let html = '';

  // ── SECTION 1: Introduction header ───────────────────────────────────────
  html += `<h3>Analysis Complete</h3>`;
  html += `<p><strong>Region:</strong> ${escapeHtml(region)} &nbsp;|&nbsp; <strong>Period:</strong> ${dateStr}</p>`;
  html += `<p><strong>Variables:</strong> ${varList}</p>`;

  // Composite method note
  const nMonths = stats && Object.values(stats)[0]?.monthly
    ? Object.keys(Object.values(stats)[0].monthly).length : 0;
  const method = nMonths > 1 ? 'Median composite (Landsat 8/9, all scenes)' : 'Median composite (Landsat 8/9)';
  html += `<p style="color:var(--text2);font-size:12.5px">${method} · ${nMonths > 0 ? nMonths + ' months' : dateStr}</p>`;

  // ── SECTION 2: RGB overview map (intro) ──────────────────────────────────
  // Check if any figure has an rgb_overview
  const firstFig = figures && Object.values(figures)[0];
  if (firstFig && firstFig.rgb_overview) {
    html += `<div class="result-section-label">Study Area</div>`;
    html += `<div class="result-img-wrap">
      <img src="${firstFig.rgb_overview}" class="result-img" loading="lazy"/>
      <div class="result-img-caption">Study Area Overview (${escapeHtml(region)}) — True Color RGB</div>
    </div>`;
  }

  // ── SECTION 3: Statistics (plain text style) ──────────────────────────────
  if (stats && Object.keys(stats).length > 0) {
    html += `<h3>Statistics</h3>`;
    html += buildStatsHTML(stats);
  }

  // ── SECTION 4: Per-variable analysis map + charts ─────────────────────────
  if (figures && Object.keys(figures).length > 0) {
    for (const [varLabel, fig] of Object.entries(figures)) {
      if (!fig) continue;

      // Analysis map with colorbar
      if (fig.analysis_map) {
        html += `<div class="result-section-label">${escapeHtml(varLabel)} Map</div>`;
        html += `<div class="result-img-wrap">
          <img src="${fig.analysis_map}" class="result-img" loading="lazy"/>
          <div class="result-img-caption">${escapeHtml(varLabel)} — ${escapeHtml(region)} · ${dateStr}</div>
        </div>`;
      }

      // Charts (histogram, class bar, monthly trend)
      if (fig.charts && fig.charts.length > 0) {
        const hasTwo = fig.charts.length >= 2;
        if (hasTwo) {
          html += `<div class="result-charts-row">`;
          for (const [chartType, chartB64] of fig.charts) {
            html += `<div class="result-chart-cell">
              <img src="${chartB64}" class="result-img" loading="lazy"/>
            </div>`;
          }
          html += `</div>`;
        } else {
          for (const [chartType, chartB64] of fig.charts) {
            html += `<div class="result-img-wrap">
              <img src="${chartB64}" class="result-img" loading="lazy"/>
            </div>`;
          }
        }
      }
    }
  }

  // ── SECTION 5: AI Insight ─────────────────────────────────────────────────
  if (insight) {
    html += `<h3>AI Insight</h3>`;
    html += `<div class="insight-text">${parseMarkdown(insight)}</div>`;
  }

  // ── SECTION 6: Map open cards ─────────────────────────────────────────────
  if (layers && layers.length > 0) {
    layers.slice(0, 4).forEach(lyr => {
      html += `
        <div class="map-open-card" onclick="focusLayer('${escapeHtml(lyr.name)}')">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polygon points="3 11 22 2 13 21 11 13 3 11"/>
          </svg>
          <div>
            <span>${escapeHtml(lyr.name)}</span>
            <span class="card-sub">Click to open map</span>
          </div>
        </div>`;
    });
  }

  // ── SECTION 7: Attributions ───────────────────────────────────────────────
  html += `<div class="result-attribution">
    <div class="attr-title">Attributions</div>
    <ul class="attr-list">
      <li>Data source: Landsat Collection 2 Level-2 Surface Reflectance (USGS/NASA)</li>
      <li>Platform: Google Earth Engine</li>
      <li>Method: ${method}</li>
      <li>Region: ${escapeHtml(region)}</li>
      <li>Time period: ${startDate} – ${endDate}</li>
      <li>Analysis date: ${new Date().toISOString().slice(0,10)}</li>
    </ul>
  </div>`;

  return html;
}

function buildStatsHTML(stats) {
  let html = '';
  for (const [varName, s] of Object.entries(stats)) {
    if (!s) continue;

    // LULC classes
    if (s.classes) {
      html += `<p><strong>${escapeHtml(varName)}</strong> — ${s.n_classes} classes, ${(s.total_ha || 0).toLocaleString()} ha total</p>`;
      html += `<ul>`;
      Object.entries(s.classes).forEach(([cls, info]) => {
        html += `<li><strong>${escapeHtml(cls)}:</strong> ${info.percentage.toFixed(1)}% (${info.hectares.toLocaleString()} ha)</li>`;
      });
      html += `</ul>`;
      continue;
    }

    // UHI
    if (s.lst_mean !== undefined) {
      html += `<p><strong>UHI</strong> — LST mean: ${s.lst_mean.toFixed(2)}°C, std: ${s.lst_std.toFixed(2)}°C</p>`;
      continue;
    }

    // Standard stats — plain text style (pict 7)
    if (s.mean !== null && s.mean !== undefined) {
      const fmt = v => v != null ? v.toFixed(4) : 'N/A';
      html += `<div class="stats-plain">
        <div class="stats-plain-var">${escapeHtml(varName)}</div>
        <div class="stats-plain-row"><span>Mean</span><span>${fmt(s.mean)}</span></div>
        <div class="stats-plain-row"><span>Median</span><span>${fmt(s.median)}</span></div>
        <div class="stats-plain-row"><span>Std Dev</span><span>${fmt(s.std)}</span></div>
        <div class="stats-plain-row"><span>Min / Max</span><span>${fmt(s.min)} / ${fmt(s.max)}</span></div>
        <div class="stats-plain-row"><span>P10 / P90</span><span>${fmt(s.p10)} / ${fmt(s.p90)}</span></div>
      </div>`;
    }
  }
  return html;
}

function renderChartsInBubble(bubble, stats, variables) {
  if (!stats) return;

  // Wait for DOM to be fully painted before rendering Plotly charts
  setTimeout(() => {
    for (const [varName, s] of Object.entries(stats)) {
      if (!s) continue;

      // Stats bar chart (mean/p10/p90) — always show for numeric vars
      const msgId = bubble.querySelector('[data-msg-id]')?.dataset?.msgId || '';
      const chartEl = bubble.querySelector(`#chart_${sanitizeId(varName)}_${msgId}`);
      if (chartEl && s.mean !== null && s.mean !== undefined) {
        const hasMonthly = s.monthly && Object.keys(s.monthly).length > 0;

        if (hasMonthly) {
          // Monthly trend line
          const months = Object.keys(s.monthly).sort();
          const values = months.map(m => s.monthly[m]);
          Plotly.newPlot(chartEl, [{
            x        : months,
            y        : values,
            type     : 'scatter',
            mode     : 'lines+markers',
            line     : { color: '#00d4b8', width: 2.5 },
            marker   : { color: '#00d4b8', size: 7, symbol: 'circle' },
            fill     : 'tozeroy',
            fillcolor: 'rgba(0,212,184,0.08)',
            name     : varName,
          }], plotlyLayout(`${varName} — Monthly Trend`), plotlyConfig());
        } else {
          // Summary bar: mean, p10, p90
          const vals  = [s.p10 || 0, s.mean || 0, s.p90 || 0];
          const labs  = ['P10', 'Mean', 'P90'];
          const cols  = ['#5a6478', '#00d4b8', '#00a896'];
          Plotly.newPlot(chartEl, [{
            type   : 'bar',
            x      : labs,
            y      : vals,
            marker : { color: cols },
            text   : vals.map(v => v.toFixed(4)),
            textposition: 'outside',
            textfont: { color: '#4a5568', size: 10 },
          }], plotlyLayout(`${varName} Statistics`, 200), plotlyConfig());
        }
      }

      // LULC charts
      if (s.classes) {
        const msgId2 = bubble.querySelector('[data-msg-id]')?.dataset?.msgId || '';
        const pieEl = bubble.querySelector(`#chart_lulc_pie_${msgId2}`);
        const barEl = bubble.querySelector(`#chart_lulc_bar_${msgId2}`);
        const names  = Object.keys(s.classes);
        const pcts   = names.map(n => s.classes[n].percentage);
        const colors = names.map(n => s.classes[n].color || '#00d4b8');
        const has    = s.total_ha;

        if (pieEl) {
          Plotly.newPlot(pieEl, [{
            type        : 'pie',
            labels      : names,
            values      : pcts,
            marker      : { colors, line: { color: '#f5f6f8', width: 1.5 } },
            textinfo    : 'label+percent',
            textfont    : { color: '#1a1d23', size: 11, family: 'DM Sans' },
            hole        : 0.38,
            pull        : names.map((_, i) => i === 0 ? 0.04 : 0),
          }], {
            ...plotlyLayout(`Land Cover Distribution<br><sub>Total: ${(has||0).toLocaleString()} ha</sub>`, 260),
            showlegend: false,
          }, plotlyConfig());
        }

        if (barEl) {
          const sorted = [...names].sort((a,b) => s.classes[b].percentage - s.classes[a].percentage);
          Plotly.newPlot(barEl, [{
            type        : 'bar',
            x           : sorted.map(n => s.classes[n].percentage),
            y           : sorted,
            orientation : 'h',
            marker      : { color: sorted.map(n => s.classes[n].color || '#00d4b8') },
            text        : sorted.map(n => s.classes[n].percentage.toFixed(1) + '%'),
            textposition: 'outside',
            textfont    : { color: '#4a5568', size: 10 },
          }], plotlyLayout('Area by Class (%)', 220), plotlyConfig());
        }
      }
    }
  }, 200); // 200ms delay ensures bubble is in DOM
}

function plotlyLayout(title, height = 200) {
  return {
    title      : { text: title, font: { size: 12, color: '#4a5568', family: 'DM Sans' } },
    height,
    margin     : { l: 50, r: 20, t: 35, b: 40 },
    paper_bgcolor: 'transparent',
    plot_bgcolor : 'transparent',
    font        : { color: '#4a5568', family: 'DM Sans', size: 11 },
    xaxis: { gridcolor: 'rgba(0,0,0,0.06)', tickcolor: 'rgba(0,0,0,0.1)', linecolor: 'rgba(0,0,0,0.08)' },
    yaxis: { gridcolor: 'rgba(0,0,0,0.06)', tickcolor: 'rgba(0,0,0,0.1)', linecolor: 'rgba(0,0,0,0.08)' },
    showlegend  : false,
  };
}

function plotlyConfig() {
  return {
    displayModeBar: false,
    responsive    : true,
  };
}

function parseMarkdown(text) {
  if (!text) return '';
  try {
    return marked.parse(text);
  } catch(e) {
    return text;
  }
}

function sanitizeId(s) {
  return s.replace(/[^a-zA-Z0-9]/g, '_');
}

function focusLayer(name) {
  const item = mapLayers.find(l => l.name === name || l.name.startsWith(name.split('|')[0].trim()));
  if (item) {
    zoomToLayer(item.id);
    updateLayerBadge(item.name);
    document.getElementById('layersPanel').style.display = 'block';
    renderLayersList();
  }
}

// ════════════════════════════════════════════════════════
// PLAN WIDGET
// ════════════════════════════════════════════════════════
// Step SVG icons — one per semantic meaning, matching images 2 & 3
const STEP_SVG = {
  // Detect / identify
  detect: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/><path d="M12 2v2M12 20v2M2 12H4M20 12h2"/></svg>`,
  // Geolocate / pin
  geo: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="11" r="3"/><path d="M12 2C8.13 2 5 5.13 5 9c0 5.25 7 13 7 13s7-7.75 7-13c0-3.87-3.13-7-7-7z"/></svg>`,
  // Analyze / GEE / globe
  analyze: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>`,
  // Parse / request
  parse: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 9h6M9 12h6M9 15h4"/></svg>`,
  // Init / boot
  init: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><path d="M12 8v4M12 16h.01"/></svg>`,
  // Layer / output
  layer: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>`,
  // AI insight
  insight: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M12 2a7 7 0 0 1 7 7c0 2.5-1.3 4.7-3.3 6l-.7 4H9l-.7-4A7 7 0 0 1 5 9a7 7 0 0 1 7-7z"/><line x1="9" y1="17" x2="15" y2="17"/></svg>`,
  // Generic
  default: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="1" fill="currentColor"/></svg>`,
};

function getStepIcon(label) {
  const l = label.toLowerCase();
  if (l.includes('init'))                                                    return STEP_SVG.init;
  if (l.includes('detect') || l.includes('identify') || l.includes('type')) return STEP_SVG.detect;
  if (l.includes('geo') || l.includes('locat') || l.includes('region'))     return STEP_SVG.geo;
  if (l.includes('layer') || l.includes('output') || l.includes('process')) return STEP_SVG.layer;
  if (l.includes('analyz') || l.includes('running') || l.includes('gee'))   return STEP_SVG.analyze;
  if (l.includes('pars'))                                                    return STEP_SVG.parse;
  if (l.includes('insight') || l.includes('generat') || l.includes('ai'))   return STEP_SVG.insight;
  return STEP_SVG.default;
}

function showPlanWidget() {
  const widget = document.getElementById('planWidget');
  widget.style.display = 'block';
  planExpanded = true;
}

function hidePlanWidget() {
  setTimeout(() => {
    const widget = document.getElementById('planWidget');
    if (widget) {
      document.getElementById('planTitle').textContent = 'Plan · Complete';
      setTimeout(() => { widget.style.display = 'none'; }, 2000);
    }
  }, 500);
}

function togglePlan() {
  planExpanded = !planExpanded;
  document.getElementById('planSteps').style.display = planExpanded ? 'flex' : 'none';
  document.querySelector('.plan-toggle').classList.toggle('collapsed', !planExpanded);
}

function updatePlanSteps(steps) {
  if (!steps) return;
  const container = document.getElementById('planSteps');
  const title     = document.getElementById('planTitle');

  const allDone  = steps.every(s => s.status === 'done' || s.status === 'error');
  const hasError = steps.some(s => s.status === 'error');
  title.textContent = hasError ? 'Plan · Error' : (allDone ? 'Plan · Complete' : 'Plan · Running');

  container.innerHTML = '';

  steps.forEach((step, i) => {
    const div = document.createElement('div');
    div.className = `plan-step step-${step.status}`;
    div.style.animationDelay = (i * 0.08) + 's';

    const svgIcon = getStepIcon(step.label);

    div.innerHTML = `
      <div class="step-icon-wrap step-icon-${step.status}">
        ${svgIcon}
        ${step.status === 'running' ? '<div class="step-ring"></div>' : ''}
      </div>
      <div class="step-body">
        <div class="step-label-text step-label-${step.status}">${escapeHtml(step.label)}</div>
      </div>
    `;
    container.appendChild(div);
  });
}

// ════════════════════════════════════════════════════════
// SEND BTN STATE
// ════════════════════════════════════════════════════════
function setSendBtnStop() {
  const btn = document.getElementById('sendBtn');
  btn.classList.add('stop');
  btn.innerHTML = `<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="4" width="16" height="16" rx="2"/></svg>`;
  btn.onclick = stopAnalysis;
}

function setSendBtnSend() {
  const btn = document.getElementById('sendBtn');
  btn.classList.remove('stop');
  btn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>`;
  btn.onclick = sendMessage;
}

function stopAnalysis() {
  stopPolling();
  removeTypingIndicator();
  hidePlanWidget();
  appendSystemMessage('Analysis stopped by user.');
}

// ════════════════════════════════════════════════════════
// ASSETS BADGE
// ════════════════════════════════════════════════════════
function updateAssetsBadge() {
  document.getElementById('assetsBadge').textContent = assetCount;
}

// ════════════════════════════════════════════════════════
// PANEL RESIZER
// ════════════════════════════════════════════════════════
(function initResizer() {
  const NAV_W    = 52;
  const MIN_CHAT = 280;

  const resizer   = document.getElementById('panelResizer');
  const chatPanel = document.getElementById('chatPanel');
  const mapPanel  = document.getElementById('mapPanel');

  let isDragging = false;
  let startX     = 0;
  let startChatW = 0;
  let currentChatW = 0;

  // ONE function that sets everything from a single chatW pixel value
  function setLayout(chatW) {
    chatW = Math.max(MIN_CHAT, Math.min(chatW, window.innerWidth - NAV_W - 200));
    currentChatW = chatW;

    chatPanel.style.width = chatW + 'px';
    mapPanel.style.left   = (NAV_W + chatW) + 'px';
    resizer.style.left    = (NAV_W + chatW - 8) + 'px';

    if (typeof map !== 'undefined' && map) map.invalidateSize({ animate: false });
  }

  // Init: read actual rendered chatPanel width AFTER CSS has been applied
  function initLayout() {
    // chatPanel.offsetWidth reads the true CSS-rendered width including calc()
    const chatW = chatPanel.offsetWidth;
    // Only override if it's valid (non-zero), else fall back to formula
    setLayout(chatW > 0 ? chatW : Math.round(window.innerWidth * 0.40) - NAV_W);
  }

  resizer.addEventListener('mousedown', (e) => {
    e.preventDefault();
    isDragging = true;
    startX     = e.clientX;
    startChatW = currentChatW;
    resizer.classList.add('dragging');
    document.body.style.cursor     = 'col-resize';
    document.body.style.userSelect = 'none';
    mapPanel.style.pointerEvents   = 'none';
  });

  document.addEventListener('mousemove', (e) => {
    if (!isDragging) return;
    setLayout(startChatW + (e.clientX - startX));
  });

  document.addEventListener('mouseup', () => {
    if (!isDragging) return;
    isDragging = false;
    resizer.classList.remove('dragging');
    document.body.style.cursor     = '';
    document.body.style.userSelect = '';
    mapPanel.style.pointerEvents   = '';
    if (typeof map !== 'undefined' && map) setTimeout(() => map.invalidateSize(), 50);
  });

  resizer.addEventListener('touchstart', (e) => {
    isDragging = true;
    startX     = e.touches[0].clientX;
    startChatW = currentChatW;
    resizer.classList.add('dragging');
    mapPanel.style.pointerEvents = 'none';
  }, { passive: true });

  document.addEventListener('touchmove', (e) => {
    if (!isDragging) return;
    setLayout(startChatW + (e.touches[0].clientX - startX));
  }, { passive: true });

  document.addEventListener('touchend', () => {
    if (!isDragging) return;
    isDragging = false;
    resizer.classList.remove('dragging');
    mapPanel.style.pointerEvents = '';
    if (typeof map !== 'undefined' && map) setTimeout(() => map.invalidateSize(), 50);
  });

  window.addEventListener('resize', initLayout);
  // Run after DOM is fully rendered so offsetWidth is accurate
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initLayout);
  } else {
    initLayout();
  }
})();

function checkHealth() {
  const dot = document.getElementById('statusDot');
  dot.className = 'status-dot checking';

  fetch('/api/health')
  .then(r => r.json())
  .then(data => {
    const ok = data.flask && data.ollama && data.gee;
    dot.className = 'status-dot ' + (ok ? 'online' : 'offline');
    dot.title = `Flask: ${data.flask ? '✓' : '✗'} | Ollama: ${data.ollama ? '✓' : '✗'} | GEE: ${data.gee ? '✓' : '✗'}`;
  })
  .catch(() => { dot.className = 'status-dot offline'; });
}
