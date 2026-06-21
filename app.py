import os
import sys
import json
import uuid
import base64
import threading
import traceback
from pathlib import Path
from flask import Flask, request, jsonify, render_template, send_from_directory
from flask_cors import CORS

# ── Add parent dir so we can import agent modules ─────────────────────────────
PARENT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PARENT_DIR))

app = Flask(__name__)
CORS(app)

@app.route('/assets/<path:filename>')
def serve_assets(filename):
    return send_from_directory('assets', filename)

# ── Initialize GEE ONCE at startup, keep credentials alive ───────────────────
import os as _os
import socket as _socket
import ee as _ee
from config import (GEE_PROJECT as _GEE_PROJECT,
                    GEE_SERVICE_ACCOUNT_FILE as _SA_FILE,
                    GEE_SERVICE_ACCOUNT_EMAIL as _SA_EMAIL)

# Fix: httplib2 (used internally by earthengine-api) tries IPv6 first and
# hangs for 10-16 minutes on networks where IPv6 TCP connections stall.
# Forcing IPv4 globally cuts startup from ~16 min back to ~4 seconds.
_orig_getaddrinfo = _socket.getaddrinfo
_socket.getaddrinfo = lambda h, p, family=0, *a, **k: \
    _orig_getaddrinfo(h, p, _socket.AF_INET, *a, **k)

# Global credentials object — refreshed before each use, never re-initialized
_GEE_CREDENTIALS = None

def _build_gee_credentials():
    """Build credentials using requests transport to avoid httplib2 slowness."""
    import google.oauth2.service_account as _sa
    import requests as _requests
    from google.auth.transport.requests import Request as _Request
    scopes = ['https://www.googleapis.com/auth/earthengine',
              'https://www.googleapis.com/auth/cloud-platform']
    creds = _sa.Credentials.from_service_account_file(_SA_FILE, scopes=scopes)
    # Use requests session instead of httplib2 for the token refresh —
    # avoids the secondary IPv6 hang during oauth2.googleapis.com token fetch
    creds.refresh(_Request(session=_requests.Session()))
    return creds

try:
    if _os.path.exists(_SA_FILE):
        _GEE_CREDENTIALS = _build_gee_credentials()
        _ee.Initialize(_GEE_CREDENTIALS, project=_GEE_PROJECT,
                       opt_url='https://earthengine.googleapis.com')
        print(f'✅ GEE initialized with service account: {_SA_EMAIL}')
    else:
        _ee.Initialize(project=_GEE_PROJECT)
        print(f'✅ GEE initialized with default credentials')
except Exception as _e:
    if 'already' not in str(_e).lower():
        print(f'⚠️  GEE startup init failed: {_e}')

# ── Job store (in-memory, keyed by job_id) ────────────────────────────────────
jobs = {}   # job_id → { status, result, error, progress, steps }

OUTPUT_DIR = os.path.expanduser('~/Downloads/satellite_agent_outputs')
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def image_to_base64(path: str) -> str:
    """Convert an image file to base64 data URI."""
    with open(path, 'rb') as f:
        data = base64.b64encode(f.read()).decode('utf-8')
    ext = Path(path).suffix.lower().strip('.')
    mime = 'jpeg' if ext in ('jpg', 'jpeg') else 'png'
    return f'data:image/{mime};base64,{data}'


def find_latest_outputs(prefix_keywords: list) -> dict:
    """Find the most recently saved output images matching keywords."""
    results = {}
    output_path = Path(OUTPUT_DIR)
    if not output_path.exists():
        return results

    files = sorted(output_path.glob('*.jpg'), key=lambda f: f.stat().st_mtime, reverse=True)
    for kw in prefix_keywords:
        kw_lower = kw.lower()
        for f in files:
            if kw_lower in f.name.lower():
                results[kw] = str(f)
                break
    return results


# City bounding boxes [W, S, E, N] — mirrors CITY_BBOX_FALLBACK in gis_functions.py
_CITY_BBOX = {
    'tokyo':      [139.40, 35.50, 139.95, 35.82],
    'osaka':      [135.35, 34.55, 135.70, 34.80],
    'beijing':    [116.10, 39.75, 116.65, 40.20],
    'shanghai':   [121.10, 30.95, 121.75, 31.55],
    'london':     [ -0.55, 51.35,  0.30, 51.70],
    'paris':      [  2.20, 48.75,  2.55, 48.95],
    'new york':   [-74.10, 40.55, -73.75, 40.90],
    'los angeles':[-118.55,33.90,-118.10,34.20],
    'jakarta':    [106.65, -6.40, 107.00, -6.05],
    'bangkok':    [100.35, 13.55, 100.90, 13.95],
    'singapore':  [103.60,  1.20, 104.05,  1.48],
    'sydney':     [150.90,-34.10, 151.35,-33.70],
    'dubai':      [ 55.10, 25.00,  55.55, 25.35],
    'mumbai':     [ 72.75, 18.85,  73.05, 19.20],
    'seoul':      [126.75, 37.40, 127.20, 37.70],
    'berlin':     [ 13.10, 52.40,  13.75, 52.70],
    'cairo':      [ 31.10, 29.90,  31.55, 30.20],
    'nairobi':    [ 36.65, -1.40,  37.10, -1.15],
    'sao paulo':  [-46.85,-23.75, -46.35,-23.45],
    'mexico city':[-99.30, 19.25, -98.95, 19.60],
}

def geocode_region(region_name: str) -> dict:
    """Geocode a region name → return bbox + center.
    Checks hardcoded city list first to avoid Nominatim returning country-level bboxes."""
    key = region_name.lower().strip()

    # Step 0: hardcoded city bbox (bypasses Nominatim country-level results)
    for city_key, bbox in _CITY_BBOX.items():
        if city_key in key or key in city_key:
            w, s, e, n = bbox
            print(f'  geocode_region: matched known city "{city_key}"')
            return {
                'success': True,
                'bbox': [w, s, e, n],
                'center': [(s + n) / 2, (w + e) / 2],
                'display_name': region_name,
            }

    # Step 1: Nominatim with size guard
    import requests as req
    try:
        url = 'https://nominatim.openstreetmap.org/search'
        params = {'q': region_name, 'format': 'json', 'limit': 5}
        headers = {'User-Agent': 'GISAgentWebApp/1.0'}
        results = req.get(url, params=params, headers=headers, timeout=10).json()
        for r in results:
            bb = r.get('boundingbox', [])
            if len(bb) != 4: continue
            s, n, w, e = float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])
            if abs(n - s) > 8 or abs(e - w) > 8:
                print(f'  geocode_region: skipping oversized result "{r.get("display_name","")[:50]}"')                
                continue
            return {
                'success': True,
                'bbox': [w, s, e, n],
                'center': [(s + n) / 2, (w + e) / 2],
                'display_name': r.get('display_name', region_name),
            }
    except Exception as ex:
        print(f'Geocode error: {ex}')
    return {'success': False, 'bbox': None, 'center': [0, 0]}


# ─────────────────────────────────────────────────────────────────────────────
# BACKGROUND ANALYSIS WORKER
# ─────────────────────────────────────────────────────────────────────────────

def _layer_label(type_label: str, region: str, start_date: str, end_date: str) -> str:
    """Format a map layer name as 'Type Region (Year)' or 'Type Region (2023–2025)'."""
    start_year = start_date[:4] if start_date else ''
    end_year   = end_date[:4]   if end_date   else ''
    year_str   = start_year if start_year == end_year else f'{start_year}–{end_year}'
    # Capitalize first word of region for display
    region_display = region.title() if region else 'Unknown'
    return f'{type_label} {region_display} ({year_str})'


