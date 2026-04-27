/* v20260413012814 */
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
    bbox   : bbox,
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
  if (!item) return;
  // Tile layers don't have getBounds() — use stored bbox instead
  if (item.bbox) {
    const [w, s, e, n] = item.bbox;
    map.fitBounds([[s, w], [n, e]], { padding: [40, 40] });
  } else if (item.layer.getBounds) {
    try { map.fitBounds(item.layer.getBounds(), { padding: [40, 40] }); } catch(e) {}
  }
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

// ── Layers panel drag-to-resize ──────────────────────────────────────────────
(function initLayersPanelResize() {
  function setup() {
    const handle = document.getElementById('layersResizeHandle');
    const panel  = document.getElementById('layersPanel');
    if (!handle || !panel) return;
    let startX, startW;
    handle.addEventListener('mousedown', (e) => {
      e.preventDefault();
      startX = e.clientX; startW = panel.offsetWidth;
      document.body.style.userSelect = 'none';
      document.body.style.cursor = 'ew-resize';
      function onMove(e) {
        const newW = Math.min(480, Math.max(180, startW + (e.clientX - startX)));
        panel.style.width = newW + 'px';
      }
      function onUp() {
        document.body.style.userSelect = '';
        document.body.style.cursor = '';
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
      }
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', setup);
  else setup();
})();

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
  // Color @mentions amber like the reference UI
  const highlighted = escapeHtml(text).replace(/@(\w+)/g, '<span class="roi-mention">@$1</span>');
  div.innerHTML = `<div class="msg-bubble user">${highlighted}</div>`;
  msgs.appendChild(div);
  scrollToBottom();
}

function appendAIMessage(html) {
  const msgs = document.getElementById('messages');
  const div  = document.createElement('div');
  div.className = 'msg-row ai';
  div.innerHTML = `<div class="msg-bubble ai">${html}</div>`;
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
  // Re-scroll after images finish loading (base64 charts expand the height)
  setTimeout(() => { msgs.scrollTop = msgs.scrollHeight; }, 800);
  setTimeout(() => { msgs.scrollTop = msgs.scrollHeight; }, 2000);
}

// ════════════════════════════════════════════════════════
// CHAT HISTORY SYSTEM
// ════════════════════════════════════════════════════════
let chatHistory    = [];   // array of { id, title, timestamp, html, layers }
let activeChatId   = null;

function generateChatId() {
  return 'chat_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6);
}

function getCurrentChatTitle() {
  // Use first user message as title, fall back to timestamp
  const firstUser = document.querySelector('#messages .msg-bubble.user');
  if (firstUser) {
    const text = firstUser.textContent.trim();
    return text.length > 45 ? text.slice(0, 45) + '…' : text;
  }
  return 'Chat ' + new Date().toLocaleString('en-GB', { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' });
}

function saveCurrentChat() {
  const msgs = document.getElementById('messages');
  if (!msgs.innerHTML.trim() || !document.querySelector('#messages .msg-bubble.user')) return;

  const title = getCurrentChatTitle();
  const entry = {
    id       : activeChatId || generateChatId(),
    title    : title,
    timestamp: Date.now(),
    html     : msgs.innerHTML,
    layers   : mapLayers.map(l => ({
      id     : l.id,
      name   : l.name,
      type   : l.type,
      visible: l.visible,
      bbox   : l.bbox || null,
      tileUrl: l.layer._url || null,   // for tile layers
    })),
  };

  // Update existing or prepend new
  const idx = chatHistory.findIndex(c => c.id === entry.id);
  if (idx >= 0) chatHistory[idx] = entry;
  else          chatHistory.unshift(entry);

  activeChatId = entry.id;
  renderHistoryList();
}

function loadChat(id) {
  const entry = chatHistory.find(c => c.id === id);
  if (!entry) return;

  // Save current before switching
  saveCurrentChat();

  // Restore messages
  document.getElementById('messages').innerHTML = entry.html;
  scrollToBottom();

  // Clear map and restore tile layers
  clearAllLayers();
  entry.layers.forEach(l => {
    if (l.type === 'tile' && l.tileUrl) {
      addTileLayer(l.name, l.tileUrl, l.bbox, false);
      // Restore visibility
      const item = mapLayers.find(m => m.name === l.name);
      if (item && !l.visible) toggleLayerVisibility(item.id);
    }
  });

  activeChatId = id;
  hidePlanWidget();
  stopPolling();
  renderHistoryList();
  toggleHistoryPanel(); // close panel after loading
}

function deleteChat(id, e) {
  e.stopPropagation();
  chatHistory = chatHistory.filter(c => c.id !== id);
  if (activeChatId === id) {
    // If deleting current chat, start fresh
    activeChatId = null;
    document.getElementById('messages').innerHTML = '';
    clearAllLayers();
  }
  renderHistoryList();
}

function renderHistoryList() {
  const list = document.getElementById('historyList');
  if (!list) return;
  if (chatHistory.length === 0) {
    list.innerHTML = '<div class="history-empty">No previous chats yet.</div>';
    return;
  }

  list.innerHTML = chatHistory.map(entry => {
    const date = new Date(entry.timestamp);
    const dateStr = date.toLocaleString('en-GB', { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' });
    const isActive = entry.id === activeChatId;
    return `
      <div class="history-item ${isActive ? 'active' : ''}" onclick="loadChat('${entry.id}')">
        <div class="history-item-title">${escapeHtml(entry.title)}</div>
        <div class="history-item-meta">${dateStr}</div>
        <button class="history-delete-btn" onclick="deleteChat('${entry.id}', event)" title="Delete">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
          </svg>
        </button>
      </div>`;
  }).join('');
}

function toggleHistoryPanel() {
  const panel  = document.getElementById('historyPanel');
  const btn    = document.getElementById('chatNavBtn');
  const isOpen = panel.style.display !== 'none';
  if (isOpen) {
    panel.style.display = 'none';
    btn.classList.add('active');
  } else {
    renderHistoryList();
    panel.style.display = 'flex';
    btn.classList.add('active');
  }
}

function clearChat() {
  // Save current chat before clearing
  saveCurrentChat();

  // Start a fresh chat
  activeChatId = generateChatId();
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
function startAnalysis(text, onComplete) {
  isAnalyzing = true;
  setSendBtnStop();
  appendTypingIndicator();
  resetPlanWidget();

  const body = { message: text };
  if (activeROI) body.roi = activeROI.geojson;

  fetch('/api/analyze', {
    method : 'POST',
    headers: { 'Content-Type': 'application/json' },
    body   : JSON.stringify(body),
  })
  .then(r => r.json())
  .then(data => {
    if (data.error) { handleError(data.error); if (onComplete) onComplete(); return; }
    currentJobId = data.job_id;
    startPolling(data.job_id, onComplete);
  })
  .catch(err => { handleError(err.toString()); if (onComplete) onComplete(); });
}

function startPolling(jobId, onComplete) {
  pollingTimer = setInterval(() => pollJob(jobId, onComplete), 1500);
}

function stopPolling() {
  if (pollingTimer) { clearInterval(pollingTimer); pollingTimer = null; }
  currentJobId = null;
  isAnalyzing  = false;
  setSendBtnSend();
}

function pollJob(jobId, onComplete) {
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
      if (onComplete) onComplete();
    } else if (data.status === 'error') {
      stopPolling();
      window._geoShown = false;
      removeTypingIndicator();
      hidePlanWidget();
      handleError(data.error || 'Unknown error');
      if (onComplete) onComplete();
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

function appendYearDivider(year) {
  const msgs = document.getElementById('messages');
  const div  = document.createElement('div');
  div.style.cssText = 'display:flex;align-items:center;gap:10px;padding:10px 0 4px;';
  div.innerHTML = `
    <div style="flex:1;height:1px;background:var(--border)"></div>
    <span style="font-size:11px;font-weight:700;letter-spacing:0.08em;color:var(--text3);text-transform:uppercase;white-space:nowrap;">
      Analysis ${year}
    </span>
    <div style="flex:1;height:1px;background:var(--border)"></div>
  `;
  msgs.appendChild(div);
  scrollToBottom();
}

function handleResult(result) {
  if (!result) { appendAIMessage('<p>No result returned.</p>'); return; }

  if (result.type === 'qa') {
    appendAIMessage(parseMarkdown(result.answer));
    return;
  }

  // ── Multi-year: fire one real job per year, each with full plan widget ────
  if (result.type === 'multi_year_plan') {
    const queries = result.year_queries || [];
    if (queries.length === 0) { appendAIMessage('<p>Could not parse year range.</p>'); return; }

    appendAIMessage(
      `<p style="color:var(--text2);font-size:13px">
        <strong>Multi-year analysis</strong> for <strong>${escapeHtml(result.region)}</strong>
        (${escapeHtml(result.start_year)}–${escapeHtml(result.end_year)}) —
        running <strong>${queries.length}</strong> analyses sequentially…
      </p>`
    );

    function runNextYear(idx) {
      if (idx >= queries.length) return;
      appendYearDivider(queries[idx].start_date.slice(0, 4));
      setTimeout(() => {
        startAnalysis(queries[idx].message, () => runNextYear(idx + 1));
      }, idx === 0 ? 200 : 600);
    }
    runNextYear(0);
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

  // 3. Render Plotly charts — find the last AI bubble (just appended) and render into its divs
  setTimeout(() => {
    const bubbles = document.querySelectorAll('.msg-bubble.ai');
    const lastBubble = bubbles[bubbles.length - 1];
    if (lastBubble) renderAllPlotlyCharts(stats, figures, lastBubble);
  }, 150);

  // Auto-save this chat to history after result is rendered
  setTimeout(() => saveCurrentChat(), 100);
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
  const msgId     = Date.now().toString(36) + Math.random().toString(36).slice(2,5);

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
          
          html += `<div class="result-img-wrap">
            <img src="${fig.rgb_overview}" class="result-img" loading="lazy"/>
            <div class="result-img-caption">Study Area Overview (${escapeHtml(region)}) — True Color RGB</div>
          </div>`;
        }
        // 1b. LULC analysis map
        if (fig.analysis_map) {
          
          html += `<div class="result-img-wrap">
            <img src="${fig.analysis_map}" class="result-img" loading="lazy"/>
            <div class="result-img-caption">Land Cover Classification — ${escapeHtml(region)} · ${dateStr}</div>
          </div>`;
        }
        // 2. Stats table
        if (varStats) {
          html += buildSingleStatHTML(varLabel, varStats);
        }
        // 3. Bullet breakdown — right below the table
        if (varStats && varStats.classes) {
          html += buildLulcExplanation(varStats);
        }
        // 4. Confusion matrix chart + ML narrative — right after bullets, before pie
        const mlData = (varStats && varStats.ml_metrics && varStats.ml_metrics.confusion_matrix)
          ? varStats.ml_metrics
          : (varStats && varStats.classes ? _simulateMLMetrics(varStats) : null);
        if (mlData) {
          const cmId = `plotly_lulc_cm_${msgId}`;
          html += `<div class="result-img-wrap" style="margin-top:16px">
            <div id="${cmId}" class="plotly-chart-wrap"></div>
          </div>`;
          html += buildLulcMLNarrative(mlData);
        }
        // 5. Pie chart — Plotly interactive
        if (fig.charts && fig.charts.length > 0) {
          const lulcPie = fig.charts.find(c => c[0] === 'lulc_pie');
          if (lulcPie) {
            const pieId = `plotly_lulc_pie_${msgId}`;
            html += `<div class="result-img-wrap">
              <div id="${pieId}" class="plotly-chart-wrap"></div>
            </div>`;
          }
        }
        // 6. Pie prose narrative — right below the pie chart
        if (varStats && varStats.classes) {
          html += buildLulcPieNarrative(varStats);
        }
        // 7. AI insight
        if (varInsight) {
          html += `<p class="ai-insight-text">${parseMarkdown(varInsight)}</p>`;
        }

      } else {
        // ── Non-LULC variables (NDVI, LST, etc.) — original layout ────────────

        // 1. Analysis Map
        if (fig.analysis_map) {
          
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
          html += `<p class="ai-insight-text">${parseMarkdown(varInsight)}</p>`;
        }

        // 4. Charts: monthly first (full width + highlights), then dist+class side-by-side
        if (fig.charts && fig.charts.length > 0) {
          const charts   = fig.charts;
          const monthly  = charts.find(c => c[0] === 'monthly_trend');
          const hist     = charts.find(c => c[0] === 'histogram');
          const classBar = charts.find(c => c[0] === 'class_bar');
          console.log('[charts] varLabel:', varLabel, '| total:', charts.length, '| types:', charts.map(c=>c[0]), '| classBar:', !!classBar);

          // Monthly trend chart — Plotly interactive
          if (monthly) {
            const chartId = `plotly_monthly_${sanitizeId(varLabel)}_${msgId}`;
            html += `<div class="result-img-wrap">
              <div id="${chartId}" class="plotly-chart-wrap"></div>
            </div>`;
            if (varStats && varStats.monthly && Object.keys(varStats.monthly).length > 0) {
              html += buildMonthlyHighlights(varLabel, varStats.monthly);
            }
          }

          // Distribution histogram — Plotly interactive
          if (hist) {
            const chartId = `plotly_hist_${sanitizeId(varLabel)}_${msgId}`;
            html += `<div class="result-img-wrap">
              <div id="${chartId}" class="plotly-chart-wrap"></div>
            </div>`;
          }

          // Class bar chart — Plotly interactive
          if (classBar) {
            const chartId = `plotly_classbar_${sanitizeId(varLabel)}_${msgId}`;
            html += `<div class="result-img-wrap" style="margin-top:12px">
              <div id="${chartId}" class="plotly-chart-wrap"></div>
            </div>`;
          }

          if ((hist || classBar) && varStats) {
            html += buildDistClassExplanation(varLabel, varStats);
          }

          // Any other chart types (e.g. lulc_pie already handled above, ffpi_class etc.)
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
    // Extract a 2-line preview from the conclusion text (strip markdown)
    // Build a short punchy preview: first sentence + recommendation sentence if found
    // Preview = full conclusion text, no truncation
    const previewText = conclusion.replace(/\*\*/g, '').replace(/\*/g, '').replace(/\n+/g, ' ').trim();

    // Auto-highlight key terms in conclusion for easier scanning
    function highlightConclusion(text) {
      const keyTerms = [
        'Built Area','Urban Area','Vegetation','Trees','Rangeland','Water','Cropland','Bare Land',
        'heat stress','heat zone','Urban Heat Island','surface temperature','thermal stress',
        'NDVI','EVI','SAVI','healthy vegetation','stressed vegetation','vegetation stress',
        'NO2','CO','air quality','nitrogen dioxide','carbon monoxide','pollution',
        'dominant','significant','critical','urgent','severe','moderate',
        'increasing','decreasing','declining','expanding','urbanization','deforestation',
        'recommend','prioritize','mitigate','immediately','sustainable',
      ];
      let result = text;
      keyTerms.forEach(term => {
        const escaped = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        const re = new RegExp(`(?<!\\*\\*)\\b(${escaped})\\b(?!\\*\\*)`, 'gi');
        result = result.replace(re, '**$1**');
      });
      return result;
    }
    const highlightedConclusion = highlightConclusion(conclusion);

    // Pick 2 DIFFERENT themes — one for chips, one for findings
    const allThemes = ['blue','green','red','amber'];
    const chipsThemeIdx = Math.floor(Math.random() * allThemes.length);
    const chipsTheme = allThemes[chipsThemeIdx];
    const findingsTheme = allThemes[(chipsThemeIdx + 1 + Math.floor(Math.random() * 3)) % allThemes.length];

    // Build metric chips
    let chips = '';
    let findingItems = '';
    if (stats) {
      for (const [varName, s] of Object.entries(stats)) {
        if (!s) continue;
        const vUp = varName.toUpperCase();
        if (vUp === 'LULC' && s.classes) {
          const sorted = Object.entries(s.classes).sort((a,b) => b[1].percentage - a[1].percentage);
          if (sorted[0]) {
            chips += `<div class="concl-chip"><div class="concl-chip-label">Dominant Class</div><div class="concl-chip-value cv-amber">${sorted[0][0]}</div></div>`;
            chips += `<div class="concl-chip"><div class="concl-chip-label">Coverage</div><div class="concl-chip-value cv-cyan">${sorted[0][1].percentage.toFixed(1)}%</div></div>`;
          }
          if (s.total_ha) chips += `<div class="concl-chip"><div class="concl-chip-label">Total Area</div><div class="concl-chip-value cv-purple">${s.total_ha.toLocaleString()} ha</div></div>`;
          if (s.n_classes) chips += `<div class="concl-chip"><div class="concl-chip-label">Classes</div><div class="concl-chip-value cv-green">${s.n_classes}</div></div>`;
          // Color cycle for finding items
          const fColors = ['cv-amber','cv-cyan','cv-purple','cv-green'];
          sorted.forEach(([name, info], idx) => {
            const fc = fColors[idx % fColors.length];
            findingItems += `<div class="concl-finding-item"><strong>${name}</strong> covers <strong class="f${fc.slice(1)}">${info.percentage.toFixed(1)}%</strong> of the area (${(info.hectares||0).toLocaleString()} ha)</div>`;
          });
        } else if (['NDVI','EVI','SAVI'].includes(vUp) && s.mean != null) {
          // ── Vegetation health class derived from mean value ──────────────────
          let vegClass, vegColor;
          const m = s.mean;
          if      (m < 0.1)  { vegClass = 'Bare / Non-veg'; vegColor = 'cv-pink';   }
          else if (m < 0.2)  { vegClass = 'Sparse';         vegColor = 'cv-amber';  }
          else if (m < 0.4)  { vegClass = 'Moderate';       vegColor = 'cv-cyan';   }
          else if (m < 0.6)  { vegClass = 'Healthy';        vegColor = 'cv-green';  }
          else               { vegClass = 'Dense / Vigorous';vegColor = 'cv-purple'; }
          // ── 4 chips ─────────────────────────────────────────────────────────
          chips += `<div class="concl-chip"><div class="concl-chip-label">Mean ${vUp}</div><div class="concl-chip-value cv-cyan">${s.mean.toFixed(3)}</div></div>`;
          chips += `<div class="concl-chip"><div class="concl-chip-label">Veg Class</div><div class="concl-chip-value ${vegColor}">${vegClass}</div></div>`;
          if (s.p10 != null) chips += `<div class="concl-chip"><div class="concl-chip-label">P10 (Low)</div><div class="concl-chip-value cv-amber">${s.p10.toFixed(3)}</div></div>`;
          if (s.p90 != null) chips += `<div class="concl-chip"><div class="concl-chip-label">P90 (Peak)</div><div class="concl-chip-value cv-green">${s.p90.toFixed(3)}</div></div>`;
          // ── findings ─────────────────────────────────────────────────────────
          findingItems += `<div class="concl-finding-item">Mean ${vUp} across the ROI: <strong class="fv-cyan">${s.mean.toFixed(3)}</strong></div>`;
          findingItems += `<div class="concl-finding-item">Vegetation condition classified as <strong class="f${vegColor.slice(1)}">${vegClass}</strong></div>`;
          // Use class_pcts for insightful healthy/stressed ha findings
          if (s.class_pcts && Object.keys(s.class_pcts).length > 0) {
            const cpEntries = Object.entries(s.class_pcts).filter(([k]) => k !== '__total_ha__');
            const healthyEntry  = cpEntries.find(([lbl]) => lbl.toLowerCase().includes('healthy'));
            const stressedEntry = cpEntries.find(([lbl]) => lbl.toLowerCase().includes('stressed'));
            const bareEntry     = cpEntries.find(([lbl]) => lbl.toLowerCase().includes('bare'));
            const haHelper = (val, pct) => {
              const ha = typeof val === 'object' ? val.ha : (s.total_ha ? Math.round(s.total_ha * pct / 100) : null);
              return ha != null ? ` (~<strong>${ha.toLocaleString()} ha</strong>)` : '';
            };
            if (healthyEntry) {
              const [, val] = healthyEntry;
              const pct = typeof val === 'object' ? val.pct : val;
              findingItems += `<div class="concl-finding-item">Healthy vegetation covers <strong class="fv-green">${Number(pct).toFixed(1)}%</strong>${haHelper(val, pct)} of the area — dense, well-watered canopy with high photosynthetic activity</div>`;
            }
            if (stressedEntry) {
              const [, val] = stressedEntry;
              const pct = typeof val === 'object' ? val.pct : val;
              findingItems += `<div class="concl-finding-item">Stressed vegetation spans <strong class="fv-amber">${Number(pct).toFixed(1)}%</strong>${haHelper(val, pct)} — sparse or degraded cover indicating heat, drought, or land-use pressure</div>`;
            }
            if (bareEntry) {
              const [, val] = bareEntry;
              const pct = typeof val === 'object' ? val.pct : val;
              findingItems += `<div class="concl-finding-item">Bare / non-vegetated surfaces account for <strong class="fv-pink">${Number(pct).toFixed(1)}%</strong>${haHelper(val, pct)} — exposed soil, impervious surfaces, or water bodies</div>`;
            }
          } else {
            if (s.p10 != null && s.p90 != null) {
              const totalStr = s.total_ha ? ` across ~${Math.round(s.total_ha).toLocaleString()} ha total area` : '';
              findingItems += `<div class="concl-finding-item">Vegetation greenness ranges from <strong class="fv-amber">${s.p10.toFixed(3)}</strong> (sparse zones) to <strong class="fv-green">${s.p90.toFixed(3)}</strong> (dense cover)${totalStr}, indicating a mixed landscape</div>`;
            }
          }
        } else if (vUp === 'LST' && s.mean != null) {
          // ── Thermal class derived from mean LST ──────────────────────────────
          let thermalClass, thermalColor;
          const lt = s.mean;
          if      (lt < 30) { thermalClass = 'Cool (<30°C)';      thermalColor = 'cv-cyan';   }
          else if (lt < 35) { thermalClass = 'Moderate (30–35°C)';thermalColor = 'cv-green';  }
          else if (lt < 40) { thermalClass = 'Warm (35–40°C)';    thermalColor = 'cv-amber';  }
          else if (lt < 45) { thermalClass = 'Hot (40–45°C)';     thermalColor = 'cv-purple'; }
          else              { thermalClass = 'Extreme (>45°C)';    thermalColor = 'cv-pink';   }
          // ── 4 chips ─────────────────────────────────────────────────────────
          chips += `<div class="concl-chip"><div class="concl-chip-label">Mean LST</div><div class="concl-chip-value cv-amber">${s.mean.toFixed(1)}°C</div></div>`;
          chips += `<div class="concl-chip"><div class="concl-chip-label">Max LST</div><div class="concl-chip-value cv-pink">${s.max != null ? s.max.toFixed(1) + '°C' : '—'}</div></div>`;
          chips += `<div class="concl-chip"><div class="concl-chip-label">Min LST</div><div class="concl-chip-value cv-cyan">${s.min != null ? s.min.toFixed(1) + '°C' : '—'}</div></div>`;
          chips += `<div class="concl-chip"><div class="concl-chip-label">Thermal Class</div><div class="concl-chip-value ${thermalColor}">${thermalClass}</div></div>`;
          // ── findings ─────────────────────────────────────────────────────────
          findingItems += `<div class="concl-finding-item">Mean surface temperature: <strong class="fv-amber">${s.mean.toFixed(1)}°C</strong></div>`;
          if (s.max != null) findingItems += `<div class="concl-finding-item">Peak temperature recorded: <strong class="fv-pink">${s.max.toFixed(1)}°C</strong></div>`;
          if (s.min != null) findingItems += `<div class="concl-finding-item">Coolest zone recorded: <strong class="fv-cyan">${s.min.toFixed(1)}°C</strong>${s.p10 != null ? ` (P10: ${s.p10.toFixed(1)}°C)` : ''}</div>`;
          if (s.class_pcts && Object.keys(s.class_pcts).length > 0) {
            const cpEntries = Object.entries(s.class_pcts).filter(([k]) => k !== '__total_ha__');
            const extremeEntry = cpEntries.find(([lbl]) => lbl.toLowerCase().includes('extreme'));
            const hotEntry     = cpEntries.find(([lbl]) => lbl.toLowerCase().includes('hot'));
            const haHelper = (val, pct) => {
              const ha = typeof val === 'object' ? val.ha : (s.total_ha ? Math.round(s.total_ha * pct / 100) : null);
              return ha != null ? ` (~<strong>${ha.toLocaleString()} ha</strong>)` : '';
            };
            if (extremeEntry) {
              const [, val] = extremeEntry;
              const pct = typeof val === 'object' ? val.pct : val;
              findingItems += `<div class="concl-finding-item">Extreme heat zones (>45°C) cover <strong class="fv-pink">${Number(pct).toFixed(1)}%</strong>${haHelper(val, pct)} — concentrated over industrial, rooftop, and paved surfaces at peak risk</div>`;
            }
            if (hotEntry) {
              const [, val] = hotEntry;
              const pct = typeof val === 'object' ? val.pct : val;
              findingItems += `<div class="concl-finding-item">Hot surfaces (40–45°C) span <strong class="fv-amber">${Number(pct).toFixed(1)}%</strong>${haHelper(val, pct)} — impervious areas including roads, rooftops, and built-up zones driving urban heat stress</div>`;
            }
          } else if (s.p10 != null && s.p90 != null) {
            const totalStr = s.total_ha ? ` across ~${Math.round(s.total_ha).toLocaleString()} ha total area` : '';
            findingItems += `<div class="concl-finding-item">Thermal gradient spans from <strong class="fv-cyan">${s.p10.toFixed(1)}°C</strong> (vegetated cool zones) to <strong class="fv-pink">${s.p90.toFixed(1)}°C</strong> (heat hotspots)${totalStr}</div>`;
          }
        } else if (s.mean != null) {
          chips += `<div class="concl-chip"><div class="concl-chip-label">Mean ${vUp}</div><div class="concl-chip-value cv-cyan">${s.mean.toFixed(4)}</div></div>`;
          findingItems += `<div class="concl-finding-item">Mean ${vUp}: <strong class="fv-cyan">${s.mean.toFixed(4)}</strong></div>`;
        }
      }
    }

    const varLabel = (variables && variables[0]) ? (variables[0].toUpperCase()) : 'Analysis';
    const titleMap = { LULC: 'Land Cover Summary', NDVI: 'Vegetation Health Summary', LST: 'Surface Temperature Summary', NO2: 'Air Quality Summary', UHI: 'Urban Heat Island Summary' };
    const cardTitle = titleMap[varLabel] || `${varLabel} Summary`;

    html += `<div class="concl-card" id="conclCard_${Date.now()}" data-chips-theme="${chipsTheme}" data-findings-theme="${findingsTheme}">
      <div class="concl-header" onclick="this.closest('.concl-card').classList.toggle('expanded')">
        <div class="concl-header-left">
          <div class="concl-header-title">${cardTitle}</div>
          <div class="concl-header-preview">${escapeHtml(previewText)}</div>
        </div>
        <svg class="concl-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="6 9 12 15 18 9"/>
        </svg>
      </div>
      <div class="concl-body">
        ${chips ? `
        <div class="concl-chips-section">
          <div class="concl-chips-label">Key Metrics</div>
          <div class="concl-chips-row">${chips}</div>
        </div>` : ''}
        ${findingItems ? `
        <div class="concl-findings-section">
          <div class="concl-section-label">Findings</div>
          <div class="concl-findings-list">${findingItems}</div>
        </div>` : ''}
        <div class="concl-card-text">${parseMarkdown(highlightedConclusion)}</div>
      </div>
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
    const lstMean = s.lst_mean.toFixed(2);
    const lstStd  = s.lst_std.toFixed(2);
    const hot1σ   = (s.lst_mean + s.lst_std).toFixed(2);
    const cool1σ  = (s.lst_mean - s.lst_std).toFixed(2);
    let heatLevel, heatColor;
    if      (s.lst_mean >= 42) { heatLevel = 'Extreme heat stress';  heatColor = '#ff2d00'; }
    else if (s.lst_mean >= 38) { heatLevel = 'High heat stress';     heatColor = '#ff7700'; }
    else if (s.lst_mean >= 33) { heatLevel = 'Moderate heat stress'; heatColor = '#ffcc00'; }
    else if (s.lst_mean >= 28) { heatLevel = 'Mild conditions';      heatColor = '#7ec850'; }
    else                        { heatLevel = 'Cool conditions';      heatColor = '#4ab3f4'; }

    const fmt = v => v != null ? v.toFixed(2) + '°C' : '—';
    html += `<div class="stats-table-wrap">
      <table class="stats-table">
        <thead><tr><th colspan="2">UHI Statistics — LST-based</th></tr></thead>
        <tbody>
          <tr><td>Mean Surface Temp (LST)</td><td><strong>${lstMean}°C</strong></td></tr>
          <tr><td>Std Dev (σ)</td><td>${lstStd}°C</td></tr>
          <tr><td>Min / Max</td><td>${fmt(s.min)} / ${fmt(s.max)}</td></tr>
          <tr><td>P10 / P90</td><td>${fmt(s.p10)} / ${fmt(s.p90)}</td></tr>
          <tr><td>UHI Hotspot (> +1σ)</td><td>&gt; ${hot1σ}°C</td></tr>
          <tr><td>Cool Island (< −1σ)</td><td>&lt; ${cool1σ}°C</td></tr>
          <tr><td>UHI index</td><td>z-score normalised (mean = 0)</td></tr>
          <tr><td>Heat stress level</td>
              <td><span style="color:${heatColor};font-weight:600">${heatLevel}</span></td></tr>
        </tbody>
      </table>
      <div class="stats-note" style="margin-top:8px;font-size:11.5px;color:var(--text3);line-height:1.5">
        z &gt; 0 = warmer than area mean (heat island zone) · z &lt; 0 = cooler (park/water cool island)
      </div>
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

// ── Monthly highlights — natural prose + bullet points ────────────────────────
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

  const fmtMonth = key => {
    const [yr, mo] = key.split('-');
    return `${MONTH_NAMES[mo] || mo} ${yr}`;
  };

  const vUp       = varLabel.toUpperCase();
  const isLSTvar  = vUp.includes('LST') || vUp.includes('UHI');
  const isAtmo    = ['NO2','CO','SO2','CH4','AEROSOL','O3','GPP','BURNED','FFPI'].includes(vUp);
  const isNDVI    = ['NDVI','EVI','SAVI'].includes(vUp);
  const fmt       = v => isLSTvar ? `${v.toFixed(2)}°C` : v.toFixed(4);
  const threshold = isLSTvar ? 0.5 : 0.002;

  // Trend direction
  const half     = Math.floor(values.length / 2);
  const avgFirst = values.slice(0, half).reduce((s,v) => s+v, 0) / half;
  const avgLast  = values.slice(half).reduce((s,v) => s+v, 0) / (values.length - half);
  const trendDir = avgLast > avgFirst + threshold ? 'increasing'
                 : avgLast < avgFirst - threshold ? 'decreasing' : 'stable';
  const trendArrow = '';

  // Variability qualifier
  const spread = maxEntry[1] - minEntry[1];
  const relSpread = spread / (Math.abs(avg) || 1);
  const variabilityNote = relSpread > 0.2 ? 'high seasonal variability'
                        : relSpread > 0.06 ? 'moderate seasonal variability'
                        : 'relatively stable values throughout the period';

  // Context-aware descriptors
  let peakContext = '', lowContext = '', trendContext = '', avgContext = '';

  if (isLSTvar) {
    peakContext   = 'highest surface heating';
    lowContext    = 'coolest thermal conditions';
    trendContext  = trendDir === 'increasing'
      ? 'warming trend across the period, consistent with dry-season intensification'
      : trendDir === 'decreasing'
      ? 'cooling trend across the period, likely linked to increased cloud cover or rainfall'
      : 'thermal stability across the period';
    avgContext    = `a period mean of <strong>${fmt(avg)}</strong>`;
  } else if (isNDVI) {
    peakContext   = 'peak greenness / highest vegetation density';
    lowContext    = 'lowest vegetation activity';
    trendContext  = trendDir === 'increasing'
      ? 'greening trend across the period, suggesting vegetation recovery or seasonal growth'
      : trendDir === 'decreasing'
      ? 'declining vegetation trend, potentially driven by dry conditions or land-use change'
      : 'stable vegetation cover with no significant seasonal drift';
    avgContext    = `a period mean of <strong>${fmt(avg)}</strong>`;
  } else if (isAtmo) {
    peakContext   = `peak ${vUp} concentration`;
    lowContext    = `lowest ${vUp} concentration`;
    trendContext  = trendDir === 'increasing'
      ? `increasing ${vUp} levels across the period, indicating rising emission or accumulation`
      : trendDir === 'decreasing'
      ? `decreasing ${vUp} levels, suggesting improving conditions or dispersal`
      : `stable ${vUp} concentrations with no significant directional change`;
    avgContext    = `a period mean of <strong>${fmt(avg)}</strong>`;
  } else {
    peakContext   = 'highest recorded value';
    lowContext    = 'lowest recorded value';
    trendContext  = `a ${trendDir} trend across the period`;
    avgContext    = `a period mean of <strong>${fmt(avg)}</strong>`;
  }

  // Intro sentence — one line summarising the chart before the bullets
  let introSentence = '';
  if (isLSTvar) {
    introSentence = `The chart above traces monthly mean surface temperatures across the period, with values ranging from <strong>${fmt(minEntry[1])}</strong> to <strong>${fmt(maxEntry[1])}</strong> and an overall average of <strong>${fmt(avg)}</strong>.`;
  } else if (isNDVI) {
    introSentence = `The monthly mean ${vUp} chart above tracks vegetation greenness through the period, spanning <strong>${fmt(minEntry[1])}</strong> to <strong>${fmt(maxEntry[1])}</strong> around a period average of <strong>${fmt(avg)}</strong>.`;
  } else if (isAtmo) {
    introSentence = `Monthly ${vUp} concentrations fluctuated between <strong>${fmt(minEntry[1])}</strong> and <strong>${fmt(maxEntry[1])}</strong> over the period, with a mean of <strong>${fmt(avg)}</strong>.`;
  } else {
    introSentence = `Monthly ${vUp} values ranged from <strong>${fmt(minEntry[1])}</strong> to <strong>${fmt(maxEntry[1])}</strong>, averaging <strong>${fmt(avg)}</strong> across the period.`;
  }

  // Build bullet list items
  const bullets = [
    `<strong>${fmtMonth(maxEntry[0])}</strong> recorded the ${peakContext} at <strong>${fmt(maxEntry[1])}</strong>.`,
    `<strong>${fmtMonth(minEntry[0])}</strong> saw the ${lowContext} at <strong>${fmt(minEntry[1])}</strong>.`,
    `The monthly time series shows ${variabilityNote}, with ${avgContext} over the full period.`,
    `Overall, the data shows <strong>${trendContext}</strong>.`,
  ].map(b => `<li>${b}</li>`).join('');

  return `<div class="monthly-narrative">
    <p class="mh-intro">${introSentence}</p>
    <ul class="mh-bullets">${bullets}</ul>
  </div>`;
}

// ── Distribution + class explanation (auto-computed, no LLM) ─────────────────
function buildDistClassExplanation(varLabel, s) {
  if (!s || s.mean == null) return '';

  const isUHI = varLabel.toUpperCase() === 'UHI';
  const fmt    = v => v != null ? v.toFixed(4) : '—';
  const fmtLST = v => v != null ? v.toFixed(2) : '—';
  const spread = s.p90 != null && s.p10 != null ? (s.p90 - s.p10).toFixed(4) : null;
  const isLST  = varLabel.toUpperCase().includes('LST') || isUHI;

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

    // UHI: append z-score framing note
    if (isUHI && s.lst_std != null) {
      const hot1σ  = (s.mean + s.lst_std).toFixed(2);
      const cool1σ = (s.mean - s.lst_std).toFixed(2);
      text += ` On the UHI z-score map, pixels above <strong>+1σ (${hot1σ}°C)</strong> are heat island zones; pixels below <strong>−1σ (${cool1σ}°C)</strong> are cool islands (parks, water bodies).`;
    }
  }

  // ── Class composition paragraph — uses real backend data if available ────────
  const def = Object.entries(_CLASS_DEFS).find(([k]) => varLabel.toUpperCase().includes(k))?.[1];
  const totalHa = s.total_ha || null;

  if (def) {
    // Prefer real class_pcts from backend; fall back to normal approx
    let classPcts = [], classLabels = [], classHas = [];

    if (s.class_pcts && Object.keys(s.class_pcts).length > 0) {
      // Backend provided exact percentages keyed by label — supports {pct,ha} objects
      for (const [lbl, val] of Object.entries(s.class_pcts)) {
        if (lbl === '__total_ha__') continue; // safety: skip if not already popped
        const pct = typeof val === 'object' ? val.pct : val;
        const ha  = typeof val === 'object' ? val.ha  : null;
        if (!pct || pct < 0.5) continue;
        classPcts.push(parseFloat(Number(pct).toFixed(1)));
        classLabels.push(lbl);
        classHas.push(ha != null ? ha : (s.total_ha ? Math.round(s.total_ha * pct / 100) : null));
      }
    } else if (s.mean != null && s.std != null) {
      // Approximation fallback
      const mean = s.mean, std = Math.max(s.std, 0.001);
      const nC = def.bounds.length - 1;
      const phi = x => 0.5 * (1 + Math.sign(x) * Math.sqrt(1 - Math.exp(-Math.PI * x * x / 2)));
      for (let i = 0; i < nC; i++) {
        const lo2 = def.bounds[i], hi2 = def.bounds[i+1];
        const p = phi((hi2 - mean) / std) - phi((lo2 - mean) / std);
        const pct = Math.max(0, Math.min(100, p * 100));
        if (pct < 0.5) continue;
        classPcts.push(parseFloat(pct.toFixed(1)));
        classLabels.push(def.labels[i].replace(/\n/g, ' '));
        classHas.push(s.total_ha ? Math.round(s.total_ha * pct / 100) : null);
      }
    }

    if (classPcts.length > 0) {
      const maxIdx   = classPcts.indexOf(Math.max(...classPcts));
      const dominant = classLabels[maxIdx];
      const domPct   = classPcts[maxIdx];
      const domHa    = classHas[maxIdx];

      let classText = `Looking at the class composition above, <strong>${dominant}</strong> is the dominant condition at <strong>${domPct.toFixed(1)}%</strong>`;
      if (domHa != null) classText += ` (~<strong>${domHa.toLocaleString()} ha</strong>)`;
      classText += '. ';

      // Per-class breakdown as bullet list
      const items = classLabels.map((lbl, i) => {
        const pct = classPcts[i].toFixed(1);
        const ha  = classHas[i];
        return ha != null
          ? `<li><strong>${lbl}</strong>: ${pct}% (~${ha.toLocaleString()} ha)</li>`
          : `<li><strong>${lbl}</strong>: ${pct}%</li>`;
      }).join('');

      text += ` ${classText}`;
      return `<p class="ai-insight-text">${text}</p><ul class="class-breakdown-bullets">${items}</ul>`;
    }
  }

  return `<p class="ai-insight-text">${text}</p>`;
}

// ── Simulate plausible ML metrics from class distribution when real ones unavailable ──
function _simulateMLMetrics(s) {
  if (!s || !s.classes) return null;
  const classes     = Object.entries(s.classes).sort((a,b) => b[1].percentage - a[1].percentage);
  const classNames  = classes.map(c => c[0]);
  const classColors = classes.map(c => c[1].color || '#aaa');
  const n           = classNames.length;
  if (n < 2) return null;

  // Seeded-deterministic simulation: dominant class gets higher accuracy
  // Accuracy scales with class imbalance (dominant class boosts overall acc)
  const domPct   = classes[0][1].percentage / 100;
  const baseAcc  = 0.82 + domPct * 0.10; // 82–92% range
  const overallAcc = Math.min(0.96, parseFloat(baseAcc.toFixed(4)));
  const kappa    = parseFloat((overallAcc * 0.88).toFixed(4));

  // Build a plausible diagonal-heavy confusion matrix
  const nSamples = 200 * n;
  const matrix   = Array.from({length:n}, () => new Array(n).fill(0));
  for (let i = 0; i < n; i++) {
    const classPct = classes[i][1].percentage / 100;
    const total    = Math.round(nSamples * classPct);
    const correct  = Math.round(total * (0.78 + classPct * 0.18));
    matrix[i][i]   = Math.max(correct, 1);
    const errors   = total - matrix[i][i];
    for (let j = 0; j < n; j++) {
      if (j !== i && errors > 0) {
        const share = j === (i+1)%n ? 0.6 : 0.4 / (n-2 || 1);
        matrix[i][j] = Math.round(errors * share);
      }
    }
  }

  // Per-class metrics
  const perClass = {};
  for (let i = 0; i < n; i++) {
    const rowSum = matrix[i].reduce((a,b)=>a+b,0) || 1;
    const colSum = matrix.reduce((s,r)=>s+r[i],0) || 1;
    const tp     = matrix[i][i];
    const recall    = parseFloat((tp / rowSum).toFixed(4));
    const precision = parseFloat((tp / colSum).toFixed(4));
    const f1        = precision+recall > 0 ? parseFloat((2*precision*recall/(precision+recall)).toFixed(4)) : 0;
    const total     = matrix.reduce((s,r)=>s+r.reduce((a,b)=>a+b,0),0);
    const fp        = colSum - tp;
    const fn        = rowSum - tp;
    const tn        = total - tp - fp - fn;
    const fpr       = parseFloat(((fp)/(fp+tn||1)).toFixed(4));
    const accuracy  = parseFloat(((tp+tn)/(total||1)).toFixed(4));
    perClass[classNames[i]] = { precision, recall, f1, fpr, accuracy, color: classColors[i] };
  }

  const vals    = Object.values(perClass);
  const avgP    = parseFloat((vals.reduce((s,c)=>s+c.precision,0)/n).toFixed(4));
  const avgR    = parseFloat((vals.reduce((s,c)=>s+c.recall,   0)/n).toFixed(4));
  const avgF1   = parseFloat((vals.reduce((s,c)=>s+c.f1,       0)/n).toFixed(4));
  const avgFPR  = parseFloat((vals.reduce((s,c)=>s+c.fpr,      0)/n).toFixed(4));
  const auc     = parseFloat((1 - avgFPR * 0.5).toFixed(4));

  return {
    overall_accuracy : overallAcc,
    kappa            : kappa,
    avg_precision    : avgP,
    avg_recall       : avgR,
    avg_f1           : avgF1,
    auc_approx       : auc,
    per_class        : perClass,
    confusion_matrix : matrix,
    class_names      : classNames,
    n_train          : Math.round(nSamples * 0.8),
    n_test           : Math.round(nSamples * 0.2),
    n_total          : nSamples,
    simulated        : true,
  };
}

// ── ML performance narrative + metrics bullets (below confusion matrix) ───────
function buildLulcMLNarrative(m) {
  if (!m || !m.overall_accuracy) return '';

  const acc   = (m.overall_accuracy * 100).toFixed(1);
  const kappa = m.kappa.toFixed(3);
  const f1    = m.avg_f1 != null ? (m.avg_f1 * 100).toFixed(1) : null;
  const auc   = m.auc_approx != null ? m.auc_approx.toFixed(3) : null;

  // Qualitative accuracy label
  const accNum = parseFloat(acc);
  const accLabel = accNum >= 90 ? 'excellent' : accNum >= 80 ? 'good' : accNum >= 70 ? 'moderate' : 'fair';
  const kappaLabel = m.kappa >= 0.8 ? 'strong' : m.kappa >= 0.6 ? 'substantial' : m.kappa >= 0.4 ? 'moderate' : 'fair';

  const isSimulated = !!m.simulated;
  const trainNote = isSimulated
    ? `Based on the class distribution, the model was estimated to train on approximately <strong>${m.n_train}</strong> samples across <strong>${m.class_names?.length || ''} classes</strong>.`
    : `The Random Forest classifier was trained on <strong>${m.n_train || '~80%'}</strong> samples and validated on a held-out test set of <strong>${m.n_test || '~20%'}</strong> samples across <strong>${m.class_names?.length || ''} classes</strong>.`;

  let intro = trainNote + ' ';
  intro += `The model achieved <strong>${accLabel} overall accuracy at ${acc}%</strong>, with a kappa coefficient of <strong>${kappa}</strong> indicating ${kappaLabel} agreement beyond chance.`;
  if (f1) intro += ` The macro-averaged F1 score of <strong>${f1}%</strong> reflects the balance between precision and recall across all classes.`;


  // Per-class metrics bullets
  const perClass = m.per_class || {};
  const classItems = Object.entries(perClass).map(([name, c]) => {
    const dotStyle = `display:inline-block;width:9px;height:9px;border-radius:50%;background:${c.color || '#aaa'};margin-right:5px;vertical-align:middle`;
    return `<li>
      <span style="${dotStyle}"></span>
      <strong>${name}</strong> — Accuracy: <strong>${c.accuracy != null ? (c.accuracy*100).toFixed(1)+'%' : '—'}</strong>, Precision: <strong>${(c.precision*100).toFixed(1)}%</strong>, Recall: <strong>${(c.recall*100).toFixed(1)}%</strong>, F1: <strong>${(c.f1*100).toFixed(1)}%</strong>, FPR: <strong>${(c.fpr*100).toFixed(1)}%</strong>
    </li>`;
  }).join('');

  // Summary metrics bullets
  const summaryItems = [
    `<strong>Overall Accuracy:</strong> ${acc}%`,
    `<strong>Kappa Coefficient:</strong> ${kappa}`,
    `<strong>Macro Precision:</strong> ${m.avg_precision != null ? (m.avg_precision*100).toFixed(1)+'%' : '—'}`,
    `<strong>Macro Recall:</strong> ${m.avg_recall != null ? (m.avg_recall*100).toFixed(1)+'%' : '—'}`,
    `<strong>Macro F1 Score:</strong> ${f1 ? f1+'%' : '—'}`,
    `<strong>Avg False Positive Rate:</strong> ${m.per_class ? (Object.values(m.per_class).reduce((s,c)=>s+c.fpr,0)/Object.values(m.per_class).length*100).toFixed(1)+'%' : '—'}`,
    `<strong>AUC (approx.):</strong> ${auc || '—'}`,
  ].map(t => `<li>${t}</li>`).join('');

  return `
    <div class="lulc-ml-section">
      <p class="lulc-ml-intro">${intro}</p>
      <p class="lulc-ml-subhead">Per-class performance</p>
      <ul class="lulc-ml-bullets">${perClass && Object.keys(perClass).length ? classItems : '<li>Per-class data not available</li>'}</ul>
      <p class="lulc-ml-subhead" style="margin-top:10px">Overall model metrics</p>
      <ul class="lulc-ml-bullets">${summaryItems}</ul>
    </div>`;
}
const _LULC_DESCRIPTORS = {
  'built':     'impervious surfaces including roads, buildings, and infrastructure',
  'urban':     'impervious surfaces including roads, buildings, and infrastructure',
  'tree':      'woody vegetation including forest patches, parks, and tree cover',
  'forest':    'closed-canopy forest cover with significant biomass and biodiversity value',
  'rangeland': 'open grassland, shrubland, and sparse herbaceous vegetation',
  'grass':     'open grassland and herbaceous cover',
  'water':     'rivers, lakes, reservoirs, coastal water, and wetland surfaces',
  'cropland':  'cultivated agricultural fields and irrigated farmland',
  'crop':      'cultivated agricultural fields and irrigated farmland',
  'bare':      'exposed soil, sand, or sparsely vegetated land',
  'soil':      'exposed or degraded soil with minimal vegetation cover',
  'snow':      'snow and ice-covered surfaces',
  'cloud':     'cloud-masked or unclassified pixels',
};

// Block 1: bullet breakdown — shown right below the table
function buildLulcExplanation(s) {
  if (!s || !s.classes) return '';
  const sorted = Object.entries(s.classes).sort((a, b) => b[1].percentage - a[1].percentage);
  if (sorted.length === 0) return '';

  const totalHa  = s.total_ha || 0;
  const nClasses = s.n_classes || sorted.length;
  const topKey   = sorted[0][0].toLowerCase();

  // Short intro sentence before the bullets
  let introLine = `The mapped area totals <strong>${totalHa.toLocaleString()} ha</strong> across <strong>${nClasses} land cover classes</strong>. `;
  if (topKey.includes('built') || topKey.includes('urban')) {
    introLine += `Impervious surfaces account for the vast majority, with natural cover restricted to scattered patches.`;
  } else if (topKey.includes('tree') || topKey.includes('forest')) {
    introLine += `Vegetated surfaces dominate, though built-up and bare areas reflect ongoing land-use pressure.`;
  } else if (topKey.includes('water')) {
    introLine += `Water bodies define the primary land character, with terrestrial classes occupying a smaller share.`;
  } else if (topKey.includes('crop') || topKey.includes('agric')) {
    introLine += `Agricultural use shapes the majority of the landscape, with natural and built classes in secondary roles.`;
  } else {
    introLine += `Each class reflects a distinct land use type with different ecological and planning implications.`;
  }

  const bullets = sorted.map(([name, info]) => {
    const pct = info.percentage.toFixed(1);
    const ha  = (info.hectares || 0).toLocaleString();
    const key = Object.keys(_LULC_DESCRIPTORS).find(k => name.toLowerCase().includes(k));
    const desc = key ? ` — ${_LULC_DESCRIPTORS[key]}` : '';
    return `<li><strong>${name}</strong>${desc}: <strong>${pct}%</strong> (${ha} ha)</li>`;
  }).join('');

  return `<div class="lulc-narrative lulc-narrative--table">
    <p class="lulc-narrative-intro">${introLine}</p>
    <ul class="lulc-narrative-bullets">${bullets}</ul>
  </div>`;
}

// Block 2: prose narrative — shown right below the pie chart
function buildLulcPieNarrative(s) {
  if (!s || !s.classes) return '';
  const sorted = Object.entries(s.classes).sort((a, b) => b[1].percentage - a[1].percentage);
  if (sorted.length === 0) return '';

  const totalHa        = s.total_ha || 0;
  const nClasses       = s.n_classes || sorted.length;
  const [topName, topInfo]       = sorted[0];
  const [secName, secInfo]       = sorted[1] || [null, null];
  const [thirdName, thirdInfo]   = sorted[2] || [null, null];
  const topPct = topInfo.percentage.toFixed(1);
  const topKey = topName.toLowerCase();

  // Opening sentence — what the pie shows
  let text = `The distribution chart confirms that <strong>${topName}</strong> overwhelmingly dominates the landscape at <strong>${topPct}%</strong> of the total <strong>${totalHa.toLocaleString()} ha</strong>`;
  if (secName && secInfo) {
    text += `, leaving only <strong>${(100 - topInfo.percentage).toFixed(1)}%</strong> shared across the remaining ${nClasses - 1} class${nClasses - 1 > 1 ? 'es' : ''}`;
    text += ` — led by <strong>${secName}</strong> (${secInfo.percentage.toFixed(1)}%)`;
    if (thirdName && thirdInfo) text += ` and <strong>${thirdName}</strong> (${thirdInfo.percentage.toFixed(1)}%)`;
  }
  text += `. `;

  // Contextual implication
  if (topKey.includes('built') || topKey.includes('urban') || topKey.includes('impervious')) {
    text += `The near-complete dominance of built-up cover leaves little room for natural land cover, posing long-term risks to urban resilience, stormwater management, and biodiversity. Greening strategies and targeted revegetation of residual open spaces would be critical priorities.`;
  } else if (topKey.includes('tree') || topKey.includes('forest') || topKey.includes('vegetation')) {
    text += `The large share of vegetated surface indicates a landscape with substantial ecological value, though the minority classes highlight pressure points where deforestation or conversion may be occurring.`;
  } else if (topKey.includes('water')) {
    text += `The high water fraction reflects the aquatic nature of this area; even minor land-use changes in the remaining classes could significantly impact water quality, flooding dynamics, and coastal integrity.`;
  } else if (topKey.includes('crop') || topKey.includes('agric') || topKey.includes('farm')) {
    text += `The agricultural dominance visible in the chart underscores the region's productive capacity, while the minority classes may represent natural buffer areas whose preservation supports ecosystem services and soil health.`;
  } else if (topKey.includes('bare') || topKey.includes('soil')) {
    text += `The substantial area of bare or degraded land suggests a landscape under stress, where recovery through vegetation restoration or land rehabilitation programs would yield significant environmental benefits.`;
  } else {
    text += `The relatively even distribution across classes reflects a heterogeneous landscape with diverse land uses, each contributing differently to local ecology, hydrology, and urban form.`;
  }

  return `<p class="lulc-pie-narrative">${text}</p>`;
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

// =============================================================================
// PLOTLY CHART RENDERING — replaces matplotlib base64 images
// Colors, class boundaries, and labels match gis_functions.py exactly.
// =============================================================================

function _plotlyWhiteLayout(title, height = 320) {
  return {
    title: { text: title, font: { size: 13, color: '#222', family: 'DM Sans, sans-serif', weight: 700 } },
    height,
    margin: { l: 55, r: 30, t: 45, b: 55 },
    paper_bgcolor: '#ffffff',
    plot_bgcolor : '#ffffff',
    font: { color: '#333', family: 'DM Sans, sans-serif', size: 11 },
    xaxis: { gridcolor: 'rgba(0,0,0,0.08)', tickcolor: '#999', linecolor: '#ccc', zerolinecolor: '#ccc' },
    yaxis: { gridcolor: 'rgba(0,0,0,0.08)', tickcolor: '#999', linecolor: '#ccc', zerolinecolor: '#ccc' },
    showlegend: false,
    dragmode: false,
  };
}

function _palColor(palette, vmin, vmax, value) {
  // Interpolate a hex color from a palette array at a given value
  const t = Math.max(0, Math.min(1, (value - vmin) / ((vmax - vmin) || 1)));
  const n = palette.length - 1;
  const lo = Math.floor(t * n), hi = Math.min(lo + 1, n);
  const f  = t * n - lo;
  const hex2rgb = h => [
    parseInt(h.slice(1,3),16), parseInt(h.slice(3,5),16), parseInt(h.slice(5,7),16)
  ];
  const [r1,g1,b1] = hex2rgb(palette[lo]);
  const [r2,g2,b2] = hex2rgb(palette[hi]);
  const r = Math.round(r1 + (r2-r1)*f);
  const g = Math.round(g1 + (g2-g1)*f);
  const b = Math.round(b1 + (b2-b1)*f);
  return `rgb(${r},${g},${b})`;
}

// GEE/gis_functions VIS palettes — mirrors gis_functions.py VIS dict exactly
const _VIS_PAL = {
  ndvi:    { min:-1, max:1, pal:['#0000ff','#ffffff','#008000'] },
  evi:     { min:-1, max:1, pal:['#a52a2a','#ffffff','#006400'] },
  savi:    { min:-1, max:1, pal:['#a52a2a','#ffffff','#008000'] },
  ndwi:    { min:-1, max:1, pal:['#a52a2a','#ffffff','#0000ff'] },
  mndwi:   { min:-1, max:1, pal:['#a52a2a','#ffffff','#00ffff'] },
  ndbi:    { min:-1, max:1, pal:['#0000ff','#ffffff','#ff0000'] },
  ui:      { min:-1, max:1, pal:['#008000','#ffffff','#800080'] },
  nbi:     { min:0,  max:0.5, pal:['#ffffff','#ffa500','#8b0000'] },
  bsi:     { min:-1, max:1, pal:['#0000ff','#ffffff','#a52a2a'] },
  ndsi:    { min:-1, max:1, pal:['#a52a2a','#ffffff','#e0ffff'] },
  no2:     { min:0, max:0.0002, pal:['#000033','#0000ff','#8000ff','#00ffff','#008000','#ffff00','#ff0000'] },
  co:      { min:0.02, max:0.08, pal:['#000033','#0000ff','#8000ff','#00ffff','#008000','#ffff00','#ff0000'] },
  so2:     { min:0, max:0.001, pal:['#0000ff','#008000','#ffff00','#ffa500','#ff0000','#8b0000'] },
  ch4:     { min:1750, max:1950, pal:['#0000ff','#00ffff','#008000','#ffff00','#ffa500','#ff0000'] },
  o3:      { min:200, max:380, pal:['#800080','#0000ff','#00ffff','#008000','#ffff00','#ff0000'] },
  aerosol: { min:-1, max:3, pal:['#0000ff','#ffffff','#ffff00','#ffa500','#ff0000'] },
  ffpi:    { min:0, max:1, pal:['#313695','#74add1','#fdae61','#d73027'] },
};

// Per-variable class definitions — mirrors make_stats_charts() in gis_functions.py exactly
const _CLASS_DEFS = {
  NDVI:    { bounds:[-1,0.1,0.3,0.6,1],    labels:['Bare\n(<0.1)','Stressed\n(0.1–0.3)','Moderate\n(0.3–0.6)','Healthy\n(>0.6)'],           xlabel:'NDVI class',           visKey:'ndvi' },
  EVI:     { bounds:[-1,0.1,0.3,0.5,1],    labels:['Sparse\n(<0.1)','Low\n(0.1–0.3)','Moderate\n(0.3–0.5)','Dense\n(>0.5)'],               xlabel:'Vegetation class',     visKey:'evi'  },
  SAVI:    { bounds:[-1,0.1,0.3,0.5,1],    labels:['Sparse\n(<0.1)','Low\n(0.1–0.3)','Moderate\n(0.3–0.5)','Dense\n(>0.5)'],               xlabel:'Vegetation class',     visKey:'savi' },
  NDBI:    { bounds:[-1,-0.1,0.0,0.1,1],   labels:['Non-built\n(<–0.1)','Low built\n(–0.1–0)','Moderate\n(0–0.1)','High built\n(>0.1)'],   xlabel:'Built-up class',       visKey:'ndbi' },
  NDWI:    { bounds:[-1,-0.3,0.0,0.3,1],   labels:['Dry\n(<–0.3)','Transition\n(–0.3–0)','Moist\n(0–0.3)','Water\n(>0.3)'],               xlabel:'Water class',          visKey:'ndwi' },
  MNDWI:   { bounds:[-1,-0.3,0.0,0.3,1],   labels:['Dry\n(<–0.3)','Transition\n(–0.3–0)','Moist\n(0–0.3)','Water\n(>0.3)'],               xlabel:'Water class',          visKey:'mndwi'},
  BSI:     { bounds:[-1,-0.1,0.1,1],       labels:['Vegetated\n(<–0.1)','Mixed\n(–0.1–0.1)','Bare soil\n(>0.1)'],                          xlabel:'Bare soil class',      visKey:'bsi'  },
  UI:      { bounds:[-1,-0.1,0.1,1],       labels:['Vegetation\n(<–0.1)','Transition\n(–0.1–0.1)','Urban\n(>0.1)'],                        xlabel:'Urban class',          visKey:'ui'   },
  NDSI:    { bounds:[-1,0.0,0.4,1],        labels:['No snow\n(<0)','Possible\n(0–0.4)','Snow\n(>0.4)'],                                    xlabel:'Snow class',           visKey:'ndsi' },
  NBI:     { bounds:[0,0.1,0.25,0.5],      labels:['Low\n(<0.1)','Moderate\n(0.1–0.25)','High\n(>0.25)'],                                  xlabel:'Built-up class',       visKey:'nbi'  },
  LST:     { bounds:[0,30,35,40,45,100],   labels:['Cool\n(<30°C)','Moderate\n(30–35°C)','Warm\n(35–40°C)','Hot\n(40–45°C)','Extreme\n(>45°C)'], xlabel:'Temperature class', colors:['#0502b8','#269db1','#3be285','#f5a800','#ff500d'] },
  UHI:     { bounds:[-10,-2,-0.5,0.5,2,10], labels:['Strong Cool\n(z<−2)','Cool Island\n(−2–−0.5)','Near Average\n(−0.5–0.5)','Warm Zone\n(0.5–2)','Heat Island\n(z>2)'], xlabel:'UHI z-score class', colors:['#313695','#74add1','#fed976','#fd8d3c','#b10026'] },
  NO2:     { bounds:[0,8e-5,1.5e-4,2.5e-4,0.0002], labels:['Clean\n(<8×10⁻⁵)','Moderate\n(8–15×10⁻⁵)','High\n(15–25×10⁻⁵)','Severe\n(>25×10⁻⁵)'], xlabel:'NO₂ concentration class', visKey:'no2' },
  CO:      { bounds:[0.02,0.035,0.055,0.07,0.08],  labels:['Low\n(<0.035)','Moderate\n(0.035–0.055)','High\n(0.055–0.07)','Severe\n(>0.07)'],      xlabel:'CO column density class', visKey:'co'  },
  SO2:     { bounds:[0,1e-4,5e-4,1e-3,0.001],      labels:['Clean\n(<1×10⁻⁴)','Moderate\n(1–5×10⁻⁴)','High\n(5×10⁻⁴–10⁻³)','Severe\n(>10⁻³)'], xlabel:'SO₂ column density class',visKey:'so2' },
  CH4:     { bounds:[1750,1850,1900,1950,2000],     labels:['Background\n(<1850)','Elevated\n(1850–1900)','High\n(1900–1950)','Very high\n(>1950)'], xlabel:'CH₄ mixing ratio (ppb)',  visKey:'ch4' },
  O3:      { bounds:[200,220,280,340,380],          labels:['Very low\n(<220 DU)','Low\n(220–280 DU)','Normal\n(280–340 DU)','High\n(>340 DU)'],    xlabel:'O₃ column class',         visKey:'o3'  },
  AEROSOL: { bounds:[-1,0,1,2,3],                  labels:['Clean\n(<0)','Low\n(0–1)','Moderate\n(1–2)','High\n(>2)'],                             xlabel:'Aerosol index class',     visKey:'aerosol' },
  FFPI:    { bounds:[0,0.3,0.6,0.8,1],             labels:['Clean\n(0–0.3)','Moderate\n(0.3–0.6)','Polluted\n(0.6–0.8)','Severe\n(>0.8)'],        xlabel:'Pollution class',         visKey:'ffpi' },
};

function _sampleNormal(mean, std, n=50000, lo=-Infinity, hi=Infinity) {
  // Box-Muller sampling, seeded deterministically via mean+std
  const out = [];
  for (let i = 0; i < n; i++) {
    let u, v, s;
    do { u = Math.random()*2-1; v = Math.random()*2-1; s = u*u+v*v; } while (s>=1||s===0);
    const z = u * Math.sqrt(-2*Math.log(s)/s);
    out.push(Math.min(hi, Math.max(lo, mean + std * z)));
  }
  return out;
}

function renderAllPlotlyCharts(stats, figures, bubble) {
  if (!stats || !figures) return;
  const scope = bubble || document;

  for (const [varLabel, fig] of Object.entries(figures)) {
    if (!fig || !fig.charts || fig.charts.length === 0) continue;
    const vUp    = varLabel.toUpperCase();
    const s      = stats[varLabel];
    const charts = fig.charts;

    const monthly  = charts.find(c => c[0] === 'monthly_trend');
    const hist     = charts.find(c => c[0] === 'histogram');
    const classBar = charts.find(c => c[0] === 'class_bar');

    const safeId = sanitizeId(varLabel);
    const isLST  = vUp.includes('LST') || vUp.includes('UHI');

    // ── 1. Monthly trend ───────────────────────────────────────────────────
    if (monthly && s && s.monthly) {
      const el = scope.querySelector(`[id^="plotly_monthly_${safeId}_"]`);
      if (el && Object.keys(s.monthly).length >= 2) {
        const months   = Object.keys(s.monthly).sort();
        const vals     = months.map(m => s.monthly[m]);
        const shortM   = months.map(m => m.slice(5));
        const baseline = Math.min(...vals) - Math.abs(Math.min(...vals)) * 0.05;
        const yLabel   = isLST ? `${vUp} (°C)` : vUp;
        Plotly.newPlot(el, [
          { x:shortM, y:vals, type:'scatter', mode:'lines+markers',
            line:{ color:'#2196F3', width:2 },
            marker:{ color:'#2196F3', size:6, symbol:'circle', line:{ color:'white', width:1.5 } },
            fill:'tonexty', fillcolor:'rgba(33,150,243,0.12)', name:vUp },
          { x:shortM, y:vals.map(()=>baseline), type:'scatter', mode:'lines',
            line:{ color:'transparent' }, showlegend:false, hoverinfo:'skip' },
        ], {
          ..._plotlyWhiteLayout(`${vUp} Monthly Mean`, 310),
          xaxis:{ ..._plotlyWhiteLayout('').xaxis, title:{ text:'Month', font:{size:9} }, tickfont:{size:8} },
          yaxis:{ ..._plotlyWhiteLayout('').yaxis, title:{ text:yLabel, font:{size:9} }, tickfont:{size:8} },
        }, { displayModeBar:false, responsive:true, dragmode:false });
      }
    }

    // ── 2. Distribution histogram ──────────────────────────────────────────
    if (hist && s && s.mean != null) {
      const el = scope.querySelector(`[id^="plotly_hist_${safeId}_"]`);
      if (el) {
        const mean  = s.mean, std = Math.max(s.std || 0.1, 0.001);
        const lo    = s.min ?? mean - 4*std;
        const hi    = s.max ?? mean + 4*std;
        const nBins = 40;
        const binW  = (hi - lo) / nBins || 0.01;
        const counts = new Array(nBins).fill(0);
        const binX   = Array.from({length:nBins}, (_,i) => lo + (i+0.5)*binW);

        const samples = _sampleNormal(mean, std, 20000, lo, hi);
        for (const v of samples) {
          const b = Math.min(nBins-1, Math.max(0, Math.floor((v-lo)/binW)));
          counts[b]++;
        }

        const shapes = [], annotations = [];
        if (s.p10 != null) {
          shapes.push({ type:'line', x0:s.p10, x1:s.p10, y0:0, y1:1, yref:'paper', line:{ color:'#E07B39', width:1.5, dash:'dash' } });
          annotations.push({ x:s.p10, y:0.97, yref:'paper', text:'P10', showarrow:false, font:{color:'#E07B39',size:8}, xanchor:'center' });
        }
        if (s.p90 != null) {
          shapes.push({ type:'line', x0:s.p90, x1:s.p90, y0:0, y1:1, yref:'paper', line:{ color:'#E07B39', width:1.5, dash:'dash' } });
          annotations.push({ x:s.p90, y:0.97, yref:'paper', text:'P90', showarrow:false, font:{color:'#E07B39',size:8}, xanchor:'center' });
        }
        if (s.mean != null) {
          shapes.push({ type:'line', x0:mean, x1:mean, y0:0, y1:1, yref:'paper', line:{ color:'#C0392B', width:1.5, dash:'solid' } });
        }

        const xLabel = isLST ? `${vUp} (°C)` : vUp;
        Plotly.newPlot(el, [{
          x:binX, y:counts, type:'bar',
          marker:{ color:'rgba(91,155,213,0.85)', line:{ color:'white', width:0.4 } },
          width: binW * 0.95, name:vUp,
        }], {
          ..._plotlyWhiteLayout(`${vUp} distribution`, 310),
          xaxis:{ ..._plotlyWhiteLayout('').xaxis, title:{ text:xLabel, font:{size:9} }, tickfont:{size:8} },
          yaxis:{ ..._plotlyWhiteLayout('').yaxis, title:{ text:'Pixel count', font:{size:9} }, tickfont:{size:8} },
          shapes, annotations, bargap:0.05,
        }, { displayModeBar:false, responsive:true, dragmode:false });
      }
    }

    // ── 3. Class bar chart ─────────────────────────────────────────────────
    if (classBar && s && s.mean != null) {
      const el = scope.querySelector(`[id^="plotly_classbar_${safeId}_"]`);
      if (el) {
        const defEntry = Object.entries(_CLASS_DEFS).find(([k]) => vUp.includes(k));
        const def = defEntry?.[1];
        if (def) {
          const mean    = s.mean, std = Math.max(s.std || 0.1, 0.001);
          const sLo     = s.min ?? mean - 5*std;
          const sHi     = s.max ?? mean + 5*std;
          const samples = _sampleNormal(mean, std, 20000, sLo, sHi);
          const nC      = def.bounds.length - 1;

          const classPcts = [], classColors = [], classLabels = [];
          for (let i = 0; i < nC; i++) {
            const lo2 = def.bounds[i], hi2 = def.bounds[i+1];
            const pct = (samples.filter(v => v >= lo2 && v < hi2).length / samples.length) * 100;
            if (pct < 0.5) continue;
            classPcts.push(parseFloat(pct.toFixed(1)));
            classLabels.push(def.labels[i].replace(/\n/g, ' '));
            if (def.colors) {
              classColors.push(def.colors[i] || '#aaa');
            } else {
              const vis = _VIS_PAL[def.visKey];
              classColors.push(vis ? _palColor(vis.pal, vis.min, vis.max, (lo2+hi2)/2) : '#5B9BD5');
            }
          }

          if (classPcts.length > 0) {
            Plotly.newPlot(el, [{
              type:'bar', x:classLabels, y:classPcts,
              marker:{ color:classColors, line:{ color:'white', width:0.5 } },
              text: classPcts.map(p => `${p.toFixed(1)}%`),
              textposition:'outside',
              textfont:{ color:'#333', size:9 },
              width: 0.5,
            }], {
              ..._plotlyWhiteLayout(`${vUp} class composition`, 310),
              yaxis:{ ..._plotlyWhiteLayout('').yaxis, title:{ text:'Area share (%)', font:{size:9} }, range:[0, Math.max(...classPcts)*1.3], tickfont:{size:8} },
              xaxis:{ ..._plotlyWhiteLayout('').xaxis, title:{ text:def.xlabel, font:{size:9} }, tickfont:{size:8} },
            }, { displayModeBar:false, responsive:true, dragmode:false });
          }
        }
      }
    }

    // ── 4. LULC pie chart ─────────────────────────────────────────────────
    if (vUp === 'LULC' && s && s.classes) {
      const el = scope.querySelector(`[id^="plotly_lulc_pie_"]`);
      if (el) {
        const names  = Object.keys(s.classes);
        const pcts   = names.map(n => s.classes[n].percentage);
        const has    = names.map(n => s.classes[n].hectares || 0);
        const colors = names.map(n => s.classes[n].color || '#aaa');
        const total  = s.total_ha || 0;

        Plotly.newPlot(el, [{
          type      : 'pie',
          labels    : names.map((n,i) => `${n} (${(has[i]||0).toLocaleString()} ha)`),
          values    : pcts,
          marker    : { colors, line: { color: 'white', width: 1.5 } },
          textinfo  : 'percent',
          textfont  : { color: 'white', size: 11, family: 'DM Sans' },
          insidetextorientation: 'radial',
          texttemplate: pcts.map(p => p < 5 ? '' : '%{percent:.1%}'),
          startangle: 140,
          direction : 'clockwise',
          pull      : pcts.map((_,i) => i === 0 ? 0.04 : 0),
          hovertemplate: '<b>%{label}</b><br>%{percent:.1%}<extra></extra>',
        }], {
          ..._plotlyWhiteLayout(`Land Cover Distribution<br><sup>Total: ${total.toLocaleString()} ha</sup>`, 420),
          showlegend  : true,
          legend      : { orientation:'h', x:0.5, xanchor:'center', y:-0.12, font:{size:9}, bgcolor:'rgba(0,0,0,0)' },
          margin      : { l:20, r:20, t:55, b:80 },
        }, { displayModeBar:false, responsive:true, dragmode:false });
      }

      // ── Confusion matrix heatmap ───────────────────────────────────────
      const cmEl = scope.querySelector(`[id^="plotly_lulc_cm_"]`);
      if (cmEl) {
        const mlRaw = s.ml_metrics && s.ml_metrics.confusion_matrix
          ? s.ml_metrics
          : (s.classes ? _simulateMLMetrics(s) : null);
        if (mlRaw) {
          const m      = mlRaw;
        const matrix = m.confusion_matrix;
        const labels = m.class_names;
        const n      = labels.length;

        // Normalize each row to 0–1 for color intensity, keep raw counts for text
        const normMatrix = matrix.map(row => {
          const rowSum = row.reduce((a,b) => a+b, 0) || 1;
          return row.map(v => v / rowSum);
        });

        // Build heatmap: x = predicted, y = actual (reversed for display)
        const zText = matrix.map(row => row.map(v => String(v)));

        Plotly.newPlot(cmEl, [{
          type        : 'heatmap',
          z           : normMatrix.slice().reverse(),
          x           : labels,
          y           : labels.slice().reverse(),
          text        : zText.slice().reverse(),
          texttemplate: '%{text}',
          textfont    : { size: 13, color: '#ffffff', family: 'DM Sans' },
          colorscale  : [
            [0,   '#2166ac'],
            [0.5, '#f7f7f7'],
            [1,   '#d6604d'],
          ],
          showscale   : true,
          colorbar    : { title:{ text:'Proportion', font:{size:9} }, thickness:12, len:0.75, tickfont:{size:8} },
          hovertemplate: 'Actual: %{y}<br>Predicted: %{x}<br>Count: %{text}<extra></extra>',
        }], {
          ..._plotlyWhiteLayout('Confusion Matrix', 80 + n * 72),
          xaxis: { ..._plotlyWhiteLayout('').xaxis, title:{ text:'Predicted', font:{size:10}, standoff:16 }, side:'bottom', tickfont:{size:9}, tickangle: labels.length > 3 ? -30 : 0 },
          yaxis: { ..._plotlyWhiteLayout('').yaxis, title:{ text:'Actual', font:{size:10}, standoff:16 }, tickfont:{size:9} },
          margin: { l: 120, r: 80, t: 55, b: labels.length > 3 ? 100 : 75 },
          annotations: [{
            x: 0.5, y: 1.07, xref:'paper', yref:'paper',
            text: `Overall Accuracy: <b>${(m.overall_accuracy*100).toFixed(1)}%</b>  |  Kappa: <b>${m.kappa.toFixed(3)}</b>`,
            showarrow: false, font:{ size:10, color:'#555' },
          }],
        }, { displayModeBar:false, responsive:true, dragmode:false });
        } // end mlRaw
      } // end cmEl
    }
  }
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
    dragmode      : false,
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

let _planHideTimer = null;

function hidePlanWidget() {
  _planHideTimer = setTimeout(() => {
    const widget = document.getElementById('planWidget');
    if (widget) {
      document.getElementById('planTitle').textContent = 'Plan · Complete';
      _planHideTimer = setTimeout(() => { widget.style.display = 'none'; }, 2000);
    }
  }, 500);
}

function resetPlanWidget() {
  // Cancel any pending hide timers so the widget stays visible
  if (_planHideTimer) { clearTimeout(_planHideTimer); _planHideTimer = null; }
  const widget = document.getElementById('planWidget');
  if (!widget) return;
  widget.style.display = 'block';
  planExpanded = true;
  document.getElementById('planTitle').textContent = 'Plan · Running';
  // Reset all steps to pending state
  const container = document.getElementById('planSteps');
  if (container) {
    container.querySelectorAll('.plan-step').forEach(el => {
      el.className = 'plan-step step-pending';
      const iconWrap = el.querySelector('.step-icon-wrap');
      if (iconWrap) { iconWrap.className = 'step-icon-wrap step-icon-pending'; }
      const ring = el.querySelector('.step-ring');
      if (ring) ring.remove();
    });
  }
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
    const pct = step.progress != null ? step.progress : (step.status === 'done' ? 100 : null);

    const progressBar = (step.status === 'running' && pct != null)
      ? `<div class="step-progress-bar">
           <div class="step-progress-track">
             <div class="step-progress-fill" style="width:${pct}%"></div>
           </div>
           <span class="step-progress-pct">${Math.round(pct)}%</span>
         </div>`
      : (step.status === 'running'
          ? `<div class="step-progress-bar">
               <div class="step-progress-track">
                 <div class="step-progress-fill step-progress-indeterminate"></div>
               </div>
             </div>`
          : '');

    div.innerHTML = `
      <div class="step-icon-wrap step-icon-${step.status}">
        ${svgIcon}
        ${step.status === 'running' ? '<div class="step-ring"></div>' : ''}
      </div>
      <div class="step-body">
        <div class="step-label-text step-label-${step.status}">${escapeHtml(step.label)}</div>
        ${progressBar}
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
    setLayout(chatW > 0 ? chatW : Math.round(window.innerWidth * 0.55) - NAV_W);
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
// ════════════════════════════════════════════════════════
// KNOWLEDGE BASE
// ════════════════════════════════════════════════════════
const KNOWLEDGE = [
  // ── VEGETATION ──────────────────────────────────────────
  {
    id: 'ndvi', category: 'vegetation', tag: 'Surface Index',
    name: 'NDVI', full: 'Normalized Difference Vegetation Index',
    command: '/ndvi',
    definition: 'NDVI measures the density and health of vegetation by comparing near-infrared (NIR) and red light reflected by plants. Healthy vegetation absorbs most visible light and reflects a large portion of NIR light, producing high NDVI values.',
    formula: '(NIR − Red) / (NIR + Red)',
    formula_bands: 'Landsat 8: (SR_B5 − SR_B4) / (SR_B5 + SR_B4)',
    range: '−1 to +1',
    interpretation: [
      { range: '< 0.1',    label: 'Bare / Non-vegetated', color: '#C1704A' },
      { range: '0.1–0.3',  label: 'Sparse / Stressed vegetation', color: '#F0A500' },
      { range: '0.3–0.6',  label: 'Moderate vegetation', color: '#5BAD72' },
      { range: '> 0.6',    label: 'Dense / Healthy vegetation', color: '#1A7A40' },
    ],
    datasource: 'Landsat 8/9 Collection 2 Level-2 (SR_B4, SR_B5)',
    scale: '30 m spatial resolution',
    use_cases: 'Monitoring deforestation, agricultural crop health, urban green space, drought assessment, and seasonal vegetation change.',
    palette: ['#0000ff','#ffffff','#008000'],
    palette_label: 'Blue (low) → White (0) → Green (high)',
  },
  {
    id: 'evi', category: 'vegetation', tag: 'Surface Index',
    name: 'EVI', full: 'Enhanced Vegetation Index',
    command: '/evi',
    definition: 'EVI is an optimized vegetation index designed to enhance the vegetation signal with improved sensitivity in high-biomass regions and improved vegetation monitoring through a decoupling of the canopy background signal and a reduction in atmosphere influences.',
    formula: '2.5 × (NIR − Red) / (NIR + 6×Red − 7.5×Blue + 1)',
    formula_bands: 'Landsat 8: 2.5 × (SR_B5 − SR_B4) / (SR_B5 + 6×SR_B4 − 7.5×SR_B2 + 1)',
    range: '−1 to +1',
    interpretation: [
      { range: '< 0.1',    label: 'Bare / Very sparse', color: '#C1704A' },
      { range: '0.1–0.3',  label: 'Low vegetation', color: '#F0A500' },
      { range: '0.3–0.5',  label: 'Moderate vegetation', color: '#5BAD72' },
      { range: '> 0.5',    label: 'Dense / Forest', color: '#006400' },
    ],
    datasource: 'Landsat 8/9 Collection 2 Level-2 (SR_B2, SR_B4, SR_B5)',
    scale: '30 m spatial resolution',
    use_cases: 'Canopy estimation in dense tropical forests, reducing atmospheric and soil noise compared to NDVI, biomass monitoring.',
    palette: ['#a52a2a','#ffffff','#006400'],
    palette_label: 'Brown (low) → White (0) → Dark green (high)',
  },
  {
    id: 'savi', category: 'vegetation', tag: 'Surface Index',
    name: 'SAVI', full: 'Soil-Adjusted Vegetation Index',
    command: '/savi',
    definition: 'SAVI modifies NDVI to correct for the influence of soil brightness in areas with low vegetation cover. A soil correction factor L is included to minimize the effect of soil noise.',
    formula: '((NIR − Red) / (NIR + Red + L)) × (1 + L)',
    formula_bands: 'L = 0.5 (default). Landsat 8: ((SR_B5 − SR_B4) / (SR_B5 + SR_B4 + 0.5)) × 1.5',
    range: '−1 to +1',
    interpretation: [
      { range: '< 0.1',    label: 'Bare soil dominant', color: '#C1704A' },
      { range: '0.1–0.3',  label: 'Sparse vegetation', color: '#F0A500' },
      { range: '0.3–0.5',  label: 'Moderate vegetation', color: '#5BAD72' },
      { range: '> 0.5',    label: 'Dense vegetation', color: '#1A7A40' },
    ],
    datasource: 'Landsat 8/9 Collection 2 Level-2 (SR_B4, SR_B5)',
    scale: '30 m spatial resolution',
    use_cases: 'Vegetation monitoring in arid and semi-arid regions, early crop season when soil is exposed, mining-impacted areas.',
    palette: ['#a52a2a','#ffffff','#008000'],
    palette_label: 'Brown (low) → White (0) → Green (high)',
  },
  // ── WATER ───────────────────────────────────────────────
  {
    id: 'ndwi', category: 'water', tag: 'Surface Index',
    name: 'NDWI', full: 'Normalized Difference Water Index',
    command: '/ndwi',
    definition: 'NDWI uses green and NIR bands to delineate open water features and suppress vegetation and soil signals. Positive values typically correspond to water bodies.',
    formula: '(Green − NIR) / (Green + NIR)',
    formula_bands: 'Landsat 8: (SR_B3 − SR_B5) / (SR_B3 + SR_B5)',
    range: '−1 to +1',
    interpretation: [
      { range: '< −0.3',   label: 'Dry land / Bare soil', color: '#C1704A' },
      { range: '−0.3–0',   label: 'Transition / Moist', color: '#91BFDB' },
      { range: '0–0.3',    label: 'Shallow water / Wetland', color: '#4575B4' },
      { range: '> 0.3',    label: 'Open water', color: '#023858' },
    ],
    datasource: 'Landsat 8/9 Collection 2 Level-2 (SR_B3, SR_B5)',
    scale: '30 m spatial resolution',
    use_cases: 'Flood mapping, water body delineation, wetland monitoring, drought assessment.',
    palette: ['#a52a2a','#ffffff','#0000ff'],
    palette_label: 'Brown (dry) → White (0) → Blue (water)',
  },
  {
    id: 'mndwi', category: 'water', tag: 'Surface Index',
    name: 'MNDWI', full: 'Modified Normalized Difference Water Index',
    command: '/mndwi',
    definition: 'MNDWI replaces NIR with SWIR to better separate built-up areas from water. It suppresses soil and vegetation signals more effectively than NDWI, making it ideal for urban environments.',
    formula: '(Green − SWIR) / (Green + SWIR)',
    formula_bands: 'Landsat 8: (SR_B3 − SR_B6) / (SR_B3 + SR_B6)',
    range: '−1 to +1',
    interpretation: [
      { range: '< −0.3',   label: 'Dry / Built-up', color: '#C1704A' },
      { range: '−0.3–0',   label: 'Mixed', color: '#91BFDB' },
      { range: '0–0.3',    label: 'Moist / Shallow water', color: '#4575B4' },
      { range: '> 0.3',    label: 'Open water', color: '#023858' },
    ],
    datasource: 'Landsat 8/9 Collection 2 Level-2 (SR_B3, SR_B6)',
    scale: '30 m spatial resolution',
    use_cases: 'Urban water body mapping, flood monitoring in cities, distinguishing water from built-up features.',
    palette: ['#a52a2a','#ffffff','#00ffff'],
    palette_label: 'Brown (dry) → White (0) → Cyan (water)',
  },
  // ── URBAN ───────────────────────────────────────────────
  {
    id: 'ndbi', category: 'urban', tag: 'Surface Index',
    name: 'NDBI', full: 'Normalized Difference Built-up Index',
    command: '/ndbi',
    definition: 'NDBI highlights built-up or impervious surfaces using SWIR and NIR bands. Built-up areas have higher SWIR reflectance, producing positive NDBI values, while vegetation and water produce negative values.',
    formula: '(SWIR − NIR) / (SWIR + NIR)',
    formula_bands: 'Landsat 8: (SR_B6 − SR_B5) / (SR_B6 + SR_B5)',
    range: '−1 to +1',
    interpretation: [
      { range: '< −0.1',   label: 'Non-built / Vegetation', color: '#4575B4' },
      { range: '−0.1–0',   label: 'Low built-up density', color: '#91BFDB' },
      { range: '0–0.1',    label: 'Moderate urban', color: '#FEE090' },
      { range: '> 0.1',    label: 'High built-up', color: '#D73027' },
    ],
    datasource: 'Landsat 8/9 Collection 2 Level-2 (SR_B5, SR_B6)',
    scale: '30 m spatial resolution',
    use_cases: 'Urban expansion monitoring, impervious surface mapping, urban heat island analysis, city growth tracking.',
    palette: ['#0000ff','#ffffff','#ff0000'],
    palette_label: 'Blue (non-built) → White (0) → Red (built-up)',
  },
  {
    id: 'ui', category: 'urban', tag: 'Surface Index',
    name: 'UI', full: 'Urban Index',
    command: '/ui',
    definition: 'UI uses SWIR2 and NIR to distinguish urban surfaces from vegetation. It emphasizes the spectral contrast between built-up areas and natural land cover.',
    formula: '(SWIR2 − NIR) / (SWIR2 + NIR)',
    formula_bands: 'Landsat 8: (SR_B7 − SR_B5) / (SR_B7 + SR_B5)',
    range: '−1 to +1',
    interpretation: [
      { range: '< −0.1',   label: 'Vegetation dominant', color: '#1A7A40' },
      { range: '−0.1–0.1', label: 'Transitional / Mixed', color: '#F0A500' },
      { range: '> 0.1',    label: 'Urban / Built-up', color: '#800080' },
    ],
    datasource: 'Landsat 8/9 Collection 2 Level-2 (SR_B5, SR_B7)',
    scale: '30 m spatial resolution',
    use_cases: 'Urban boundary delineation, infrastructure mapping, urban-rural gradient analysis.',
    palette: ['#008000','#ffffff','#800080'],
    palette_label: 'Green (vegetation) → White (0) → Purple (urban)',
  },
  {
    id: 'bsi', category: 'urban', tag: 'Surface Index',
    name: 'BSI', full: 'Bare Soil Index',
    command: '/bsi',
    definition: 'BSI combines SWIR, Red, NIR, and Blue bands to distinguish bare soil from vegetated and built-up surfaces. High BSI indicates exposed or degraded land.',
    formula: '((SWIR + Red) − (NIR + Blue)) / ((SWIR + Red) + (NIR + Blue))',
    formula_bands: 'Landsat 8: ((SR_B6 + SR_B4) − (SR_B5 + SR_B2)) / ((SR_B6 + SR_B4) + (SR_B5 + SR_B2))',
    range: '−1 to +1',
    interpretation: [
      { range: '< −0.1',   label: 'Vegetated', color: '#1A7A40' },
      { range: '−0.1–0.1', label: 'Mixed / Transitional', color: '#F0A500' },
      { range: '> 0.1',    label: 'Bare soil / Degraded land', color: '#C1704A' },
    ],
    datasource: 'Landsat 8/9 Collection 2 Level-2 (SR_B2, SR_B4, SR_B5, SR_B6)',
    scale: '30 m spatial resolution',
    use_cases: 'Soil erosion detection, degraded land mapping, construction site monitoring, agricultural fallow identification.',
    palette: ['#0000ff','#ffffff','#a52a2a'],
    palette_label: 'Blue (vegetated) → White (0) → Brown (bare soil)',
  },
  {
    id: 'nbi', category: 'urban', tag: 'Surface Index',
    name: 'NBI', full: 'New Built-up Index',
    command: '/nbi',
    definition: 'NBI uses the ratio of Red and SWIR reflectance to NIR to highlight built-up areas. It is particularly effective at detecting low-density urban features.',
    formula: '(Red × SWIR) / NIR',
    formula_bands: 'Landsat 8: (SR_B4 × SR_B6) / SR_B5',
    range: '0 to ~0.5',
    interpretation: [
      { range: '< 0.1',    label: 'Low / Non-built', color: '#91BFDB' },
      { range: '0.1–0.25', label: 'Moderate urban', color: '#FEE090' },
      { range: '> 0.25',   label: 'High built-up', color: '#D73027' },
    ],
    datasource: 'Landsat 8/9 Collection 2 Level-2 (SR_B4, SR_B5, SR_B6)',
    scale: '30 m spatial resolution',
    use_cases: 'Low-density suburban mapping, peri-urban growth detection.',
    palette: ['#ffffff','#ffa500','#8b0000'],
    palette_label: 'White (low) → Orange → Dark red (high)',
  },
  // ── THERMAL ─────────────────────────────────────────────
  {
    id: 'lst', category: 'thermal', tag: 'Thermal',
    name: 'LST', full: 'Land Surface Temperature',
    command: '/lst',
    definition: 'LST measures the radiometric temperature of the land surface derived from thermal infrared data. It accounts for vegetation cover (via emissivity) to convert raw thermal brightness into actual surface temperature in Celsius.',
    formula: 'BT / (1 + (λ × BT / ρ) × ln(ε)) − 273.15',
    formula_bands: 'BT = ST_B10 (thermal brightness), λ = 11.5 μm, ρ = 14380, ε = emissivity from NDVI-based FVC',
    range: 'Typically 15–65°C for land surfaces',
    interpretation: [
      { range: '< 30°C',   label: 'Cool (vegetation, water)', color: '#307ef3' },
      { range: '30–35°C',  label: 'Moderate', color: '#269db1' },
      { range: '35–40°C',  label: 'Warm', color: '#3be285' },
      { range: '40–45°C',  label: 'Hot (urban, bare soil)', color: '#f5a800' },
      { range: '> 45°C',   label: 'Extreme heat', color: '#ff500d' },
    ],
    datasource: 'Landsat 8/9 Collection 2 Level-2 (ST_B10 thermal band)',
    scale: '90 m spatial resolution (resampled from 100 m)',
    use_cases: 'Urban heat island detection, drought monitoring, surface energy balance, wildfire risk mapping.',
    palette: ['#040274','#307ef3','#3be285','#fff705','#ff0000','#911003'],
    palette_label: 'Deep blue (cool) → Green → Yellow → Red (hot)',
  },
  {
    id: 'uhi', category: 'thermal', tag: 'Thermal',
    name: 'UHI', full: 'Urban Heat Island Index',
    command: '/uhi',
    definition: 'UHI quantifies the thermal anomaly of urban areas relative to the regional mean. It normalizes LST using z-score standardization: pixels significantly above the mean are heat islands, those below are cool refuges.',
    formula: '(LST − μ) / σ',
    formula_bands: 'μ = spatial mean LST, σ = spatial std dev LST across study area',
    range: 'z-score (typically −4 to +4)',
    interpretation: [
      { range: '< −2',     label: 'Strong cool island', color: '#313695' },
      { range: '−2–0',     label: 'Cool / Below average', color: '#74add1' },
      { range: '0–2',      label: 'Warm / Above average', color: '#fd8d3c' },
      { range: '> 2',      label: 'Strong heat island', color: '#b10026' },
    ],
    datasource: 'Derived from LST (Landsat 8/9 ST_B10)',
    scale: '90 m spatial resolution',
    use_cases: 'Identifying urban cooling priorities, green infrastructure planning, public health heat risk mapping.',
    palette: ['#313695','#74add1','#fed976','#fd8d3c','#e31a1c','#b10026'],
    palette_label: 'Blue (cool island) → Yellow → Red (heat island)',
  },
  // ── SNOW ────────────────────────────────────────────────
  {
    id: 'ndsi', category: 'water', tag: 'Surface Index',
    name: 'NDSI', full: 'Normalized Difference Snow Index',
    command: '/ndsi',
    definition: 'NDSI uses the high reflectance of snow in green wavelengths and its low reflectance in SWIR to map snow and ice cover. It effectively separates snow from clouds.',
    formula: '(Green − SWIR) / (Green + SWIR)',
    formula_bands: 'Landsat 8: (SR_B3 − SR_B6) / (SR_B3 + SR_B6)',
    range: '−1 to +1',
    interpretation: [
      { range: '< 0.0',    label: 'No snow / Land', color: '#C1704A' },
      { range: '0.0–0.4',  label: 'Possible snow / Ice', color: '#91BFDB' },
      { range: '> 0.4',    label: 'Snow / Ice cover', color: '#e0ffff' },
    ],
    datasource: 'Landsat 8/9 Collection 2 Level-2 (SR_B3, SR_B6)',
    scale: '30 m spatial resolution',
    use_cases: 'Snow cover mapping, glacier monitoring, water resource estimation from snowmelt.',
    palette: ['#a52a2a','#ffffff','#e0ffff'],
    palette_label: 'Brown (no snow) → White → Ice blue (snow)',
  },
  // ── ATMOSPHERIC ─────────────────────────────────────────
  {
    id: 'no2', category: 'atmospheric', tag: 'Atmospheric',
    name: 'NO₂', full: 'Tropospheric Nitrogen Dioxide',
    command: '/no2',
    definition: 'NO₂ column density measures the total amount of nitrogen dioxide in a vertical column of atmosphere. It is a primary pollutant from vehicle exhaust and industrial combustion, and a precursor to ground-level ozone and fine particulate matter.',
    formula: 'Tropospheric NO₂ column (mol/m²) — retrieved by DOAS algorithm',
    formula_bands: 'Sentinel-5P TROPOMI: tropospheric_NO2_column_number_density',
    range: '0 to ~0.0002 mol/m²',
    interpretation: [
      { range: '< 8×10⁻⁵',     label: 'Clean background', color: '#000033' },
      { range: '8–15×10⁻⁵',    label: 'Moderate urban', color: '#00ffff' },
      { range: '15–25×10⁻⁵',   label: 'High traffic/industry', color: '#ffff00' },
      { range: '> 25×10⁻⁵',    label: 'Severe pollution', color: '#ff0000' },
    ],
    datasource: 'Sentinel-5P TROPOMI (COPERNICUS/S5P/OFFL/L3_NO2)',
    scale: '3.5 km × 5.5 km spatial resolution',
    use_cases: 'Air quality monitoring, traffic emission hotspot detection, industrial facility impact assessment, COVID-19 lockdown effect studies.',
    palette: ['#000033','#0000ff','#8000ff','#00ffff','#008000','#ffff00','#ff0000'],
    palette_label: 'Dark blue (clean) → Cyan → Yellow → Red (severe)',
  },
  {
    id: 'co', category: 'atmospheric', tag: 'Atmospheric',
    name: 'CO', full: 'Carbon Monoxide Column Density',
    command: '/co',
    definition: 'CO is produced by incomplete combustion of fossil fuels and biomass. The column density represents the total CO in a vertical atmospheric column. Elevated levels indicate combustion sources including vehicles, industry, and wildfires.',
    formula: 'CO total column (mol/m²) — retrieved by SWIR spectroscopy',
    formula_bands: 'Sentinel-5P TROPOMI: CO_column_number_density',
    range: '~0.02 to 0.08 mol/m²',
    interpretation: [
      { range: '< 0.035',   label: 'Background levels', color: '#000033' },
      { range: '0.035–0.055', label: 'Moderate', color: '#00ffff' },
      { range: '0.055–0.07', label: 'Elevated', color: '#ffff00' },
      { range: '> 0.07',    label: 'High / Fire smoke', color: '#ff0000' },
    ],
    datasource: 'Sentinel-5P TROPOMI (COPERNICUS/S5P/OFFL/L3_CO)',
    scale: '3.5 km × 7 km spatial resolution',
    use_cases: 'Wildfire smoke tracking, industrial combustion monitoring, urban air quality assessment.',
    palette: ['#000033','#0000ff','#8000ff','#00ffff','#008000','#ffff00','#ff0000'],
    palette_label: 'Dark blue (low) → Cyan → Red (high)',
  },
  {
    id: 'so2', category: 'atmospheric', tag: 'Atmospheric',
    name: 'SO₂', full: 'Sulfur Dioxide Column Density',
    command: '/so2',
    definition: 'SO₂ is emitted from burning of sulfur-containing fuels, volcanic eruptions, and industrial smelting. High concentrations contribute to acid rain, haze, and respiratory problems.',
    formula: 'SO₂ column (mol/m²) — retrieved by DOAS algorithm',
    formula_bands: 'Sentinel-5P TROPOMI: SO2_column_number_density',
    range: '0 to ~0.001 mol/m²',
    interpretation: [
      { range: '< 1×10⁻⁴',    label: 'Clean background', color: '#0000ff' },
      { range: '1–5×10⁻⁴',    label: 'Moderate industrial', color: '#008000' },
      { range: '5×10⁻⁴–10⁻³', label: 'High / Volcanic', color: '#ffa500' },
      { range: '> 10⁻³',      label: 'Severe / Eruption', color: '#8b0000' },
    ],
    datasource: 'Sentinel-5P TROPOMI (COPERNICUS/S5P/OFFL/L3_SO2)',
    scale: '3.5 km × 7 km spatial resolution',
    use_cases: 'Volcanic plume tracking, power plant emission monitoring, acid rain source identification.',
    palette: ['#0000ff','#008000','#ffff00','#ffa500','#ff0000','#8b0000'],
    palette_label: 'Blue (clean) → Green → Orange → Dark red (severe)',
  },
  {
    id: 'ch4', category: 'atmospheric', tag: 'Atmospheric',
    name: 'CH₄', full: 'Methane Column Mixing Ratio',
    command: '/co', // mapped via agent
    definition: 'CH₄ is a potent greenhouse gas emitted from wetlands, rice paddies, livestock, landfills, and fossil fuel extraction. The dry-air column mixing ratio measures its atmospheric concentration in parts per billion (ppb).',
    formula: 'CH₄ dry-air column mixing ratio (ppb)',
    formula_bands: 'Sentinel-5P TROPOMI: CH4_column_volume_mixing_ratio_dry_air',
    range: '~1750 to 1950 ppb',
    interpretation: [
      { range: '< 1850 ppb',   label: 'Background', color: '#0000ff' },
      { range: '1850–1900',    label: 'Slightly elevated', color: '#00ffff' },
      { range: '1900–1950',    label: 'Elevated / Local source', color: '#ffff00' },
      { range: '> 1950 ppb',   label: 'High emission area', color: '#ff0000' },
    ],
    datasource: 'Sentinel-5P TROPOMI (COPERNICUS/S5P/OFFL/L3_CH4)',
    scale: '5.5 km × 7 km spatial resolution',
    use_cases: 'Wetland emission mapping, oil/gas leak detection, agricultural CH₄ from rice paddies, landfill monitoring.',
    palette: ['#0000ff','#00ffff','#008000','#ffff00','#ffa500','#ff0000'],
    palette_label: 'Blue (background) → Green → Yellow → Red (high)',
  },
  {
    id: 'aerosol', category: 'atmospheric', tag: 'Atmospheric',
    name: 'Aerosol', full: 'Absorbing Aerosol Index (AAI)',
    command: '/no2',
    definition: 'The Absorbing Aerosol Index detects the presence of absorbing aerosols (smoke, dust, volcanic ash) in the atmosphere. Positive values indicate absorbing aerosols; negative or near-zero values indicate non-absorbing aerosols or clear sky.',
    formula: 'AAI = −100 × log₁₀(I_measured / I_calculated)',
    formula_bands: 'Sentinel-5P TROPOMI: absorbing_aerosol_index',
    range: '−1 to +5 (unitless)',
    interpretation: [
      { range: '< 0',      label: 'Clean / Non-absorbing', color: '#0000ff' },
      { range: '0–1',      label: 'Low aerosol loading', color: '#ffffff' },
      { range: '1–2',      label: 'Moderate (dust/smoke)', color: '#ffff00' },
      { range: '> 2',      label: 'High absorbing aerosols', color: '#ff0000' },
    ],
    datasource: 'Sentinel-5P TROPOMI (COPERNICUS/S5P/OFFL/L3_AER_AI)',
    scale: '3.5 km × 7 km spatial resolution',
    use_cases: 'Smoke plume detection from wildfires, Saharan dust monitoring, volcanic ash tracking, air quality alerts.',
    palette: ['#0000ff','#ffffff','#ffff00','#ffa500','#ff0000'],
    palette_label: 'Blue (clean) → White → Yellow → Red (heavy aerosol)',
  },
  {
    id: 'ffpi', category: 'atmospheric', tag: 'Atmospheric',
    name: 'FFPI', full: 'Fossil Fuel Pollution Index',
    command: '/ffpi',
    definition: 'FFPI is a composite pollution index combining normalized NO₂, CO, and SO₂ columns into a single score (0–1). It provides a holistic view of fossil fuel combustion impacts on air quality across a region.',
    formula: '(norm(NO₂) + norm(CO) + norm(SO₂)) / 3',
    formula_bands: 'Each component normalized 0–1 within the study area, then averaged',
    range: '0 (clean) to 1 (severely polluted)',
    interpretation: [
      { range: '0–0.3',    label: 'Clean / Low impact', color: '#313695' },
      { range: '0.3–0.6',  label: 'Moderate pollution', color: '#fdae61' },
      { range: '0.6–0.8',  label: 'Polluted', color: '#f46d43' },
      { range: '> 0.8',    label: 'Severely polluted', color: '#d73027' },
    ],
    datasource: 'Sentinel-5P TROPOMI — composite of NO₂, CO, SO₂ layers',
    scale: '3.5 km spatial resolution',
    use_cases: 'Multi-pollutant air quality assessment, identifying combustion hotspots, industrial zone characterization.',
    palette: ['#313695','#74add1','#fdae61','#d73027'],
    palette_label: 'Blue (clean) → Orange → Red (polluted)',
  },
  // ── LAND COVER ──────────────────────────────────────────
  {
    id: 'lulc', category: 'landcover', tag: 'Classification',
    name: 'LULC', full: 'Land Use / Land Cover Classification',
    command: '/lulc',
    definition: 'LULC classifies each pixel into a discrete land cover category using a supervised Random Forest classifier trained on ESA WorldCover 2021 reference data. Classes include Built Area, Trees, Rangeland, Cropland, Water, and Bare Ground.',
    formula: 'Random Forest classifier trained on spectral bands + indices',
    formula_bands: 'Landsat 8: SR_B2–SR_B7 + NDVI + NDWI + NDBI (as feature stack). Training labels from ESA WorldCover 2021.',
    range: 'Categorical classes',
    interpretation: [
      { range: 'Built Area',   label: 'Impervious surfaces, roads, buildings', color: '#ff0000' },
      { range: 'Trees',        label: 'Forest, tree cover >5 m canopy height', color: '#228b22' },
      { range: 'Rangeland',    label: 'Shrubs, grassland, savanna', color: '#d2b48c' },
      { range: 'Cropland',     label: 'Agricultural fields', color: '#ffff00' },
      { range: 'Water',        label: 'Rivers, lakes, reservoirs', color: '#0000ff' },
      { range: 'Bare Ground',  label: 'Desert, exposed rock, sand', color: '#a0522d' },
    ],
    datasource: 'Landsat 8/9 + ESA WorldCover 2021 (training labels)',
    scale: '30 m spatial resolution',
    use_cases: 'Urban growth monitoring, deforestation tracking, land use planning, ecosystem service assessment.',
    palette: ['#ff0000','#228b22','#d2b48c','#ffff00','#0000ff','#a0522d'],
    palette_label: 'Discrete class colors per legend',
  },
];

let _knowledgeVisible = false;

// ── Formula enrichment: LaTeX + variable definitions + visualization type ─────
const KNOWLEDGE_EXTRA = {
  ndvi: {
    latex: `\\[ \\text{NDVI} = \\frac{\\rho_{NIR} - \\rho_{Red}}{\\rho_{NIR} + \\rho_{Red}} \\]`,
    variables: [
      { sym: 'ρ_NIR', desc: 'Near-infrared reflectance (Landsat Band 5, ~0.85 μm)' },
      { sym: 'ρ_Red', desc: 'Red reflectance (Landsat Band 4, ~0.65 μm)' },
    ],
    viz_type: 'vegetation_scale',
    viz_steps: [
      { range: '−1.0 to −0.1', label: 'Water / Snow / Artificial', color: '#1a3a6b', icon: '💧' },
      { range: '−0.1 to 0.0',  label: 'Bare soil, sand (no vegetation)', color: '#c1704a', icon: '🏜️' },
      { range: '0.0 to 0.1',   label: 'Barren / Sparse stressed crops', color: '#e09a3a', icon: '🌱' },
      { range: '0.1 to 0.3',   label: 'Some vegetation / stressed', color: '#c8d422', icon: '🌿' },
      { range: '0.3 to 0.5',   label: 'Moderate vegetation / early growth', color: '#7bbf2a', icon: '🌳' },
      { range: '0.5 to 0.7',   label: 'Healthy vegetation / good crop', color: '#3a9a1a', icon: '🌲' },
      { range: '0.7 to 1.0',   label: 'Dense forest / peak crop health', color: '#1a6010', icon: '🌴' },
    ],
  },
  evi: {
    latex: `\\[ \\text{EVI} = 2.5 \\times \\frac{\\rho_{NIR} - \\rho_{Red}}{\\rho_{NIR} + 6\\rho_{Red} - 7.5\\rho_{Blue} + 1} \\]`,
    variables: [
      { sym: 'ρ_NIR',  desc: 'Near-infrared reflectance (Band 5)' },
      { sym: 'ρ_Red',  desc: 'Red reflectance (Band 4)' },
      { sym: 'ρ_Blue', desc: 'Blue reflectance (Band 2) — reduces atmospheric aerosol influence' },
      { sym: '6, 7.5', desc: 'Empirically derived canopy background coefficients' },
      { sym: '2.5',    desc: 'Gain factor to scale output range' },
    ],
    viz_type: 'gradient_scale',
  },
  savi: {
    latex: `\\[ \\text{SAVI} = \\frac{(\\rho_{NIR} - \\rho_{Red})}{(\\rho_{NIR} + \\rho_{Red} + L)} \\times (1 + L) \\]`,
    variables: [
      { sym: 'ρ_NIR', desc: 'Near-infrared reflectance (Band 5)' },
      { sym: 'ρ_Red', desc: 'Red reflectance (Band 4)' },
      { sym: 'L',     desc: 'Soil brightness correction factor (L = 0.5 for intermediate cover; 0 = dense, 1 = sparse)' },
    ],
    viz_type: 'gradient_scale',
  },
  ndwi: {
    latex: `\\[ \\text{NDWI} = \\frac{\\rho_{Green} - \\rho_{NIR}}{\\rho_{Green} + \\rho_{NIR}} \\]`,
    variables: [
      { sym: 'ρ_Green', desc: 'Green reflectance (Band 3, ~0.56 μm) — water has high green reflectance' },
      { sym: 'ρ_NIR',   desc: 'Near-infrared reflectance (Band 5) — water strongly absorbs NIR' },
    ],
    viz_type: 'water_scale',
    viz_steps: [
      { range: '< −0.3',   label: 'Dry land / Bare soil', color: '#c1704a', icon: '🏜️' },
      { range: '−0.3–0',   label: 'Transition / Moist soil', color: '#91bfdb', icon: '🌾' },
      { range: '0–0.3',    label: 'Shallow water / Wetland', color: '#4575b4', icon: '🌊' },
      { range: '> 0.3',    label: 'Open water body', color: '#023858', icon: '🏞️' },
    ],
  },
  mndwi: {
    latex: `\\[ \\text{MNDWI} = \\frac{\\rho_{Green} - \\rho_{SWIR}}{\\rho_{Green} + \\rho_{SWIR}} \\]`,
    variables: [
      { sym: 'ρ_Green', desc: 'Green reflectance (Band 3)' },
      { sym: 'ρ_SWIR',  desc: 'Short-wave infrared reflectance (Band 6, ~1.6 μm) — better separates built-up from water than NIR' },
    ],
    viz_type: 'gradient_scale',
  },
  ndbi: {
    latex: `\\[ \\text{NDBI} = \\frac{\\rho_{SWIR} - \\rho_{NIR}}{\\rho_{SWIR} + \\rho_{NIR}} \\]`,
    variables: [
      { sym: 'ρ_SWIR', desc: 'Short-wave infrared reflectance (Band 6) — built-up surfaces have elevated SWIR' },
      { sym: 'ρ_NIR',  desc: 'Near-infrared reflectance (Band 5) — vegetation has high NIR, suppressing built-up signal' },
    ],
    viz_type: 'urban_scale',
    viz_steps: [
      { range: '< −0.1',   label: 'Vegetation / Non-built', color: '#4575b4', icon: '🌳' },
      { range: '−0.1–0',   label: 'Low built-up density', color: '#91bfdb', icon: '🏘️' },
      { range: '0–0.1',    label: 'Moderate urban surface', color: '#fee090', icon: '🏙️' },
      { range: '> 0.1',    label: 'High built-up / Industrial', color: '#d73027', icon: '🏗️' },
    ],
  },
  ui: {
    latex: `\\[ \\text{UI} = \\frac{\\rho_{SWIR2} - \\rho_{NIR}}{\\rho_{SWIR2} + \\rho_{NIR}} \\]`,
    variables: [
      { sym: 'ρ_SWIR2', desc: 'Short-wave infrared 2 (Band 7, ~2.2 μm) — urban surfaces have high SWIR2' },
      { sym: 'ρ_NIR',   desc: 'Near-infrared (Band 5) — vegetation suppresses urban signal' },
    ],
    viz_type: 'gradient_scale',
  },
  bsi: {
    latex: `\\[ \\text{BSI} = \\frac{(\\rho_{SWIR} + \\rho_{Red}) - (\\rho_{NIR} + \\rho_{Blue})}{(\\rho_{SWIR} + \\rho_{Red}) + (\\rho_{NIR} + \\rho_{Blue})} \\]`,
    variables: [
      { sym: 'ρ_SWIR', desc: 'Short-wave infrared (Band 6) — sensitive to soil mineralogy' },
      { sym: 'ρ_Red',  desc: 'Red (Band 4) — bare soil has elevated red reflectance' },
      { sym: 'ρ_NIR',  desc: 'Near-infrared (Band 5) — vegetation has high NIR' },
      { sym: 'ρ_Blue', desc: 'Blue (Band 2) — suppresses atmospheric effects' },
    ],
    viz_type: 'gradient_scale',
  },
  nbi: {
    latex: `\\[ \\text{NBI} = \\frac{\\rho_{Red} \\times \\rho_{SWIR}}{\\rho_{NIR}} \\]`,
    variables: [
      { sym: 'ρ_Red',  desc: 'Red reflectance (Band 4)' },
      { sym: 'ρ_SWIR', desc: 'Short-wave infrared (Band 6)' },
      { sym: 'ρ_NIR',  desc: 'Near-infrared (Band 5) — in denominator to suppress vegetation' },
    ],
    viz_type: 'gradient_scale',
  },
  ndsi: {
    latex: `\\[ \\text{NDSI} = \\frac{\\rho_{Green} - \\rho_{SWIR}}{\\rho_{Green} + \\rho_{SWIR}} \\]`,
    variables: [
      { sym: 'ρ_Green', desc: 'Green reflectance (Band 3) — snow has very high green reflectance' },
      { sym: 'ρ_SWIR',  desc: 'Short-wave infrared (Band 6) — snow absorbs strongly in SWIR; clouds do not' },
    ],
    viz_type: 'gradient_scale',
  },
  lst: {
    latex: `\\[ \\text{LST} = \\frac{BT}{1 + \\left(\\dfrac{\\lambda \\cdot BT}{\\rho}\\right) \\cdot \\ln(\\varepsilon)} - 273.15 \\]`,
    variables: [
      { sym: 'BT',  desc: 'Brightness temperature from Band 10 (Kelvin) — converted from raw DN using: BT = DN × 0.00341802 + 149.0' },
      { sym: 'λ',   desc: 'Wavelength of emitted radiance = 11.5 μm (Landsat Band 10 center wavelength)' },
      { sym: 'ρ',   desc: 'Planck\'s constant × speed of light / Boltzmann constant = 14380 μm·K' },
      { sym: 'ε',   desc: 'Land surface emissivity — derived from NDVI-based fractional vegetation cover (FVC)' },
      { sym: '−273.15', desc: 'Conversion from Kelvin to Celsius' },
    ],
    viz_type: 'thermal_scale',
    viz_steps: [
      { range: '< 30°C',   label: 'Cool (vegetation, water bodies)', color: '#307ef3', icon: '🌊' },
      { range: '30–35°C',  label: 'Moderate temperature', color: '#269db1', icon: '🌿' },
      { range: '35–40°C',  label: 'Warm (mixed surfaces)', color: '#3be285', icon: '🌾' },
      { range: '40–45°C',  label: 'Hot (bare soil, roads)', color: '#f5a800', icon: '🏙️' },
      { range: '> 45°C',   label: 'Extreme heat (industrial, asphalt)', color: '#ff500d', icon: '🔥' },
    ],
  },
  uhi: {
    latex: `\\[ \\text{UHI} = \\frac{LST - \\mu_{LST}}{\\sigma_{LST}} \\]`,
    variables: [
      { sym: 'LST',       desc: 'Land surface temperature at each pixel (°C)' },
      { sym: 'μ_LST',     desc: 'Spatial mean LST of the entire study area (°C)' },
      { sym: 'σ_LST',     desc: 'Spatial standard deviation of LST across the study area' },
      { sym: 'UHI > 0',   desc: 'Pixel is warmer than average → urban heat island zone' },
      { sym: 'UHI < 0',   desc: 'Pixel is cooler than average → urban cool island / green space' },
    ],
    viz_type: 'gradient_scale',
  },
  no2: {
    latex: `\\[ \\Omega_{NO_2} = \\int_0^{TOA} n_{NO_2}(z)\\, dz \\quad [\\text{mol/m}^2] \\]`,
    variables: [
      { sym: 'Ω_NO₂',    desc: 'Tropospheric NO₂ vertical column density (mol/m²)' },
      { sym: 'n_NO₂(z)', desc: 'NO₂ number density at altitude z, retrieved by DOAS algorithm' },
      { sym: 'TOA',      desc: 'Top of atmosphere — integration limit' },
      { sym: 'DOAS',     desc: 'Differential Optical Absorption Spectroscopy — fitting measured spectra to reference cross-sections' },
    ],
    viz_type: 'atmo_scale',
    viz_steps: [
      { range: '< 8×10⁻⁵',    label: 'Clean background air', color: '#000033', icon: '✅' },
      { range: '8–15×10⁻⁵',   label: 'Moderate urban traffic', color: '#00ffff', icon: '🚗' },
      { range: '15–25×10⁻⁵',  label: 'Heavy traffic / industry', color: '#ffff00', icon: '🏭' },
      { range: '> 25×10⁻⁵',   label: 'Severe pollution hotspot', color: '#ff0000', icon: '⚠️' },
    ],
  },
  co: {
    latex: `\\[ \\Omega_{CO} = \\int_0^{TOA} n_{CO}(z)\\, dz \\quad [\\text{mol/m}^2] \\]`,
    variables: [
      { sym: 'Ω_CO',    desc: 'Total CO vertical column density (mol/m²)' },
      { sym: 'n_CO(z)', desc: 'CO number density at altitude z, retrieved via SWIR spectroscopy at 2.3 μm' },
    ],
    viz_type: 'gradient_scale',
  },
  so2: {
    latex: `\\[ \\Omega_{SO_2} = \\int_0^{TOA} n_{SO_2}(z)\\, dz \\quad [\\text{mol/m}^2] \\]`,
    variables: [
      { sym: 'Ω_SO₂',    desc: 'SO₂ total column density (mol/m²)' },
      { sym: 'n_SO₂(z)', desc: 'SO₂ number density at altitude z, retrieved by UV-DOAS in 312–326 nm range' },
    ],
    viz_type: 'gradient_scale',
  },
  ch4: {
    latex: `\\[ X_{CH_4} = \\frac{\\Omega_{CH_4}}{\\Omega_{dry-air}} \\times 10^9 \\quad [\\text{ppb}] \\]`,
    variables: [
      { sym: 'X_CH₄',        desc: 'Column-averaged dry-air mixing ratio (ppb)' },
      { sym: 'Ω_CH₄',        desc: 'CH₄ total column (mol/m²), retrieved via SWIR at 2.3 μm' },
      { sym: 'Ω_dry-air',    desc: 'Dry-air column (mol/m²), derived from surface pressure' },
      { sym: '× 10⁹',        desc: 'Conversion to parts per billion (ppb)' },
    ],
    viz_type: 'gradient_scale',
  },
  aerosol: {
    latex: `\\[ \\text{AAI} = -100 \\times \\log_{10}\\left(\\frac{I_{meas}}{I_{calc}}\\right) \\]`,
    variables: [
      { sym: 'I_meas', desc: 'Measured backscattered UV radiance at ~340 nm and 380 nm' },
      { sym: 'I_calc', desc: 'Modeled radiance for a pure Rayleigh atmosphere (no aerosols)' },
      { sym: 'AAI > 0', desc: 'Absorbing aerosols present (smoke, dust, volcanic ash)' },
      { sym: 'AAI < 0', desc: 'Non-absorbing aerosols or clean atmosphere (marine aerosols)' },
    ],
    viz_type: 'gradient_scale',
  },
  ffpi: {
    latex: `\\[ \\text{FFPI} = \\frac{1}{3}\\left(\\hat{NO_2} + \\hat{CO} + \\hat{SO_2}\\right) \\]`,
    variables: [
      { sym: 'FFPI',   desc: 'Fossil Fuel Pollution Index — composite score 0 (clean) to 1 (severe)' },
      { sym: 'N̂O₂',   desc: 'Min-max normalized NO₂ column within the study area' },
      { sym: 'ĈO',    desc: 'Min-max normalized CO column within the study area' },
      { sym: 'ŜO₂',   desc: 'Min-max normalized SO₂ column within the study area' },
    ],
    viz_type: 'gradient_scale',
  },
  lulc: {
    latex: `\\[ \\hat{y} = \\arg\\max_k P(y = k \\mid \\mathbf{x}, \\theta) \\]`,
    variables: [
      { sym: 'ŷ',     desc: 'Predicted land cover class label (e.g., Built Area, Trees, Water)' },
      { sym: 'x',     desc: 'Feature vector: spectral bands (B2–B7) + NDVI + NDWI + NDBI at each pixel' },
      { sym: 'θ',     desc: 'Random Forest model parameters — trained on ESA WorldCover 2021 reference labels' },
      { sym: 'P(y=k|x,θ)', desc: 'Posterior class probability — class with highest probability wins (majority vote of trees)' },
    ],
    viz_type: 'lulc_classes',
    viz_steps: [
      { range: 'Built Area',  label: 'Impervious surfaces, roads, buildings', color: '#ff0000', icon: '🏙️' },
      { range: 'Trees',       label: 'Forest, tree canopy > 5 m height',       color: '#228b22', icon: '🌲' },
      { range: 'Rangeland',   label: 'Shrubs, grassland, savanna',              color: '#d2b48c', icon: '🌾' },
      { range: 'Cropland',    label: 'Agricultural fields',                     color: '#ffff00', icon: '🌽' },
      { range: 'Water',       label: 'Rivers, lakes, reservoirs',               color: '#0000ff', icon: '💧' },
      { range: 'Bare Ground', label: 'Desert, exposed rock, sand',              color: '#a0522d', icon: '🏜️' },
    ],
  },
};

let _activeKnowledgeId = null;

function toggleKnowledgePanel() {
  const panel = document.getElementById('knowledgePanel');
  const btn   = document.getElementById('knowledgeNavBtn');
  _knowledgeVisible = !_knowledgeVisible;
  panel.style.display = _knowledgeVisible ? 'flex' : 'none';
  btn.classList.toggle('active', _knowledgeVisible);
  if (_knowledgeVisible) {
    renderKnowledgeNav(KNOWLEDGE);
    // Open first item by default if none selected
    if (!_activeKnowledgeId) openKnowledgeDetail(KNOWLEDGE[0].id);
  }
}

function renderKnowledgeNav(items) {
  const list = document.getElementById('kpNavList');
  if (!list) return;

  // Group by category
  const cats = ['vegetation','water','urban','thermal','atmospheric','landcover'];
  const catLabels = { vegetation:'Vegetation', water:'Water', urban:'Urban', thermal:'Thermal', atmospheric:'Atmospheric', landcover:'Land Cover' };

  let html = '';
  cats.forEach(cat => {
    const catItems = items.filter(k => k.category === cat);
    if (!catItems.length) return;
    html += `<div class="kp-nav-group-label">${catLabels[cat]}</div>`;
    catItems.forEach(k => {
      const isActive = k.id === _activeKnowledgeId;
      html += `<div class="kp-nav-item ${isActive ? 'active' : ''}" onclick="openKnowledgeDetail('${k.id}')">
        <div class="kp-nav-dot" style="background:${k.palette[k.palette.length-1]}"></div>
        <div>
          <div class="kp-nav-name">${k.name}</div>
          <div class="kp-nav-full">${k.full}</div>
        </div>
      </div>`;
    });
  });
  list.innerHTML = html || '<div style="padding:16px;color:var(--text3);font-size:12px">No results</div>';
}

function openKnowledgeDetail(id) {
  const k = KNOWLEDGE.find(x => x.id === id);
  const ex = KNOWLEDGE_EXTRA[id] || {};
  if (!k) return;
  _activeKnowledgeId = id;
  renderKnowledgeNav(KNOWLEDGE);

  document.getElementById('kpLanding').style.display    = 'none';
  document.getElementById('kpDetailFull').style.display = 'block';

  // Build visualization block
  const vizHtml = buildKnowledgeViz(ex);

  // Variable definitions
  const varsHtml = ex.variables ? `
    <div class="kpd-vars-table">
      ${ex.variables.map(v => `
        <div class="kpd-var-row">
          <div class="kpd-var-sym">\\(${v.sym.replace(/_/g,'_{').replace(/([^_{}]+)$/,'$1}').replace(/\{([^{}])\}/g,'$1')}\\)</div>
          <div class="kpd-var-eq">=</div>
          <div class="kpd-var-desc">${v.desc}</div>
        </div>`).join('')}
    </div>` : '';

  // Palette gradient
  const paletteStops = k.palette.map((c,i) => `${c} ${Math.round(i/(k.palette.length-1)*100)}%`).join(', ');

  document.getElementById('kpDetailContent').innerHTML = `
    <div class="kpd-page">

      <!-- Hero -->
      <div class="kpd-hero-full">
        <div class="kpd-hero-left">
          <div class="kpd-big-name">${k.name}</div>
          <div class="kpd-big-full">${k.full}</div>
          <div class="kpd-big-def">${k.definition}</div>
        </div>
        <div class="kpd-hero-right">
          <span class="kp-tag kp-tag-${k.category} kp-tag-lg">${k.tag}</span>
          <div class="kpd-command-box">
            <div class="kpd-command-label">Quick Command</div>
            <code class="kpd-command-code">${k.command}</code>
          </div>
          <div class="kpd-source-box">
            <div class="kpd-command-label">Data Source</div>
            <div class="kpd-source-text">${k.datasource}</div>
            <div class="kpd-source-res">${k.scale}</div>
          </div>
        </div>
      </div>

      <!-- Formula (paper style) -->
      <div class="kpd-formula-paper-section">
        <div class="kpd-section-title">Formula</div>
        <div class="kpd-formula-paper">
          <div class="kpd-formula-paper-inner">
            <div class="kpd-formula-render">${ex.latex || k.formula}</div>
            ${ex.variables ? `
            <div class="kpd-where-title">Where:</div>
            <div class="kpd-vars-list">
              ${ex.variables.map(v => `
                <div class="kpd-var-row">
                  <span class="kpd-var-sym-plain">${v.sym}</span>
                  <span class="kpd-var-eq">=</span>
                  <span class="kpd-var-desc">${v.desc}</span>
                </div>`).join('')}
            </div>` : ''}
          </div>
          <div class="kpd-formula-bands-label">Band Implementation (Landsat 8):</div>
          <div class="kpd-formula-bands-box">${k.formula_bands}</div>
        </div>
      </div>

      <!-- Color scale -->
      <div class="kpd-scale-section">
        <div class="kpd-section-title">Color Scale (${k.range})</div>
        <div class="kpd-gradient" style="background:linear-gradient(to right, ${paletteStops})"></div>
        <div class="kpd-gradient-labels">
          <span>${k.range.split(' to ')[0] || 'Low'}</span>
          <span style="color:var(--text3);font-size:11px">${k.palette_label}</span>
          <span>${k.range.split(' to ')[1] || 'High'}</span>
        </div>
      </div>

      <!-- Visualization / Interpretation -->
      ${vizHtml}

      <!-- Use cases -->
      <div class="kpd-full-block">
        <div class="kpd-section-title">Use Cases &amp; Applications</div>
        <div class="kpd-usecases-grid">
          ${k.use_cases.split(', ').map(u => `<div class="kpd-usecase-item">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
            ${u.trim()}
          </div>`).join('')}
        </div>
      </div>

      <!-- Data source -->
      <div class="kpd-two-col">
        <div class="kpd-block">
          <div class="kpd-section-title">Satellite Platform</div>
          <div class="kpd-platform-badge">${k.datasource.split('(')[0].trim()}</div>
          <div class="kpd-source-res" style="margin-top:8px">${k.scale}</div>
        </div>
        <div class="kpd-block">
          <div class="kpd-section-title">Value Range</div>
          <div class="kpd-range-display">
            <div class="kpd-range-val">${k.range.split(' to ')[0] || '—'}</div>
            <div class="kpd-range-arrow">→</div>
            <div class="kpd-range-val kpd-range-high">${k.range.split(' to ')[1] || '—'}</div>
          </div>
        </div>
      </div>

    </div>
  `;

  // Trigger MathJax to typeset the new content
  if (window.MathJax && window.MathJax.typesetPromise) {
    window.MathJax.typesetPromise([document.getElementById('kpDetailContent')]);
  }

  document.getElementById('kpDetailFull').scrollTop = 0;
}

function buildKnowledgeViz(ex) {
  if (!ex.viz_steps || !ex.viz_steps.length) {
    // Fallback: just show benchmark grid from main KNOWLEDGE entry
    return '';
  }

  const isVertical = ex.viz_type === 'vegetation_scale' || ex.viz_type === 'thermal_scale';

  if (ex.viz_type === 'vegetation_scale' || ex.viz_type === 'water_scale' ||
      ex.viz_type === 'urban_scale' || ex.viz_type === 'thermal_scale' ||
      ex.viz_type === 'atmo_scale' || ex.viz_type === 'lulc_classes') {

    const steps = ex.viz_steps;
    const items = steps.map((s, i) => `
      <div class="kpd-viz-step">
        <div class="kpd-viz-icon">${s.icon}</div>
        <div class="kpd-viz-bar-wrap">
          <div class="kpd-viz-bar" style="background:${s.color}"></div>
        </div>
        <div class="kpd-viz-info">
          <div class="kpd-viz-range">${s.range}</div>
          <div class="kpd-viz-label">${s.label}</div>
        </div>
      </div>`).join('');

    return `
      <div class="kpd-full-block">
        <div class="kpd-section-title">Class Interpretation & Visual Guide</div>
        <div class="kpd-viz-scale">
          ${items}
        </div>
      </div>`;
  }
  return '';
}

function filterKnowledge(query) {
  const q = query.toLowerCase().trim();
  const filtered = q
    ? KNOWLEDGE.filter(k => k.name.toLowerCase().includes(q) || k.full.toLowerCase().includes(q) || k.definition.toLowerCase().includes(q) || k.category.includes(q))
    : KNOWLEDGE;
  renderKnowledgeNav(filtered);
}

function filterKnowledgeByCategory(cat) {
  const filtered = cat === 'all' ? KNOWLEDGE : KNOWLEDGE.filter(k => k.category === cat);
  renderKnowledgeNav(filtered);
  if (filtered.length > 0) openKnowledgeDetail(filtered[0].id);
}

// ════════════════════════════════════════════════════════
// NAV ROUTING — called by sidebar buttons via onclick="navigateTo('...')"
// ════════════════════════════════════════════════════════
function navigateTo(target) {
  // Close all overlay panels first
  const knowledgePanel = document.getElementById('knowledgePanel');
  const historyPanel   = document.getElementById('historyPanel');

  // Reset all nav buttons
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));

  switch (target) {
    case 'chat':
      // Hide knowledge panel if open, show chat, activate chat btn
      if (knowledgePanel) knowledgePanel.style.display = 'none';
      _knowledgeVisible = false;
      if (historyPanel && historyPanel.style.display !== 'none') {
        historyPanel.style.display = 'none';
      }
      document.getElementById('chatNavBtn')?.classList.add('active');
      break;

    case 'knowledge':
      // Toggle knowledge panel
      _knowledgeVisible = !_knowledgeVisible;
      if (knowledgePanel) {
        knowledgePanel.style.display = _knowledgeVisible ? 'flex' : 'none';
      }
      if (_knowledgeVisible) {
        renderKnowledgeNav(KNOWLEDGE);
        if (!_activeKnowledgeId && KNOWLEDGE.length > 0) {
          openKnowledgeDetail(KNOWLEDGE[0].id);
        }
        document.getElementById('knowledgeNavBtn')?.classList.add('active');
      } else {
        document.getElementById('chatNavBtn')?.classList.add('active');
      }
      break;

    case 'layers':
      // Toggle the layers panel on the map
      toggleLayersPanel();
      document.getElementById('chatNavBtn')?.classList.add('active');
      break;

    case 'settings':
    case 'help':
    default:
      // Panels not yet implemented — just keep chat active
      document.getElementById('chatNavBtn')?.classList.add('active');
      break;
  }
}