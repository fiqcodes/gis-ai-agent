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
PARENT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(PARENT_DIR))

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


def geocode_region(region_name: str) -> dict:
    """Geocode a region name using Nominatim → return bbox + center."""
    import requests as req
    try:
        url = 'https://nominatim.openstreetmap.org/search'
        params = {'q': region_name, 'format': 'json', 'limit': 1}
        headers = {'User-Agent': 'GISAgentWebApp/1.0'}
        resp = req.get(url, params=params, headers=headers, timeout=10).json()
        if resp:
            bb = resp[0]['boundingbox']
            s, n, w, e = float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])
            return {
                'success': True,
                'bbox': [w, s, e, n],
                'center': [(s + n) / 2, (w + e) / 2],
                'display_name': resp[0].get('display_name', region_name),
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
        multi_year  = parsed.get('multi_year')  # list of ints e.g. [2023,2024,2025] or None

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
            'intent': intent, 'multi_year': multi_year,
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

        from gis_functions import SURFACE_INDEX_MAP, ATMO_INDEX_MAP

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

        # ── Multi-year orchestration ──────────────────────────────────────────
        # Detect years to process: either a list from LLM or a single period
        import datetime as _dt_my
        if multi_year and isinstance(multi_year, list) and len(multi_year) > 1:
            year_list = sorted(int(y) for y in multi_year)
        else:
            # Single year/period — just one iteration
            year_list = None

        # Helper: run one year's full analysis and return (layers, figures, stats)
        def _run_year(yr_start, yr_end, yr_label):
            """Run surface + atmo + lulc for one date window. Returns (layers, figures, stats)."""
            from gis_functions import (
                load_landsat, compute_lst, compute_uhi,
                get_stats, SURFACE_INDEX_MAP, VIS,
                get_thumb, make_rgb_overview, make_analysis_map, make_stats_charts,
            )
            yr_layers  = []
            yr_figures = {}
            yr_stats   = {}
            yr_bbox    = geo.get('bbox')
            lst_img_yr = None

            # ── Surface vars ─────────────────────────────────────────────────
            if surface_vars:
                try:
                    lc_yr, comp_yr = load_landsat(study_area_main, yr_start, yr_end)
                    cnt = lc_yr.size().getInfo()
                    print(f'    [{yr_label}] {cnt} scenes')

                    # RGB overview
                    rgb_b64_yr = None
                    if yr_bbox and comp_yr:
                        try:
                            rgb_b64_yr = make_rgb_overview(
                                comp_yr, study_area_main, region_name, yr_bbox)
                        except: pass

                    lst_img_yr = None
                    for v in surface_vars:
                        try:
                            if v == 'rgb':
                                mid = comp_yr.clip(study_area_main).getMapId(VIS['rgb'])
                                yr_layers.append({
                                    'name'    : f'True Color ({yr_label})',
                                    'tile_url': mid['tile_fetcher'].url_format,
                                    'type'    : 'tile', 'bbox': yr_bbox,
                                })
                            elif v == 'lst':
                                lst_img_yr, _ = compute_lst(comp_yr, study_area_main)
                                s = get_stats(lst_img_yr, 'LST', study_area_main, scale=90)
                                # monthly
                                try:
                                    monthly = {}
                                    s_dt = _dt_my.datetime.strptime(yr_start, '%Y-%m-%d').replace(day=1)
                                    e_dt = _dt_my.datetime.strptime(yr_end,   '%Y-%m-%d')
                                    cur  = s_dt
                                    while cur <= e_dt:
                                        ms_ = cur.strftime('%Y-%m-%d')
                                        me_ = (cur.replace(year=cur.year+1,month=1,day=1)
                                               if cur.month==12 else
                                               cur.replace(month=cur.month+1,day=1)).strftime('%Y-%m-%d')
                                        m_sc = lc_yr.filterDate(ms_, me_)
                                        if m_sc.size().getInfo() > 0:
                                            th = m_sc.select('ST_B10').median().subtract(273.15)
                                            ms_v = th.reduceRegion(ee.Reducer.mean(),
                                                study_area_main, 90, maxPixels=1e9).getInfo()
                                            val = list(ms_v.values())[0] if ms_v else None
                                            if val is not None:
                                                monthly[cur.strftime('%Y-%m')] = round(val, 4)
                                        cur = (cur.replace(year=cur.year+1,month=1,day=1)
                                               if cur.month==12 else
                                               cur.replace(month=cur.month+1,day=1))
                                    s['monthly'] = monthly
                                except: s['monthly'] = {}
                                yr_stats['LST'] = s
                                mid = lst_img_yr.clip(study_area_main).getMapId(VIS['lst'])
                                yr_layers.append({'name': f'LST ({yr_label})',
                                    'tile_url': mid['tile_fetcher'].url_format,
                                    'type': 'tile', 'bbox': yr_bbox})
                                if yr_bbox:
                                    arr = get_thumb(lst_img_yr.clip(study_area_main),
                                                    VIS['lst'], study_area_main, dim=512)
                                    vis_dyn = dict(VIS['lst'])
                                    a_map = make_analysis_map(arr, vis_dyn, f'LST ({yr_label})',
                                                              region_name, yr_bbox)
                                    chts  = make_stats_charts(yr_stats, 'lst', 'LST')
                                    yr_figures[f'LST_{yr_label}'] = {
                                        'analysis_map': a_map, 'charts': chts,
                                        'rgb_overview': rgb_b64_yr, 'year': yr_label,
                                        'img_arr': arr, 'vis_params': vis_dyn,
                                    }

                            elif v == 'uhi':
                                if lst_img_yr is None:
                                    lst_img_yr, _ = compute_lst(comp_yr, study_area_main)
                                uhi_img_yr, lm_yr, ls_yr = compute_uhi(lst_img_yr, study_area_main)
                                uhi_zs = {}
                                try:
                                    uhi_zs = get_stats(uhi_img_yr.clip(study_area_main),
                                                       'UHI', study_area_main, scale=90)
                                except: pass
                                z_min = max(uhi_zs.get('min',-4.0) or -4.0, -5.0)
                                z_max = min(uhi_zs.get('max', 4.0) or  4.0,  5.0)
                                uhi_vis_yr = {**VIS['uhi'], 'min': z_min, 'max': z_max}
                                yr_stats['UHI'] = {
                                    'mean': lm_yr, 'std': ls_yr, 'lst_mean': lm_yr,
                                    'lst_std': ls_yr,
                                    'min': (get_stats(lst_img_yr,'LST',study_area_main,scale=90)
                                             or {}).get('min'),
                                    'max': (get_stats(lst_img_yr,'LST',study_area_main,scale=90)
                                             or {}).get('max'),
                                    **{k: uhi_zs.get(k) for k in ('z_mean','z_std','z_min','z_max')},
                                }
                                mid = uhi_img_yr.clip(study_area_main).getMapId(uhi_vis_yr)
                                yr_layers.append({'name': f'UHI ({yr_label})',
                                    'tile_url': mid['tile_fetcher'].url_format,
                                    'type': 'tile', 'bbox': yr_bbox})
                                if yr_bbox:
                                    arr = get_thumb(uhi_img_yr.clip(study_area_main),
                                                    uhi_vis_yr, study_area_main, dim=512)
                                    a_map = make_analysis_map(arr, uhi_vis_yr,
                                                              f'UHI ({yr_label})',
                                                              region_name, yr_bbox)
                                    chts = make_stats_charts(yr_stats, 'uhi', 'UHI')
                                    yr_figures[f'UHI_{yr_label}'] = {
                                        'analysis_map': a_map, 'charts': chts,
                                        'rgb_overview': rgb_b64_yr, 'year': yr_label,
                                        'img_arr': arr, 'vis_params': uhi_vis_yr,
                                    }

                            elif v in SURFACE_INDEX_MAP:
                                lbl, func, vis_key, scale = SURFACE_INDEX_MAP[v]
                                img_yr = func(comp_yr)
                                s = get_stats(img_yr, lbl, study_area_main, scale=scale)
                                try:
                                    monthly = {}
                                    s_dt = _dt_my.datetime.strptime(yr_start,'%Y-%m-%d').replace(day=1)
                                    e_dt = _dt_my.datetime.strptime(yr_end,'%Y-%m-%d')
                                    cur  = s_dt
                                    while cur <= e_dt:
                                        ms_ = cur.strftime('%Y-%m-%d')
                                        me_ = (cur.replace(year=cur.year+1,month=1,day=1)
                                               if cur.month==12 else
                                               cur.replace(month=cur.month+1,day=1)).strftime('%Y-%m-%d')
                                        m_sc = lc_yr.filterDate(ms_, me_)
                                        if m_sc.size().getInfo() > 0:
                                            mi_ = func(m_sc.median())
                                            ms_v = mi_.reduceRegion(ee.Reducer.mean(),
                                                study_area_main, scale, maxPixels=1e9).getInfo()
                                            val = ms_v.get(lbl)
                                            if val is not None:
                                                monthly[cur.strftime('%Y-%m')] = round(val,6)
                                        cur = (cur.replace(year=cur.year+1,month=1,day=1)
                                               if cur.month==12 else
                                               cur.replace(month=cur.month+1,day=1))
                                    s['monthly'] = monthly
                                except: s['monthly'] = {}
                                yr_stats[lbl] = s

                                # Dynamic vis range per year
                                vis_dyn = dict(VIS[vis_key])
                                if s.get('p10') is not None and s.get('p90') is not None:
                                    margin = (s['p90'] - s['p10']) * 0.1
                                    vis_dyn['min'] = s['p10'] - margin
                                    vis_dyn['max'] = s['p90'] + margin

                                mid = img_yr.clip(study_area_main).getMapId(vis_dyn)
                                yr_layers.append({'name': f'{lbl} ({yr_label})',
                                    'tile_url': mid['tile_fetcher'].url_format,
                                    'type': 'tile', 'bbox': yr_bbox})
                                if yr_bbox:
                                    arr = get_thumb(img_yr.clip(study_area_main),
                                                    vis_dyn, study_area_main, dim=512)
                                    a_map = make_analysis_map(arr, vis_dyn,
                                                              f'{lbl} ({yr_label})',
                                                              region_name, yr_bbox)
                                    chts = make_stats_charts(yr_stats, v, lbl)
                                    yr_figures[f'{lbl}_{yr_label}'] = {
                                        'analysis_map': a_map, 'charts': chts,
                                        'rgb_overview': rgb_b64_yr, 'year': yr_label,
                                        'img_arr': arr, 'vis_params': vis_dyn,
                                    }
                        except Exception as _ve:
                            print(f'    [{yr_label}][{v}] failed: {_ve}')
                except Exception as _se:
                    print(f'  Surface [{yr_label}] failed: {_se}')

            # ── LULC ─────────────────────────────────────────────────────────
            if lulc_vars:
                try:
                    from gis_functions import compute_lulc, make_lulc_charts
                    import numpy as np
                    import matplotlib.pyplot as plt
                    import matplotlib.patches as mpatches
                    lulc_r = compute_lulc(study_area_main, yr_start, yr_end, region_name)
                    if lulc_r['success']:
                        yr_stats['LULC'] = lulc_r['stats']
                        lv = lulc_r['vis_params']
                        lc = lulc_r['lulc_img'].clip(study_area_main)
                        if 'sld_style' in lv:
                            mid = lc.sldStyle(lv['sld_style']).getMapId({})
                        else:
                            mid = lc.getMapId(lv)
                        yr_layers.append({'name': f'Land Cover ({yr_label})',
                            'tile_url': mid['tile_fetcher'].url_format,
                            'type': 'tile', 'bbox': yr_bbox,
                            'lulc_stats': lulc_r['stats']})
                        lulc_map_b64 = None
                        if yr_bbox:
                            try:
                                if 'sld_style' in lv:
                                    arr = get_thumb(lc.sldStyle(lv['sld_style']),
                                                    {}, study_area_main, dim=512)
                                else:
                                    arr = get_thumb(lc, lv, study_area_main, dim=512)
                                classes_d = lulc_r['stats'].get('classes', {})
                                w_b, s_b, e_b, n_b = yr_bbox
                                fig, ax = plt.subplots(figsize=(6,5))
                                ax.imshow(arr, extent=[w_b,e_b,s_b,n_b],
                                          aspect='auto', origin='upper')
                                patches = [mpatches.Patch(color=info['color'],
                                    label=f"{cls} ({info['percentage']:.1f}%)")
                                    for cls, info in classes_d.items()]
                                ax.legend(handles=patches, loc='lower right',
                                          fontsize=7, framealpha=0.85, edgecolor='#ccc',
                                          title='Land Cover', title_fontsize=8)
                                ax.set_title(f'Land Cover ({yr_label}) — {region_name}',
                                             fontsize=10, fontweight='bold', pad=8)
                                ax.axis('off')
                                plt.tight_layout()
                                from gis_functions import fig_to_base64
                                lulc_map_b64 = fig_to_base64(fig)
                            except Exception as _lme:
                                print(f'  LULC map [{yr_label}] failed: {_lme}')
                        lulc_charts = make_lulc_charts(lulc_r['stats'])
                        yr_figures[f'LULC_{yr_label}'] = {
                            'analysis_map': lulc_map_b64,
                            'charts': lulc_charts,
                            'rgb_overview': None,
                            'year': yr_label,
                            'lulc_stats': lulc_r['stats'],
                        }
                except Exception as _le:
                    print(f'  LULC [{yr_label}] failed: {_le}')

            return yr_layers, yr_figures, yr_stats

        # ── Run analysis for each year (or single period) ────────────────────
        all_stats  = {}
        layers     = []
        figures    = {}

        if year_list:
            # Multi-year: one composite per year
            per_year_stats   = {}   # {year_str: {var: stats}}
            per_year_figures = {}   # {year_str: {fig_key: figure}}
            n_years = len(year_list)
            for yi, yr in enumerate(year_list):
                yr_str   = str(yr)
                yr_start = f'{yr}-01-01'
                yr_end   = f'{yr}-12-31'
                pct = 30 + int((yi + 1) / n_years * 60)
                update_step(3, 'running', pct)
                print(f'  ── Year {yr_str} ({yi+1}/{n_years}) ──')
                y_layers, y_figures, y_stats = _run_year(yr_start, yr_end, yr_str)
                layers.extend(y_layers)
                figures.update(y_figures)
                per_year_stats[yr_str]   = y_stats
                per_year_figures[yr_str] = y_figures
                # Merge stats using year prefix for insight generation
                for k, v2 in y_stats.items():
                    all_stats[f'{k}_{yr_str}'] = v2

            # ── Build multi-year grid maps (one grid per variable) ───────────
            try:
                from gis_functions import (
                    make_multiyear_grid_map, make_multiyear_trend_chart,
                    make_multiyear_combined_monthly_chart, make_multiyear_lulc_grid,
                )
                n_years_count = len(year_list)

                # Collect per-var panels across years
                var_panels = {}  # var_key → [(yr_str, img_arr, vis_params)]
                lulc_years = []  # [(yr_str, lulc_stats)]
                for yr_str, y_figs in per_year_figures.items():
                    for fig_key, fig_data in y_figs.items():
                        if not isinstance(fig_data, dict): continue
                        if fig_data.get('img_arr') is None: continue
                        # Strip year suffix to get var key e.g. "NDVI_2023" → "NDVI"
                        var_key = fig_key.rsplit('_', 1)[0]
                        var_panels.setdefault(var_key, []).append((
                            yr_str,
                            fig_data['img_arr'],
                            fig_data['vis_params'],
                        ))
                    # LULC
                    lulc_fig = y_figs.get(f'LULC_{yr_str}')
                    if lulc_fig and lulc_fig.get('lulc_stats'):
                        lulc_years.append((yr_str, lulc_fig['lulc_stats']))

                # Multi-year grid per variable
                for var_key, panels in var_panels.items():
                    panels_sorted = sorted(panels, key=lambda x: x[0])
                    grid_b64 = make_multiyear_grid_map(panels_sorted, var_key, region_name)

                    # Trend chart: yearly mean
                    yr_stats_for_var = {
                        yr_str2: (per_year_stats.get(yr_str2) or {}).get(var_key, {})
                        for yr_str2 in [p[0] for p in panels_sorted]
                    }
                    trend_chart = None
                    if n_years_count <= 3:
                        trend_chart = make_multiyear_combined_monthly_chart(
                            yr_stats_for_var, var_key)
                    if trend_chart is None:
                        trend_chart = make_multiyear_trend_chart(yr_stats_for_var, var_key)

                    # Collect per-year individual charts for distribution + class
                    all_yr_charts = []
                    for yr_str2, y_figs in sorted(per_year_figures.items()):
                        yr_fig = y_figs.get(f'{var_key}_{yr_str2}')
                        if yr_fig and yr_fig.get('charts'):
                            for ct, cb64 in yr_fig['charts']:
                                all_yr_charts.append((f'{ct}_{yr_str2}', cb64))

                    charts_out = []
                    if trend_chart:
                        charts_out.append(trend_chart)
                    charts_out.extend(all_yr_charts)

                    figures[f'{var_key}_multiyear'] = {
                        'analysis_map' : grid_b64,
                        'charts'       : charts_out,
                        'rgb_overview' : None,
                        'is_multiyear' : True,
                        'years'        : [p[0] for p in panels_sorted],
                    }

                # LULC multi-year pie grid
                if lulc_years:
                    lulc_grid_b64 = make_multiyear_lulc_grid(
                        sorted(lulc_years, key=lambda x: x[0]), region_name)
                    lulc_yr_charts = []
                    for yr_str2, y_figs in sorted(per_year_figures.items()):
                        lf = y_figs.get(f'LULC_{yr_str2}')
                        if lf and lf.get('charts'):
                            for ct, cb64 in lf['charts']:
                                lulc_yr_charts.append((f'{ct}_{yr_str2}', cb64))
                    figures['LULC_multiyear'] = {
                        'analysis_map' : lulc_grid_b64,
                        'charts'       : lulc_yr_charts,
                        'rgb_overview' : None,
                        'is_multiyear' : True,
                        'years'        : [y[0] for y in lulc_years],
                    }

            except Exception as _mye:
                import traceback as _tb_my; _tb_my.print_exc()
                print(f'  Multi-year grid/charts failed: {_mye}')

        else:
            # Single period — run analysis as before
            y_layers, y_figures, y_stats = _run_year(start_date, end_date, '')
            layers.extend(y_layers)
            figures.update(y_figures)
            all_stats.update(y_stats)

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
            'multi_year'  : year_list,
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