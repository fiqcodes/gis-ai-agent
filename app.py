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
        matplotlib.use('Agg')  # Must be set before importing pyplot in thread
        import matplotlib.pyplot as plt
        plt.close('all')  # Close any existing figures

        # ── Step 1: import agent modules ──────────────────────────────────────
        update_step(0, 'running', 10)
        import ee
        from config import GEE_PROJECT, OLLAMA_URL, OLLAMA_MODEL, OUTPUT_DIR as OUT
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
        geo = geocode_region(region_name)
        job['geo'] = geo
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

        # Get precise bbox from GEE geometry (more accurate than Nominatim)
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

        surface_vars = [v for v in variables_with_rgb if v in surface_keys]
        atmo_vars    = [v for v in variables if v in atmo_keys]
        lulc_vars    = ['lulc'] if 'lulc' in variables else []

        all_stats  = {}
        layer_imgs = {}   # variable → base64 image

        # Surface analysis — getMapId() for interactive tile layers
        if surface_vars:
            update_step(3, 'running', 30)
            try:
                from gis_functions import (
                    load_landsat, compute_lst, compute_uhi,
                    get_stats, SURFACE_INDEX_MAP, VIS,
                )
                study_area_surf = resolve_region(region_name)
                landsat_col, composite = load_landsat(study_area_surf, start_date, end_date)
                count = landsat_col.size().getInfo()
                print(f'  {count} Landsat scenes loaded')
                lst_img = None

                for v in surface_vars:
                    try:
                        if v == 'rgb':
                            map_id = composite.clip(study_area_surf).getMapId(VIS['rgb'])
                            tile_url = map_id['tile_fetcher'].url_format
                            layers.append({
                                'name'     : 'True Color (RGB)',
                                'tile_url' : tile_url,
                                'type'     : 'tile',
                                'bbox'     : geo.get('bbox'),
                            })
                            print('  ✓ RGB tile layer ready')

                        elif v == 'lst':
                            lst_img, _ = compute_lst(composite, study_area_surf)
                            s = get_stats(lst_img, 'LST', study_area_surf, scale=90)
                            all_stats['LST'] = s
                            map_id = lst_img.clip(study_area_surf).getMapId(VIS['lst'])
                            tile_url = map_id['tile_fetcher'].url_format
                            layers.append({
                                'name'    : 'LST (°C)',
                                'tile_url': tile_url,
                                'type'    : 'tile',
                                'bbox'    : geo.get('bbox'),
                            })
                            print('  ✓ LST tile layer ready')

                        elif v == 'uhi':
                            if lst_img is None:
                                lst_img, _ = compute_lst(composite, study_area_surf)
                            uhi_img, lst_mean, lst_std = compute_uhi(lst_img, study_area_surf)
                            all_stats['UHI'] = {'mean': 0.0, 'lst_mean': lst_mean, 'lst_std': lst_std}
                            map_id = uhi_img.clip(study_area_surf).getMapId(VIS['uhi'])
                            tile_url = map_id['tile_fetcher'].url_format
                            layers.append({
                                'name'    : f'UHI (mean={lst_mean:.1f}°C)',
                                'tile_url': tile_url,
                                'type'    : 'tile',
                                'bbox'    : geo.get('bbox'),
                            })
                            print('  ✓ UHI tile layer ready')

                        elif v in SURFACE_INDEX_MAP:
                            label, func, vis_key, scale = SURFACE_INDEX_MAP[v]
                            img = func(composite)
                            s   = get_stats(img, label, study_area_surf, scale=scale)
                            all_stats[label] = s
                            map_id = img.clip(study_area_surf).getMapId(VIS[vis_key])
                            tile_url = map_id['tile_fetcher'].url_format
                            layers.append({
                                'name'    : label,
                                'tile_url': tile_url,
                                'type'    : 'tile',
                                'bbox'    : geo.get('bbox'),
                            })
                            print(f'  ✓ {label} tile layer ready')

                    except Exception as ve:
                        print(f'  [{v}] failed: {ve}')
                        import traceback as _tb2; _tb2.print_exc()

            except Exception as se:
                print(f'Surface analysis error: {se}')
                import traceback as _tb3; _tb3.print_exc()

        update_step(3, 'running', 55)

        # Atmospheric analysis — tile layers
        if atmo_vars:
            try:
                from gis_functions import ATMO_INDEX_MAP, VIS, get_stats, compute_ffpi
                study_area_atmo = resolve_region(region_name)

                for v in atmo_vars:
                    try:
                        if v == 'ffpi':
                            ffpi_img, _ = compute_ffpi(study_area_atmo, start_date, end_date)
                            s = get_stats(ffpi_img, 'FFPI', study_area_atmo, scale=3500)
                            all_stats['FFPI'] = s
                            map_id = ffpi_img.clip(study_area_atmo).getMapId(VIS['ffpi'])
                            layers.append({
                                'name'    : 'FFPI Score',
                                'tile_url': map_id['tile_fetcher'].url_format,
                                'type'    : 'tile',
                                'bbox'    : geo.get('bbox'),
                            })

                        elif v in ATMO_INDEX_MAP:
                            label, func, vis_key, unit = ATMO_INDEX_MAP[v]
                            img, col = func(study_area_atmo, start_date, end_date)
                            c = col.size().getInfo()
                            if c > 0:
                                band_name = img.bandNames().getInfo()[0]
                                s = get_stats(img, band_name, study_area_atmo, scale=3500)
                                all_stats[label] = s
                                map_id = img.clip(study_area_surf).getMapId(VIS[vis_key])
                                layers.append({
                                    'name'    : f'{label} ({unit})',
                                    'tile_url': map_id['tile_fetcher'].url_format,
                                    'type'    : 'tile',
                                    'bbox'    : geo.get('bbox'),
                                })
                                print(f'  ✓ {label} tile layer ready')
                    except Exception as ve:
                        print(f'  [{v}] atmo failed: {ve}')

            except Exception as ae:
                print(f'Atmo analysis error: {ae}')

        # LULC analysis — tile layer
        if lulc_vars:
            update_step(3, 'running', 70)
            try:
                from gis_functions import compute_lulc
                study_area_lulc = resolve_region(region_name)
                lulc_result = compute_lulc(study_area_lulc, start_date, end_date, region_name)
                if lulc_result['success']:
                    all_stats['LULC'] = lulc_result['stats']
                    map_id = lulc_result['lulc_img'].clip(study_area_lulc).getMapId(lulc_result['vis_params'])
                    layers.append({
                        'name'    : 'Land Cover Classification',
                        'tile_url': map_id['tile_fetcher'].url_format,
                        'type'    : 'tile',
                        'bbox'    : geo.get('bbox'),
                        'lulc_stats': lulc_result['stats'],
                    })
                    print('  ✓ LULC tile layer ready')
            except Exception as le:
                print(f'LULC analysis error: {le}')

        update_step(3, 'done', 100)

        # ── Step 5: Layers already collected via GEE URLs above ─────────────
        update_step(4, 'running', 80)
        print(f'  {len(layers)} layers ready for map display')
        update_step(4, 'done', 100)

        # ── Step 6: Generate AI insight ───────────────────────────────────────
        update_step(5, 'running', 50)
        web_context = fetch_web_context(region_name, start_date, end_date, variables)
        insight     = generate_insight(region_name, start_date, end_date, all_stats, variables)
        update_step(5, 'done', 100)

        job['status'] = 'complete'
        job['result'] = {
            'type'       : 'analysis',
            'region'     : region_name,
            'start_date' : start_date,
            'end_date'   : end_date,
            'variables'  : variables,
            'stats'      : all_stats,
            'layers'     : layers,
            'geo'        : geo,
            'insight'    : insight or '',
            'web_context': web_context or '',
        }

    except Exception as ex:
        import traceback as _tb
        _tb.print_exc()
        job['status'] = 'error'
        job['error']  = str(ex)


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
    try:
        import ee
        from config import GEE_PROJECT
        ee.Initialize(project=GEE_PROJECT)
        status['gee'] = True
    except:
        pass
    return jsonify(status)


if __name__ == '__main__':
    print('🛰️  GIS Agent WebApp starting...')
    print(f'   Output dir: {OUTPUT_DIR}')
    print('   Open: http://127.0.0.1:8080')
    app.run(debug=True, port=8080, host='0.0.0.0', threaded=True)
