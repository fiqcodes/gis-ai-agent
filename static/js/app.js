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
// ── Basemap definitions ───────────────────────────────────────────────────────
const BASEMAPS = {
  esri: {
    url  : 'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    attr : 'Tiles © Esri',
    maxZoom: 19,
  },
  google: {
    url  : 'https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}',
    attr : 'Imagery © Google',
    maxZoom: 20,
  },
  googlehybrid: {
    url  : 'https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}',
    attr : 'Imagery © Google',
    maxZoom: 20,
  },
  esriclarity: {
    url  : 'https://clarity.maptiles.arcgis.com/arcgis/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    attr : 'Tiles © Esri',
    maxZoom: 19,
  },
  opentopomap: {
    url  : 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
    attr : '© OpenTopoMap contributors',
    maxZoom: 17,
  },
};

let activeBasemapLayer = null;
let activeBasemapKey   = 'esri';

function initMap() {
  map = L.map('map', {
    center: [20, 0],
    zoom: 3,
    zoomControl: false,
    attributionControl: true,
  });

  // Default basemap
  const bm = BASEMAPS[activeBasemapKey];
  activeBasemapLayer = L.tileLayer(bm.url, { attribution: bm.attr, maxZoom: bm.maxZoom });
  activeBasemapLayer.addTo(map);

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
  const lp = document.getElementById('layersPanel');
  lp.style.display = 'block';
  lp.classList.remove('panel-hidden');
  const fb2 = document.getElementById('layersFloatBtn');
  if (fb2) fb2.style.display = 'none';
}

function addROIOverlayFromBbox(regionName, bbox) {
  // No zoom during analysis — zoom happens when first tile layer arrives
  // Just store bbox globally for reference
  window._currentBbox = bbox;
}

function addTileLayer(name, tileUrl, bbox, shouldZoom = false) {
  // GEE tile layer — interactive, pans/zooms correctly
  const tileLayer = L.tileLayer(tileUrl, {
    opacity    : 0.85,
    maxZoom    : 18,
    tileSize   : 256,
    attribution: 'Google Earth Engine',
  });
  tileLayer.addTo(map);

  // Zoom to this region's bbox only when explicitly requested (first layer of a new batch)
  if (shouldZoom && bbox) {
    const [w, s, e, n] = bbox;
    tileLayer.once('load', () => {
      map.fitBounds([[s, w], [n, e]], { padding: [40, 40] });
    });
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
    bbox   : bbox || null,
  });

  renderLayersList();
  updateLayerBadge(name);
  const panel = document.getElementById('layersPanel');
  panel.style.display = 'block';
  panel.classList.remove('panel-hidden');
  const fb = document.getElementById('layersFloatBtn');
  if (fb) fb.style.display = 'none';
  console.log('✓ Tile layer added:', name);
}

