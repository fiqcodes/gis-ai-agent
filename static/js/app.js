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
  const { region, start_date, end_date, variables, stats, layers, geo, insight } = result;

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
  let html = buildResultHTML(region, start_date, end_date, variables, stats, layers, insight);
  const bubble = appendAIMessage(html);

  // 3. Render Plotly charts inside the bubble
  renderChartsInBubble(bubble, stats, variables);
}

function buildResultHTML(region, startDate, endDate, variables, stats, layers, insight) {
  const varList = (variables || []).map(v => v.toUpperCase()).join(', ');
  const dateStr = `${startDate} → ${endDate}`;

  let html = `<h3>Analysis Complete</h3>`;
  html += `<p><strong>Region:</strong> ${escapeHtml(region)} &nbsp;|&nbsp; <strong>Period:</strong> ${dateStr}</p>`;
  html += `<p><strong>Variables:</strong> ${varList}</p>`;

  // Stats table
  if (stats && Object.keys(stats).length > 0) {
    html += buildStatsHTML(stats);
  }

  // AI Insight
  if (insight) {
    html += `<h3>AI Insight</h3>`;
    html += `<div class="insight-text">${parseMarkdown(insight)}</div>`;
  }

  // Chart containers — unique IDs per message to avoid conflicts
  const msgId = Date.now();
  if (stats) {
    Object.keys(stats).forEach(varName => {
      const s = stats[varName];
      if (s && s.mean !== undefined && s.mean !== null) {
        html += `<div class="chart-container" id="chart_${sanitizeId(varName)}_${msgId}" style="height:220px;min-height:220px"></div>`;
      }
      if (s && s.classes) {
        html += `<div class="chart-container" id="chart_lulc_pie_${msgId}" style="height:260px;min-height:260px"></div>`;
        html += `<div class="chart-container" id="chart_lulc_bar_${msgId}" style="height:220px;min-height:220px"></div>`;
      }
    });
  }
  html += `<div data-msg-id="${msgId}" style="display:none"></div>`;

  // Map open cards for each layer
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
        </div>
      `;
    });
  }

  return html;
}

function buildStatsHTML(stats) {
  let html = `<h3>Statistics</h3>`;

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

    // UHI special
    if (s.lst_mean !== undefined) {
      html += `<p><strong>UHI</strong> — LST mean: ${s.lst_mean.toFixed(2)}°C, std: ${s.lst_std.toFixed(2)}°C</p>`;
      continue;
    }

    // Standard stats
    if (s.mean !== null && s.mean !== undefined) {
      html += `
        <table class="stats-table">
          <thead><tr>
            <th>${escapeHtml(varName)}</th><th>Value</th>
          </tr></thead>
          <tbody>
            <tr><td>Mean</td><td>${s.mean.toFixed(4)}</td></tr>
            <tr><td>Median</td><td>${(s.median||0).toFixed(4)}</td></tr>
            <tr><td>Std Dev</td><td>${(s.std||0).toFixed(4)}</td></tr>
            <tr><td>Min / Max</td><td>${(s.min||0).toFixed(4)} / ${(s.max||0).toFixed(4)}</td></tr>
            <tr><td>P10 / P90</td><td>${(s.p10||0).toFixed(4)} / ${(s.p90||0).toFixed(4)}</td></tr>
          </tbody>
        </table>
      `;
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
const STEP_ICONS = {
  pending: '○',
  running: '◎',
  done   : '✓',
  error  : '✗',
};

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
    div.className = 'plan-step';
    div.style.animationDelay = (i * 0.08) + 's';

    const iconContent = step.status === 'running'
      ? `<span class="step-spin">◎</span>`
      : STEP_ICONS[step.status] || '○';

    const progressHTML = step.status === 'running' ? `
      <div class="step-progress">
        <div class="step-progress-bar" style="width:${step.progress || 0}%"></div>
      </div>
      <div class="step-sub">${step.progress || 0}%</div>
    ` : '';

    div.innerHTML = `
      <div class="step-icon ${step.status}">${iconContent}</div>
      <div class="step-body">
        <div class="step-label ${step.status}">${escapeHtml(step.label)}</div>
        ${progressHTML}
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
  const resizer    = document.getElementById('panelResizer');
  const chatPanel  = document.getElementById('chatPanel');
  const mapPanel   = document.getElementById('mapPanel');
  const navW       = 52; // matches --nav-w
  const MIN_CHAT   = 280;
  const MAX_CHAT   = window.innerWidth * 0.75;

  let isDragging = false;
  let startX     = 0;
  let startWidth = 0;

  resizer.addEventListener('mousedown', (e) => {
    isDragging = true;
    startX     = e.clientX;
    startWidth = chatPanel.getBoundingClientRect().width;

    resizer.classList.add('dragging');
    document.body.style.cursor    = 'col-resize';
    document.body.style.userSelect = 'none';

    // Prevent map interactions while dragging
    mapPanel.style.pointerEvents = 'none';
  });

  document.addEventListener('mousemove', (e) => {
    if (!isDragging) return;

    const delta    = e.clientX - startX;
    let newWidth   = startWidth + delta;

    // Clamp between min and max
    newWidth = Math.max(MIN_CHAT, Math.min(newWidth, window.innerWidth - navW - 200));

    const pct = (newWidth + navW) / window.innerWidth * 100;

    // Update chat panel width
    chatPanel.style.width = (newWidth) + 'px';

    // Update map panel left
    mapPanel.style.left = (newWidth + navW) + 'px';

    // Keep resizer in sync
    resizer.style.left = (newWidth + navW) + 'px';

    // Invalidate map size so tiles re-render correctly
    if (map) map.invalidateSize({ animate: false });
  });

  document.addEventListener('mouseup', () => {
    if (!isDragging) return;
    isDragging = false;

    resizer.classList.remove('dragging');
    document.body.style.cursor     = '';
    document.body.style.userSelect = '';
    mapPanel.style.pointerEvents   = '';

    // Final map refresh
    if (map) setTimeout(() => map.invalidateSize(), 50);
  });

  // Touch support
  resizer.addEventListener('touchstart', (e) => {
    const touch = e.touches[0];
    isDragging = true;
    startX     = touch.clientX;
    startWidth = chatPanel.getBoundingClientRect().width;
    resizer.classList.add('dragging');
    mapPanel.style.pointerEvents = 'none';
  }, { passive: true });

  document.addEventListener('touchmove', (e) => {
    if (!isDragging) return;
    const touch  = e.touches[0];
    const delta  = touch.clientX - startX;
    let newWidth = Math.max(MIN_CHAT, Math.min(startWidth + delta, window.innerWidth - navW - 200));

    chatPanel.style.width = newWidth + 'px';
    mapPanel.style.left   = (newWidth + navW) + 'px';
    resizer.style.left    = (newWidth + navW) + 'px';

    if (map) map.invalidateSize({ animate: false });
  }, { passive: true });

  document.addEventListener('touchend', () => {
    if (!isDragging) return;
    isDragging = false;
    resizer.classList.remove('dragging');
    mapPanel.style.pointerEvents = '';
    if (map) setTimeout(() => map.invalidateSize(), 50);
  });
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
