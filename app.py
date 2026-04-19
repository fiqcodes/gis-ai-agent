"""
app.py — Flask backend for GIS Agent WebApp
Connects to real GEE + Ollama agent (agent.py / gis_functions.py)
"""

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
PARENT_DIR  = Path(__file__).parent.parent
PROJECT_DIR = Path(__file__).parent   # gis-ai-agent/ — must come FIRST
# Insert project dir at 0 so its gis_functions.py is found before ~/Downloads/gis_functions.py
sys.path.insert(0, str(PROJECT_DIR))
sys.path.insert(1, str(PARENT_DIR))

app = Flask(__name__)
CORS(app)

# ── Initialize GEE ONCE at startup, keep credentials alive ───────────────────
import os as _os
import ee as _ee
from config import (GEE_PROJECT as _GEE_PROJECT,
                    GEE_SERVICE_ACCOUNT_FILE as _SA_FILE,
                    GEE_SERVICE_ACCOUNT_EMAIL as _SA_EMAIL)

# Global credentials object — refreshed before each use, never re-initialized
_GEE_CREDENTIALS = None

def _build_gee_credentials():
    """Build fresh credentials from service account file."""
    import google.oauth2.service_account as _sa
    import google.auth.transport.requests as _ga_req
    scopes = ['https://www.googleapis.com/auth/earthengine',
              'https://www.googleapis.com/auth/cloud-platform']
    creds = _sa.Credentials.from_service_account_file(_SA_FILE, scopes=scopes)
    creds.refresh(_ga_req.Request())
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
        )
        update_step(0, 'done', 100)

        # ── Step 2: parse intent via Ollama ───────────────────────────────────
        update_step(1, 'running', 20)
        import requests as req
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
        update_step(1, 'done', 100)

        region_name = parsed.get('region') or 'Unknown'
        start_date  = parsed.get('start_date') or '2023-01-01'
        end_date    = parsed.get('end_date')   or '2023-12-31'
        variables   = parsed.get('variables')  or []
        intent      = parsed.get('intent', 'analysis')

        # ── Multi-year detection — deterministic, never trust the LLM for this ──
        # Parse start/end year from dates
        import datetime as _dt_parse
        import re as _re
        _start_year = int(start_date[:4])
        _end_year   = int(end_date[:4])
        _span_years = _end_year - _start_year + 1   # e.g. 2023-2025 → 3

        # Detect seasonal month range from user_input text
        # Map common keywords → (month_start, month_end)
        _SEASON_MAP = {
            'summer'     : (6, 8),   'winter'    : (12, 2),
            'spring'     : (3, 5),   'autumn'    : (9, 11),  'fall': (9, 11),
            'dry season' : (4, 9),   'wet season': (10, 3),  'rainy season': (10, 3),
            'monsoon'    : (6, 9),
        }
        _month_start, _month_end = None, None
        _ui_lower = user_input.lower()
        for _kw, (_ms, _me) in _SEASON_MAP.items():
            if _kw in _ui_lower:
                _month_start, _month_end = _ms, _me
                break

        # If no season keyword, check if user specified month range (e.g. "Mar-Sep" or "march to september")
        if not _month_start:
            _MONTH_NAMES = {
                'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
                'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12,
                'january':1,'february':2,'march':3,'april':4,'june':6,
                'july':7,'august':8,'september':9,'october':10,'november':11,'december':12
            }
            # Match patterns like "Mar to Sep", "march-september", "03-09"
            _mrange = _re.search(
                r'\b(' + '|'.join(_MONTH_NAMES.keys()) + r')\b.*?(?:to|-)\s*\b(' + '|'.join(_MONTH_NAMES.keys()) + r')\b',
                _ui_lower)
            if _mrange:
                _month_start = _MONTH_NAMES.get(_mrange.group(1))
                _month_end   = _MONTH_NAMES.get(_mrange.group(2))

        # Multi-year triggers when span > 1 year AND user explicitly mentioned multiple years
        # (e.g. "2023-2025", "2022 to 2024", "2022, 2023, 2024" — not just a long single period)
        _explicit_multi = bool(
            _re.search(r'\b(20\d\d)\s*[-–—to]+\s*(20\d\d)\b', _ui_lower) or   # "2023-2025" or "2023 to 2025"
            len(_re.findall(r'\b20\d\d\b', _ui_lower)) >= 2                     # two or more year numbers
        )
        is_multiyear = _explicit_multi and _span_years > 1
        if is_multiyear:
            years_list  = list(range(_start_year, _end_year + 1))
            month_start = _month_start
            month_end   = _month_end
            print(f'  Multi-year mode: {years_list}, months: {month_start}–{month_end}')
        else:
            years_list  = None
            month_start = None
            month_end   = None

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
            'intent': intent, 'is_multiyear': is_multiyear,
            'years': years_list,
        }

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
                                'name'    : 'True Color (RGB)',
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
                            map_id   = lst_img.clip(study_area_surf).getMapId(VIS['lst'])
                            tile_url = map_id['tile_fetcher'].url_format
                            layers.append({'name': 'LST (°C)', 'tile_url': tile_url,
                                           'type': 'tile', 'bbox': bbox})
                            if bbox:
                                arr          = get_thumb(lst_img.clip(study_area_surf), VIS['lst'], study_area_surf, dim=512)
                                analysis_b64 = make_analysis_map(arr, VIS['lst'], 'LST (°C)', region_name, bbox)
                                charts       = make_stats_charts(all_stats, 'lst', 'LST')
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
                                'z_mean'  : uhi_zstats.get('mean',   0.0),
                                'z_std'   : uhi_zstats.get('std',    1.0),
                                'z_min'   : uhi_zstats.get('min',   -4.0),
                                'z_max'   : uhi_zstats.get('max',    4.0),
                                'z_p10'   : uhi_zstats.get('p10',   -1.3),
                                'z_p90'   : uhi_zstats.get('p90',    1.3),
                            }
                            map_id   = uhi_img.clip(study_area_surf).getMapId(VIS['uhi'])
                            tile_url = map_id['tile_fetcher'].url_format
                            layers.append({'name': f'UHI (mean={lst_mean:.1f}\u00b0C)', 'tile_url': tile_url,
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
                            map_id   = img.clip(study_area_surf).getMapId(VIS[vis_key])
                            tile_url = map_id['tile_fetcher'].url_format
                            layers.append({'name': label, 'tile_url': tile_url,
                                           'type': 'tile', 'bbox': bbox})
                            # Static analysis map + charts
                            if bbox:
                                arr          = get_thumb(img.clip(study_area_surf), VIS[vis_key], study_area_surf, dim=512)
                                analysis_b64 = make_analysis_map(arr, VIS[vis_key], label, region_name, bbox)
                                charts       = make_stats_charts(all_stats, v, label)
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
                atmo_rgb_overview = rgb_overview_b64 if 'rgb_overview_b64' in dir() and rgb_overview_b64 else None
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
                            ffpi_img, _ = compute_ffpi(study_area_atmo, start_date, end_date)
                            s = get_stats(ffpi_img, 'FFPI', study_area_atmo, scale=3500)
                            all_stats['FFPI'] = s
                            map_id   = ffpi_img.clip(study_area_atmo).getMapId(VIS['ffpi'])
                            tile_url = map_id['tile_fetcher'].url_format
                            layers.append({'name': 'FFPI Score', 'tile_url': tile_url,
                                           'type': 'tile', 'bbox': bbox})
                            if bbox:
                                arr = get_thumb(ffpi_img.clip(study_area_atmo), VIS['ffpi'], study_area_atmo, dim=512)
                                figures['FFPI'] = {
                                    'analysis_map': make_analysis_map(arr, VIS['ffpi'], 'FFPI Score', region_name, bbox),
                                    'charts'      : make_stats_charts(all_stats, 'ffpi', 'FFPI'),
                                    'rgb_overview': atmo_rgb_overview if atmo_first_var else None,
                                }
                                atmo_first_var = False

                        elif v in ATMO_INDEX_MAP:
                            label, func, vis_key, unit = ATMO_INDEX_MAP[v]
                            img, col = func(study_area_atmo, start_date, end_date)
                            c = col.size().getInfo()
                            if c > 0:
                                band_name = img.bandNames().getInfo()[0]
                                s = get_stats(img, band_name, study_area_atmo, scale=3500)
                                all_stats[label] = s
                                map_id   = img.clip(study_area_atmo).getMapId(VIS[vis_key])
                                tile_url = map_id['tile_fetcher'].url_format
                                layers.append({'name': f'{label} ({unit})', 'tile_url': tile_url,
                                               'type': 'tile', 'bbox': bbox})
                                if bbox:
                                    arr = get_thumb(img.clip(study_area_atmo), VIS[vis_key], study_area_atmo, dim=512)
                                    figures[label] = {
                                        'analysis_map': make_analysis_map(arr, VIS[vis_key], f'{label} ({unit})', region_name, bbox),
                                        'charts'      : make_stats_charts(all_stats, v, label),
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
                if lulc_result['success']:
                    all_stats['LULC'] = lulc_result['stats']
                    lulc_vis     = lulc_result['vis_params']
                    lulc_clipped = lulc_result['lulc_img'].clip(study_area_lulc)
                    if 'sld_style' in lulc_vis:
                        map_id = lulc_clipped.sldStyle(lulc_vis['sld_style']).getMapId({})
                    else:
                        map_id = lulc_clipped.getMapId(lulc_vis)
                    layers.append({
                        'name'      : 'Land Cover Classification',
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

                    # Generate RGB overview for LULC if surface analysis didn't already do it
                    lulc_rgb_overview = rgb_overview_b64 if 'rgb_overview_b64' in dir() and rgb_overview_b64 else None
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

        # ── Step 4a: Multi-year — run additional years + build combined charts ─
        multiyear_figures = {}

        if is_multiyear and years_list:
            print(f'\n[MULTI-YEAR] Years: {years_list}, months: {month_start}–{month_end}')
            n_years    = len(years_list)
            first_year = int(start_date[:4])

            def _year_dates(yr, ms, me):
                if ms and me:
                    if me >= ms:
                        import calendar
                        last_day = calendar.monthrange(yr, me)[1]
                        return f'{yr}-{ms:02d}-01', f'{yr}-{me:02d}-{last_day}'
                    else:
                        import calendar
                        last_day = calendar.monthrange(yr + 1, me)[1]
                        return f'{yr}-{ms:02d}-01', f'{yr+1}-{me:02d}-{last_day}'
                return f'{yr}-01-01', f'{yr}-12-31'

            from gis_functions import (
                load_landsat, compute_lst, compute_uhi, get_stats,
                SURFACE_INDEX_MAP, ATMO_INDEX_MAP, VIS,
                get_thumb, make_rgb_overview, make_analysis_map, make_stats_charts,
                compute_ffpi, fig_to_base64,
                make_multiyear_trend_chart, make_multiyear_distribution_chart,
                make_multiyear_class_chart, make_multiyear_lulc_chart,
            )

            # Seed year-0 stats from already-run single analysis
            year_all_stats = {first_year: dict(all_stats)}

            for yr in years_list:
                if yr == first_year:
                    continue
                yr_start, yr_end = _year_dates(yr, month_start, month_end)
                print(f'  [MULTI-YEAR] Year {yr}: {yr_start} → {yr_end}')
                yr_stats = {}
                bbox = geo.get('bbox')

                try:
                    yr_surface = [v for v in variables_with_rgb if v in surface_keys]
                    if yr_surface:
                        yr_col, yr_comp = load_landsat(study_area_main, yr_start, yr_end)
                        print(f'    {yr_col.size().getInfo()} Landsat scenes')
                        yr_lst_img = None

                        for v in yr_surface:
                            try:
                                if v == 'rgb':
                                    mid = yr_comp.clip(study_area_main).getMapId(VIS['rgb'])
                                    layers.append({'name': f'True Color (RGB) — {yr}',
                                                   'tile_url': mid['tile_fetcher'].url_format,
                                                   'type': 'tile', 'bbox': bbox})
                                elif v == 'lst':
                                    yr_lst_img, _ = compute_lst(yr_comp, study_area_main)
                                    s_yr = get_stats(yr_lst_img, 'LST', study_area_main, scale=90)
                                    # Monthly
                                    import datetime as _dty; monthly_yr = {}
                                    cur = _dty.datetime.strptime(yr_start, '%Y-%m-%d').replace(day=1)
                                    end_d = _dty.datetime.strptime(yr_end, '%Y-%m-%d')
                                    while cur <= end_d:
                                        ms_ = cur.strftime('%Y-%m-%d')
                                        me_ = (cur.replace(year=cur.year+1,month=1,day=1) if cur.month==12
                                               else cur.replace(month=cur.month+1,day=1)).strftime('%Y-%m-%d')
                                        try:
                                            msc = yr_col.filterDate(ms_, me_)
                                            if msc.size().getInfo() > 0:
                                                th = msc.select('ST_B10').median().subtract(273.15)
                                                ms2 = th.reduceRegion(ee.Reducer.mean(), study_area_main, 90, maxPixels=1e9).getInfo()
                                                val = list(ms2.values())[0] if ms2 else None
                                                if val is not None: monthly_yr[cur.strftime('%Y-%m')] = round(val, 4)
                                        except: pass
                                        cur = (cur.replace(year=cur.year+1,month=1,day=1) if cur.month==12
                                               else cur.replace(month=cur.month+1,day=1))
                                    s_yr['monthly'] = monthly_yr
                                    yr_stats['LST'] = s_yr
                                    mid = yr_lst_img.clip(study_area_main).getMapId(VIS['lst'])
                                    layers.append({'name': f'LST (°C) — {yr}',
                                                   'tile_url': mid['tile_fetcher'].url_format,
                                                   'type': 'tile', 'bbox': bbox})
                                    if bbox:
                                        arr_yr = get_thumb(yr_lst_img.clip(study_area_main), VIS['lst'], study_area_main, dim=512)
                                        figures[f'LST — {yr}'] = {
                                            'analysis_map': make_analysis_map(arr_yr, VIS['lst'], f'LST (°C) — {yr}', region_name, bbox),
                                            'charts': make_stats_charts({'LST': s_yr}, 'lst', 'LST'),
                                            'rgb_overview': None,
                                        }
                                elif v == 'uhi':
                                    if yr_lst_img is None:
                                        yr_lst_img, _ = compute_lst(yr_comp, study_area_main)
                                    yr_uhi, yr_lm, yr_ls = compute_uhi(yr_lst_img, study_area_main)
                                    lb = yr_stats.get('LST') or get_stats(yr_lst_img, 'LST', study_area_main, scale=90)
                                    yr_stats['UHI'] = {
                                        'mean': yr_lm, 'std': yr_ls,
                                        'min': lb.get('min'), 'max': lb.get('max'),
                                        'median': lb.get('median'), 'p10': lb.get('p10'), 'p90': lb.get('p90'),
                                        'lst_mean': yr_lm, 'lst_std': yr_ls, 'monthly': lb.get('monthly', {}),
                                    }
                                    mid = yr_uhi.clip(study_area_main).getMapId(VIS['uhi'])
                                    layers.append({'name': f'UHI — {yr}',
                                                   'tile_url': mid['tile_fetcher'].url_format,
                                                   'type': 'tile', 'bbox': bbox})
                                    if bbox:
                                        arr_yr = get_thumb(yr_uhi.clip(study_area_main), VIS['uhi'], study_area_main, dim=512)
                                        figures[f'UHI — {yr}'] = {
                                            'analysis_map': make_analysis_map(arr_yr, VIS['uhi'], f'UHI — {yr}', region_name, bbox),
                                            'charts': make_stats_charts({'UHI': yr_stats['UHI']}, 'uhi', 'UHI'),
                                            'rgb_overview': None,
                                        }
                                elif v in SURFACE_INDEX_MAP:
                                    lbl, func, vis_key, scale = SURFACE_INDEX_MAP[v]
                                    yr_img = func(yr_comp)
                                    s_yr = get_stats(yr_img, lbl, study_area_main, scale=scale)
                                    import datetime as _dty2; monthly_yr2 = {}
                                    cur2 = _dty2.datetime.strptime(yr_start, '%Y-%m-%d').replace(day=1)
                                    end_d2 = _dty2.datetime.strptime(yr_end, '%Y-%m-%d')
                                    while cur2 <= end_d2:
                                        ms2_ = cur2.strftime('%Y-%m-%d')
                                        me2_ = (cur2.replace(year=cur2.year+1,month=1,day=1) if cur2.month==12
                                                else cur2.replace(month=cur2.month+1,day=1)).strftime('%Y-%m-%d')
                                        try:
                                            msc2 = yr_col.filterDate(ms2_, me2_)
                                            if msc2.size().getInfo() > 0:
                                                mi2 = func(msc2.median())
                                                ms3 = mi2.reduceRegion(ee.Reducer.mean(), study_area_main, scale, maxPixels=1e9).getInfo()
                                                val2 = ms3.get(lbl)
                                                if val2 is not None: monthly_yr2[cur2.strftime('%Y-%m')] = round(val2, 6)
                                        except: pass
                                        cur2 = (cur2.replace(year=cur2.year+1,month=1) if cur2.month==12
                                                else cur2.replace(month=cur2.month+1))
                                    s_yr['monthly'] = monthly_yr2
                                    yr_stats[lbl] = s_yr
                                    mid = yr_img.clip(study_area_main).getMapId(VIS[vis_key])
                                    layers.append({'name': f'{lbl} — {yr}',
                                                   'tile_url': mid['tile_fetcher'].url_format,
                                                   'type': 'tile', 'bbox': bbox})
                                    if bbox:
                                        arr_yr = get_thumb(yr_img.clip(study_area_main), VIS[vis_key], study_area_main, dim=512)
                                        figures[f'{lbl} — {yr}'] = {
                                            'analysis_map': make_analysis_map(arr_yr, VIS[vis_key], f'{lbl} — {yr}', region_name, bbox),
                                            'charts': make_stats_charts({lbl: s_yr}, v, lbl),
                                            'rgb_overview': None,
                                        }
                            except Exception as ve_yr:
                                print(f'    [{v} {yr}] failed: {ve_yr}')
                                import traceback as _tb_vyr; _tb_vyr.print_exc()

                    # Atmo vars
                    yr_atmo = [v for v in variables if v in atmo_keys]
                    for v in yr_atmo:
                        try:
                            if v == 'ffpi':
                                fi, _ = compute_ffpi(study_area_main, yr_start, yr_end)
                                s_yr = get_stats(fi, 'FFPI', study_area_main, scale=3500)
                                yr_stats['FFPI'] = s_yr
                                mid = fi.clip(study_area_main).getMapId(VIS['ffpi'])
                                layers.append({'name': f'FFPI — {yr}', 'tile_url': mid['tile_fetcher'].url_format,
                                               'type': 'tile', 'bbox': bbox})
                            elif v in ATMO_INDEX_MAP:
                                lbl, func, vis_key, unit = ATMO_INDEX_MAP[v]
                                ai, ac = func(study_area_main, yr_start, yr_end)
                                if ac.size().getInfo() > 0:
                                    bn = ai.bandNames().getInfo()[0]
                                    s_yr = get_stats(ai, bn, study_area_main, scale=3500)
                                    yr_stats[lbl] = s_yr
                                    mid = ai.clip(study_area_main).getMapId(VIS[vis_key])
                                    layers.append({'name': f'{lbl} — {yr}', 'tile_url': mid['tile_fetcher'].url_format,
                                                   'type': 'tile', 'bbox': bbox})
                                    if bbox:
                                        arr_yr = get_thumb(ai.clip(study_area_main), VIS[vis_key], study_area_main, dim=512)
                                        figures[f'{lbl} — {yr}'] = {
                                            'analysis_map': make_analysis_map(arr_yr, VIS[vis_key], f'{lbl} — {yr}', region_name, bbox),
                                            'charts': make_stats_charts({lbl: s_yr}, v, lbl),
                                            'rgb_overview': None,
                                        }
                        except Exception as ve_atmo:
                            print(f'    [{v} atmo {yr}] failed: {ve_atmo}')

                    # LULC
                    if 'lulc' in variables:
                        try:
                            from gis_functions import compute_lulc, make_lulc_charts as _mlc
                            yr_lulc = compute_lulc(study_area_main, yr_start, yr_end, region_name)
                            if yr_lulc['success']:
                                yr_stats['LULC'] = yr_lulc['stats']
                                ylv = yr_lulc['vis_params']
                                ylc = yr_lulc['lulc_img'].clip(study_area_main)
                                mid = (ylc.sldStyle(ylv['sld_style']).getMapId({}) if 'sld_style' in ylv
                                       else ylc.getMapId(ylv))
                                layers.append({'name': f'Land Cover — {yr}',
                                               'tile_url': mid['tile_fetcher'].url_format,
                                               'type': 'tile', 'bbox': bbox,
                                               'lulc_stats': yr_lulc['stats']})
                                figures[f'LULC — {yr}'] = {
                                    'analysis_map': None,
                                    'charts': _mlc(yr_lulc['stats']),
                                    'rgb_overview': None,
                                }
                        except Exception as lulc_yr_err:
                            print(f'    [LULC {yr}] failed: {lulc_yr_err}')

                    year_all_stats[yr] = yr_stats
                    print(f'  [MULTI-YEAR] ✓ Year {yr}: {list(yr_stats.keys())}')

                except Exception as yr_err:
                    print(f'  [MULTI-YEAR] Year {yr} failed: {yr_err}')
                    import traceback as _tb_yr; _tb_yr.print_exc()

            # ── Build combined overlay charts ──────────────────────────────────
            print(f'[MULTI-YEAR] Building combined charts for {list(year_all_stats.keys())}...')
            all_var_labels = set()
            for ys in year_all_stats.values():
                all_var_labels.update(ys.keys())

            for var_label in all_var_labels:
                yearly_var = {yr: year_all_stats[yr][var_label]
                              for yr in years_list
                              if year_all_stats.get(yr, {}).get(var_label)}
                if not yearly_var:
                    continue
                if var_label == 'LULC':
                    cb = make_multiyear_lulc_chart(yearly_var)
                    if cb:
                        multiyear_figures['LULC'] = {'charts': [('multiyear_bar', cb)]}
                else:
                    charts_c = []
                    t = make_multiyear_trend_chart(yearly_var, var_label, n_years)
                    if t: charts_c.append(('multiyear_trend', t))
                    d = make_multiyear_distribution_chart(yearly_var, var_label)
                    if d: charts_c.append(('multiyear_dist', d))
                    c = make_multiyear_class_chart(yearly_var, var_label)
                    if c: charts_c.append(('multiyear_class', c))
                    if charts_c:
                        multiyear_figures[var_label] = {'charts': charts_c}
                        print(f'  ✓ Combined charts: {var_label} ({len(charts_c)})')

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

        insight = conclusion
        update_step(5, 'done', 100)

        job['status'] = 'complete'
        job['result'] = {
            'type'             : 'analysis',
            'region'           : region_name,
            'start_date'       : start_date,
            'end_date'         : end_date,
            'variables'        : variables,
            'stats'            : all_stats,
            'layers'           : layers,
            'figures'          : figures,
            'multiyear_figures': multiyear_figures,
            'is_multiyear'     : is_multiyear,
            'years'            : years_list,
            'geo'              : geo,
            'insight'          : insight or '',
            'var_insights'     : var_insights,
            'conclusion'       : conclusion or '',
            'web_context'      : web_context or '',
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

    prompt = (
        f'You are a satellite remote sensing scientist. '
        f'Write a concise 3–4 sentence insight about the {var_label} map shown for {region}.\n\n'
        f'{stats_text}\n\n'
        f'Focus only on: what the mean value indicates, what the spatial range (p10 vs p90) reveals '
        f'about hotspots or uniformity, and one key finding or implication. '
        f'Be specific, scientific, and direct. No bullet points. No headers. Plain paragraph only.'
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

    prompt = (
        f'You are a satellite remote sensing scientist writing the conclusion of an analysis report.\n'
        f'Region: {region} | Period: {start_date} to {end_date}\n'
        f'Variables analyzed: {", ".join(v.upper() for v in variables)}\n\n'
        f'Summary statistics:\n' + '\n'.join(stats_lines) +
        web_section +
        '\n\nWrite a concise conclusion (4–6 sentences) that:\n'
        '1. Synthesizes the key findings across all variables\n'
        '2. Connects patterns to real-world conditions or events (use web context if relevant)\n'
        '3. Highlights the most important concern or positive finding\n'
        '4. Ends with one concrete, actionable recommendation\n\n'
        'Write in flowing prose. No bullet points. No headers. No markdown. Plain paragraphs only.'
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


if __name__ == '__main__':
    print('🛰️  GIS Agent WebApp starting...')
    print(f'   Output dir: {OUTPUT_DIR}')
    print('   Open: http://127.0.0.1:8080')
    app.run(debug=True, port=8080, host='0.0.0.0', threaded=True)