function toggleLayerVisibility(id) {
  const item = mapLayers.find(l => l.id === id);
  if (!item) return;
  if (item.visible) {
    // Hide: set opacity to 0 to preserve layer stack order
    if (item.layer.setOpacity) {
      item.layer.setOpacity(0);
    } else if (item.layer.setStyle) {
      item.layer.setStyle({ opacity: 0, fillOpacity: 0 });
    }
    item.visible = false;
  } else {
    // Show: restore opacity
    if (item.layer.setOpacity) {
      item.layer.setOpacity(0.85);
    } else if (item.layer.setStyle) {
      // Restore ROI vector style
      item.layer.setStyle({ opacity: 1, fillOpacity: 0.05, color: '#00d4b8', weight: 1.5 });
    }
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

  // Update floating button count badge
  const countEl = document.getElementById('layersFloatCount');
  if (countEl) {
    const n = mapLayers.length;
    countEl.textContent = n;
    countEl.classList.toggle('zero', n === 0);
  }

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
  const panel   = document.getElementById('layersPanel');
  const floatBtn = document.getElementById('layersFloatBtn');
  const isHidden = panel.classList.contains('panel-hidden') ||
                   panel.style.display === 'none' ||
                   getComputedStyle(panel).display === 'none';
  if (isHidden) {
    panel.style.display = 'block';
    panel.classList.remove('panel-hidden');
    if (floatBtn) floatBtn.style.display = 'none';
    renderLayersList();
  } else {
    panel.style.display = 'none';
    panel.classList.add('panel-hidden');
    if (floatBtn) floatBtn.style.display = 'flex';
  }
}

function toggleMapPanel() {
  const mp  = document.getElementById('mapPanel');
  const btn = document.getElementById('collapseMapBtn');
  mp.classList.toggle('collapsed');
  btn.style.transform = mp.classList.contains('collapsed') ? 'rotate(180deg)' : '';
}

// ════════════════════════════════════════════════════════
// BASEMAP SWITCHER
// ════════════════════════════════════════════════════════
function toggleBasemapMenu() {
  const menu = document.getElementById('basemapMenu');
  const isVisible = menu.style.display !== 'none';
  menu.style.display = isVisible ? 'none' : 'block';
  // Close on outside click
  if (!isVisible) {
    setTimeout(() => {
      document.addEventListener('click', closeBasemapMenuOnOutside, { once: true });
    }, 10);
  }
}

function closeBasemapMenuOnOutside(e) {
  const switcher = document.getElementById('basemapSwitcher');
  if (switcher && !switcher.contains(e.target)) {
    document.getElementById('basemapMenu').style.display = 'none';
  }
}

function switchBasemap(key) {
  if (key === activeBasemapKey) {
    document.getElementById('basemapMenu').style.display = 'none';
    return;
  }
  const bm = BASEMAPS[key];
  if (!bm) return;

  // Remove old basemap
  if (activeBasemapLayer) map.removeLayer(activeBasemapLayer);

  // Add new basemap at the bottom of the layer stack
  activeBasemapLayer = L.tileLayer(bm.url, { attribution: bm.attr, maxZoom: bm.maxZoom });
  activeBasemapLayer.addTo(map);
  activeBasemapLayer.bringToBack();

  // Update active state in menu
  document.querySelectorAll('.basemap-option').forEach(el => {
    el.classList.toggle('active', el.dataset.basemap === key);
  });

  activeBasemapKey = key;
  document.getElementById('basemapMenu').style.display = 'none';
  console.log('Basemap switched to:', key);
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
  const { region, start_date, end_date, variables, stats, layers, geo, insight, figures, var_insights, conclusion } = result;

  // Add new GEE tile layers on top of existing ones — do NOT clear previous layers.
  // Users can toggle or remove individual layers from the layers panel.
  // RGB goes last so it sits at the bottom of the new layer stack.
  if (layers && layers.length > 0) {
    console.log('Loading', layers.length, 'tile layers onto map');
    const sorted = [
      ...layers.filter(l =>  l.name.toLowerCase().includes('rgb') ||  l.name.toLowerCase().includes('true color')),
      ...layers.filter(l => !l.name.toLowerCase().includes('rgb') && !l.name.toLowerCase().includes('true color')),
    ];
    // Track existing tile count before adding so we only zoom on the first new layer
    const existingTileCount = mapLayers.filter(l => l.type === 'tile').length;
    sorted.forEach((lyr, i) => {
      console.log('Layer', i, lyr.name, 'type:', lyr.type, 'has tile_url:', !!lyr.tile_url);
      if (lyr.tile_url && lyr.type === 'tile') {
        const isRGB = lyr.name.toLowerCase().includes('rgb') || lyr.name.toLowerCase().includes('true color');
        // Skip duplicate RGB layers for the same region — only add if no existing RGB
        // layer already covers the same bbox (same region, different analysis run)
        if (isRGB && lyr.bbox) {
          const [w, s, e, n] = lyr.bbox;
          const alreadyHasRGB = mapLayers.some(existing => {
            if (!existing.bbox) return false;
            const [ew, es, ee, en] = existing.bbox;
            // Consider it a duplicate if bboxes overlap within ~0.01 degrees
            return (
              existing.name.toLowerCase().includes('rgb') ||
              existing.name.toLowerCase().includes('true color')
            ) && Math.abs(ew - w) < 0.01 && Math.abs(es - s) < 0.01 &&
               Math.abs(ee - e) < 0.01 && Math.abs(en - n) < 0.01;
          });
          if (alreadyHasRGB) {
            console.log('Skipping duplicate RGB layer for same region:', lyr.name);
            return;
          }
        }
        addTileLayer(lyr.name, lyr.tile_url, lyr.bbox, existingTileCount + i === existingTileCount);
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
  let html = buildResultHTML(region, start_date, end_date, variables, stats, layers, figures, var_insights || {}, conclusion || insight || '');
  appendAIMessage(html);
}

// ── Shared variable description map ──────────────────────────────────────────
const VAR_DESC_MAP = {
  'NDVI'   : 'Normalized Difference Vegetation Index (NDVI)',
  'EVI'    : 'Enhanced Vegetation Index (EVI)',
  'SAVI'   : 'Soil-Adjusted Vegetation Index (SAVI)',
  'NDWI'   : 'Normalized Difference Water Index (NDWI)',
  'MNDWI'  : 'Modified Normalized Difference Water Index (MNDWI)',
  'NDBI'   : 'Normalized Difference Built-up Index (NDBI)',
  'UI'     : 'Urban Index (UI)',
  'NBI'    : 'New Built-up Index (NBI)',
  'BSI'    : 'Bare Soil Index (BSI)',
  'NDSI'   : 'Normalized Difference Snow Index (NDSI)',
  'LST'    : 'Land Surface Temperature (LST)',
  'UHI'    : 'Urban Heat Island index (UHI)',
  'RGB'    : 'True Color composite (RGB)',
  'NO2'    : 'tropospheric NO₂ column density',
  'CO'     : 'carbon monoxide (CO) column density',
  'SO2'    : 'sulfur dioxide (SO₂) column density',
  'CH4'    : 'methane (CH₄) column mixing ratio',
  'O3'     : 'ozone (O₃) column density',
  'AEROSOL': 'absorbing aerosol index (AAI)',
  'GPP'    : 'Gross Primary Production (GPP)',
  'BURNED' : 'burned area detection',
  'FFPI'   : 'Fossil Fuel Pollution Index (FFPI)',
  'LULC'   : 'Land Use / Land Cover classification (LULC)',
};

function buildResultHTML(region, startDate, endDate, variables, stats, layers, figures, varInsights, conclusion) {
  const dateStr   = `${startDate} → ${endDate}`;
  const startYear = startDate.slice(0, 4);
  const endYear   = endDate.slice(0, 4);
  const sameYear  = startYear === endYear;
  const yearRange = sameYear ? startYear : `${startYear}–${endYear}`;
  const isMultiYear = startYear !== endYear;

  const atmoVars = ['no2','co','so2','ch4','o3','aerosol','gpp','burned','ffpi'];
  const isAtmo  = (variables || []).some(v => atmoVars.includes(v.toLowerCase()));
  const isMixed = (variables || []).some(v => atmoVars.includes(v.toLowerCase())) &&
                  (variables || []).some(v => !atmoVars.includes(v.toLowerCase()));
  const satellite = isMixed
    ? 'Landsat 8/9 and Sentinel-5P (Copernicus) satellite data'
    : isAtmo
      ? 'Sentinel-5P (Copernicus) satellite data'
      : 'Landsat 8/9 Collection 2 Level-2 Surface Reflectance data';
  const compositeType = isMultiYear ? 'a multi-year median composite' : 'a median composite';
  const compositeDesc = isMultiYear
    ? 'represents typical conditions over those periods'
    : 'represents the typical surface conditions over that period';

  const firstStats   = stats && Object.values(stats).find(s => s && s.monthly);
  const nMonths      = firstStats ? Object.keys(firstStats.monthly || {}).length : 0;
  const varFullNames = (variables || []).map(v => VAR_DESC_MAP[v.toUpperCase()] || v.toUpperCase()).join(' and ');

  let html = '';

  // ── HEADER ────────────────────────────────────────────────────────────────
  html += `<h3>Analysis Complete</h3>`;
  html += `<p>
    The analysis was completed for <strong>${escapeHtml(region)}</strong>, covering
    <strong>${startDate}</strong> to <strong>${endDate}</strong>.
    It used ${satellite} to compute ${varFullNames}.
    The result is ${compositeType}, so it ${compositeDesc}.
  </p>`;
  if (nMonths > 1) {
    html += `<p>
      <strong>${nMonths} monthly composites</strong> were processed across ${yearRange},
      enabling seasonal pattern analysis.
    </p>`;
  }

  // ── RGB OVERVIEW (once, at top) ───────────────────────────────────────────
  // For LULC-only analyses the rgb_overview lives inside the per-variable block below,
  // so skip it here to avoid rendering it twice.
  const allFigKeys = figures ? Object.keys(figures) : [];
  const isLulcOnly = allFigKeys.length === 1 && allFigKeys[0].toUpperCase() === 'LULC';
  const firstFig = !isLulcOnly && figures && Object.values(figures).find(f => f && f.rgb_overview && f !== figures['LULC']);
  if (firstFig && firstFig.rgb_overview) {
    html += `<div class="result-section-label">Study Area</div>`;
    html += `<div class="result-img-wrap">
      <img src="${firstFig.rgb_overview}" class="result-img" loading="lazy"/>
      <div class="result-img-caption">Study Area Overview (${escapeHtml(region)}) — True Color RGB</div>
    </div>`;
  }

  // ── PER-VARIABLE STORY BLOCKS ─────────────────────────────────────────────
  if (figures && Object.keys(figures).length > 0) {
    for (const [varLabel, fig] of Object.entries(figures)) {
      if (!fig) continue;
      const varStats   = stats && stats[varLabel];
      const varInsight = varInsights && varInsights[varLabel];
      const isLULC     = varLabel.toUpperCase() === 'LULC';

      html += `<div class="var-section">`;

      // For LULC: show RGB overview first (same as non-LULC vars), then LULC map
      if (isLULC) {
        // 1a. RGB overview (same as study area block for other vars)
        if (fig.rgb_overview) {
          html += `<div class="result-section-label">Study Area</div>`;
          html += `<div class="result-img-wrap">
            <img src="${fig.rgb_overview}" class="result-img" loading="lazy"/>
            <div class="result-img-caption">Study Area Overview (${escapeHtml(region)}) — True Color RGB</div>
          </div>`;
        }
        // 1b. LULC analysis map
        if (fig.analysis_map) {
          html += `<div class="result-section-label">Land Cover Map</div>`;
          html += `<div class="result-img-wrap">
            <img src="${fig.analysis_map}" class="result-img" loading="lazy"/>
            <div class="result-img-caption">Land Cover Classification — ${escapeHtml(region)} · ${dateStr}</div>
          </div>`;
        }
        // 2. Stats table
        if (varStats) {
          html += buildSingleStatHTML(varLabel, varStats);
        }
        // 3. Charts: only pie chart (bar chart removed)
        if (fig.charts && fig.charts.length > 0) {
          const lulcPie = fig.charts.find(c => c[0] === 'lulc_pie');
          if (lulcPie) {
            html += `<div class="result-section-label" style="margin-top:16px">Area Distribution</div>`;
            html += `<div class="result-img-wrap">
              <img src="${lulcPie[1]}" class="result-img" loading="lazy"/>
            </div>`;
          }
        }
        // 4. LULC text explanation (auto-computed from class stats)
        if (varStats && varStats.classes) {
          html += buildLulcExplanation(varStats);
        }
        // 5. AI insight
        if (varInsight) {
          html += `<div class="var-insight-block">
            <div class="var-insight-text">${escapeHtml(varInsight)}</div>
          </div>`;
        }

      } else {
        // ── Non-LULC variables (NDVI, LST, etc.) — original layout ────────────

        // 1. Analysis Map
        if (fig.analysis_map) {
          html += `<div class="result-section-label">${escapeHtml(varLabel)} Map</div>`;
          html += `<div class="result-img-wrap">
            <img src="${fig.analysis_map}" class="result-img" loading="lazy"/>
            <div class="result-img-caption">${escapeHtml(varLabel)} — ${escapeHtml(region)} · ${dateStr}</div>
          </div>`;
        }

        // 2. Stats table (below the map)
        if (varStats) {
          html += buildSingleStatHTML(varLabel, varStats);
        }

        // 3. Map-level AI insight
        if (varInsight) {
          html += `<div class="var-insight-block">
            <div class="var-insight-text">${escapeHtml(varInsight)}</div>
          </div>`;
        }

        // 4. Charts: monthly first (full width + highlights), then dist+class side-by-side
        if (fig.charts && fig.charts.length > 0) {
          const charts   = fig.charts;
          const monthly  = charts.find(c => c[0] === 'monthly_trend');
          const hist     = charts.find(c => c[0] === 'histogram');
          const classBar = charts.find(c => c[0] === 'class_bar');

          // Monthly trend chart
          if (monthly) {
            html += `<div class="result-section-label" style="margin-top:16px">Monthly Trend</div>`;
            html += `<div class="result-img-wrap">
              <img src="${monthly[1]}" class="result-img" loading="lazy"/>
            </div>`;
            if (varStats && varStats.monthly && Object.keys(varStats.monthly).length > 0) {
              html += buildMonthlyHighlights(varLabel, varStats.monthly);
            }
          }

          // Distribution + Class bar side by side
          const sideBySide = [hist, classBar].filter(Boolean);
          if (sideBySide.length > 0) {
            html += `<div class="result-section-label" style="margin-top:16px">Distribution &amp; Class Composition</div>`;
            if (sideBySide.length === 2) {
              html += `<div class="result-charts-row">`;
              for (const chart of sideBySide) {
                html += `<div class="result-chart-cell">
                  <img src="${chart[1]}" class="result-img" loading="lazy"/>
                </div>`;
              }
              html += `</div>`;
              if (varStats) {
                html += buildDistClassExplanation(varLabel, varStats);
              }
            } else {
              const chart = sideBySide[0];
              html += `<div class="result-img-wrap">
                <img src="${chart[1]}" class="result-img" loading="lazy"/>
              </div>`;
              if (varStats) {
                html += buildDistClassExplanation(varLabel, varStats);
              }
            }
          }

          // Any other chart types
          const shown = new Set([monthly, hist, classBar].filter(Boolean).map(c => c[0]));
          for (const [type, b64] of charts) {
            if (!shown.has(type)) {
              html += `<div class="result-img-wrap">
                <img src="${b64}" class="result-img" loading="lazy"/>
              </div>`;
            }
          }
        }
      }

      html += `</div>`; // end .var-section
    }
  }

  // ── CONCLUSION ────────────────────────────────────────────────────────────
  if (conclusion) {
    html += `<div class="conclusion-block">
      <div class="conclusion-header">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="10"/>
          <path d="M12 16v-4M12 8h.01"/>
        </svg>
        Conclusion
      </div>
      <div class="conclusion-text">${escapeHtml(conclusion)}</div>
    </div>`;
  }

  // ── ATTRIBUTIONS ─────────────────────────────────────────────────────────
  const methodStr = isMultiYear
    ? `Multi-year median composite (${yearRange})`
    : `Median composite (${startYear})`;
  html += `<div class="result-attribution">
    <div class="attr-title">Attributions</div>
    <ul class="attr-list">
      <li>Data source: ${satellite}</li>
      <li>Platform: Google Earth Engine</li>
      <li>Method: ${methodStr}</li>
      <li>Region: ${escapeHtml(region)}</li>
      <li>Time period: ${startDate} – ${endDate}</li>
      <li>Analysis date: ${new Date().toISOString().slice(0,10)}</li>
    </ul>
  </div>`;

  return html;
}

// ── Build stats table for a single variable ───────────────────────────────────
function buildSingleStatHTML(varLabel, s) {
  if (!s) return '';
  let html = '';

  // LULC classes
  if (s.classes) {
    html += `<div class="stats-table-wrap">`;
    html += `<table class="stats-table">
      <thead><tr><th>Land Cover Class</th><th>Area (ha)</th><th>Share</th></tr></thead>
      <tbody>`;
    // Sort by percentage descending (largest share first)
    Object.entries(s.classes)
      .sort((a, b) => b[1].percentage - a[1].percentage)
      .forEach(([cls, info]) => {
      const pct = info.percentage.toFixed(1);
      html += `<tr>
        <td><span class="lulc-dot" style="background:${info.color || '#00d4b8'}"></span>${escapeHtml(cls)}</td>
        <td>${(info.hectares || 0).toLocaleString()}</td>
        <td>
          <div class="pct-bar-wrap">
            <div class="pct-bar" style="width:${Math.min(pct,100)}%"></div>
            <span>${pct}%</span>
          </div>
        </td>
      </tr>`;
    });
    html += `</tbody></table>`;
    if (s.total_ha) html += `<div class="stats-total">Total: ${s.total_ha.toLocaleString()} ha across ${s.n_classes} classes</div>`;
    html += `</div>`;
    return html;
  }

  // UHI special
  if (s.lst_mean !== undefined) {
    html += `<div class="stats-table-wrap">
      <table class="stats-table">
        <tbody>
          <tr><td>LST Mean</td><td>${s.lst_mean.toFixed(2)}°C</td></tr>
          <tr><td>LST Std Dev</td><td>${s.lst_std.toFixed(2)}°C</td></tr>
          <tr><td>UHI</td><td>z-score normalised</td></tr>
        </tbody>
      </table>
    </div>`;
    return html;
  }

  // Standard numeric stats
  if (s.mean !== null && s.mean !== undefined) {
    const fmt = v => v != null ? v.toFixed(4) : '—';
    html += `<div class="stats-table-wrap">
      <table class="stats-table">
        <tbody>
          <tr><td>Mean</td><td>${fmt(s.mean)}</td></tr>
          <tr><td>Median</td><td>${fmt(s.median)}</td></tr>
          <tr><td>Std Dev</td><td>${fmt(s.std)}</td></tr>
          <tr><td>Min / Max</td><td>${fmt(s.min)} / ${fmt(s.max)}</td></tr>
          <tr><td>P10 / P90</td><td>${fmt(s.p10)} / ${fmt(s.p90)}</td></tr>
        </tbody>
      </table>
    </div>`;
  }
  return html;
}

// ── Monthly highlights auto-computed from monthly stats ───────────────────────
function buildMonthlyHighlights(varLabel, monthly) {
  if (!monthly || Object.keys(monthly).length < 2) return '';

  const MONTH_NAMES = {
    '01':'Jan','02':'Feb','03':'Mar','04':'Apr','05':'May','06':'Jun',
    '07':'Jul','08':'Aug','09':'Sep','10':'Oct','11':'Nov','12':'Dec'
  };

  const entries  = Object.entries(monthly).sort((a,b) => a[0].localeCompare(b[0]));
  const values   = entries.map(e => e[1]);
  const maxEntry = entries.reduce((a,b) => b[1] > a[1] ? b : a);
  const minEntry = entries.reduce((a,b) => b[1] < a[1] ? b : a);
  const avg      = values.reduce((s,v) => s + v, 0) / values.length;
  const range    = maxEntry[1] - minEntry[1];

  const fmtMonth = key => {
    const [yr, mo] = key.split('-');
    return `${MONTH_NAMES[mo] || mo} ${yr}`;
  };
  const fmt4 = v => v.toFixed(4);

  // Trend direction (compare first half vs second half)
  const half   = Math.floor(values.length / 2);
  const avgFirst = values.slice(0, half).reduce((s,v) => s+v, 0) / half;
  const avgLast  = values.slice(half).reduce((s,v) => s+v, 0) / (values.length - half);
  const trend    = avgLast > avgFirst + 0.002 ? '↑ increasing' : avgLast < avgFirst - 0.002 ? '↓ decreasing' : '→ stable';

  return `<div class="monthly-highlights">
    <div class="mh-item mh-peak">
      <span class="mh-label">Peak</span>
      <span class="mh-value">${fmtMonth(maxEntry[0])} — ${fmt4(maxEntry[1])}</span>
    </div>
    <div class="mh-item mh-low">
      <span class="mh-label">Lowest</span>
      <span class="mh-value">${fmtMonth(minEntry[0])} — ${fmt4(minEntry[1])}</span>
    </div>
    <div class="mh-item">
      <span class="mh-label">Period avg</span>
      <span class="mh-value">${fmt4(avg)}</span>
    </div>
    <div class="mh-item">
      <span class="mh-label">Range</span>
      <span class="mh-value">${fmt4(range)}</span>
    </div>
    <div class="mh-item">
      <span class="mh-label">Trend</span>
      <span class="mh-value">${trend}</span>
    </div>
  </div>`;
}

// ── Distribution + class explanation (auto-computed, no LLM) ─────────────────
function buildDistClassExplanation(varLabel, s) {
  if (!s || s.mean == null) return '';

  const fmt    = v => v != null ? v.toFixed(4) : '—';
  const fmtLST = v => v != null ? v.toFixed(2) : '—';
  const spread = s.p90 != null && s.p10 != null ? (s.p90 - s.p10).toFixed(4) : null;
  const isLST  = varLabel.toUpperCase().includes('LST');

  // ── Distribution sentence ────────────────────────────────────────────────
  let text = isLST
    ? `The distribution centers around a mean of <strong>${fmtLST(s.mean)}°C</strong>`
    : `The distribution centers around a mean of <strong>${fmt(s.mean)}</strong>`;
  if (s.median != null) text += isLST ? ` (median ${fmtLST(s.median)}°C)` : ` (median ${fmt(s.median)})`;
  if (s.std    != null) text += isLST
    ? `, with a standard deviation of <strong>${fmtLST(s.std)}°C</strong>`
    : `, with a standard deviation of <strong>${fmt(s.std)}</strong>`;
  text += '. ';

  if (spread) {
    text += isLST
      ? `The interquartile spread from P10 (${fmtLST(s.p10)}°C) to P90 (${fmtLST(s.p90)}°C) `
      : `The interquartile spread from P10 (${fmt(s.p10)}) to P90 (${fmt(s.p90)}) `;
    const spreadVal = parseFloat(spread);
    if (isLST) {
      if (spreadVal < 5)       text += 'is narrow, indicating spatially uniform surface temperatures.';
      else if (spreadVal < 12) text += 'shows moderate spatial variability, with cooler vegetated areas and warmer built surfaces coexisting.';
      else                     text += 'is wide, pointing to significant spatial contrasts — hotspots and low-value zones coexist.';
    } else {
      if (spreadVal < 0.1)       text += 'is narrow, indicating spatially uniform conditions.';
      else if (spreadVal < 0.25) text += 'shows moderate spatial variability across the region.';
      else                       text += 'is wide, pointing to significant spatial contrasts — hotspots and low-value zones coexist.';
    }
    text += ' ';
  }

  // ── NDVI class note ──────────────────────────────────────────────────────
  if (varLabel.toUpperCase().includes('NDVI') && s.mean != null) {
    const m = s.mean;
    if (m < 0.1)      text += 'The area is predominantly bare or non-vegetated.';
    else if (m < 0.3) text += 'Vegetation is sparse to moderately stressed across most of the area.';
    else if (m < 0.5) text += 'Moderate vegetation cover dominates, typical of mixed urban-green areas.';
    else              text += 'Dense, healthy vegetation is the dominant land signal.';
  }

  // ── Other index class notes ──────────────────────────────────────────────
  if (varLabel.toUpperCase().includes('NDBI') && s.mean != null) {
    const m = s.mean;
    if (m < -0.1)      text += 'The area is predominantly non-built, with vegetation or natural surfaces dominating.';
    else if (m < 0.0)  text += 'Low to moderate built-up density — typical of mixed urban-suburban zones.';
    else if (m < 0.1)  text += 'Moderate built-up intensity indicates significant impervious surface coverage.';
    else               text += 'High built-up index signals a densely urbanized landscape with limited permeable surfaces.';
  }

  if ((varLabel.toUpperCase().includes('NDWI') || varLabel.toUpperCase().includes('MNDWI')) && s.mean != null) {
    const m = s.mean;
    if (m < -0.3)     text += 'Dry land conditions dominate — water bodies are sparse or absent.';
    else if (m < 0.0) text += 'Transition zone between dry and moist surfaces — some water bodies or soil moisture present.';
    else if (m < 0.3) text += 'Moist conditions or shallow water bodies are present across a notable portion of the area.';
    else              text += 'Open water or high moisture content dominates the landscape.';
  }

  if (varLabel.toUpperCase().includes('BSI') && s.mean != null) {
    const m = s.mean;
    if (m < -0.1)    text += 'The area is largely vegetated with minimal bare soil exposure.';
    else if (m < 0.1) text += 'Mixed conditions — bare soil coexists with vegetated and built surfaces.';
    else              text += 'Bare soil dominates, indicating degraded land, agricultural fields, or active construction.';
  }

  if (varLabel.toUpperCase() === 'UI' && s.mean != null) {
    const m = s.mean;
    if (m < -0.1)    text += 'Vegetation dominates — the urban footprint is relatively low.';
    else if (m < 0.1) text += 'Transitional landscape mixing urban surfaces and green cover.';
    else              text += 'Urban surfaces dominate, consistent with a densely developed area.';
  }

  if ((varLabel.toUpperCase().includes('EVI') || varLabel.toUpperCase().includes('SAVI')) && s.mean != null) {
    const m = s.mean;
    if (m < 0.1)      text += 'Sparse vegetation signal — bare or heavily degraded land surface.';
    else if (m < 0.3) text += 'Low to moderate vegetation density, with stressed or patchy canopy cover.';
    else if (m < 0.5) text += 'Moderate vegetation productivity — mixed urban-green or agricultural landscapes.';
    else              text += 'Dense and productive vegetation cover, indicating healthy forest or cropland.';
  }

  // ── Atmospheric class notes ──────────────────────────────────────────────
  if (varLabel.toUpperCase().includes('NO2') && s.mean != null) {
    const m = s.mean;
    const p90 = s.p90 || m;
    text += `With a mean of <strong>${m.toExponential(2)} mol/m²</strong>, `;
    if (m < 0.00008)       text += 'NO₂ levels are relatively low, suggesting limited local combustion sources.';
    else if (m < 0.00015)  text += 'moderate NO₂ concentrations indicate active traffic and industrial emissions in the region.';
    else                   text += 'elevated NO₂ points to significant combustion activity — likely dense traffic corridors and industrial zones.';
    if (p90 > m * 1.3) text += ` The P90 value of ${p90.toExponential(2)} mol/m² highlights localized hotspots where emissions are substantially higher than the regional average.`;
  }

  if (varLabel.toUpperCase().includes('CO') && s.mean != null) {
    const m = s.mean;
    text += `The mean CO column density of <strong>${m.toExponential(2)} mol/m²</strong> `;
    if (m < 0.03)      text += 'is within background levels, suggesting limited local combustion activity.';
    else if (m < 0.06) text += 'indicates moderate CO loading, consistent with urban traffic and biomass burning.';
    else               text += 'is elevated, pointing to significant combustion sources — vehicles, industry, or fire activity.';
  }

  if (varLabel.toUpperCase().includes('SO2') && s.mean != null) {
    const m = s.mean;
    text += `SO₂ mean of <strong>${m.toExponential(2)} mol/m²</strong> `;
    if (m < 0.0002)    text += 'is near background — industrial and volcanic sources appear limited.';
    else if (m < 0.001) text += 'suggests moderate sulfur emissions, potentially from industrial facilities or coal combustion.';
    else               text += 'is high, indicative of significant industrial activity, power plants, or volcanic degassing.';
  }

  if (varLabel.toUpperCase().includes('CH4') && s.mean != null) {
    const m = s.mean;
    text += `Methane mixing ratios average <strong>${m.toFixed(0)} ppb</strong>. `;
    if (m < 1850)      text += 'Values are near the global background, suggesting limited local CH₄ sources.';
    else if (m < 1900) text += 'Slightly elevated CH₄ may indicate agricultural activity, wetlands, or landfill emissions.';
    else               text += 'Elevated CH₄ signals significant biogenic or anthropogenic sources such as rice paddies, livestock, or waste sites.';
  }

  if (varLabel.toUpperCase().includes('AEROSOL') && s.mean != null) {
    const m = s.mean;
    text += `The absorbing aerosol index (AAI) mean of <strong>${fmt(s.mean)}</strong> `;
    if (m < 0)         text += 'is negative, typical of marine aerosols or clean background air.';
    else if (m < 1)    text += 'is low, indicating minor aerosol loading with limited impact on air quality.';
    else if (m < 2)    text += 'indicates moderate aerosol loading — possible smoke, dust, or urban haze.';
    else               text += 'is high, pointing to significant absorbing aerosols from biomass burning, dust storms, or industrial smoke.';
  }

  // ── LST heat class note ──────────────────────────────────────────────────
  if (isLST && s.mean != null) {
    const mean = s.mean;
    const p90  = s.p90  || mean;
    const p10  = s.p10  || mean;

    // Infer dominant heat class from mean
    let dominantClass;
    if      (mean < 30) { dominantClass = 'cool (<30°C)';       }
    else if (mean < 35) { dominantClass = 'moderate (30–35°C)'; }
    else if (mean < 40) { dominantClass = 'warm (35–40°C)';     }
    else if (mean < 45) { dominantClass = 'hot (40–45°C)';      }
    else                { dominantClass = 'extreme (>45°C)';     }

    text += `The mean surface temperature places the region predominantly in the <strong>${dominantClass}</strong> thermal class. `;

    // Hotspot warning if P90 crosses a danger threshold
    if (p90 >= 45) {
      text += `The P90 value of <strong>${fmtLST(p90)}°C</strong> indicates that a significant portion of the landscape reaches extreme heat levels, posing risks for outdoor comfort and urban infrastructure. `;
    } else if (p90 >= 40) {
      text += `The P90 value of <strong>${fmtLST(p90)}°C</strong> shows that hot surface zones are present, likely concentrated over impervious surfaces such as roads, rooftops, and industrial areas. `;
    }

    // Cool refuges note if P10 is meaningfully cooler
    if (p90 - p10 > 6) {
      text += `Cooler zones near <strong>${fmtLST(p10)}°C</strong> (P10) likely correspond to vegetated parks, water bodies, or shaded areas that act as thermal refuges within the urban fabric.`;
    }
  }

  return `<div class="dist-explanation">${text}</div>`;
}

// ── LULC class explanation (auto-computed from class stats) ──────────────────
function buildLulcExplanation(s) {
  if (!s || !s.classes) return '';

  const classes  = s.classes;
  const totalHa  = s.total_ha || 0;
  const nClasses = s.n_classes || Object.keys(classes).length;

  // Sort classes by percentage descending
  const sorted = Object.entries(classes)
    .sort((a, b) => b[1].percentage - a[1].percentage);

  if (sorted.length === 0) return '';

  const [topName, topInfo]    = sorted[0];
  const [secName, secInfo]    = sorted[1] || [null, null];

  const topPct = topInfo.percentage.toFixed(1);
  const topHa  = (topInfo.hectares || 0).toLocaleString();

  let text = `The land cover is dominated by <strong>${topName}</strong>, covering <strong>${topPct}%</strong> (${topHa} ha) of the study area`;

  if (secName && secInfo) {
    text += `, followed by <strong>${secName}</strong> at <strong>${secInfo.percentage.toFixed(1)}%</strong>`;
  }
  text += `. `;

  if (totalHa > 0) {
    text += `The total mapped area spans <strong>${totalHa.toLocaleString()} ha</strong> across ${nClasses} land cover classes. `;
  }

  // Contextual note based on dominant class
  const topKey = topName.toLowerCase();
  if (topKey.includes('built') || topKey.includes('urban') || topKey.includes('impervious')) {
    text += `The high proportion of built-up surface indicates a highly urbanized landscape, which is associated with reduced permeability, elevated surface temperatures, and diminished green space.`;
  } else if (topKey.includes('tree') || topKey.includes('forest') || topKey.includes('vegetation')) {
    text += `The prevalence of tree/forest cover suggests a landscape with significant vegetative carbon storage and biodiversity value, though fragmentation pressure from surrounding land uses warrants monitoring.`;
  } else if (topKey.includes('water')) {
    text += `The dominance of water bodies reflects the aquatic character of this region, with implications for flood risk, aquatic biodiversity, and local microclimate regulation.`;
  } else if (topKey.includes('crop') || topKey.includes('agric') || topKey.includes('farm')) {
    text += `Agricultural land is the primary land use, highlighting the region's role in food production and the importance of monitoring soil health and irrigation patterns.`;
  } else if (topKey.includes('bare') || topKey.includes('soil')) {
    text += `Bare or sparsely vegetated surfaces dominate, indicating degraded land, active construction, or arid conditions that may accelerate erosion and surface heating.`;
  }

  return `<div class="dist-explanation">${text}</div>`;
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
    setLayout(chatW > 0 ? chatW : Math.round(window.innerWidth * 0.60) - NAV_W);
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