def run_analysis_job(job_id: str, user_input: str, roi_geojson: dict = None):
    """Run the full LangGraph agent in a background thread."""
    job = jobs[job_id]

    def update_step(idx, status, pct=None):
        job['steps'][idx]['status'] = status
        if pct is not None:
            job['steps'][idx]['progress'] = pct

    try:
        job['status'] = 'running'

        # ── Step 0: Set matplotlib to non-interactive backend for threading ───
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        plt.close('all')

        # ── Step 1: Initialize GEE fresh for this thread ──────────────────────
        update_step(0, 'running', 10)
        import ee
        from config import GEE_PROJECT

        def init_gee():
            """Initialize GEE fresh for this thread — delegates to gis_functions."""
            try:
                from gis_functions import gee_init_for_thread
                gee_init_for_thread()
                print('  GEE initialized ✓')
                return True
            except Exception as e:
                print(f'  GEE init error: {e}')
                return False

        if not init_gee():
            job['status'] = 'error'
            job['error']  = 'GEE initialization failed. Check service account credentials.'
            return
        from config import GEE_PROJECT, OLLAMA_URL, OLLAMA_MODEL, OUTPUT_DIR as OUT
        import importlib, gis_functions as _gf_mod
        importlib.reload(_gf_mod)
        from gis_functions import (
            SURFACE_INDEX_MAP, ATMO_INDEX_MAP, KEYWORD_MAP, SYSTEM_PROMPT,
            resolve_region, fetch_web_context, generate_insight,
            _extract_variables_from_text, _extract_region_and_dates,
        )

        # ── Local helpers (defined here so they're always in scope) ──────────
        _APP_CLASS_BOUNDS_LOCAL = {
            'NDVI':  ([-1, 0.1, 0.3, 0.6, 1],        ['Bare (<0.1)', 'Stressed (0.1–0.3)', 'Moderate (0.3–0.6)', 'Healthy (>0.6)']),
            'EVI':   ([-1, 0.1, 0.3, 0.5, 1],         ['Sparse (<0.1)', 'Low (0.1–0.3)', 'Moderate (0.3–0.5)', 'Dense (>0.5)']),
            'SAVI':  ([-1, 0.1, 0.3, 0.5, 1],         ['Sparse (<0.1)', 'Low (0.1–0.3)', 'Moderate (0.3–0.5)', 'Dense (>0.5)']),
            'NDBI':  ([-1, -0.1, 0.0, 0.1, 1],        ['Non-built (<-0.1)', 'Low built (-0.1–0)', 'Moderate (0–0.1)', 'High built (>0.1)']),
            'NDWI':  ([-1, -0.3, 0.0, 0.3, 1],        ['Dry (<-0.3)', 'Transition (-0.3–0)', 'Moist (0–0.3)', 'Water (>0.3)']),
            'MNDWI': ([-1, -0.3, 0.0, 0.3, 1],        ['Dry (<-0.3)', 'Transition (-0.3–0)', 'Moist (0–0.3)', 'Water (>0.3)']),
            'BSI':   ([-1, -0.1, 0.1, 1],             ['Vegetated (<-0.1)', 'Mixed (-0.1–0.1)', 'Bare soil (>0.1)']),
            'UI':    ([-1, -0.1, 0.1, 1],             ['Vegetation (<-0.1)', 'Transition (-0.1–0.1)', 'Urban (>0.1)']),
            'NDSI':  ([-1, 0.0, 0.4, 1],              ['No snow (<0)', 'Possible (0–0.4)', 'Snow (>0.4)']),
            'NBI':   ([0, 0.1, 0.25, 0.5],            ['Low (<0.1)', 'Moderate (0.1–0.25)', 'High (>0.25)']),
            'LST':     ([0, 30, 35, 40, 45, 100],          ['Cool (<30°C)', 'Moderate (30–35°C)', 'Warm (35–40°C)', 'Hot (40–45°C)', 'Extreme (>45°C)']),
            'NO2':     ([0, 8e-5, 1.5e-4, 2.5e-4, 1],        ['Clean (<8e-5)', 'Moderate (8–15e-5)', 'High (15–25e-5)', 'Severe (>25e-5)']),
            'CO':      ([0.02, 0.035, 0.055, 0.07, 0.08],    ['Low (<0.035)', 'Moderate (0.035–0.055)', 'High (0.055–0.07)', 'Severe (>0.07)']),
            'SO2':     ([0, 1e-4, 5e-4, 1e-3, 0.01],         ['Clean (<1e-4)', 'Moderate (1–5e-4)', 'High (5e-4–1e-3)', 'Severe (>1e-3)']),
            'CH4':     ([1750, 1850, 1900, 1950, 2100],       ['Background (<1850)', 'Elevated (1850–1900)', 'High (1900–1950)', 'Very high (>1950)']),
            'O3':      ([200, 220, 280, 340, 400],             ['Very low (<220 DU)', 'Low (220–280 DU)', 'Normal (280–340 DU)', 'High (>340 DU)']),
            'AEROSOL': ([-1, 0, 1, 2, 4],                     ['Clean (<0)', 'Low (0–1)', 'Moderate (1–2)', 'High (>2)']),
            'GPP':     ([0, 0.001, 0.003, 0.006, 0.02],        ['Very low (<0.001)', 'Low (0.001–0.003)', 'Moderate (0.003–0.006)', 'High (>0.006)']),
            'FFPI':    ([0, 0.35, 0.55, 0.75, 1],             ['Clean (0–0.35)', 'Moderate (0.35–0.55)', 'Polluted (0.55–0.75)', 'Severe (>0.75)']),
        }

        def _compute_area_stats(ee_image, band_name, study_area, var_label, scale):
            import ee as _ee
            key = var_label.upper()
            if key not in _APP_CLASS_BOUNDS_LOCAL:
                return None, {}
            bounds, labels = _APP_CLASS_BOUNDS_LOCAL[key]
            pixel_area_ha = (scale ** 2) / 10000.0
            try:
                total_pixels = ee_image.select(band_name).reduceRegion(
                    reducer=_ee.Reducer.count(),
                    geometry=study_area, scale=scale, maxPixels=1e9
                ).getInfo().get(band_name, 0)
                if not total_pixels:
                    return None, {}
                total_ha   = round(total_pixels * pixel_area_ha, 1)
                class_pcts = {}
                for i in range(len(bounds) - 1):
                    lo, hi = bounds[i], bounds[i + 1]
                    mask  = ee_image.select(band_name).gte(lo).And(ee_image.select(band_name).lt(hi))
                    count = ee_image.select(band_name).updateMask(mask).reduceRegion(
                        reducer=_ee.Reducer.count(),
                        geometry=study_area, scale=scale, maxPixels=1e9
                    ).getInfo().get(band_name, 0)
                    pct = round((count / total_pixels) * 100, 1) if total_pixels else 0
                    ha  = round(count * pixel_area_ha, 1)
                    if pct > 0:
                        class_pcts[labels[i]] = {'pct': pct, 'ha': ha, 'total_ha': total_ha}
                print(f'  ✓ {var_label} area stats: {total_ha:,.0f} ha, {len(class_pcts)} classes')
                return total_ha, class_pcts
            except Exception as _e:
                print(f'  {var_label} area stats failed: {_e}')
                return None, {}

        def _make_class_bar_b64(class_pcts, title, xlabel):
            try:
                import matplotlib
                matplotlib.use('Agg')
                import matplotlib.pyplot as _plt
                import io as _io, base64 as _b64
                _COLOR_MAP = {
                    # LST
                    'Cool (<30°C)': '#0502b8', 'Moderate (30–35°C)': '#269db1',
                    'Warm (35–40°C)': '#3be285', 'Hot (40–45°C)': '#f5a800',
                    'Extreme (>45°C)': '#ff500d',
                    # NDVI/index
                    'Bare (<0.1)': '#d4a96a', 'Stressed (0.1–0.3)': '#a8c97f',
                    'Moderate (0.3–0.6)': '#5aaa4f', 'Healthy (>0.6)': '#1a6e1a',
                    'Non-built (<-0.1)': '#4fc3f7', 'Low built (-0.1–0)': '#b0bec5',
                    'High built (>0.1)': '#e53935',
                    'Dry (<-0.3)': '#bf8c4c', 'Transition (-0.3–0)': '#a5d6a7',
                    'Moist (0–0.3)': '#42a5f5', 'Water (>0.3)': '#0d47a1',
                    # NO2 — palette ['#000033','#0000ff','#8000ff','#00ffff','#008000','#ffff00','#ff0000'], min=0 max=0.0002
                    'Clean (<8e-5)':      '#1a00ff',  # ~20% of palette → blue
                    'Moderate (8–15e-5)': '#00c68c',  # ~57% → cyan-green
                    'High (15–25e-5)':    '#ffbf00',  # ~87% → amber/orange
                    'Severe (>25e-5)':    '#ff0000',  # top → red
                    # CO — same palette ['#000033'…'#ff0000'], min=0.02 max=0.08
                    'Low (<0.035)':           '#000033',
                    'Moderate (0.035–0.055)': '#8000ff',
                    'High (0.055–0.07)':      '#ffff00',
                    'Severe (>0.07)':         '#ff0000',
                    # SO2 — palette ['#0000ff','#008000','#ffff00','#ffa500','#ff0000','#8b0000']
                    'Clean (<1e-4)':      '#0000ff',
                    'Moderate (1–5e-4)':  '#008000',
                    'High (5e-4–1e-3)':   '#ffa500',
                    'Severe (>1e-3)':     '#8b0000',
                    # CH4 — palette ['#0000ff','#00ffff','#008000','#ffff00','#ffa500','#ff0000']
                    'Background (<1850)':    '#0000ff',
                    'Elevated (1850–1900)':  '#00ffff',
                    'High (1900–1950)':      '#ffa500',
                    'Very high (>1950)':     '#ff0000',
                    # O3 — palette ['#800080','#0000ff','#00ffff','#008000','#ffff00','#ff0000']
                    'Very low (<220 DU)':  '#800080',
                    'Low (220–280 DU)':    '#0000ff',
                    'Normal (280–340 DU)': '#00cc88',
                    'High (>340 DU)':      '#ffff00',
                    # Aerosol — palette ['#0000ff','#ffffff','#ffff00','#ffa500','#ff0000']
                    'Clean (<0)':      '#0000ff',
                    'Low (0–1)':       '#ffff44',
                    'Moderate (1–2)':  '#ffa500',
                    'High (>2)':       '#ff0000',
                    # GPP — palette ['#ffffff','#a8ddb5','#238b45','#00441b']
                    'Very low (<0.001)':         '#ffffff',
                    'Low (0.001–0.003)':         '#a8ddb5',
                    'Moderate (0.003–0.006)':    '#238b45',
                    'High (>0.006)':             '#00441b',
                    # FFPI
                    'Clean (0–0.35)': '#313695', 'Moderate (0.35–0.55)': '#74add1',
                    'Polluted (0.55–0.75)': '#fdae61', 'Severe (>0.75)': '#d73027',
                }
                _FALLBACK = ['#000033','#00ccff','#ffff00','#ff0000','#74add1','#fdae61']
                pairs = []
                for lbl, val in class_pcts.items():
                    pct = val['pct'] if isinstance(val, dict) else float(val)
                    if pct <= 0.1: continue
                    # Normalize: try exact, then en-dash, then hyphen variants
                    _lbl_norm_en  = lbl.replace('-', '\u2013').replace('\u2014', '\u2013')
                    _lbl_norm_hyp = lbl.replace('\u2013', '-').replace('\u2014', '-')
                    color = (_COLOR_MAP.get(lbl)
                             or _COLOR_MAP.get(_lbl_norm_en)
                             or _COLOR_MAP.get(_lbl_norm_hyp)
                             or next((v for k, v in _COLOR_MAP.items()
                                      if k.replace('\u2013','-').replace('\u2014','-') ==
                                         lbl.replace('\u2013','-').replace('\u2014','-')), None)
                             or _FALLBACK[len(pairs) % len(_FALLBACK)])
                    print(f'  [bar] lbl={repr(lbl)} matched={lbl in _COLOR_MAP} color={color}')
                    disp = lbl.replace(' (', '\n(') if ' (' in lbl else lbl
                    pairs.append((disp, pct, color))
                if not pairs:
                    return None
                cls, pct_vals, col_vals = zip(*pairs)
                fig, ax = _plt.subplots(figsize=(max(5, len(pairs) * 1.3), 3.5))
                bars = ax.bar(cls, pct_vals, color=col_vals, edgecolor='white', linewidth=0.5, width=0.55)
                _max_pct = max(pct_vals)
                ax.set_ylim(0, _max_pct * 1.35)
                for bar, pct in zip(bars, pct_vals):
                    # Clamp label y so tiny bars (<5% of max) always show label above bar
                    _label_y = max(bar.get_height() + _max_pct * 0.02, _max_pct * 0.05)
                    ax.text(bar.get_x() + bar.get_width() / 2,
                            _label_y,
                            f'{pct:.1f}%', ha='center', va='bottom', fontsize=8,
                            fontweight='bold', color='#333')
                ax.set_xlabel(xlabel, fontsize=9)
                ax.set_ylabel('Area share (%)', fontsize=9)
                ax.set_title(title, fontsize=10, fontweight='bold')
                ax.spines['top'].set_visible(False)
                ax.spines['right'].set_visible(False)
                fig.tight_layout()
                buf = _io.BytesIO()
                fig.savefig(buf, format='png', dpi=120, bbox_inches='tight')
                _plt.close(fig)
                buf.seek(0)
                b64 = _b64.b64encode(buf.read()).decode('utf-8')
                return f'data:image/png;base64,{b64}'
            except Exception as _e:
                print(f'  _make_class_bar_b64 failed: {_e}')
                return None
        # ── End local helpers ─────────────────────────────────────────────────

        update_step(0, 'done', 100)

        # ── Step 2: parse intent — pre-check first, Ollama as fallback ─────────
        # Deterministic keyword detection prevents Ollama from hallucinating
        # "Land Cover Jakarta 2025" as a "question" instead of an analysis.
        update_step(1, 'running', 20)
        import requests as req

        _pre_vars   = _extract_variables_from_text(user_input)
        _pre_region, _pre_start, _pre_end = _extract_region_and_dates(user_input)

        if _pre_vars and _pre_region and _pre_start:
            # Full pre-check: skip Ollama entirely
            print(f'  [pre-check] vars={_pre_vars} region={_pre_region} — skipping Ollama')
            _vars_out = [KEYWORD_MAP.get(v.lower(), v.lower()) for v in _pre_vars]
            _vars_out = list(dict.fromkeys(_vars_out))
            parsed = {
                'intent'    : 'analysis',
                'region'    : _pre_region,
                'start_date': _pre_start,
                'end_date'  : _pre_end,
                'variables' : _vars_out,
            }
        else:
            # Fall back to Ollama
            resp = req.post(OLLAMA_URL,
                json={'model': OLLAMA_MODEL,
                      'messages': [
                          {'role': 'system', 'content': SYSTEM_PROMPT},
                          {'role': 'user',   'content': user_input}],
                      'stream': False}, timeout=60)
            data = resp.json()
            raw = data.get('message', {}).get('content', '{}').strip()
            if '```' in raw:
                raw = raw.split('```')[1]
                if raw.startswith('json'): raw = raw[4:]
            s = raw.find('{'); e = raw.rfind('}') + 1
            parsed = json.loads(raw[s:e]) if s >= 0 and e > s else {}

            # If pre-check found vars or region, merge them in (override LLM guesses)
            if _pre_vars:
                _vars_merged = [KEYWORD_MAP.get(v.lower(), v.lower()) for v in _pre_vars]
                parsed['variables']  = list(dict.fromkeys(_vars_merged))
                parsed['intent']     = 'analysis'
            if _pre_region:
                parsed['region']     = _pre_region
            if _pre_start:
                parsed['start_date'] = _pre_start
                parsed['end_date']   = parsed.get('end_date') or _pre_end

        update_step(1, 'done', 100)

        region_name = parsed.get('region') or 'Unknown'
        start_date  = parsed.get('start_date') or '2023-01-01'
        end_date    = parsed.get('end_date')   or '2023-12-31'
        variables   = parsed.get('variables')  or []
        intent      = parsed.get('intent', 'analysis')

        # ── Guard: if user mentioned only ONE year, collapse dates to that year ──
        import re as _re
        _years_in_input = _re.findall(r'(20\d{2}|19\d{2})', user_input)
        _unique_years   = list(dict.fromkeys(_years_in_input))
        if len(_unique_years) == 1:
            _forced_year = _unique_years[0]
            start_date   = f'{_forced_year}-01-01'
            end_date     = f'{_forced_year}-12-31'
            print(f'  [parse] Single year {_forced_year} detected — forcing date range.')

        # Normalize variables
        normalized = []
        for v in variables:
            vl = v.lower().strip()
            normalized.append(KEYWORD_MAP.get(vl, vl))
        variables = list(dict.fromkeys(normalized))

        # Always add rgb for true color layer
        if 'rgb' not in variables:
            variables_with_rgb = variables + ['rgb']
        else:
            variables_with_rgb = variables

        job['parsed'] = {
            'region': region_name, 'start_date': start_date,
            'end_date': end_date, 'variables': variables,
            'intent': intent,
        }

        # ── Multi-year detection: return year list, frontend fires one job per year ──
        # Only when user explicitly typed 2+ different years in their input.
        if intent != 'question':
            try:
                sy = int(start_date[:4]); ey = int(end_date[:4])
                if ey > sy and len(_unique_years) >= 2:
                    year_queries = []
                    for yr in range(sy, ey + 1):
                        year_queries.append({
                            'message'   : f"{' '.join(variables) or 'lulc'} in {region_name} in {yr}",
                            'start_date': f'{yr}-01-01',
                            'end_date'  : f'{yr}-12-31',
                            'region'    : region_name,
                            'variables' : variables,
                        })
                    update_step(1, 'done', 100)
                    for i in range(2, 6): update_step(i, 'done', 100)
                    job['status'] = 'complete'
                    job['result'] = {
                        'type'        : 'multi_year_plan',
                        'region'      : region_name,
                        'start_year'  : str(sy),
                        'end_year'    : str(ey),
                        'year_queries': year_queries,
                    }
                    return
            except Exception:
                pass  # fall through to normal single-year flow

        # Handle QA intent
        if intent == 'question':
            update_step(2, 'running', 50)
            qa_resp = req.post(OLLAMA_URL,
                json={'model': OLLAMA_MODEL,
                      'messages': [
                          {'role': 'system', 'content': 'You are an expert in satellite remote sensing and GIS.'},
                          {'role': 'user',   'content': user_input}],
                      'stream': False}, timeout=60)
            answer = qa_resp.json()['message']['content'].strip()
            update_step(2, 'done', 100)
            job['status']  = 'complete'
            job['result']  = {'type': 'qa', 'answer': answer}
            return

        # ── Step 3: Geocode / resolve region ─────────────────────────────────
        update_step(2, 'running', 30)

        # If a custom ROI was drawn by the user, build GEE geometry from it directly
        # and skip Nominatim geocoding — this is the fix for ROI being ignored
        if roi_geojson:
            try:
                geom = roi_geojson.get('geometry') or roi_geojson
                study_area_main = ee.Geometry(geom)
                coords = study_area_main.bounds().getInfo()['coordinates'][0]
                xs = [c[0] for c in coords]
                ys = [c[1] for c in coords]
                precise_bbox = [min(xs), min(ys), max(xs), max(ys)]
                geo = {
                    'success': True,
                    'bbox'   : precise_bbox,
                    'center' : [(min(ys)+max(ys))/2, (min(xs)+max(xs))/2],
                }
                job['geo'] = geo
                print(f'  Using custom ROI geometry, bbox: {precise_bbox}')
            except Exception as roi_err:
                print(f'  ROI geometry error: {roi_err}, falling back to geocode')
                geo = geocode_region(region_name)
                job['geo'] = geo
                study_area_main = None
        else:
            geo = geocode_region(region_name)
            job['geo'] = geo
            study_area_main = None

        update_step(2, 'done', 100)

        # Record existing files BEFORE analysis so we can find NEW ones after
        import time as _time
        pre_analysis_time = _time.time()
        print(f'  Pre-analysis snapshot at t={pre_analysis_time:.0f}')

        # ── Step 4: Run GEE analysis ──────────────────────────────────────────
        update_step(3, 'running', 10)

        # SURFACE_INDEX_MAP and ATMO_INDEX_MAP already imported via reload above

        surface_keys = list(SURFACE_INDEX_MAP.keys()) + ['lst', 'uhi', 'rgb']
        atmo_keys    = list(ATMO_INDEX_MAP.keys()) + ['ffpi']
        layers = []   # will collect GEE tile URLs

        # Resolve region ONCE — reused for surface, LULC, and atmo
        # Skip if already resolved from custom ROI above
        if study_area_main is None:
            try:
                study_area_main = resolve_region(region_name)
                coords = study_area_main.bounds().getInfo()['coordinates'][0]
                xs = [c[0] for c in coords]
                ys = [c[1] for c in coords]
                precise_bbox = [min(xs), min(ys), max(xs), max(ys)]
                geo['bbox'] = precise_bbox
                job['geo']  = geo
                print(f'  Precise bbox from GEE: {precise_bbox}')
            except Exception as bbox_err:
                print(f'  Bbox from GEE failed: {bbox_err}, using Nominatim bbox')
        else:
            print(f'  Using pre-resolved ROI geometry')

        surface_vars = [v for v in variables_with_rgb if v in surface_keys]
        atmo_vars    = [v for v in variables if v in atmo_keys]
        lulc_vars    = ['lulc'] if 'lulc' in variables else []

        all_stats  = {}
        layers     = []
        figures    = {}   # label → { 'overview': b64, 'analysis_map': b64, 'charts': [...] }

        # Surface analysis — tile layers + static figures
        if surface_vars:
            update_step(3, 'running', 30)
            try:
                from gis_functions import (
                    load_landsat, compute_lst, compute_uhi,
                    get_stats, SURFACE_INDEX_MAP, VIS,
                    get_thumb, make_rgb_overview, make_analysis_map, make_stats_charts,
                )
                study_area_surf = study_area_main
                landsat_col, composite = load_landsat(study_area_surf, start_date, end_date)
                count = landsat_col.size().getInfo()
                print(f'  {count} Landsat scenes loaded')
                lst_img = None
                bbox    = geo.get('bbox')

                # ── RGB overview map (static, for intro section) ──────────────
                rgb_overview_b64 = None
                if bbox and composite:
                    try:
                        rgb_overview_b64 = make_rgb_overview(
                            composite, study_area_surf, region_name, bbox)
                        print('  ✓ RGB overview map generated')
                    except Exception as re:
                        print(f'  RGB overview failed: {re}')

                for v in surface_vars:
                    try:
                        if v == 'rgb':
                            map_id   = composite.clip(study_area_surf).getMapId(VIS['rgb'])
                            tile_url = map_id['tile_fetcher'].url_format
                            layers.append({
                                'name'    : _layer_label('True Color', region_name, start_date, end_date),
                                'tile_url': tile_url,
                                'type'    : 'tile',
                                'bbox'    : bbox,
                            })
                            print('  ✓ RGB tile layer ready')

                        elif v == 'lst':
                            lst_img, _ = compute_lst(composite, study_area_surf)
                            s = get_stats(lst_img, 'LST', study_area_surf, scale=90)
                            # Monthly LST — use ST_B10 thermal band directly (avoids NDVI issues on small composites)
                            try:
                                import datetime as _dt
                                monthly  = {}
                                start_dt = _dt.datetime.strptime(start_date, '%Y-%m-%d').replace(day=1)
                                end_dt   = _dt.datetime.strptime(end_date,   '%Y-%m-%d')
                                cur = start_dt
                                while cur <= end_dt:
                                    m_s = cur.strftime('%Y-%m-%d')
                                    m_e = (cur.replace(year=cur.year+1, month=1, day=1)
                                           if cur.month == 12
                                           else cur.replace(month=cur.month+1, day=1)).strftime('%Y-%m-%d')
                                    try:
                                        m_scenes = landsat_col.filterDate(m_s, m_e)
                                        if m_scenes.size().getInfo() > 0:
                                            # landsat_col already has apply_scaling applied,
                                            # so ST_B10 is already in Kelvin — just subtract 273.15
                                            thermal = (m_scenes.select('ST_B10').median()
                                                       .subtract(273.15))
                                            ms = thermal.reduceRegion(
                                                reducer=ee.Reducer.mean(),
                                                geometry=study_area_surf, scale=90, maxPixels=1e9
                                            ).getInfo()
                                            val = list(ms.values())[0] if ms else None
                                            if val is not None:
                                                monthly[cur.strftime('%Y-%m')] = round(val, 4)
                                    except: pass
                                    cur = (cur.replace(year=cur.year+1, month=1, day=1)
                                           if cur.month == 12
                                           else cur.replace(month=cur.month+1, day=1))
                                s['monthly'] = monthly
                                print(f'  ✓ LST monthly: {len(monthly)} months')
                            except Exception as lst_me:
                                s['monthly'] = {}
                                print(f'  LST monthly failed: {lst_me}')
                            all_stats['LST'] = s
                            # ── Enrich with real per-class area (ha) ─────────
                            _total_ha, _class_pcts = _compute_area_stats(
                                lst_img, 'LST', study_area_surf, 'LST', scale=90)
                            if _total_ha:
                                all_stats['LST']['total_ha']   = _total_ha
                                all_stats['LST']['class_pcts'] = _class_pcts
                            map_id   = lst_img.clip(study_area_surf).getMapId(VIS['lst'])
                            tile_url = map_id['tile_fetcher'].url_format
                            layers.append({'name': _layer_label('LST', region_name, start_date, end_date), 'tile_url': tile_url,
                                           'type': 'tile', 'bbox': bbox})
                            if bbox:
                                arr          = get_thumb(lst_img.clip(study_area_surf), VIS['lst'], study_area_surf, dim=512)
                                analysis_b64 = make_analysis_map(arr, VIS['lst'], 'LST (°C)', region_name, bbox)
                                charts       = make_stats_charts(all_stats, 'lst', 'LST')
                                # Always replace class bar with real GEE data if available
                                _cp_for_bar  = all_stats['LST'].get('class_pcts') or {}
                                print(f'  LST class_pcts for bar: {list(_cp_for_bar.keys())}')
                                if _cp_for_bar:
                                    real_bar = _make_class_bar_b64(
                                        _cp_for_bar, 'LST class composition', 'Temperature class')
                                    if real_bar:
                                        charts = [(t, d) for t, d in charts if t != 'class_bar']
                                        charts.insert(0, ('class_bar', real_bar))
                                        print(f'  ✓ LST class bar replaced with real GEE data')
                                figures['LST'] = {'analysis_map': analysis_b64, 'charts': charts,
                                                  'rgb_overview': rgb_overview_b64}
                            print('  ✓ LST ready')

                        elif v == 'uhi':
                            if lst_img is None:
                                lst_img, _ = compute_lst(composite, study_area_surf)
                            uhi_img, lst_mean, lst_std = compute_uhi(lst_img, study_area_surf)

                            # Get LST stats for min/max/p10/p90 (reuse if already computed)
                            lst_base = all_stats.get('LST') or {}
                            if not lst_base:
                                try:
                                    lst_base = get_stats(lst_img, 'LST', study_area_surf, scale=90)
                                except Exception:
                                    lst_base = {}

                            # Monthly LST - reuse from LST block if available
                            uhi_monthly = dict(lst_base.get('monthly', {}))
                            if not uhi_monthly:
                                try:
                                    import datetime as _dt2
                                    cur2 = _dt2.datetime.strptime(start_date, '%Y-%m-%d').replace(day=1)
                                    end_dt2 = _dt2.datetime.strptime(end_date, '%Y-%m-%d')
                                    while cur2 <= end_dt2:
                                        m_s2 = cur2.strftime('%Y-%m-%d')
                                        m_e2 = (cur2.replace(year=cur2.year+1, month=1, day=1)
                                                if cur2.month == 12
                                                else cur2.replace(month=cur2.month+1, day=1)).strftime('%Y-%m-%d')
                                        try:
                                            m_sc2 = landsat_col.filterDate(m_s2, m_e2)
                                            if m_sc2.size().getInfo() > 0:
                                                ms2 = (m_sc2.select('ST_B10').median().subtract(273.15)
                                                       .reduceRegion(ee.Reducer.mean(), study_area_surf, 90, maxPixels=1e9).getInfo())
                                                val2 = list(ms2.values())[0] if ms2 else None
                                                if val2 is not None:
                                                    uhi_monthly[cur2.strftime('%Y-%m')] = round(val2, 4)
                                        except: pass
                                        cur2 = (cur2.replace(year=cur2.year+1, month=1, day=1)
                                                if cur2.month == 12
                                                else cur2.replace(month=cur2.month+1, day=1))
                                except Exception as uhi_me:
                                    print(f'  UHI monthly failed: {uhi_me}')

                            # Compute actual UHI z-score image stats (mean≈0, std≈1 by construction)
                            uhi_zstats = {}
                            try:
                                uhi_zstats = get_stats(uhi_img.clip(study_area_surf), 'UHI', study_area_surf, scale=90)
                                print(f'  UHI z-score stats: mean={uhi_zstats.get("mean"):.3f}, std={uhi_zstats.get("std"):.3f}')
                            except Exception as _ze:
                                print(f'  UHI z-score stats failed: {_ze}')

                            # Store enriched stats — lst_mean is the real temp; z_* are UHI image stats
                            # class_pcts uses z-score bounds to match _CLASS_DEFS.UHI on the frontend.
                            # We pre-compute them here from z-score stats so frontend skips Monte Carlo.
                            _z_mean = uhi_zstats.get('mean', 0.0)
                            _z_std  = uhi_zstats.get('std',  1.0) or 1.0
                            _z_min  = uhi_zstats.get('min',  -4.0)
                            _z_max  = uhi_zstats.get('max',   4.0)
                            try:
                                import numpy as _np_uhi
                                _rng_uhi = _np_uhi.random.default_rng(42)
                                _z_samp  = _np_uhi.clip(
                                    _rng_uhi.normal(_z_mean, _z_std, 100000), _z_min, _z_max)
                                _uhi_class_pcts = {
                                    'Strong Cool (z < −2)'      : {'pct': round(float(_np_uhi.mean(_z_samp < -2)           * 100), 1)},
                                    'Cool Island (−2 to −0.5)'  : {'pct': round(float(_np_uhi.mean((_z_samp >= -2)  & (_z_samp < -0.5)) * 100), 1)},
                                    'Near Average (−0.5 to 0.5)': {'pct': round(float(_np_uhi.mean((_z_samp >= -0.5) & (_z_samp < 0.5))  * 100), 1)},
                                    'Warm Zone (0.5 to 2)'      : {'pct': round(float(_np_uhi.mean((_z_samp >= 0.5)  & (_z_samp < 2))    * 100), 1)},
                                    'Heat Island (z > 2)'       : {'pct': round(float(_np_uhi.mean(_z_samp >= 2)             * 100), 1)},
                                }
                            except Exception as _cpz_err:
                                print(f'  UHI class_pcts failed: {_cpz_err}')
                                _uhi_class_pcts = {
                                    'Strong Cool (z < −2)'      : {'pct': 2.3},
                                    'Cool Island (−2 to −0.5)'  : {'pct': 24.2},
                                    'Near Average (−0.5 to 0.5)': {'pct': 38.3},
                                    'Warm Zone (0.5 to 2)'      : {'pct': 24.2},
                                    'Heat Island (z > 2)'       : {'pct': 2.3},
                                }
                            all_stats['UHI'] = {
                                'mean'    : lst_mean,
                                'std'     : lst_std,
                                'min'     : lst_base.get('min'),
                                'max'     : lst_base.get('max'),
                                'median'  : lst_base.get('median'),
                                'p10'     : lst_base.get('p10'),
                                'p90'     : lst_base.get('p90'),
                                'monthly' : uhi_monthly,
                                'lst_mean': lst_mean,
                                'lst_std' : lst_std,
                                # Actual UHI z-score image statistics (used for zone class chart)
                                'z_mean'  : _z_mean,
                                'z_std'   : _z_std,
                                'z_min'   : _z_min,
                                'z_max'   : _z_max,
                                'z_p10'   : uhi_zstats.get('p10', -1.3),
                                'z_p90'   : uhi_zstats.get('p90',  1.3),
                                # Pre-computed z-score class percentages — consumed by frontend class bar
                                'class_pcts': _uhi_class_pcts,
                            }
                            map_id   = uhi_img.clip(study_area_surf).getMapId(VIS['uhi'])
                            tile_url = map_id['tile_fetcher'].url_format
                            layers.append({'name': _layer_label('UHI', region_name, start_date, end_date), 'tile_url': tile_url,
                                           'type': 'tile', 'bbox': bbox})
                            if bbox:
                                try:
                                    arr          = get_thumb(uhi_img.clip(study_area_surf), VIS['uhi'], study_area_surf, dim=512)
                                    analysis_b64 = make_analysis_map(arr, VIS['uhi'], f'UHI (mean={lst_mean:.1f}\u00b0C)', region_name, bbox)
                                    uhi_charts   = make_stats_charts(all_stats, 'uhi', 'UHI')

                                    # ── Direct UHI heat class chart — generated here in app.py
                                    # so it works regardless of what make_stats_charts produces.
                                    # Uses the same temperature bins as LST.
                                    try:
                                        import numpy as _np2
                                        import matplotlib.pyplot as _plt2
                                        from gis_functions import fig_to_base64 as _f2b
                                        _uhi_s   = all_stats['UHI']
                                        _mean_t  = float(_uhi_s.get('lst_mean') or _uhi_s.get('mean') or lst_mean)
                                        _std_t   = float(_uhi_s.get('lst_std')  or _uhi_s.get('std')  or lst_std)
                                        _min_t   = float(_uhi_s['min'])  if _uhi_s.get('min')  is not None else _mean_t - 15
                                        _max_t   = float(_uhi_s['max'])  if _uhi_s.get('max')  is not None else _mean_t + 15
                                        if _std_t <= 0: _std_t = 3.0
                                        if _max_t <= _min_t: _max_t = _min_t + 40.0
                                        _rng  = _np2.random.default_rng(42)
                                        _samp = _np2.clip(_rng.normal(_mean_t, _std_t, 50000), _min_t, _max_t)
                                        _cls_names = ['Cool\n(<30°C)', 'Moderate\n(30–35°C)', 'Warm\n(35–40°C)', 'Hot\n(40–45°C)', 'Extreme\n(>45°C)']
                                        _cls_pcts  = [
                                            float(_np2.mean(_samp < 30) * 100),
                                            float(_np2.mean((_samp >= 30) & (_samp < 35)) * 100),
                                            float(_np2.mean((_samp >= 35) & (_samp < 40)) * 100),
                                            float(_np2.mean((_samp >= 40) & (_samp < 45)) * 100),
                                            float(_np2.mean(_samp >= 45) * 100),
                                        ]
                                        _cls_colors = ['#0502b8', '#269db1', '#3be285', '#f5a800', '#ff500d']
                                        _pairs = [(n, p, c) for n, p, c in zip(_cls_names, _cls_pcts, _cls_colors) if p > 0.1]
                                        if _pairs:
                                            _cn, _pv, _cv = zip(*_pairs)
                                            _fig, _ax = _plt2.subplots(figsize=(6, 3.5))
                                            _bars = _ax.bar(_cn, _pv, color=_cv, edgecolor='white', linewidth=0.5, width=0.6)
                                            _ax.set_ylim(0, max(_pv) * 1.3)
                                            for _bar, _pct in zip(_bars, _pv):
                                                _ax.text(_bar.get_x() + _bar.get_width() / 2,
                                                         _bar.get_height() + max(_pv) * 0.02,
                                                         f'{_pct:.1f}%', ha='center', va='bottom',
                                                         fontsize=8, fontweight='bold', color='#333')
                                            _ax.set_xlabel('Temperature class', fontsize=9)
                                            _ax.set_ylabel('Area share (%)', fontsize=9)
                                            _ax.set_title('UHI heat class composition', fontsize=10, fontweight='bold')
                                            _ax.spines['top'].set_visible(False)
                                            _ax.spines['right'].set_visible(False)
                                            _fig.tight_layout()
                                            # Only append if make_stats_charts didn't already produce a class_bar
                                            has_class_bar = any(t == 'class_bar' for t, _ in uhi_charts)
                                            if not has_class_bar:
                                                uhi_charts.append(('class_bar', _f2b(_fig)))
                                                print('  ✓ UHI heat class chart injected from app.py')
                                            _plt2.close(_fig)
                                    except Exception as _uhi_cls_err:
                                        print(f'  UHI class chart injection failed: {_uhi_cls_err}')

                                    figures['UHI'] = {
                                        'analysis_map': analysis_b64,
                                        'charts'      : uhi_charts,
                                        'rgb_overview': rgb_overview_b64,
                                    }
                                    print(f'  \u2713 UHI figures ready ({len(uhi_charts)} charts)')
                                except Exception as uhi_fig_err:
                                    print(f'  UHI figures failed: {uhi_fig_err}')
                                    figures['UHI'] = {'analysis_map': None, 'charts': [], 'rgb_overview': rgb_overview_b64}
                            print('  \u2713 UHI ready')

                        elif v in SURFACE_INDEX_MAP:
                            label, func, vis_key, scale = SURFACE_INDEX_MAP[v]
                            img = func(composite)
                            s   = get_stats(img, label, study_area_surf, scale=scale)
                            # Monthly stats
                            try:
                                import datetime
                                monthly  = {}
                                start_dt = datetime.datetime.strptime(start_date, '%Y-%m-%d')
                                end_dt   = datetime.datetime.strptime(end_date,   '%Y-%m-%d')
                                cur = start_dt.replace(day=1)
                                while cur <= end_dt:
                                    m_s = cur.strftime('%Y-%m-%d')
                                    m_e = (cur.replace(year=cur.year+1, month=1, day=1)
                                           if cur.month == 12
                                           else cur.replace(month=cur.month+1, day=1)).strftime('%Y-%m-%d')
                                    m_scenes = landsat_col.filterDate(m_s, m_e)
                                    if m_scenes.size().getInfo() > 0:
                                        m_comp = m_scenes.median()
                                        m_img  = func(m_comp)
                                        ms = m_img.reduceRegion(
                                            reducer=ee.Reducer.mean(),
                                            geometry=study_area_surf, scale=scale, maxPixels=1e9
                                        ).getInfo()
                                        val = ms.get(label)
                                        if val is not None:
                                            monthly[cur.strftime('%Y-%m')] = round(val, 6)
                                    cur = (cur.replace(year=cur.year+1, month=1)
                                           if cur.month == 12
                                           else cur.replace(month=cur.month+1))
                                s['monthly'] = monthly
                            except Exception as me:
                                s['monthly'] = {}
                            all_stats[label] = s
                            # ── Enrich with real per-class area (ha) ─────────
                            _total_ha, _class_pcts = _compute_area_stats(
                                img, label, study_area_surf, label, scale=scale)
                            if _total_ha:
                                all_stats[label]['total_ha']   = _total_ha
                                all_stats[label]['class_pcts'] = _class_pcts
                            map_id   = img.clip(study_area_surf).getMapId(VIS[vis_key])
                            tile_url = map_id['tile_fetcher'].url_format
                            layers.append({'name': _layer_label(label, region_name, start_date, end_date), 'tile_url': tile_url,
                                           'type': 'tile', 'bbox': bbox})
                            # Static analysis map + charts
                            if bbox:
                                arr          = get_thumb(img.clip(study_area_surf), VIS[vis_key], study_area_surf, dim=512)
                                analysis_b64 = make_analysis_map(arr, VIS[vis_key], label, region_name, bbox)
                                charts       = make_stats_charts(all_stats, v, label)
                                # Always replace class bar with real GEE data if available
                                _cp_for_bar = all_stats[label].get('class_pcts') or {}
                                print(f'  {label} class_pcts for bar: {list(_cp_for_bar.keys())}')
                                if _cp_for_bar:
                                    _xlabel_map = {
                                        'NDVI':'NDVI class', 'EVI':'Vegetation class', 'SAVI':'Vegetation class',
                                        'NDBI':'Built-up class', 'NDWI':'Water class', 'MNDWI':'Water class',
                                        'BSI':'Bare soil class', 'UI':'Urban class',
                                        'NDSI':'Snow class', 'NBI':'Built-up class',
                                    }
                                    _xu = next((xv for xk, xv in _xlabel_map.items()
                                                if xk in label.upper()), f'{label} class')
                                    real_bar = _make_class_bar_b64(
                                        _cp_for_bar, f'{label} class composition', _xu)
                                    if real_bar:
                                        charts = [(t, d) for t, d in charts if t != 'class_bar']
                                        charts.insert(0, ('class_bar', real_bar))
                                        print(f'  ✓ {label} class bar replaced with real GEE data')
                                figures[label] = {
                                    'analysis_map': analysis_b64,
                                    'charts'      : charts,
                                    'rgb_overview': rgb_overview_b64,
                                }
                            print(f'  ✓ {label} ready')

                    except Exception as ve:
                        print(f'  [{v}] failed: {ve}')
                        import traceback as _tb2; _tb2.print_exc()

            except Exception as se:
                print(f'Surface analysis error: {se}')
                import traceback as _tb3; _tb3.print_exc()

        update_step(3, 'running', 55)

        # Atmospheric analysis — tile layers + static figures
        if atmo_vars:
            try:
                from gis_functions import ATMO_INDEX_MAP, VIS, get_stats, compute_ffpi, get_thumb, make_analysis_map, make_stats_charts
                study_area_atmo = study_area_main
                bbox = geo.get('bbox')

                # Generate one RGB overview for all atmo vars (reuse if surface already made one)
                atmo_rgb_overview = locals().get('rgb_overview_b64') or None
                if atmo_rgb_overview is None and bbox and not surface_vars and not lulc_vars:
                    try:
                        from gis_functions import make_rgb_overview, load_landsat
                        _, atmo_composite = load_landsat(study_area_atmo, start_date, end_date)
                        atmo_rgb_overview = make_rgb_overview(
                            atmo_composite, study_area_atmo, region_name, bbox)
                        print('  ✓ Atmo RGB overview generated')
                    except Exception as arge:
                        print(f'  Atmo RGB overview failed: {arge}')
                atmo_first_var = True   # attach rgb_overview only to first atmo figure

                for v in atmo_vars:
                    try:
                        if v == 'ffpi':
                            from gis_functions import compute_no2, compute_co, compute_so2
                            ffpi_img, _ = compute_ffpi(study_area_atmo, start_date, end_date)
                            s = get_stats(ffpi_img, 'FFPI', study_area_atmo, scale=3500)
                            print(f'  [DEBUG] FFPI pixel stats: mean={s.get("mean"):.4f} '
                                  f'min={s.get("min"):.4f} max={s.get("max"):.4f} '
                                  f'p10={s.get("p10"):.4f} p90={s.get("p90"):.4f}')

                            # ── Monthly stats — same pattern as other atmo vars ──────────
                            try:
                                import datetime as _dt_ffpi
                                ffpi_no2_col = (ee.ImageCollection('COPERNICUS/S5P/OFFL/L3_NO2')
                                                  .filterDate(start_date, end_date)
                                                  .filterBounds(study_area_atmo)
                                                  .select('tropospheric_NO2_column_number_density'))
                                ffpi_co_col  = (ee.ImageCollection('COPERNICUS/S5P/OFFL/L3_CO')
                                                  .filterDate(start_date, end_date)
                                                  .filterBounds(study_area_atmo)
                                                  .select('CO_column_number_density'))
                                ffpi_so2_col = (ee.ImageCollection('COPERNICUS/S5P/OFFL/L3_SO2')
                                                  .filterDate(start_date, end_date)
                                                  .filterBounds(study_area_atmo)
                                                  .select('SO2_column_number_density'))
                                ffpi_monthly = {}
                                start_dt_f = _dt_ffpi.datetime.strptime(start_date, '%Y-%m-%d').replace(day=1)
                                end_dt_f   = _dt_ffpi.datetime.strptime(end_date,   '%Y-%m-%d')
                                cur_f = start_dt_f
                                while cur_f <= end_dt_f:
                                    m_s_f = cur_f.strftime('%Y-%m-%d')
                                    nxt_f = (cur_f.replace(year=cur_f.year+1, month=1, day=1)
                                             if cur_f.month == 12
                                             else cur_f.replace(month=cur_f.month+1, day=1))
                                    m_e_f = nxt_f.strftime('%Y-%m-%d')
                                    try:
                                        m_no2 = ffpi_no2_col.filterDate(m_s_f, m_e_f)
                                        m_co  = ffpi_co_col.filterDate(m_s_f,  m_e_f)
                                        m_so2 = ffpi_so2_col.filterDate(m_s_f, m_e_f)
                                        if (m_no2.size().getInfo() > 0 and
                                            m_co.size().getInfo()  > 0 and
                                            m_so2.size().getInfo() > 0):
                                            # Re-compute FFPI for this month using same normalisation
                                            no2_m = m_no2.mean().rename('NO2')
                                            co_m  = m_co.mean().rename('CO')
                                            so2_m = m_so2.mean().rename('SO2')
                                            # Absolute normalisation — same SE-Asian ceilings as compute_ffpi
                                            def _norm_abs_m(img, lo, hi):
                                                return img.max(lo).min(hi).subtract(lo).divide(hi - lo)
                                            ffpi_m = (_norm_abs_m(no2_m, 2e-5, 1.8e-4)
                                                        .add(_norm_abs_m(co_m,  0.018, 0.025))
                                                        .add(_norm_abs_m(so2_m, 0.0,   1.5e-4))
                                                        .divide(3).rename('FFPI'))
                                            ms_f = ffpi_m.reduceRegion(
                                                reducer=ee.Reducer.mean(),
                                                geometry=study_area_atmo, scale=3500, maxPixels=1e9
                                            ).getInfo()
                                            val_f = ms_f.get('FFPI')
                                            if val_f is not None:
                                                ffpi_monthly[cur_f.strftime('%Y-%m')] = round(float(val_f), 6)
                                    except: pass
                                    cur_f = nxt_f
                                s['monthly'] = ffpi_monthly
                                print(f'  ✓ FFPI monthly: {len(ffpi_monthly)} months')
                            except Exception as _ffpi_me:
                                s['monthly'] = {}
                                print(f'  FFPI monthly failed: {_ffpi_me}')

                            # ── Real per-class area (ha) — same as other atmo vars ────────
                            _total_ha_f, _class_pcts_f = _compute_area_stats(
                                ffpi_img, 'FFPI', study_area_atmo, 'FFPI', scale=3500)
                            if _total_ha_f:
                                s['total_ha']   = _total_ha_f
                                s['class_pcts'] = _class_pcts_f

                            all_stats['FFPI'] = s
                            map_id   = ffpi_img.clip(study_area_atmo).getMapId(VIS['ffpi'])
                            tile_url = map_id['tile_fetcher'].url_format
                            layers.append({'name': _layer_label('FFPI', region_name, start_date, end_date), 'tile_url': tile_url,
                                           'type': 'tile', 'bbox': bbox})
                            if bbox:
                                arr    = get_thumb(ffpi_img.clip(study_area_atmo), VIS['ffpi'], study_area_atmo, dim=512)
                                charts = make_stats_charts(all_stats, 'ffpi', 'FFPI')
                                # Replace simulated class bar with real GEE data bar ──────
                                _cp_f = all_stats['FFPI'].get('class_pcts') or {}
                                if _cp_f:
                                    real_bar_f = _make_class_bar_b64(
                                        _cp_f, 'FFPI class composition', 'Pollution class')
                                    if real_bar_f:
                                        charts = [(t, d) for t, d in charts if t != 'class_bar']
                                        charts.insert(0, ('class_bar', real_bar_f))
                                        print('  ✓ FFPI class bar: real GEE data')
                                figures['FFPI'] = {
                                    'analysis_map': make_analysis_map(arr, VIS['ffpi'], 'FFPI Score', region_name, bbox),
                                    'charts'      : charts,
                                    'rgb_overview': atmo_rgb_overview if atmo_first_var else None,
                                }
                                atmo_first_var = False
                            print('  ✓ FFPI ready')

                        elif v in ATMO_INDEX_MAP:
                            label, func, vis_key, unit = ATMO_INDEX_MAP[v]
                            img, col = func(study_area_atmo, start_date, end_date)
                            c = col.size().getInfo()
                            if c > 0:
                                band_name      = img.bandNames().getInfo()[0]
                                orig_band_name = col.first().bandNames().getInfo()[0]
                                s = get_stats(img, band_name, study_area_atmo, scale=3500)

                                # Monthly stats — same pattern as LST
                                try:
                                    import datetime as _dt_a
                                    atmo_monthly = {}
                                    start_dt_a = _dt_a.datetime.strptime(start_date, '%Y-%m-%d').replace(day=1)
                                    end_dt_a   = _dt_a.datetime.strptime(end_date,   '%Y-%m-%d')
                                    cur_a = start_dt_a
                                    while cur_a <= end_dt_a:
                                        m_s_a = cur_a.strftime('%Y-%m-%d')
                                        nxt   = (cur_a.replace(year=cur_a.year+1, month=1, day=1)
                                                 if cur_a.month == 12
                                                 else cur_a.replace(month=cur_a.month+1, day=1))
                                        m_e_a = nxt.strftime('%Y-%m-%d')
                                        try:
                                            m_col_a = col.filterDate(m_s_a, m_e_a)
                                            if m_col_a.size().getInfo() > 0:
                                                m_img_a   = m_col_a.mean().select(orig_band_name).rename(band_name)
                                                m_stats_a = m_img_a.reduceRegion(
                                                    reducer=ee.Reducer.mean(),
                                                    geometry=study_area_atmo, scale=3500, maxPixels=1e9
                                                ).getInfo()
                                                val_a = m_stats_a.get(band_name)
                                                if val_a is not None:
                                                    atmo_monthly[cur_a.strftime('%Y-%m')] = round(float(val_a), 8)
                                        except: pass
                                        cur_a = nxt
                                    s['monthly'] = atmo_monthly
                                    print(f'  ✓ {label} monthly: {len(atmo_monthly)} months')
                                except Exception as _me:
                                    s['monthly'] = {}

                                all_stats[label] = s

                                # Real per-class area (ha) — same as LST
                                _total_ha_a, _class_pcts_a = _compute_area_stats(
                                    img, band_name, study_area_atmo, label, scale=3500)
                                if _total_ha_a:
                                    all_stats[label]['total_ha']   = _total_ha_a
                                    all_stats[label]['class_pcts'] = _class_pcts_a

                                map_id   = img.clip(study_area_atmo).getMapId(VIS[vis_key])
                                tile_url = map_id['tile_fetcher'].url_format
                                layers.append({'name': _layer_label(label, region_name, start_date, end_date),
                                               'tile_url': tile_url, 'type': 'tile', 'bbox': bbox})
                                if bbox:
                                    arr    = get_thumb(img.clip(study_area_atmo), VIS[vis_key], study_area_atmo, dim=512)
                                    charts = make_stats_charts(all_stats, v, label)
                                    _cp_a  = all_stats[label].get('class_pcts') or {}
                                    if _cp_a:
                                        real_bar_a = _make_class_bar_b64(
                                            _cp_a, f'{label} class composition', f'{label} concentration class')
                                        if real_bar_a:
                                            charts = [(t, d) for t, d in charts if t != 'class_bar']
                                            charts.insert(0, ('class_bar', real_bar_a))
                                            print(f'  ✓ {label} class bar: real GEE data')
                                    figures[label] = {
                                        'analysis_map': make_analysis_map(arr, VIS[vis_key], f'{label} ({unit})', region_name, bbox),
                                        'charts'      : charts,
                                        'rgb_overview': atmo_rgb_overview if atmo_first_var else None,
                                    }
                                    atmo_first_var = False
                                print(f'  ✓ {label} ready')
                    except Exception as ve:
                        print(f'  [{v}] atmo failed: {ve}')

            except Exception as ae:
                print(f'Atmo analysis error: {ae}')

        # LULC analysis — tile layer + static map + charts
        if lulc_vars:
            update_step(3, 'running', 70)
            try:
                from gis_functions import compute_lulc, make_lulc_charts
                study_area_lulc = study_area_main
                lulc_result = compute_lulc(study_area_lulc, start_date, end_date, region_name)
                if not lulc_result['success']:
                    _fail_msg = lulc_result.get('message', 'Unknown LULC error')
                    print(f'  LULC failed: {_fail_msg}')
                    _lulc_rgb_err = locals().get('rgb_overview_b64') or None
                    all_stats['LULC'] = {'error': _fail_msg}
                    figures['LULC'] = {
                        'error'       : _fail_msg,
                        'analysis_map': None,
                        'charts'      : [],
                        'rgb_overview': _lulc_rgb_err,
                    }
                elif lulc_result['success']:
                    all_stats['LULC'] = lulc_result['stats']

                    # ── Run validation in app.py — independent of gis_functions version ──
                    _ml = lulc_result.get('ml_metrics', {})
                    if not _ml:
                        # Try to compute validation here using returned GEE objects
                        _clf  = lulc_result.get('_classifier')
                        _ts   = lulc_result.get('_test_set')
                        _sids = lulc_result.get('_sampled_ids', [])
                        _ntot = lulc_result.get('_n_total', 0)
                        if _clf is not None and _ts is not None and _sids:
                            try:
                                from gis_functions import ESRI_CLASSES
                                import ee
                                print('  [app] Running validation (aggregate_array)...', flush=True)
                                _tested     = _ts.classify(_clf)
                                _act        = _tested.aggregate_array('landcover').getInfo()
                                _pred       = _tested.aggregate_array('classification').getInfo()
                                print(f'  [app] Labels: {len(_act)} actual, {len(_pred)} predicted', flush=True)
                                if _act and _pred and len(_act) == len(_pred):
                                    _seen   = set(int(x) for x in _act if x is not None)
                                    _order  = [c for c in _sids if c in _seen] or sorted(_seen)
                                    _imap   = {c: i for i, c in enumerate(_order)}
                                    _nc     = len(_order)
                                    _mat    = [[0]*_nc for _ in range(_nc)]
                                    for a, p in zip(_act, _pred):
                                        ai = _imap.get(int(a) if a is not None else -1, -1)
                                        pi = _imap.get(int(p) if p is not None else -1, -1)
                                        if ai >= 0 and pi >= 0:
                                            _mat[ai][pi] += 1
                                    _tot  = sum(sum(r) for r in _mat)
                                    _corr = sum(_mat[i][i] for i in range(_nc))
                                    _oa   = round(_corr/_tot, 4) if _tot else 0.0
                                    _rs   = [sum(_mat[i]) for i in range(_nc)]
                                    _cs   = [sum(_mat[r][i] for r in range(_nc)) for i in range(_nc)]
                                    _pe   = sum(_rs[i]*_cs[i] for i in range(_nc))/(_tot**2) if _tot else 0
                                    _kap  = round((_oa-_pe)/(1-_pe), 4) if (1-_pe) > 0 else 0.0
                                    _cnames = [ESRI_CLASSES[c][0] for c in _order]
                                    _ccolors= [ESRI_CLASSES[c][1] for c in _order]
                                    _pl,_rl,_fl,_fpl,_al = [],[],[],[],[]
                                    for i in range(_nc):
                                        tp=_mat[i][i]; fp=_cs[i]-tp; fn=_rs[i]-tp; tn=_tot-tp-fp-fn
                                        p=round(tp/(tp+fp),4) if tp+fp>0 else 0.0
                                        r=round(tp/(tp+fn),4) if tp+fn>0 else 0.0
                                        f=round(2*p*r/(p+r),4) if p+r>0 else 0.0
                                        fpr=round(fp/(fp+tn),4) if fp+tn>0 else 0.0
                                        ac=round((tp+tn)/_tot,4) if _tot>0 else 0.0
                                        _pl.append(p);_rl.append(r);_fl.append(f);_fpl.append(fpr);_al.append(ac)
                                    _ml = {
                                        'overall_accuracy': _oa, 'kappa': _kap,
                                        'avg_precision': round(sum(_pl)/_nc,4),
                                        'avg_recall'   : round(sum(_rl)/_nc,4),
                                        'avg_f1'       : round(sum(_fl)/_nc,4),
                                        'auc_approx'   : round(1.0-sum(_fpl)/_nc,4),
                                        'per_class': {_cnames[i]: {
                                            'precision':_pl[i],'recall':_rl[i],'f1':_fl[i],
                                            'fpr':_fpl[i],'color':_ccolors[i],'accuracy':_al[i]
                                        } for i in range(_nc)},
                                        'confusion_matrix': _mat, 'class_names': _cnames,
                                        'n_train': int(_ntot*0.8), 'n_test': int(_ntot*0.2),
                                        'n_total': _ntot,
                                    }
                                    lulc_result['ml_metrics'] = _ml
                                    print(f'  [app] ✓ Validation: OA={_oa:.3f} | Kappa={_kap:.3f}', flush=True)
                            except Exception as _ve:
                                import traceback as _vtb
                                print(f'  [app] Validation failed: {type(_ve).__name__}: {_ve}', flush=True)
                                _vtb.print_exc()

                    if lulc_result.get('ml_metrics'):
                        all_stats['LULC']['ml_metrics'] = lulc_result['ml_metrics']
                    lulc_vis     = lulc_result['vis_params']
                    lulc_clipped = lulc_result['lulc_img'].clip(study_area_lulc)
                    if 'sld_style' in lulc_vis:
                        map_id = lulc_clipped.sldStyle(lulc_vis['sld_style']).getMapId({})
                    else:
                        map_id = lulc_clipped.getMapId(lulc_vis)
                    layers.append({
                        'name'      : _layer_label('Land Cover', region_name, start_date, end_date),
                        'tile_url'  : map_id['tile_fetcher'].url_format,
                        'type'      : 'tile',
                        'bbox'      : geo.get('bbox'),
                        'lulc_stats': lulc_result['stats'],
                    })
                    print('  ✓ LULC tile layer ready')

                    # Static map thumbnail
                    bbox = geo.get('bbox')
                    lulc_map_b64 = None
                    if bbox:
                        try:
                            import numpy as np
                            import matplotlib.pyplot as plt
                            import matplotlib.patches as mpatches
                            # Use the styled image (with sld_style applied) for the thumbnail
                            # so colours match the LULC classification, not a grayscale default
                            if 'sld_style' in lulc_vis:
                                styled_lulc = lulc_clipped.sldStyle(lulc_vis['sld_style'])
                                arr = get_thumb(styled_lulc, {}, study_area_lulc, dim=512)
                            else:
                                arr = get_thumb(lulc_clipped, lulc_vis, study_area_lulc, dim=512)
                            classes_data = lulc_result['stats'].get('classes', {})
                            w, s_bb, e, n_bb = bbox
                            fig, ax = plt.subplots(figsize=(7, 6))
                            ax.imshow(arr, extent=[w, e, s_bb, n_bb], aspect='auto', origin='upper')
                            patches = [mpatches.Patch(color=info['color'],
                                       label=f"{cls} ({info['percentage']:.1f}%)")
                                       for cls, info in classes_data.items()]
                            ax.legend(handles=patches, loc='lower right', fontsize=7,
                                      framealpha=0.85, edgecolor='#ccc',
                                      title='Land Cover', title_fontsize=8)
                            lon_ticks = np.linspace(w, e, 5)
                            lat_ticks = np.linspace(s_bb, n_bb, 5)
                            ax.set_xticks(lon_ticks)
                            ax.set_yticks(lat_ticks)
                            ax.set_xticklabels([f'{v:.2f}°' for v in lon_ticks], fontsize=8, color='#555')
                            ax.set_yticklabels([f'{v:.2f}°' for v in lat_ticks], fontsize=8, color='#555')
                            ax.grid(False)
                            ax.set_title(f'Land Cover — {region_name}', fontsize=11, fontweight='bold', pad=10)
                            for spine in ax.spines.values():
                                spine.set_edgecolor('#cccccc'); spine.set_linewidth(0.8)
                            ax.text(0.01, 0.01, '© Landsat / Google Earth Engine',
                                    transform=ax.transAxes, fontsize=7, color='white',
                                    bbox=dict(boxstyle='round,pad=0.2', facecolor='black', alpha=0.4))
                            plt.tight_layout()
                            from gis_functions import fig_to_base64
                            lulc_map_b64 = fig_to_base64(fig)
                            print('  ✓ LULC static map generated')
                        except Exception as lme:
                            print(f'  LULC static map failed: {lme}')

                    lulc_charts = make_lulc_charts(lulc_result['stats'])

                    # ── Generate confusion matrix + metrics panel images ───────
                    # Same approach as pie chart — generate as b64 and pass in charts
                    ml = lulc_result.get('ml_metrics', {})
                    if ml and ml.get('confusion_matrix') and ml.get('class_names'):
                        try:
                            import matplotlib
                            matplotlib.use('Agg')
                            import matplotlib.pyplot as plt
                            import matplotlib.colors as mcolors
                            import io, base64 as _b64m
                            from gis_functions import fig_to_base64

                            # ── Confusion matrix heatmap ──────────────────────
                            cm      = ml['confusion_matrix']
                            cnames  = ml['class_names']
                            n       = len(cnames)
                            oa      = ml.get('overall_accuracy', 0)
                            kap     = ml.get('kappa', 0)

                            cm_arr = [[cm[r][c] for c in range(n)] for r in range(n)]
                            row_totals = [sum(cm_arr[r]) for r in range(n)]
                            # Normalize per row for color (proportion)
                            cm_norm = [[cm_arr[r][c]/row_totals[r] if row_totals[r]>0 else 0
                                        for c in range(n)] for r in range(n)]

                            fig, ax = plt.subplots(figsize=(max(7, n*1.2), max(6, n*1.0)))
                            fig.patch.set_facecolor('white')
                            cmap = mcolors.LinearSegmentedColormap.from_list(
                                'cm', ['#1565c0', '#e3f2fd', '#ffcdd2', '#c62828'])
                            im = ax.imshow(cm_norm, cmap=cmap, vmin=0, vmax=1, aspect='auto')

                            ax.set_xticks(range(n)); ax.set_yticks(range(n))
                            ax.set_xticklabels(cnames, rotation=30, ha='right', fontsize=9)
                            ax.set_yticklabels(cnames, fontsize=9)
                            ax.set_xlabel('Predicted', fontsize=11, labelpad=8)
                            ax.set_ylabel('Actual', fontsize=11, labelpad=8)
                            ax.set_title(
                                f'Confusion Matrix\nOverall Accuracy: {oa*100:.1f}%  |  Kappa: {kap:.3f}',
                                fontsize=12, fontweight='bold', pad=12)

                            for r in range(n):
                                for c in range(n):
                                    val  = cm_arr[r][c]
                                    bg   = cm_norm[r][c]
                                    fc   = 'white' if bg > 0.45 else '#1a1a1a'
                                    ax.text(c, r, str(val), ha='center', va='center',
                                            fontsize=9, fontweight='bold', color=fc)

                            cbar = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
                            cbar.set_label('Proportion', fontsize=9)
                            cbar.ax.tick_params(labelsize=8)

                            plt.tight_layout()
                            lulc_charts.append(('lulc_confusion_matrix', fig_to_base64(fig)))
                            plt.close(fig)
                            print('  ✓ Confusion matrix chart generated')

                            # ── Per-class metrics panel ───────────────────────
                            per_class = ml.get('per_class', {})
                            apr = ml.get('avg_precision', 0)
                            arc = ml.get('avg_recall', 0)
                            af1 = ml.get('avg_f1', 0)
                            auc = ml.get('auc_approx', None)
                            fpr_vals = [v.get('fpr',0) for v in per_class.values()]
                            avg_fpr  = sum(fpr_vals)/len(fpr_vals) if fpr_vals else 0
                            n_cls    = len(per_class)

                            row_h   = 0.45
                            panel_h = 0.9 + n_cls*row_h + 1.6
                            fig2, ax2 = plt.subplots(figsize=(9, panel_h))
                            ax2.set_facecolor('#1a1a2e'); fig2.patch.set_facecolor('#1a1a2e')
                            ax2.axis('off')

                            def fy(ly): return 1.0 - ly/panel_h
                            WHITE='#ffffff'; LGRAY='#aaaaaa'

                            fig2.text(0.03, fy(0.15), 'Per-class Performance',
                                      color=WHITE, fontsize=11, fontweight='bold',
                                      transform=fig2.transFigure)
                            hdrs = ['Class','Accuracy','Precision','Recall','F1 Score','FPR']
                            hxs  = [0.03, 0.28, 0.42, 0.56, 0.70, 0.84]
                            for hx,ht in zip(hxs,hdrs):
                                fig2.text(hx, fy(0.42), ht, color=LGRAY, fontsize=8,
                                          fontweight='bold', transform=fig2.transFigure)

                            tab10 = plt.cm.get_cmap('tab10')
                            for i,(cls_name,cls_m) in enumerate(per_class.items()):
                                ry  = fy(0.62 + i*row_h)
                                p   = cls_m.get('precision',0)
                                r   = cls_m.get('recall',0)
                                f1  = cls_m.get('f1',0)
                                acc = cls_m.get('accuracy',0)
                                fpr = cls_m.get('fpr',0)
                                raw_c = cls_m.get('color',None)
                                if raw_c and isinstance(raw_c,str) and raw_c.startswith('#'):
                                    dc = raw_c
                                else:
                                    rgba = tab10(i%10)
                                    dc = '#{:02x}{:02x}{:02x}'.format(
                                        int(rgba[0]*255),int(rgba[1]*255),int(rgba[2]*255))
                                fig2.text(hxs[0]-0.005, ry, '●', color=dc, fontsize=11,
                                          transform=fig2.transFigure, va='center')
                                fig2.text(hxs[0]+0.025, ry, cls_name, color=WHITE,
                                          fontsize=9, fontweight='bold',
                                          transform=fig2.transFigure, va='center')
                                for xi,(cx,val) in enumerate(zip(hxs[1:],
                                    [f'{acc*100:.1f}%',f'{p*100:.1f}%',
                                     f'{r*100:.1f}%', f'{f1*100:.1f}%',f'{fpr*100:.1f}%'])):
                                    raw_v = [acc,p,r,f1,fpr][xi]
                                    if xi in (1,2,3):
                                        vc = '#4ade80' if raw_v>=0.7 else '#facc15' if raw_v>=0.4 else '#f87171'
                                    else: vc = WHITE
                                    fig2.text(cx, ry, val, color=vc, fontsize=9,
                                              transform=fig2.transFigure, va='center')

                            sep_y = fy(0.62+n_cls*row_h+0.12)
                            fig2.add_artist(plt.Line2D([0.03,0.97],[sep_y,sep_y],
                                color='#555577', lw=0.6, transform=fig2.transFigure))

                            omy = fy(0.62+n_cls*row_h+0.40)
                            fig2.text(0.03, omy, 'Overall Model Metrics', color=WHITE,
                                      fontsize=10, fontweight='bold', transform=fig2.transFigure)

                            overall_items = [
                                ('Overall Accuracy', f'{oa*100:.1f}%'),
                                ('Kappa Coefficient', f'{kap:.3f}'),
                                ('Macro Precision', f'{(apr or 0)*100:.1f}%'),
                                ('Macro Recall', f'{(arc or 0)*100:.1f}%'),
                                ('Macro F1 Score', f'{(af1 or 0)*100:.1f}%'),
                                ('Avg False Positive Rate', f'{avg_fpr*100:.1f}%'),
                            ]
                            if auc: overall_items.append(('AUC (approx.)', f'{auc:.3f}'))
                            for idx,(lbl,val) in enumerate(overall_items):
                                xp = 0.03 if idx%2==0 else 0.52
                                ry2 = fy(0.62+n_cls*row_h+0.78+(idx//2)*0.40)
                                fig2.text(xp, ry2, f'• {lbl}:', color=LGRAY,
                                          fontsize=8.5, transform=fig2.transFigure)
                                fig2.text(xp+0.22, ry2, val, color=WHITE,
                                          fontsize=8.5, fontweight='bold',
                                          transform=fig2.transFigure)

                            plt.tight_layout(pad=0.1)
                            lulc_charts.append(('lulc_metrics_panel',
                                fig_to_base64(fig2)))
                            plt.close(fig2)
                            print('  ✓ Metrics panel chart generated')

                        except Exception as cm_err:
                            print(f'  Confusion matrix/metrics chart failed: {cm_err}')
                            import traceback; traceback.print_exc()

                    # Generate RGB overview for LULC if surface analysis didn't already do it
                    lulc_rgb_overview = locals().get('rgb_overview_b64') or None
                    if lulc_rgb_overview is None and bbox:
                        try:
                            from gis_functions import make_rgb_overview, load_landsat
                            _, lulc_composite = load_landsat(study_area_lulc, start_date, end_date)
                            lulc_rgb_overview = make_rgb_overview(
                                lulc_composite, study_area_lulc, region_name, bbox)
                            print('  ✓ LULC RGB overview generated')
                        except Exception as lrge:
                            print(f'  LULC RGB overview failed: {lrge}')

                    figures['LULC'] = {
                        'analysis_map': lulc_map_b64,
                        'charts'      : lulc_charts,
                        'rgb_overview': lulc_rgb_overview,
                    }
                    print(f'  ✓ LULC figures ready ({len(lulc_charts)} charts)')
            except Exception as le:
                import traceback as _tb4; _tb4.print_exc()
                print(f'LULC analysis error: {le}')

        update_step(3, 'done', 100)

        # ── Step 5: Layers already collected via GEE URLs above ─────────────
        update_step(4, 'running', 80)
        print(f'  {len(layers)} layers ready for map display')
        update_step(4, 'done', 100)

        # ── Step 6: Generate AI insights ──────────────────────────────────────
        update_step(5, 'running', 20)
        web_context = fetch_web_context(region_name, start_date, end_date, variables)

        # Per-variable focused insights (one short LLM call per variable)
        var_insights = {}
        non_lulc_vars = [v for v in all_stats if 'LULC' not in v.upper()]
        for i, var_label in enumerate(non_lulc_vars):
            pct = 20 + int((i + 1) / max(len(non_lulc_vars), 1) * 50)
            update_step(5, 'running', pct)
            insight_text = generate_var_insight(
                var_label, all_stats, region_name, start_date, end_date)
            if insight_text:
                var_insights[var_label] = insight_text

        # Overall conclusion (web context + all stats)
        update_step(5, 'running', 80)
        conclusion = generate_conclusion(
            region_name, start_date, end_date, all_stats, variables, web_context or '')

        # Keep legacy insight for backward compat (just reuse conclusion)
        insight = conclusion
        update_step(5, 'done', 100)

        job['status'] = 'complete'
        job['result'] = {
            'type'        : 'analysis',
            'region'      : region_name,
            'start_date'  : start_date,
            'end_date'    : end_date,
            'variables'   : variables,
            'stats'       : all_stats,
            'layers'      : layers,
            'figures'     : figures,
            'geo'         : geo,
            'insight'     : insight or '',
            'var_insights': var_insights,
            'conclusion'  : conclusion or '',
            'web_context' : web_context or '',
        }

    except Exception as ex:
        import traceback as _tb
        _tb.print_exc()
        job['status'] = 'error'
        job['error']  = str(ex)


# ─────────────────────────────────────────────────────────────────────────────
# PER-VARIABLE INSIGHT + CONCLUSION GENERATORS
# ─────────────────────────────────────────────────────────────────────────────

UNIT_LOOKUP_INLINE = {
    'NDVI': 'index (-1 to 1)', 'EVI': 'index (-1 to 1)', 'SAVI': 'index (-1 to 1)',
    'NDWI': 'index (-1 to 1)', 'MNDWI': 'index (-1 to 1)', 'NDBI': 'index (-1 to 1)',
    'UI': 'index (-1 to 1)', 'BSI': 'index (-1 to 1)', 'NDSI': 'index (-1 to 1)',
    'NBI': 'index (0 to 0.5)', 'LST': '°C', 'CO': 'mol/m²', 'NO2': 'mol/m²',
    'SO2': 'mol/m²', 'CH4': 'ppb', 'O3': 'Dobson Units', 'Aerosol': 'unitless AAI',
    'GPP': 'kgC/m²/8-day', 'FFPI': '0–1 normalized',
}

def generate_var_insight(var_label: str, stats: dict, region: str, start_date: str, end_date: str) -> str:
    """Generate a short focused LLM insight for a single variable's map + stats."""
    import requests as req
    from config import OLLAMA_URL, OLLAMA_MODEL

    s = stats.get(var_label) or {}
    if not s or s.get('mean') is None:
        return ''

    unit = next((v for k, v in UNIT_LOOKUP_INLINE.items() if k.upper() in var_label.upper()), 'index')
    fmt = lambda v: f'{v:.4f}' if v is not None else 'N/A'

    stats_text = (
        f'Variable: {var_label} [{unit}]\n'
        f'Region: {region} | Period: {start_date} to {end_date}\n'
        f'Mean: {fmt(s.get("mean"))} | Median: {fmt(s.get("median"))} | '
        f'Std Dev: {fmt(s.get("std"))}\n'
        f'Min: {fmt(s.get("min"))} | Max: {fmt(s.get("max"))}\n'
        f'P10: {fmt(s.get("p10"))} | P90: {fmt(s.get("p90"))}'
    )

    # Variable descriptions — prevents LLM from hallucinating on unfamiliar acronyms
    _VAR_DESCRIPTIONS = {
        'NO2':     'NO2 (Nitrogen Dioxide) — a satellite-derived tropospheric column concentration (mol/m²) from Sentinel-5P TROPOMI. High values indicate traffic, industrial combustion, and power plants.',
        'CO':      'CO (Carbon Monoxide) — tropospheric column density (mol/m²) from Sentinel-5P. Elevated values indicate biomass burning, vehicle exhaust, or industrial emissions.',
        'SO2':     'SO2 (Sulfur Dioxide) — total column concentration (mol/m²) from Sentinel-5P. High values indicate industrial point sources, coal combustion, or volcanic activity.',
        'CH4':     'CH4 (Methane) — atmospheric mixing ratio (ppb) from Sentinel-5P. Elevated values indicate agriculture, landfills, livestock, wetlands, or fossil fuel leaks.',
        'O3':      'O3 (Tropospheric Ozone) — total column (DU) from Sentinel-5P. Low values near urban centers indicate NOx titration; high values indicate photochemical smog.',
        'AEROSOL': 'AEROSOL — Absorbing Aerosol Index (AAI) from Sentinel-5P UV. Positive values indicate smoke, dust, or industrial aerosols; negative values indicate clean marine air.',
        'FFPI':    'FFPI (Fossil Fuel Pollution Index) — a GEE-derived composite index (0–1) combining normalised NO2, CO, and SO2 columns. High values indicate areas of intense fossil fuel combustion. This is NOT an institution or organisation.',
        'GPP':     'GPP (Gross Primary Production) — 8-day vegetation carbon uptake (kgC/m²/8-day) from MODIS MOD17A2H. High values indicate dense, productive vegetation.',
        'BURNED':  'Burned Area — burn date day-of-year (DOY) from MODIS MCD64A1. Values represent the day fire was detected; used to identify fire seasonality and spatial extent.',
        'LST':     'LST (Land Surface Temperature) — daytime surface temperature (°C) from Landsat thermal band. High values indicate urban heat island effects and impervious surfaces.',
    }
    var_up = var_label.upper()
    var_desc = next((desc for key, desc in _VAR_DESCRIPTIONS.items() if key in var_up), f'{var_label} — satellite-derived index')

    prompt = (
        f'You are a satellite remote sensing scientist. '
        f'Write a concise 3–4 sentence insight about the {var_label} map shown for {region}.\n\n'
        f'Variable definition: {var_desc}\n\n'
        f'{stats_text}\n\n'
        f'Focus only on: what the mean value indicates about the environmental condition, '
        f'what the spatial range (p10 vs p90) reveals about hotspots or uniformity, '
        f'and one key finding or implication for the region. '
        f'Be specific, scientific, and direct. No bullet points. No headers. Plain paragraph only. '
        f'Do NOT mention any institutions, organisations, or unrelated topics.'
    )

    try:
        resp = req.post(OLLAMA_URL,
            json={'model': OLLAMA_MODEL,
                  'messages': [{'role': 'user', 'content': prompt}],
                  'stream': False},
            timeout=60)
        return resp.json()['message']['content'].strip()
    except Exception as e:
        return f'Insight unavailable: {e}'


def generate_conclusion(region: str, start_date: str, end_date: str,
                        all_stats: dict, variables: list, web_context: str) -> str:
    """Generate a short concluding synthesis using all stats + web context."""
    import requests as req
    from config import OLLAMA_URL, OLLAMA_MODEL

    if not all_stats:
        return ''

    stats_lines = []
    for var, s in all_stats.items():
        if isinstance(s, dict) and s.get('mean') is not None:
            stats_lines.append(
                f'  {var}: mean={s["mean"]:.4f}, p10={s.get("p10","N/A")}, p90={s.get("p90","N/A")}'
            )
        elif isinstance(s, dict) and 'lst_mean' in s:
            stats_lines.append(f'  UHI: LST mean={s["lst_mean"]:.2f}°C, std={s["lst_std"]:.2f}°C')
        elif isinstance(s, dict) and 'classes' in s:
            top = sorted(s['classes'].items(), key=lambda x: -x[1].get('percentage', 0))[:3]
            top_str = ', '.join(f'{k} {v["percentage"]:.1f}%' for k, v in top)
            stats_lines.append(f'  LULC: top classes — {top_str}')

    web_section = (
        f'\nReal-world context (use to ground conclusions):\n{web_context}\n'
        if web_context else ''
    )

    # Variable descriptions for conclusion context
    _CONC_VAR_DESC = {
        'NO2':'tropospheric NO2 column (mol/m², traffic/industrial combustion)',
        'CO':'CO column density (mol/m², combustion/burning)',
        'SO2':'SO2 column (mol/m², industrial/volcanic)',
        'CH4':'methane mixing ratio (ppb, agriculture/landfills/fossil fuels)',
        'O3':'tropospheric ozone (DU, photochemical smog)',
        'AEROSOL':'absorbing aerosol index (smoke/dust/industrial haze)',
        'FFPI':'Fossil Fuel Pollution Index 0-1 composite of NO2+CO+SO2 (NOT an institution)',
        'GPP':'Gross Primary Production (kgC/m²/8-day, vegetation productivity)',
        'BURNED':'burn date DOY from MODIS (fire seasonality)',
        'LST':'Land Surface Temperature (°C, urban heat)',
        'NDVI':'vegetation greenness index (-1 to 1)',
        'LULC':'Land Use Land Cover classification',
    }
    var_desc_lines = []
    for v in variables:
        vup = v.upper()
        desc = next((d for k, d in _CONC_VAR_DESC.items() if k in vup), vup)
        var_desc_lines.append(f'  {vup}: {desc}')

    prompt = (
        f'You are a satellite remote sensing scientist writing the conclusion of an analysis report.\n'
        f'Region: {region} | Period: {start_date} to {end_date}\n'
        f'Variables analyzed (definitions):\n' + '\n'.join(var_desc_lines) + '\n\n'
        f'Summary statistics:\n' + '\n'.join(stats_lines) +
        web_section +
        '\n\nWrite a concise conclusion (4–6 sentences) that:\n'
        '1. Synthesizes the key findings across all variables\n'
        '2. Connects patterns to real-world conditions or events (use web context if relevant)\n'
        '3. Highlights the most important concern or positive finding\n'
        '4. Ends with one concrete, actionable recommendation\n\n'
        'Write in flowing prose. No bullet points. No headers. No markdown. Plain paragraphs only. '
        'Stay strictly on-topic about the satellite data analysis. Do NOT mention any institutions, '
        'organisations, or topics unrelated to the remote sensing variables listed above.'
    )

    try:
        resp = req.post(OLLAMA_URL,
            json={'model': OLLAMA_MODEL,
                  'messages': [{'role': 'user', 'content': prompt}],
                  'stream': False},
            timeout=90)
        return resp.json()['message']['content'].strip()
    except Exception as e:
        return f'Conclusion unavailable: {e}'


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/analyze', methods=['POST'])
def analyze():
    """Start an analysis job. Returns job_id immediately."""
    body       = request.json or {}
    user_input = body.get('message', '').strip()
    roi_geojson= body.get('roi', None)

    if not user_input:
        return jsonify({'error': 'No message provided'}), 400

    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        'status'  : 'queued',
        'result'  : None,
        'error'   : None,
        'parsed'  : {},
        'geo'     : {},
        'steps'   : [
            {'label': 'Initializing agent',          'status': 'pending', 'progress': 0},
            {'label': 'Parsing request',             'status': 'pending', 'progress': 0},
            {'label': 'Geolocating region',          'status': 'pending', 'progress': 0},
            {'label': 'Running GEE analysis',        'status': 'pending', 'progress': 0},
            {'label': 'Processing output layers',    'status': 'pending', 'progress': 0},
            {'label': 'Generating AI insight',       'status': 'pending', 'progress': 0},
        ],
    }

    thread = threading.Thread(
        target=run_analysis_job,
        args=(job_id, user_input, roi_geojson),
        daemon=True,
    )
    thread.start()

    return jsonify({'job_id': job_id})


@app.route('/api/job/<job_id>', methods=['GET'])
def job_status(job_id):
    """Poll job status."""
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    job = jobs[job_id]
    return jsonify({
        'status' : job['status'],
        'steps'  : job['steps'],
        'parsed' : job.get('parsed', {}),
        'geo'    : job.get('geo', {}),
        'result' : job['result'],
        'error'  : job['error'],
    })


@app.route('/api/geocode', methods=['POST'])
def geocode():
    """Geocode a place name."""
    body = request.json or {}
    name = body.get('region', '').strip()
    if not name:
        return jsonify({'error': 'No region provided'}), 400
    return jsonify(geocode_region(name))


@app.route('/outputs/<path:filename>')
def serve_output(filename):
    """Serve saved output images."""
    return send_from_directory(OUTPUT_DIR, filename)


@app.route('/api/debug/<job_id>', methods=['GET'])
def debug_job(job_id):
    """Debug endpoint - shows job details without base64 images."""
    if job_id not in jobs:
        return jsonify({'error': 'Job not found'}), 404
    job = jobs[job_id]
    result = job.get('result') or {}
    layers = result.get('layers', [])
    # Show layer info without the actual base64 data
    layers_info = [{'name': l['name'], 'has_image': bool(l.get('image')),
                    'image_len': len(l.get('image','')),'bbox': l.get('bbox')} for l in layers]
    return jsonify({
        'status' : job['status'],
        'error'  : job['error'],
        'parsed' : job.get('parsed',{}),
        'geo'    : job.get('geo',{}),
        'layers' : layers_info,
        'stats_keys': list(result.get('stats',{}).keys()),
    })


@app.route('/api/health', methods=['GET'])
def health():
    """Health check — verify GEE + Ollama connectivity."""
    import requests as req
    status = {'flask': True, 'ollama': False, 'gee': False}
    try:
        r = req.get('http://localhost:11434/api/tags', timeout=3)
        models = [m['name'] for m in r.json().get('models', [])]
        status['ollama'] = True
        status['ollama_models'] = models
    except:
        pass
    # GEE check: just verify credentials file exists — DO NOT call ee.Reset()
    # or ee.Initialize() here as it corrupts the GEE state for worker threads
    try:
        from config import GEE_SERVICE_ACCOUNT_FILE
        status['gee'] = os.path.exists(GEE_SERVICE_ACCOUNT_FILE)
    except:
        pass
    return jsonify(status)

# ── Research report store (job_id → file path) ────────────────────────────────
report_jobs = {}   # report_job_id → { status, path, error }
 
REPORTS_DIR = os.path.expanduser('~/Downloads/satellite_agent_outputs/reports')
os.makedirs(REPORTS_DIR, exist_ok=True)
 
 
def _run_research_job(report_job_id: str, analysis_result: dict):
    """Background thread: run Research Agent and write DOCX."""
    try:
        # Import here so it reloads fresh each time (same pattern as GIS agent)
        import importlib
        import sys
 
        # Make sure the webapp directory is on the path
        webapp_dir = Path(__file__).parent
        if str(webapp_dir) not in sys.path:
            sys.path.insert(0, str(webapp_dir))
 
        import research_agent as _ra
        importlib.reload(_ra)
 
        pdf_path  = _ra.generate_research_paper(analysis_result, REPORTS_DIR)
        if pdf_path and Path(pdf_path).exists():
            report_jobs[report_job_id]['status'] = 'complete'
            report_jobs[report_job_id]['path']   = pdf_path
            report_jobs[report_job_id]['filename'] = Path(pdf_path).name
        else:
            report_jobs[report_job_id]['status'] = 'error'
            report_jobs[report_job_id]['error']  = 'PDF generation failed — check server logs.'
    except Exception as ex:
        import traceback
        traceback.print_exc()
        report_jobs[report_job_id]['status'] = 'error'
        report_jobs[report_job_id]['error']  = str(ex)
 
 
@app.route('/api/generate_report', methods=['POST'])
def generate_report():
    """
    Start a Research Agent job in the background.
 
    Body JSON:
        { "job_id": "<gis_job_id>" }   — pulls analysis result from `jobs` store
        OR
        { "result": { ... } }           — inline analysis result (for multi-year summary)
 
    Returns immediately:
        { "report_job_id": "<uuid>" }
    """
    body = request.json or {}
 
    # Resolve the analysis result
    gis_job_id = body.get('job_id')
    if gis_job_id:
        if gis_job_id not in jobs:
            return jsonify({'error': 'GIS job not found'}), 404
        gis_job = jobs[gis_job_id]
        if gis_job['status'] != 'complete':
            return jsonify({'error': 'GIS job not yet complete'}), 400
        analysis_result = gis_job.get('result') or {}
        if analysis_result.get('type') not in ('analysis',):
            return jsonify({'error': 'Job is not an analysis result (must be type=analysis)'}), 400
    elif body.get('result'):
        analysis_result = body['result']
    else:
        return jsonify({'error': 'Provide job_id or result'}), 400
 
    report_job_id = str(uuid.uuid4())
    report_jobs[report_job_id] = {
        'status'  : 'running',
        'path'    : None,
        'filename': None,
        'error'   : None,
    }
 
    thread = threading.Thread(
        target=_run_research_job,
        args=(report_job_id, analysis_result),
        daemon=True,
    )
    thread.start()
    print(f'[Research] Started report job {report_job_id} for region={analysis_result.get("region")}')
 
    return jsonify({'report_job_id': report_job_id})
 
 
@app.route('/api/report_status/<report_job_id>', methods=['GET'])
def report_status(report_job_id):
    """Poll Research Agent job status."""
    if report_job_id not in report_jobs:
        return jsonify({'error': 'Report job not found'}), 404
    job = report_jobs[report_job_id]
    return jsonify({
        'status'  : job['status'],
        'filename': job.get('filename'),
        'error'   : job.get('error'),
    })
 
 
@app.route('/api/report/<filename>', methods=['GET'])
def download_report(filename):
    """Serve a generated research paper PDF for download."""
    safe_name = Path(filename).name   # prevent path traversal
    file_path = Path(REPORTS_DIR) / safe_name
    if not file_path.exists():
        return jsonify({'error': 'File not found'}), 404
    as_attachment = request.args.get('download') == '1'
    return send_from_directory(
        REPORTS_DIR,
        safe_name,
        as_attachment=as_attachment,
        download_name=safe_name,
        mimetype='application/pdf',
    )

if __name__ == '__main__':
    print('🛰️  GIS Agent WebApp starting...')
    print(f'   Output dir: {OUTPUT_DIR}')
    print('   Open: http://127.0.0.1:8080')
    app.run(debug=True, port=8080, host='0.0.0.0', threaded=True)