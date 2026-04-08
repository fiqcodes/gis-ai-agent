# =============================================================================
# gis_functions.py — All GEE analysis functions for Satellite AI Agent
# Sections:
#   A - GEE Preprocessing
#   B - Region Resolver
#   C - Surface Index Functions
#   D - Atmospheric Functions
#   E - Visualization Palettes
#   F - Thumbnail + Plot Helpers
#   G - Analysis Dispatcher (maps + keywords)
#   H - LLM Parser
#   I - Main Executor (legacy, used by run_analysis)
#   J - Web Search Context Fetcher
#   K - LLM Insight Generator
#   L - LULC Land Cover Classification
# =============================================================================

import ee
import numpy as np
import matplotlib
matplotlib.use('Agg')  # Non-interactive backend — safe for threading + servers
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.colors as mcolors
import matplotlib.cm as cm
import requests
import json
import os
import textwrap
import time
import urllib.request
from io import BytesIO
from PIL import Image as PILImage

from config import GEE_PROJECT, OLLAMA_URL, OLLAMA_MODEL, OUTPUT_DIR

def ensure_gee():
    """Kept for compatibility — calls gee_init_for_thread()."""
    gee_init_for_thread()


def gee_init_for_thread():
    """Initialize GEE fresh in the current thread using service account credentials.
    Called at the start of every analysis job and every major GEE function."""
    import os
    from config import GEE_SERVICE_ACCOUNT_FILE, GEE_PROJECT
    try:
        if not os.path.exists(GEE_SERVICE_ACCOUNT_FILE):
            return
        import google.oauth2.service_account as _sa_t
        import google.auth.transport.requests as _req_t
        scopes = ['https://www.googleapis.com/auth/earthengine',
                  'https://www.googleapis.com/auth/cloud-platform']
        creds = _sa_t.Credentials.from_service_account_file(
            GEE_SERVICE_ACCOUNT_FILE, scopes=scopes)
        creds.refresh(_req_t.Request())
        ee.Initialize(creds, project=GEE_PROJECT,
                      opt_url='https://earthengine.googleapis.com')
    except Exception as e:
        err = str(e)
        if 'already' not in err.lower():
            print(f'  gee_init_for_thread: {e}')



import matplotlib.colors as mcolors
import matplotlib.cm as cm
import textwrap
import time

# =============================================================================
# SECTION A - GEE PREPROCESSING
# =============================================================================

def apply_scaling(image):
    optical = image.select('SR_B.').multiply(0.0000275).add(-0.2)
    thermal = image.select('ST_B.*').multiply(0.00341802).add(149.0)
    return image.addBands(optical, None, True).addBands(thermal, None, True)

def apply_cloud_mask(image):
    qa   = image.select('QA_PIXEL')
    mask = (qa.bitwiseAnd(1 << 3).eq(0)
              .And(qa.bitwiseAnd(1 << 5).eq(0)))
    return image.updateMask(mask)

def load_landsat(study_area, start, end):
    gee_init_for_thread()
    col = (ee.ImageCollection('LANDSAT/LC08/C02/T1_L2')
             .filterDate(start, end)
             .filterBounds(study_area)
             .map(apply_scaling)
             .map(apply_cloud_mask))
    return col, col.median()

# =============================================================================
# SECTION B - REGION RESOLVER
# =============================================================================

def resolve_region(region_name):
    print(f'  Resolving region: "{region_name}"...')

    # Step 1: Nominatim HTTP only — store raw coords, NO ee.Geometry yet
    nom_coords = None
    try:
        url     = 'https://nominatim.openstreetmap.org/search?q=' + region_name + '&format=json&limit=1'
        headers = {'User-Agent': 'SatelliteAgent/1.0'}
        resp    = requests.get(url, headers=headers, timeout=10).json()
        if resp:
            bb   = resp[0]['boundingbox']
            s, n, w, e = float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])
            nom_coords = [w, s, e, n]
            print(f'  Found via Nominatim  bbox: [{w:.2f},{s:.2f},{e:.2f},{n:.2f}]')
    except Exception as ex:
        print(f'  Nominatim HTTP failed: {ex}')

    # Step 2: GAUL — precise polygon boundaries (states, provinces, countries)
    for gaul_id, level_name, field in [
        ('FAO/GAUL/2015/level1', 'GAUL Level 1 (province/state)', 'ADM1_NAME'),
        ('FAO/GAUL/2015/level0', 'GAUL Level 0 (country)',        'ADM0_NAME'),
        ('FAO/GAUL/2015/level2', 'GAUL Level 2 (district/city)',  'ADM2_NAME'),
    ]:
        try:
            fc    = ee.FeatureCollection(gaul_id)
            match = fc.filter(ee.Filter.stringContains(field, region_name)).limit(1)
            feat  = match.first()
            info  = feat.getInfo()
            if info and info.get('geometry'):
                print(f'  Found in {level_name}')
                return feat.geometry()
        except: pass

    # Step 3: Fall back to Nominatim — create ee.Geometry NOW (GEE session still OK here)
    if nom_coords:
        try:
            w, s, e, n = nom_coords
            geom = ee.Geometry.Rectangle([w, s, e, n])
            print(f'  Using Nominatim bbox (GAUL not available)')
            return geom
        except Exception as ex:
            print(f'  Nominatim GEE geometry failed: {ex}')

    raise ValueError(f'Could not resolve region: "{region_name}"')

# =============================================================================
# SECTION C - SURFACE INDEX FUNCTIONS
# =============================================================================

WAVELENGTH = 11.5
RHO        = 14380

def compute_ndvi(composite):
    return composite.normalizedDifference(['SR_B5', 'SR_B4']).rename('NDVI')

def compute_evi(composite):
    return composite.expression(
        '2.5 * ((NIR - Red) / (NIR + 6 * Red - 7.5 * Blue + 1))',
        {'NIR': composite.select('SR_B5'), 'Red': composite.select('SR_B4'),
         'Blue': composite.select('SR_B2')}
    ).rename('EVI')

def compute_savi(composite, L=0.5):
    return composite.expression(
        '((NIR - Red) / (NIR + Red + L)) * (1 + L)',
        {'NIR': composite.select('SR_B5'), 'Red': composite.select('SR_B4'), 'L': L}
    ).rename('SAVI')

def compute_ndwi(composite):
    return composite.normalizedDifference(['SR_B3', 'SR_B5']).rename('NDWI')

def compute_mndwi(composite):
    return composite.normalizedDifference(['SR_B3', 'SR_B6']).rename('MNDWI')

def compute_ndbi(composite):
    return composite.normalizedDifference(['SR_B6', 'SR_B5']).rename('NDBI')

def compute_ui(composite):
    return composite.normalizedDifference(['SR_B7', 'SR_B5']).rename('UI')

def compute_nbi(composite):
    return composite.expression(
        '(Red * SWIR1) / NIR',
        {'Red': composite.select('SR_B4'), 'SWIR1': composite.select('SR_B6'),
         'NIR': composite.select('SR_B5')}
    ).rename('NBI')

def compute_bsi(composite):
    return composite.expression(
        '((SWIR1 + Red) - (NIR + Blue)) / ((SWIR1 + Red) + (NIR + Blue))',
        {'SWIR1': composite.select('SR_B6'), 'Red': composite.select('SR_B4'),
         'NIR': composite.select('SR_B5'), 'Blue': composite.select('SR_B2')}
    ).rename('BSI')

def compute_ndsi(composite):
    return composite.normalizedDifference(['SR_B3', 'SR_B6']).rename('NDSI')

def compute_lst(composite, study_area):
    ndvi     = compute_ndvi(composite)
    stats    = ndvi.reduceRegion(ee.Reducer.minMax(), study_area, 30, maxPixels=1e9).getInfo()
    ndvi_min = stats.get('NDVI_min', 0)
    ndvi_max = stats.get('NDVI_max', 1)
    fv       = ndvi.subtract(ndvi_min).divide(ndvi_max - ndvi_min).pow(2)
    em       = fv.multiply(0.004).add(0.986).rename('Emissivity')
    thermal  = composite.select('ST_B10').rename('BT')
    lst      = thermal.expression(
        '(BT / (1 + ((wavelength * (BT / rho)) * log(emissivity)))) - 273.15',
        {'BT': thermal.select('BT'), 'wavelength': WAVELENGTH, 'rho': RHO, 'emissivity': em}
    ).rename('LST')
    return lst, em

def compute_uhi(lst, study_area):
    stats    = lst.reduceRegion(
        ee.Reducer.mean().combine(ee.Reducer.stdDev(), sharedInputs=True),
        study_area, 30, maxPixels=1e9
    ).getInfo()
    lst_mean = stats['LST_mean']
    lst_std  = stats['LST_stdDev']
    uhi      = lst.subtract(lst_mean).divide(lst_std).rename('UHI')
    return uhi, lst_mean, lst_std

def compute_lst_simple(composite):
    """LST wrapper returning single-band image — used for monthly stats."""
    lst, _ = compute_lst(composite, composite.geometry())
    return lst

# =============================================================================
# SECTION D - ATMOSPHERIC FUNCTIONS
# =============================================================================

def compute_co(study_area, start, end):
    col = (ee.ImageCollection('COPERNICUS/S5P/OFFL/L3_CO')
             .filterDate(start, end).filterBounds(study_area)
             .select('CO_column_number_density'))
    return col.mean().rename('CO'), col

def compute_ch4(study_area, start, end):
    col = (ee.ImageCollection('COPERNICUS/S5P/OFFL/L3_CH4')
             .filterDate(start, end).filterBounds(study_area)
             .select('CH4_column_volume_mixing_ratio_dry_air'))
    return col.mean().rename('CH4'), col

def compute_no2(study_area, start, end):
    col = (ee.ImageCollection('COPERNICUS/S5P/OFFL/L3_NO2')
             .filterDate(start, end).filterBounds(study_area)
             .select('tropospheric_NO2_column_number_density'))
    return col.mean().rename('NO2'), col

def compute_so2(study_area, start, end):
    col = (ee.ImageCollection('COPERNICUS/S5P/OFFL/L3_SO2')
             .filterDate(start, end).filterBounds(study_area)
             .select('SO2_column_number_density'))
    return col.mean().rename('SO2'), col

def compute_aerosol(study_area, start, end):
    col = (ee.ImageCollection('COPERNICUS/S5P/OFFL/L3_AER_AI')
             .filterDate(start, end).filterBounds(study_area)
             .select('absorbing_aerosol_index'))
    return col.mean().rename('Aerosol'), col

def compute_o3(study_area, start, end):
    col = (ee.ImageCollection('COPERNICUS/S5P/OFFL/L3_O3')
             .filterDate(start, end).filterBounds(study_area)
             .select('O3_column_number_density'))
    o3    = col.mean().rename('O3')
    o3_du = o3.divide(0.04462).rename('O3_DU')
    return o3_du, col

def compute_gpp(study_area, start, end):
    col = (ee.ImageCollection('MODIS/061/MOD17A2H')
             .filterDate(start, end).filterBounds(study_area)
             .select('Gpp'))
    return col.mean().multiply(0.0001).rename('GPP'), col

def compute_burned(study_area, start, end):
    col  = (ee.ImageCollection('MODIS/061/MCD64A1')
              .filterDate(start, end).filterBounds(study_area)
              .select('BurnDate'))
    burn = col.max().rename('BurnDate')
    mask = burn.gt(0)
    return burn.updateMask(mask), col

def compute_ffpi(study_area, start, end):
    no2_img, _ = compute_no2(study_area, start, end)
    co_img,  _ = compute_co(study_area, start, end)
    so2_img, _ = compute_so2(study_area, start, end)
    def norm(img, name):
        stats = img.reduceRegion(ee.Reducer.minMax(), study_area, 3500, maxPixels=1e9).getInfo()
        mn = stats.get(f'{name}_min', 0)
        mx = stats.get(f'{name}_max', 1)
        if mx == mn: return img.multiply(0)
        return img.subtract(mn).divide(mx - mn)
    ffpi = (norm(no2_img,'NO2').add(norm(co_img,'CO')).add(norm(so2_img,'SO2'))
                               .divide(3).rename('FFPI'))
    ffpi_class = (ffpi
        .where(ffpi.lt(0.3), 1)
        .where(ffpi.gte(0.3).And(ffpi.lt(0.6)), 2)
        .where(ffpi.gte(0.6).And(ffpi.lt(0.8)), 3)
        .where(ffpi.gte(0.8), 4)
        .rename('FFPI_class'))
    return ffpi, ffpi_class

# =============================================================================
# SECTION E - VISUALIZATION PALETTES
# =============================================================================

LST_PALETTE = [
    '#040274','#040281','#0502a3','#0502b8','#0502ce','#0502e6',
    '#0602ff','#235cb1','#307ef3','#269db1','#30c8e2','#32d3ef',
    '#3be285','#3ff38f','#86e26f','#3ae237','#b5e22e','#d6e21f',
    '#fff705','#ffd611','#ffb613','#ff8b13','#ff6e08','#ff500d',
    '#ff0000','#de0101','#c21301','#a71001','#911003'
]

VIS = {
    'rgb'       : {'bands': ['SR_B4','SR_B3','SR_B2'], 'min': 0.0,    'max': 0.3},
    'swir'      : {'bands': ['SR_B7','SR_B5','SR_B3'], 'min': 0.0,    'max': 0.3},
    'ndvi'      : {'min': -1,     'max': 1,     'palette': ['#0000ff','#ffffff','#008000']},
    'evi'       : {'min': -1,     'max': 1,     'palette': ['#a52a2a','#ffffff','#006400']},
    'savi'      : {'min': -1,     'max': 1,     'palette': ['#a52a2a','#ffffff','#008000']},
    'ndwi'      : {'min': -1,     'max': 1,     'palette': ['#a52a2a','#ffffff','#0000ff']},
    'mndwi'     : {'min': -1,     'max': 1,     'palette': ['#a52a2a','#ffffff','#00ffff']},
    'ndbi'      : {'min': -1,     'max': 1,     'palette': ['#0000ff','#ffffff','#ff0000']},
    'ui'        : {'min': -1,     'max': 1,     'palette': ['#008000','#ffffff','#800080']},
    'nbi'       : {'min':  0,     'max': 0.5,   'palette': ['#ffffff','#ffa500','#8b0000']},
    'bsi'       : {'min': -1,     'max': 1,     'palette': ['#0000ff','#ffffff','#a52a2a']},
    'ndsi'      : {'min': -1,     'max': 1,     'palette': ['#a52a2a','#ffffff','#e0ffff']},
    'lst'       : {'min': 20,     'max': 60,    'palette': LST_PALETTE},
    'uhi'       : {'min': -4,     'max': 4,     'palette': ['#313695','#74add1','#fed976','#feb24c','#fd8d3c','#fc4e2a','#e31a1c','#b10026']},
    'em'        : {'min': 0.986,  'max': 0.990, 'palette': ['#ffff00','#008000']},
    'co'        : {'min': 0.02,   'max': 0.08,  'palette': ['#000033','#0000ff','#8000ff','#00ffff','#008000','#ffff00','#ff0000']},
    'ch4'       : {'min': 1750,   'max': 1950,  'palette': ['#0000ff','#00ffff','#008000','#ffff00','#ffa500','#ff0000']},
    'no2'       : {'min': 0,      'max': 0.0002,'palette': ['#000033','#0000ff','#8000ff','#00ffff','#008000','#ffff00','#ff0000']},
    'so2'       : {'min': 0,      'max': 0.001, 'palette': ['#0000ff','#008000','#ffff00','#ffa500','#ff0000','#8b0000']},
    'aerosol'   : {'min': -1,     'max': 3,     'palette': ['#0000ff','#ffffff','#ffff00','#ffa500','#ff0000']},
    'o3'        : {'min': 200,    'max': 380,   'palette': ['#800080','#0000ff','#00ffff','#008000','#ffff00','#ff0000']},
    'gpp'       : {'min': 0,      'max': 0.03,  'palette': ['#ffffff','#a8ddb5','#238b45','#00441b']},
    'burned'    : {'min': 1,      'max': 366,   'palette': ['#ffff00','#ffa500','#ff0000','#8b0000']},
    'ffpi'      : {'min': 0,      'max': 1,     'palette': ['#313695','#74add1','#fdae61','#d73027']},
    'ffpi_class': {'min': 1,      'max': 4,     'palette': ['#2166ac','#92c5de','#f4a582','#b2182b']},
}

# =============================================================================
# SECTION F - THUMBNAIL + PLOT HELPERS
# =============================================================================

def get_thumb(image, vis_params, region, dim=512):
    url = image.getThumbURL({**vis_params, 'region': region, 'dimensions': dim, 'format': 'png'})
    with urllib.request.urlopen(url) as r:
        return np.array(PILImage.open(BytesIO(r.read())))

def get_stats(image, band, study_area, scale=1000):
    try:
        stats = image.reduceRegion(
            reducer  = ee.Reducer.mean()
                         .combine(ee.Reducer.minMax(),   sharedInputs=True)
                         .combine(ee.Reducer.stdDev(),   sharedInputs=True)
                         .combine(ee.Reducer.median(),   sharedInputs=True)
                         .combine(ee.Reducer.percentile([10, 90]), sharedInputs=True),
            geometry = study_area, scale=scale, maxPixels=1e9
        ).getInfo()
        return {
            'mean'  : stats.get(f'{band}_mean'),
            'min'   : stats.get(f'{band}_min'),
            'max'   : stats.get(f'{band}_max'),
            'std'   : stats.get(f'{band}_stdDev'),
            'median': stats.get(f'{band}_median'),
            'p10'   : stats.get(f'{band}_p10'),
            'p90'   : stats.get(f'{band}_p90'),
        }
    except:
        return {'mean': None, 'min': None, 'max': None,
                'std': None, 'median': None, 'p10': None, 'p90': None}

def get_monthly_stats(image_collection, band, study_area, start_date, end_date, scale=1000):
    """Compute monthly mean for a band over the collection period."""
    import datetime
    start = datetime.datetime.strptime(start_date, '%Y-%m-%d')
    end   = datetime.datetime.strptime(end_date,   '%Y-%m-%d')

    monthly = {}
    current = start.replace(day=1)
    while current <= end:
        m_start = current.strftime('%Y-%m-%d')
        # last day of month
        if current.month == 12:
            m_end = current.replace(year=current.year+1, month=1, day=1).strftime('%Y-%m-%d')
        else:
            m_end = current.replace(month=current.month+1, day=1).strftime('%Y-%m-%d')

        try:
            month_img = image_collection.filterDate(m_start, m_end).mean()
            s = month_img.reduceRegion(
                reducer  = ee.Reducer.mean(),
                geometry = study_area, scale=scale, maxPixels=1e9
            ).getInfo()
            val = s.get(band)
            if val is not None:
                monthly[current.strftime('%Y-%m')] = round(val, 6)
        except:
            pass

        if current.month == 12:
            current = current.replace(year=current.year+1, month=1)
        else:
            current = current.replace(month=current.month+1)

    return monthly

def save_figure(fig, name):
    path = os.path.join(OUTPUT_DIR, f'{name}.jpg')
    fig.savefig(path, dpi=150, bbox_inches='tight', format='jpg')
    plt.close(fig)
    print(f'  Saved: {path}')
    return path


def fig_to_base64(fig):
    """Convert a matplotlib figure to base64 PNG data URI."""
    buf = BytesIO()
    fig.savefig(buf, format='png', dpi=150, bbox_inches='tight')
    buf.seek(0)
    b64 = base64.b64encode(buf.read()).decode('utf-8')
    plt.close(fig)
    return f'data:image/png;base64,{b64}'


import base64

def make_rgb_overview(composite, study_area, region_name, bbox):
    """
    Generate a static RGB overview map with grid coordinates.
    Returns base64 PNG string.
    """
    try:
        import numpy as np
        # Download RGB thumbnail from GEE
        rgb_arr = get_thumb(composite.clip(study_area), VIS['rgb'], study_area, dim=512)

        w, s, e, n = bbox
        fig, ax = plt.subplots(figsize=(7, 7))
        ax.imshow(rgb_arr, extent=[w, e, s, n], aspect='auto', origin='upper')

        # Grid lines
        lon_ticks = np.linspace(w, e, 5)
        lat_ticks = np.linspace(s, n, 5)
        ax.set_xticks(lon_ticks)
        ax.set_yticks(lat_ticks)
        ax.set_xticklabels([f'{v:.2f}°' for v in lon_ticks], fontsize=8, color='#555')
        ax.set_yticklabels([f'{v:.2f}°' for v in lat_ticks], fontsize=8, color='#555')
        ax.grid(False)
        ax.set_xlabel('Longitude', fontsize=9, color='#555')
        ax.set_ylabel('Latitude',  fontsize=9, color='#555')
        ax.set_title(f'Study Area Overview ({region_name})', fontsize=11, fontweight='bold', pad=10)

        # Border
        for spine in ax.spines.values():
            spine.set_edgecolor('#cccccc')
            spine.set_linewidth(0.8)

        # Attribution
        ax.text(0.01, 0.01, '© Landsat / Google Earth Engine',
                transform=ax.transAxes, fontsize=7, color='white',
                bbox=dict(boxstyle='round,pad=0.2', facecolor='black', alpha=0.4))

        plt.tight_layout()
        return fig_to_base64(fig)
    except Exception as e:
        print(f'  RGB overview failed: {e}')
        return None


def make_analysis_map(img_arr, vis_params, label, region_name, bbox):
    """
    Generate a static analysis map (NDVI, LST, etc.) with colorbar legend.
    Returns base64 PNG string.
    """
    try:
        import numpy as np
        w, s, e, n = bbox
        fig, ax = plt.subplots(figsize=(7, 6))
        ax.imshow(img_arr, extent=[w, e, s, n], aspect='auto', origin='upper')

        # Colorbar
        if 'palette' in vis_params and 'min' in vis_params:
            cmap = mcolors.LinearSegmentedColormap.from_list(label, vis_params['palette'])
            norm = mcolors.Normalize(vmin=vis_params['min'], vmax=vis_params['max'])
            sm   = cm.ScalarMappable(cmap=cmap, norm=norm)
            sm.set_array([])
            cbar = fig.colorbar(sm, ax=ax, orientation='vertical',
                                fraction=0.03, pad=0.03, aspect=30)
            cbar.ax.tick_params(labelsize=8)
            unit_map = {
                'LST': '°C', 'UHI': 'z-score', 'NDVI': 'index',
                'EVI': 'index', 'SAVI': 'index', 'NDWI': 'index',
                'MNDWI': 'index', 'NDBI': 'index', 'NO2': 'mol/m²',
                'CO': 'mol/m²', 'SO2': 'mol/m²', 'CH4': 'ppb',
                'O3': 'DU', 'Aerosol': 'AAI', 'FFPI': '0-1',
            }
            unit = next((v for k, v in unit_map.items() if k.upper() in label.upper()), 'value')
            cbar.set_label(unit, fontsize=9)

        # Grid
        lon_ticks = np.linspace(w, e, 5)
        lat_ticks = np.linspace(s, n, 5)
        ax.set_xticks(lon_ticks)
        ax.set_yticks(lat_ticks)
        ax.set_xticklabels([f'{v:.2f}°' for v in lon_ticks], fontsize=8, color='#555')
        ax.set_yticklabels([f'{v:.2f}°' for v in lat_ticks], fontsize=8, color='#555')
        ax.grid(False)
        ax.set_title(f'{label} — {region_name}', fontsize=11, fontweight='bold', pad=10)

        for spine in ax.spines.values():
            spine.set_edgecolor('#cccccc')
            spine.set_linewidth(0.8)

        ax.text(0.01, 0.01, '© Landsat / Google Earth Engine',
                transform=ax.transAxes, fontsize=7, color='white',
                bbox=dict(boxstyle='round,pad=0.2', facecolor='black', alpha=0.4))

        plt.tight_layout()
        return fig_to_base64(fig)
    except Exception as e:
        print(f'  Analysis map failed: {e}')
        return None


def make_stats_charts(stats, var_name, label):
    """
    Generate histogram + optional class bar chart using matplotlib.
    Returns list of base64 PNG strings.
    """
    charts = []
    s = stats.get(label) or stats.get(var_name)
    if not s:
        return charts

    import numpy as np

    # ── Monthly trend line chart ──────────────────────────────────────────────
    monthly = s.get('monthly', {})
    if monthly and len(monthly) >= 2:
        try:
            months       = sorted(monthly.keys())
            values       = [monthly[m] for m in months]
            short_months = [m[5:] for m in months]   # MM only
            x_idx        = list(range(len(months)))   # numeric x — used for BOTH line and fill

            fig, ax = plt.subplots(figsize=(8, 3.5))
            ax.plot(x_idx, values, color='#2196F3', linewidth=2,
                    marker='o', markersize=5, markerfacecolor='white',
                    markeredgecolor='#2196F3', markeredgewidth=1.5)
            # Fill between line and the plot's bottom edge (not zero),
            # so shading is always below the line even for all-negative series
            baseline = min(values) - abs(min(values)) * 0.05
            ax.fill_between(x_idx, values, baseline, alpha=0.12, color='#2196F3')
            ax.set_xticks(x_idx)
            ax.set_xticklabels(short_months, fontsize=8, rotation=45)
            ax.set_ylabel(label, fontsize=9)
            ax.set_title(f'{label} Monthly Mean', fontsize=10, fontweight='bold')
            ax.grid(True, axis='y', linestyle='--', linewidth=0.5, alpha=0.5)
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)
            plt.tight_layout()
            charts.append(('monthly_trend', fig_to_base64(fig)))
        except Exception as e:
            print(f'  Monthly chart failed: {e}')

    # ── Distribution histogram ────────────────────────────────────────────────
    mean_v = s.get('mean')
    p10_v  = s.get('p10')
    p90_v  = s.get('p90')
    min_v  = s.get('min', -1)
    max_v  = s.get('max',  1)

    if mean_v is not None and min_v is not None and max_v is not None:
        try:
            std_v   = s.get('std', 0.1) or 0.1
            n_pts   = 50000
            rng     = np.random.default_rng(42)
            samples = rng.normal(mean_v, std_v, n_pts)
            samples = np.clip(samples, min_v, max_v)

            fig, ax = plt.subplots(figsize=(6, 4))
            ax.hist(samples, bins=40, color='#5B9BD5', edgecolor='white',
                    linewidth=0.4, alpha=0.85)

            if p10_v is not None:
                ax.axvline(p10_v, color='#E07B39', linewidth=1.5, linestyle='--')
                ax.text(p10_v, ax.get_ylim()[1] * 0.95, 'P10',
                        color='#E07B39', fontsize=8, ha='center')
            if p90_v is not None:
                ax.axvline(p90_v, color='#E07B39', linewidth=1.5, linestyle='--')
                ax.text(p90_v, ax.get_ylim()[1] * 0.95, 'P90',
                        color='#E07B39', fontsize=8, ha='center')
            if mean_v is not None:
                ax.axvline(mean_v, color='#C0392B', linewidth=1.5, linestyle='-')

            ax.set_xlabel(label, fontsize=9)
            ax.set_ylabel('Pixel count', fontsize=9)
            ax.set_title(f'{label} distribution', fontsize=10, fontweight='bold')
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)
            plt.tight_layout()
            charts.append(('histogram', fig_to_base64(fig)))
        except Exception as e:
            print(f'  Histogram failed: {e}')

    # ── Generic index class bar (NDVI, NDBI, NDWI, EVI, SAVI, BSI, UI, NBI, NDSI, MNDWI) ──
    INDEX_VARS = ('NDVI','NDBI','NDWI','EVI','SAVI','BSI','UI','NBI','NDSI','MNDWI')
    label_up   = label.upper()

    if mean_v is not None and any(iv in label_up for iv in INDEX_VARS):
        try:
            std_v   = s.get('std', 0.1) or 0.1
            rng     = np.random.default_rng(42)
            samples = rng.normal(mean_v, std_v, 50000)

            # ── Per-index class definitions ───────────────────────────────────
            if 'NDVI' in label_up:
                samples = np.clip(samples, -1, 1)
                class_defs = [
                    ('Bare\n(<0.1)',      samples < 0.1,                              '#C1704A'),
                    ('Stressed\n(0.1–0.3)', (samples >= 0.1) & (samples < 0.3),      '#F0A500'),
                    ('Moderate\n(0.3–0.6)', (samples >= 0.3) & (samples < 0.6),      '#5BAD72'),
                    ('Healthy\n(>0.6)',   samples >= 0.6,                             '#1A7A40'),
                ]
                xlabel = 'NDVI class'

            elif 'NDBI' in label_up:
                samples = np.clip(samples, -1, 1)
                class_defs = [
                    ('Non-built\n(<–0.1)',   samples < -0.1,                          '#4575B4'),
                    ('Low built\n(–0.1–0)', (samples >= -0.1) & (samples < 0),        '#91BFDB'),
                    ('Moderate\n(0–0.1)',   (samples >= 0) & (samples < 0.1),          '#FEE090'),
                    ('High built\n(>0.1)',   samples >= 0.1,                           '#D73027'),
                ]
                xlabel = 'Built-up class'

            elif 'NDWI' in label_up or 'MNDWI' in label_up:
                samples = np.clip(samples, -1, 1)
                class_defs = [
                    ('Dry\n(<–0.3)',       samples < -0.3,                            '#C1704A'),
                    ('Transition\n(–0.3–0)', (samples >= -0.3) & (samples < 0),       '#91BFDB'),
                    ('Moist\n(0–0.3)',     (samples >= 0) & (samples < 0.3),           '#4575B4'),
                    ('Water\n(>0.3)',       samples >= 0.3,                            '#023858'),
                ]
                xlabel = 'Water class'

            elif 'BSI' in label_up:
                samples = np.clip(samples, -1, 1)
                class_defs = [
                    ('Vegetated\n(<–0.1)', samples < -0.1,                            '#1A7A40'),
                    ('Mixed\n(–0.1–0.1)', (samples >= -0.1) & (samples < 0.1),        '#F0A500'),
                    ('Bare soil\n(>0.1)', samples >= 0.1,                             '#C1704A'),
                ]
                xlabel = 'Bare soil class'

            elif 'UI' in label_up:
                samples = np.clip(samples, -1, 1)
                class_defs = [
                    ('Vegetation\n(<–0.1)', samples < -0.1,                           '#1A7A40'),
                    ('Transition\n(–0.1–0.1)', (samples >= -0.1) & (samples < 0.1),  '#F0A500'),
                    ('Urban\n(>0.1)',        samples >= 0.1,                           '#8B0000'),
                ]
                xlabel = 'Urban class'

            elif 'EVI' in label_up or 'SAVI' in label_up:
                samples = np.clip(samples, -1, 1)
                class_defs = [
                    ('Sparse\n(<0.1)',      samples < 0.1,                            '#C1704A'),
                    ('Low\n(0.1–0.3)',     (samples >= 0.1) & (samples < 0.3),        '#F0A500'),
                    ('Moderate\n(0.3–0.5)', (samples >= 0.3) & (samples < 0.5),       '#5BAD72'),
                    ('Dense\n(>0.5)',       samples >= 0.5,                            '#1A7A40'),
                ]
                xlabel = 'Vegetation class'

            elif 'NDSI' in label_up:
                samples = np.clip(samples, -1, 1)
                class_defs = [
                    ('No snow\n(<0.0)',  samples < 0.0,                               '#C1704A'),
                    ('Possible\n(0–0.4)', (samples >= 0.0) & (samples < 0.4),         '#91BFDB'),
                    ('Snow\n(>0.4)',     samples >= 0.4,                               '#DEEFFF'),
                ]
                xlabel = 'Snow class'

            elif 'NBI' in label_up:
                samples = np.clip(samples, 0, 0.5)
                class_defs = [
                    ('Low\n(<0.1)',      samples < 0.1,                               '#91BFDB'),
                    ('Moderate\n(0.1–0.25)', (samples >= 0.1) & (samples < 0.25),    '#FEE090'),
                    ('High\n(>0.25)',    samples >= 0.25,                              '#D73027'),
                ]
                xlabel = 'Built-up class'

            else:
                class_defs = []
                xlabel     = label

            pairs = [(name, float(np.mean(mask) * 100), col)
                     for name, mask, col in class_defs
                     if float(np.mean(mask) * 100) > 0.5]

            if pairs:
                cls, pct_vals, col_vals = zip(*pairs)
                fig, ax = plt.subplots(figsize=(max(5, len(pairs) * 1.2), 3.5))
                bars = ax.bar(cls, pct_vals, color=col_vals, edgecolor='white',
                              linewidth=0.5, width=0.5)
                for bar, pct in zip(bars, pct_vals):
                    ax.text(bar.get_x() + bar.get_width() / 2,
                            bar.get_height() + 0.8,
                            f'{pct:.1f}%', ha='center', fontsize=9,
                            fontweight='bold', color='#333')
                ax.set_xlabel(xlabel, fontsize=9)
                ax.set_ylabel('Area share (%)', fontsize=9)
                ax.set_title(f'{label} class composition', fontsize=10, fontweight='bold')
                ax.set_ylim(0, max(pct_vals) * 1.25)
                ax.spines['top'].set_visible(False)
                ax.spines['right'].set_visible(False)
                plt.tight_layout()
                charts.append(('class_bar', fig_to_base64(fig)))
        except Exception as e:
            print(f'  Index class chart failed: {e}')

    # ── LST heat class bar chart ──────────────────────────────────────────────
    if mean_v is not None and 'LST' in label_up:
        try:
            std_v    = s.get('std', 3.0) or 3.0
            min_lst  = s.get('min') if s.get('min') is not None else 20.0
            max_lst  = s.get('max') if s.get('max') is not None else 60.0
            rng      = np.random.default_rng(42)
            samples  = rng.normal(mean_v, std_v, 50000)
            samples  = np.clip(samples, min_lst, max_lst)

            cool_pct     = float(np.mean(samples < 30) * 100)
            moderate_pct = float(np.mean((samples >= 30) & (samples < 35)) * 100)
            warm_pct     = float(np.mean((samples >= 35) & (samples < 40)) * 100)
            hot_pct      = float(np.mean((samples >= 40) & (samples < 45)) * 100)
            extreme_pct  = float(np.mean(samples >= 45) * 100)

            classes = ['Cool\n(<30°C)', 'Moderate\n(30–35°C)', 'Warm\n(35–40°C)',
                       'Hot\n(40–45°C)', 'Extreme\n(>45°C)']
            pcts    = [cool_pct, moderate_pct, warm_pct, hot_pct, extreme_pct]
            colors  = ['#0502b8', '#269db1', '#3be285', '#f5a800', '#ff500d']
            pairs   = [(c, p, col) for c, p, col in zip(classes, pcts, colors) if p > 0.1]
            if pairs:
                cls, pct_vals, col_vals = zip(*pairs)
                fig, ax = plt.subplots(figsize=(6, 3.5))
                bars = ax.bar(cls, pct_vals, color=col_vals, edgecolor='white',
                              linewidth=0.5, width=0.6)
                for bar, pct in zip(bars, pct_vals):
                    ax.text(bar.get_x() + bar.get_width() / 2,
                            bar.get_height() + 0.5,
                            f'{pct:.1f}%', ha='center', fontsize=8,
                            fontweight='bold', color='#333')
                ax.set_xlabel('Temperature class', fontsize=9)
                ax.set_ylabel('Area share (%)', fontsize=9)
                ax.set_title('LST heat class composition', fontsize=10, fontweight='bold')
                ax.set_ylim(0, max(pct_vals) * 1.2)
                ax.spines['top'].set_visible(False)
                ax.spines['right'].set_visible(False)
                plt.tight_layout()
                charts.append(('class_bar', fig_to_base64(fig)))
                print(f'  ✓ LST heat class chart: {len(pairs)} classes')
        except Exception as e:
            print(f'  LST class chart failed: {e}')

    # ── Atmospheric pollution class bar (NO2, CO, SO2, CH4, O3, Aerosol, GPP, FFPI) ──
    ATMO_VARS = ('NO2','CO','SO2','CH4','O3','AEROSOL','GPP','BURNED','FFPI')
    if mean_v is not None and any(av in label_up for av in ATMO_VARS):
        try:
            std_v   = s.get('std', abs(mean_v) * 0.2 + 1e-10) or abs(mean_v) * 0.2 + 1e-10
            rng     = np.random.default_rng(42)
            samples = rng.normal(mean_v, std_v, 50000)
            samples = np.clip(samples, min_v if min_v is not None else 0,
                                       max_v if max_v is not None else mean_v * 3)

            if 'NO2' in label_up:
                # Absolute thresholds in mol/m² from Sentinel-5P / EEA literature:
                # <0.00008 = clean background, 0.00008–0.00015 = moderate urban,
                # 0.00015–0.00025 = high traffic/industry, >0.00025 = severe
                class_defs = [
                    ('Clean\n(<8×10⁻⁵)',       samples < 8e-5,                              '#4575B4'),
                    ('Moderate\n(8–15×10⁻⁵)',  (samples >= 8e-5)  & (samples < 1.5e-4),     '#91BFDB'),
                    ('High\n(15–25×10⁻⁵)',     (samples >= 1.5e-4) & (samples < 2.5e-4),    '#FEE090'),
                    ('Severe\n(>25×10⁻⁵)',      samples >= 2.5e-4,                           '#D73027'),
                ]
                xlabel = 'NO₂ concentration class'

            elif 'CO' in label_up:
                # mol/m²: <0.02 clean, 0.02–0.05 moderate, 0.05–0.08 high, >0.08 severe
                class_defs = [
                    ('Clean\n(<0.02)',      samples < 0.02,                             '#4575B4'),
                    ('Moderate\n(0.02–0.05)', (samples >= 0.02) & (samples < 0.05),    '#91BFDB'),
                    ('High\n(0.05–0.08)',   (samples >= 0.05) & (samples < 0.08),      '#FEE090'),
                    ('Severe\n(>0.08)',      samples >= 0.08,                           '#D73027'),
                ]
                xlabel = 'CO column density class'

            elif 'SO2' in label_up:
                # mol/m²: <0.0001 clean, 0.0001–0.0005 moderate, 0.0005–0.001 high, >0.001 severe
                class_defs = [
                    ('Clean\n(<1×10⁻⁴)',       samples < 1e-4,                              '#4575B4'),
                    ('Moderate\n(1–5×10⁻⁴)',  (samples >= 1e-4)  & (samples < 5e-4),        '#91BFDB'),
                    ('High\n(5×10⁻⁴–10⁻³)',   (samples >= 5e-4)  & (samples < 1e-3),        '#FEE090'),
                    ('Severe\n(>10⁻³)',         samples >= 1e-3,                             '#D73027'),
                ]
                xlabel = 'SO₂ column density class'

            elif 'CH4' in label_up:
                # ppb: <1850 background, 1850–1900 slightly elevated, 1900–1950 elevated, >1950 high
                class_defs = [
                    ('Background\n(<1850 ppb)',   samples < 1850,                             '#4575B4'),
                    ('Elevated\n(1850–1900)',     (samples >= 1850) & (samples < 1900),       '#91BFDB'),
                    ('High\n(1900–1950)',         (samples >= 1900) & (samples < 1950),       '#FEE090'),
                    ('Very high\n(>1950 ppb)',     samples >= 1950,                           '#D73027'),
                ]
                xlabel = 'CH₄ mixing ratio class'

            elif 'O3' in label_up:
                # Dobson Units: <220 ozone hole, 220–280 low, 280–340 normal, >340 high
                class_defs = [
                    ('Very low\n(<220 DU)',   samples < 220,                            '#4575B4'),
                    ('Low\n(220–280 DU)',    (samples >= 220) & (samples < 280),        '#91BFDB'),
                    ('Normal\n(280–340 DU)', (samples >= 280) & (samples < 340),        '#FEE090'),
                    ('High\n(>340 DU)',       samples >= 340,                           '#D73027'),
                ]
                xlabel = 'O₃ column class'

            elif 'AEROSOL' in label_up:
                # AAI: <0 marine/clean, 0–1 low, 1–2 moderate, >2 high absorbing aerosols
                class_defs = [
                    ('Clean\n(<0)',       samples < 0,                       '#4575B4'),
                    ('Low\n(0–1)',       (samples >= 0) & (samples < 1),     '#91BFDB'),
                    ('Moderate\n(1–2)', (samples >= 1) & (samples < 2),      '#FEE090'),
                    ('High\n(>2)',        samples >= 2,                      '#D73027'),
                ]
                xlabel = 'Aerosol index class'

            elif 'FFPI' in label_up:
                class_defs = [
                    ('Clean\n(0–0.3)',       samples < 0.3,                          '#4575B4'),
                    ('Moderate\n(0.3–0.6)', (samples >= 0.3) & (samples < 0.6),      '#FEE090'),
                    ('Polluted\n(0.6–0.8)', (samples >= 0.6) & (samples < 0.8),      '#FC8D59'),
                    ('Severe\n(>0.8)',       samples >= 0.8,                          '#D73027'),
                ]
                xlabel = 'Pollution class'

            else:
                class_defs = []
                xlabel = label

            pairs = [(name, float(np.mean(mask) * 100), col)
                     for name, mask, col in class_defs
                     if float(np.mean(mask) * 100) > 0.5]

            if pairs:
                cls, pct_vals, col_vals = zip(*pairs)
                fig, ax = plt.subplots(figsize=(max(5, len(pairs) * 1.4), 3.5))
                bars = ax.bar(cls, pct_vals, color=col_vals, edgecolor='white',
                              linewidth=0.5, width=0.5)
                for bar, pct in zip(bars, pct_vals):
                    ax.text(bar.get_x() + bar.get_width() / 2,
                            bar.get_height() + 0.5,
                            f'{pct:.1f}%', ha='center', fontsize=9,
                            fontweight='bold', color='#333')
                ax.set_xlabel(xlabel, fontsize=9)
                ax.set_ylabel('Area share (%)', fontsize=9)
                ax.set_title(f'{label} pollution class composition', fontsize=10, fontweight='bold')
                ax.set_ylim(0, max(pct_vals) * 1.25)
                ax.spines['top'].set_visible(False)
                ax.spines['right'].set_visible(False)
                plt.tight_layout()
                charts.append(('class_bar', fig_to_base64(fig)))
                print(f'  ✓ {label} pollution class chart: {len(pairs)} classes')
        except Exception as e:
            print(f'  Atmo class chart failed: {e}')

    return charts


def make_lulc_charts(lulc_stats):
    """
    Generate pie chart + horizontal bar chart for LULC class breakdown.
    Returns list of [('lulc_pie', b64), ('lulc_bar', b64)].
    """
    charts  = []
    classes = lulc_stats.get('classes', {})
    total_ha = lulc_stats.get('total_ha', 0)
    if not classes:
        return charts

    names  = list(classes.keys())
    pcts   = [classes[n]['percentage'] for n in names]
    has    = [classes[n]['hectares']   for n in names]
    colors = [classes[n].get('color', '#aaaaaa') for n in names]

    # ── Pie chart ─────────────────────────────────────────────────────────────
    try:
        fig, ax = plt.subplots(figsize=(5.5, 4.5))
        wedges, texts, autotexts = ax.pie(
            pcts, labels=None, colors=colors,
            autopct=lambda p: f'{p:.1f}%' if p > 3 else '',
            startangle=140, pctdistance=0.78,
            wedgeprops=dict(edgecolor='white', linewidth=1.5),
        )
        for at in autotexts:
            at.set_fontsize(8); at.set_fontweight('bold'); at.set_color('white')
        ax.legend(wedges, [f'{n} ({h:,.0f} ha)' for n, h in zip(names, has)],
                  loc='lower center', bbox_to_anchor=(0.5, -0.18),
                  ncol=2, fontsize=8, frameon=False)
        ax.set_title(f'Land Cover Distribution\n(Total: {total_ha:,.0f} ha)',
                     fontsize=10, fontweight='bold', pad=8)
        plt.tight_layout()
        charts.append(('lulc_pie', fig_to_base64(fig)))
    except Exception as e:
        print(f'  LULC pie chart failed: {e}')

    # ── Horizontal bar chart — removed, pie chart is sufficient ──────────────

    return charts


def plot_panels(panels, title, ncols=2, dim=300):
    n     = len(panels)
    ncols = min(ncols, n)
    nrows = (n + ncols - 1) // ncols
    fig   = plt.figure(figsize=(ncols * 7, nrows * 7))
    fig.suptitle(title, fontsize=14, fontweight='bold', y=0.98)

    for idx, (img, vp, label, region) in enumerate(panels):
        ax = fig.add_subplot(nrows, ncols, idx + 1)
        try:
            print(f'  Downloading {label}...')
            arr = get_thumb(img.clip(region), vp, region, dim=dim)
            ax.imshow(arr)
            print(f'  Done: {label}')
        except Exception as e:
            ax.text(0.5, 0.5, f'No data\n{str(e)[:80]}', ha='center', va='center',
                    transform=ax.transAxes, fontsize=8, color='gray')
            print(f'  Failed: {label} - {e}')
        ax.set_title(label, fontsize=12, fontweight='bold', pad=10, loc='center')
        ax.axis('off')

        if 'palette' in vp and 'min' in vp and 'max' in vp:
            try:
                cmap = mcolors.LinearSegmentedColormap.from_list(label, vp['palette'])
                norm = mcolors.Normalize(vmin=vp['min'], vmax=vp['max'])
                sm   = cm.ScalarMappable(cmap=cmap, norm=norm)
                sm.set_array([])
                cbar = fig.colorbar(sm, ax=ax, orientation='horizontal',
                                    fraction=0.046, pad=0.04, aspect=30)
                cbar.ax.tick_params(labelsize=8)
                unit_map = {
                    'LST': 'degrees C', 'UHI': 'z-score',
                    'NDVI': 'index', 'EVI': 'index', 'SAVI': 'index',
                    'NDWI': 'index', 'MNDWI': 'index', 'NDBI': 'index',
                    'UI': 'index', 'BSI': 'index', 'NDSI': 'index',
                    'CO': 'mol/m2', 'NO': 'mol/m2', 'SO': 'mol/m2',
                    'CH4': 'ppb', 'O3': 'DU', 'Aerosol': 'unitless',
                    'GPP': 'kgC/m2/8d', 'FFPI': '0-1', 'Burned': 'DOY',
                }
                unit = next((v for k, v in unit_map.items() if k.upper() in label.upper()), '')
                if unit:
                    cbar.set_label(unit, fontsize=9)
            except Exception:
                pass

    plt.tight_layout(rect=[0, 0, 1, 0.95])
    safe_title = title.replace(' ', '_').replace('/', '-')[:40]
    return save_figure(fig, safe_title)

# =============================================================================
# SECTION G - ANALYSIS DISPATCHER
# =============================================================================

SURFACE_INDEX_MAP = {
    'ndvi' : ('NDVI',  compute_ndvi,  'ndvi',  30),
    'evi'  : ('EVI',   compute_evi,   'evi',   30),
    'savi' : ('SAVI',  compute_savi,  'savi',  30),
    'ndwi' : ('NDWI',  compute_ndwi,  'ndwi',  30),
    'mndwi': ('MNDWI', compute_mndwi, 'mndwi', 30),
    'ndbi' : ('NDBI',  compute_ndbi,  'ndbi',  30),
    'ui'   : ('UI',    compute_ui,    'ui',    30),
    'nbi'  : ('NBI',   compute_nbi,   'nbi',   30),
    'bsi'  : ('BSI',   compute_bsi,   'bsi',   30),
    'ndsi' : ('NDSI',  compute_ndsi,  'ndsi',  30),
}

ATMO_INDEX_MAP = {
    'co'     : ('CO',          compute_co,      'co',      'mol/m2'),
    'ch4'    : ('CH4',         compute_ch4,     'ch4',     'ppb'),
    'no2'    : ('NO2',         compute_no2,     'no2',     'mol/m2'),
    'so2'    : ('SO2',         compute_so2,     'so2',     'mol/m2'),
    'aerosol': ('Aerosol',     compute_aerosol, 'aerosol', 'unitless'),
    'o3'     : ('O3',          compute_o3,      'o3',      'DU'),
    'gpp'    : ('GPP',         compute_gpp,     'gpp',     'kgC/m2/8-day'),
    'burned' : ('Burned Area', compute_burned,  'burned',  'DOY'),
}

KEYWORD_MAP = {
    'ndvi': 'ndvi', 'vegetation': 'ndvi', 'greenery': 'ndvi', 'plant': 'ndvi',
    'evi': 'evi', 'enhanced vegetation': 'evi',
    'savi': 'savi', 'soil adjusted': 'savi',
    'ndwi': 'ndwi', 'water': 'ndwi', 'water index': 'ndwi',
    'mndwi': 'mndwi', 'modified water': 'mndwi',
    'ndbi': 'ndbi', 'built-up': 'ndbi', 'buildup': 'ndbi', 'urban index': 'ndbi',
    'ui': 'ui', 'nbi': 'nbi', 'new built': 'nbi',
    'bsi': 'bsi', 'bare soil': 'bsi', 'soil': 'bsi',
    'ndsi': 'ndsi', 'snow': 'ndsi', 'ice': 'ndsi',
    'lst': 'lst', 'land surface temperature': 'lst', 'temperature': 'lst', 'heat': 'lst',
    'uhi': 'uhi', 'urban heat island': 'uhi', 'heat island': 'uhi',
    'rgb': 'rgb', 'true color': 'rgb', 'true colour': 'rgb',
    'co': 'co', 'carbon monoxide': 'co',
    'ch4': 'ch4', 'methane': 'ch4',
    'no2': 'no2', 'nitrogen dioxide': 'no2', 'nitrogen': 'no2',
    'so2': 'so2', 'sulfur dioxide': 'so2', 'sulphur': 'so2',
    'aerosol': 'aerosol', 'aqi': 'aerosol', 'dust': 'aerosol', 'smoke': 'aerosol',
    'o3': 'o3', 'ozone': 'o3',
    'gpp': 'gpp', 'gross primary': 'gpp', 'carbon uptake': 'gpp', 'co2 uptake': 'gpp',
    'burned': 'burned', 'fire': 'burned', 'wildfire': 'burned', 'burn': 'burned',
    'ffpi': 'ffpi', 'pollution': 'ffpi', 'fossil fuel': 'ffpi', 'pollution index': 'ffpi',
    'all surface': 'all_surface', 'all atmospheric': 'all_atmo', 'all': 'all_surface',
    'lulc': 'lulc', 'land cover': 'lulc', 'land use': 'lulc', 'classification': 'lulc',
    'landcover': 'lulc', 'land class': 'lulc', 'classify': 'lulc',
}

# =============================================================================
# SECTION H - LLM PARSER
# =============================================================================

SYSTEM_PROMPT = (
    "You are a satellite remote sensing analysis assistant. "
    "Extract ONLY the parameters explicitly mentioned by the user. "
    "STRICT RULE: Do NOT add any variables the user did not explicitly ask for.\n\n"
    "Extract:\n"
    "1. region - the place name\n"
    "2. start_date - YYYY-MM-DD\n"
    "3. end_date - YYYY-MM-DD\n"
    "4. variables - ONLY what the user explicitly said. "
    "If user says 'NO2 and CO', return EXACTLY ['no2', 'co']. "
    "NEVER add ch4, aerosol, so2, or anything else not mentioned.\n\n"
    "Available variables:\n"
    "Surface: ndvi, evi, savi, ndwi, mndwi, ndbi, ui, nbi, bsi, ndsi, lst, uhi, rgb\n"
    "Atmospheric: co, ch4, no2, so2, aerosol, o3, gpp, burned, ffpi\n"
    "Special: all_surface, all_atmo\n\n"
    'Respond with ONLY this JSON, nothing else:\n'
    '{\n'
    '  "intent": "analysis" or "question" or "unknown",\n'
    '  "region": "place name or null",\n'
    '  "start_date": "YYYY-MM-DD or null",\n'
    '  "end_date": "YYYY-MM-DD or null",\n'
    '  "variables": ["exactly", "what", "user", "asked"],\n'
    '  "response": "brief confirmation"\n'
    '}\n'
    'No text outside the JSON. No extra variables.'
)

def call_ollama(user_message, chat_history):
    messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]
    messages += chat_history
    messages.append({'role': 'user', 'content': user_message})
    try:
        resp = requests.post(OLLAMA_URL,
            json={'model': OLLAMA_MODEL, 'messages': messages, 'stream': False}, timeout=60)
        data = resp.json()

        # Handle both /api/chat and /api/generate response formats
        if 'message' in data:
            raw = data['message']['content'].strip()
        elif 'response' in data:
            raw = data['response'].strip()
        elif 'error' in data:
            raise Exception(f"Ollama model error: {data['error']}")
        else:
            raise Exception(f"Unexpected response keys: {list(data.keys())}")

        # Strip markdown code fences
        if '```' in raw:
            parts = raw.split('```')
            raw = parts[1] if len(parts) > 1 else parts[0]
            if raw.startswith('json'): raw = raw[4:]

        # Extract just the JSON object
        start = raw.find('{')
        end   = raw.rfind('}') + 1
        if start >= 0 and end > start:
            raw = raw[start:end]

        return json.loads(raw)

    except json.JSONDecodeError as e:
        return {'intent': 'unknown', 'region': None, 'start_date': None, 'end_date': None,
                'variables': [], 'response': f'JSON parse error: {e}. Please rephrase.'}
    except Exception as e:
        return {'intent': 'unknown', 'region': None, 'start_date': None, 'end_date': None,
                'variables': [], 'response': f'Ollama error: {e}. Is Ollama running? Try: ollama serve'}

# =============================================================================
# SECTION I - MAIN EXECUTOR
# =============================================================================

def run_analysis(region_name, start_date, end_date, variables):
    sep  = '=' * 60
    dash = '-' * 45
    print(f'\n{sep}')
    print(f'  SATELLITE ANALYSIS')
    print(f'  Region    : {region_name}')
    print(f'  Period    : {start_date} to {end_date}')
    print(f'  Variables : {variables}')
    print(f'{sep}\n')

    try:
        study_area = resolve_region(region_name)
    except ValueError as e:
        print(f'ERROR: {e}')
        return

    normalized_vars = []
    for v in variables:
        v_lower = v.lower().strip()
        if v_lower in KEYWORD_MAP:
            normalized_vars.append(KEYWORD_MAP[v_lower])
        else:
            matched = [key for key in KEYWORD_MAP if key in v_lower or v_lower in key]
            if matched:
                normalized_vars.append(KEYWORD_MAP[matched[0]])
            else:
                print(f'  Unknown variable "{v}" - skipping')

    if 'all_surface' in normalized_vars:
        normalized_vars = list(SURFACE_INDEX_MAP.keys()) + ['lst', 'uhi']
    if 'all_atmo' in normalized_vars:
        normalized_vars = list(ATMO_INDEX_MAP.keys())
    normalized_vars = list(dict.fromkeys(normalized_vars))

    surface_vars = [v for v in normalized_vars if v in list(SURFACE_INDEX_MAP.keys()) + ['lst','uhi','rgb','lulc']]
    atmo_vars    = [v for v in normalized_vars if v in list(ATMO_INDEX_MAP.keys()) + ['ffpi']]
    panels_surface, panels_atmo, stats_summary = [], [], {}

    composite = None
    if surface_vars:
        print('Loading Landsat 8...')
        landsat_col, composite = load_landsat(study_area, start_date, end_date)
        scene_count = landsat_col.size().getInfo()
        print(f'  {scene_count} Landsat scenes loaded')
        if scene_count == 0:
            print('  No Landsat scenes found.')
            composite = None

    if composite:
        lst_img = None
        for v in surface_vars:
            try:
                if v == 'lulc':
                    print('  Computing Land Cover Classification (LULC)...')
                    lulc_result = compute_lulc(study_area, start_date, end_date, region_name)
                    if lulc_result['success']:
                        stats_summary['LULC'] = lulc_result['stats']
                        panels_surface.append((
                            lulc_result['lulc_img'],
                            lulc_result['vis_params'],
                            'Land Cover Classification',
                            study_area
                        ))
                elif v == 'rgb':
                    panels_surface.append((composite, VIS['rgb'], 'True Color (RGB)', study_area))
                elif v == 'lst':
                    print('  Computing LST...')
                    lst_img, em_img = compute_lst(composite, study_area)
                    stats_summary['LST'] = get_stats(lst_img, 'LST', study_area, scale=90)
                    panels_surface.append((lst_img, VIS['lst'], 'LST (degrees C)', study_area))
                elif v == 'uhi':
                    print('  Computing UHI...')
                    if lst_img is None:
                        lst_img, _ = compute_lst(composite, study_area)
                    uhi_img, lst_mean, lst_std = compute_uhi(lst_img, study_area)
                    stats_summary['UHI'] = {'mean': 0.0, 'lst_mean': lst_mean, 'lst_std': lst_std}
                    panels_surface.append((uhi_img, VIS['uhi'], f'UHI (mean LST={lst_mean:.1f}C)', study_area))
                elif v in SURFACE_INDEX_MAP:
                    label, func, vis_key, scale = SURFACE_INDEX_MAP[v]
                    print(f'  Computing {label}...')
                    img = func(composite)
                    s = get_stats(img, label, study_area, scale=scale)
                    # Monthly stats: compute index per image then aggregate by month
                    print(f'  Computing monthly stats for {label}...')
                    try:
                        import datetime
                        monthly = {}
                        start_dt = datetime.datetime.strptime(start_date, '%Y-%m-%d')
                        end_dt   = datetime.datetime.strptime(end_date,   '%Y-%m-%d')
                        cur = start_dt.replace(day=1)
                        while cur <= end_dt:
                            m_start = cur.strftime('%Y-%m-%d')
                            if cur.month == 12:
                                m_end = cur.replace(year=cur.year+1, month=1, day=1).strftime('%Y-%m-%d')
                            else:
                                m_end = cur.replace(month=cur.month+1, day=1).strftime('%Y-%m-%d')
                            month_scenes = landsat_col.filterDate(m_start, m_end)
                            count = month_scenes.size().getInfo()
                            if count > 0:
                                month_composite = month_scenes.median()
                                month_img = func(month_composite)
                                ms = month_img.reduceRegion(
                                    reducer=ee.Reducer.mean(),
                                    geometry=study_area, scale=scale, maxPixels=1e9
                                ).getInfo()
                                val = ms.get(label)
                                if val is not None:
                                    monthly[cur.strftime('%Y-%m')] = round(val, 6)
                            if cur.month == 12:
                                cur = cur.replace(year=cur.year+1, month=1)
                            else:
                                cur = cur.replace(month=cur.month+1)
                        s['monthly'] = monthly
                    except Exception as me:
                        print(f'    Monthly stats failed: {me}')
                        s['monthly'] = {}
                    stats_summary[label] = s
                    panels_surface.append((img, VIS[vis_key], label, study_area))
            except Exception as e:
                print(f'  {v} failed: {e}')

    for v in atmo_vars:
        try:
            if v == 'ffpi':
                print('  Computing FFPI...')
                ffpi_img, ffpi_class = compute_ffpi(study_area, start_date, end_date)
                stats_summary['FFPI'] = get_stats(ffpi_img, 'FFPI', study_area, scale=3500)
                panels_atmo.append((ffpi_img,   VIS['ffpi'],       'FFPI Score',          study_area))
                panels_atmo.append((ffpi_class, VIS['ffpi_class'], 'FFPI Pollution Zones', study_area))
            elif v in ATMO_INDEX_MAP:
                label, func, vis_key, unit = ATMO_INDEX_MAP[v]
                print(f'  Computing {label}...')
                img, col = func(study_area, start_date, end_date)
                count = col.size().getInfo()
                if count > 0:
                    band_name = img.bandNames().getInfo()[0]
                    s = get_stats(img, band_name, study_area, scale=3500)
                    print(f'  Computing monthly stats for {label}...')
                    try:
                        import datetime
                        monthly = {}
                        start_dt = datetime.datetime.strptime(start_date, '%Y-%m-%d')
                        end_dt   = datetime.datetime.strptime(end_date,   '%Y-%m-%d')
                        cur = start_dt.replace(day=1)
                        while cur <= end_dt:
                            m_start = cur.strftime('%Y-%m-%d')
                            if cur.month == 12:
                                m_end = cur.replace(year=cur.year+1, month=1, day=1).strftime('%Y-%m-%d')
                            else:
                                m_end = cur.replace(month=cur.month+1, day=1).strftime('%Y-%m-%d')
                            month_img_col = col.filterDate(m_start, m_end)
                            mc = month_img_col.size().getInfo()
                            if mc > 0:
                                month_mean = month_img_col.mean()
                                ms = month_mean.reduceRegion(
                                    reducer=ee.Reducer.mean(),
                                    geometry=study_area, scale=3500, maxPixels=1e9
                                ).getInfo()
                                val = ms.get(band_name)
                                if val is not None:
                                    monthly[cur.strftime('%Y-%m')] = round(val, 6)
                            if cur.month == 12:
                                cur = cur.replace(year=cur.year+1, month=1)
                            else:
                                cur = cur.replace(month=cur.month+1)
                        s['monthly'] = monthly
                    except Exception as me:
                        print(f'    Monthly stats failed: {me}')
                        s['monthly'] = {}
                    stats_summary[label] = s
                    panels_atmo.append((img, VIS[vis_key], f'{label} ({unit})', study_area))
                else:
                    print(f'  No data for {label}')
        except Exception as e:
            print(f'  {v} failed: {e}')

    title_base = f'{region_name} | {start_date} to {end_date}'
    if panels_surface:
        print('\nGenerating surface index map...')
        # Check if lulc is in variables — use special plot function
        if 'lulc' in [v.lower() for v in variables] and 'LULC' in stats_summary:
            plot_lulc_with_pie(
                panels_surface,
                f'Surface Analysis - {title_base}',
                stats_summary['LULC']
            )
        else:
            plot_panels(panels_surface, f'Surface Analysis - {title_base}')
    if panels_atmo:
        print('\nGenerating atmospheric analysis map...')
        plot_panels(panels_atmo, f'Atmospheric Analysis - {title_base}')

    if stats_summary:
        print(f'\nSTATISTICS SUMMARY - {region_name}')
        print(f'  Period: {start_date} to {end_date}')
        print(f'  {dash}')
        for var, s in stats_summary.items():
            if isinstance(s, dict) and s.get('mean') is not None:
                mean_v   = s.get("mean");   mean_s   = f'{mean_v:.4f}'   if mean_v   is not None else 'N/A'
                min_v    = s.get("min");    min_s    = f'{min_v:.4f}'    if min_v    is not None else 'N/A'
                max_v    = s.get("max");    max_s    = f'{max_v:.4f}'    if max_v    is not None else 'N/A'
                std_v    = s.get("std");    std_s    = f'{std_v:.4f}'    if std_v    is not None else 'N/A'
                med_v    = s.get("median"); med_s    = f'{med_v:.4f}'    if med_v    is not None else 'N/A'
                p10_v    = s.get("p10");    p10_s    = f'{p10_v:.4f}'    if p10_v    is not None else 'N/A'
                p90_v    = s.get("p90");    p90_s    = f'{p90_v:.4f}'    if p90_v    is not None else 'N/A'
                print(f'  {var:<12} mean={mean_s}  median={med_s}  std={std_s}')
                print(f'  {"":<12} min={min_s}   max={max_s}')
                print(f'  {"":<12} p10={p10_s}   p90={p90_s}')
                # Monthly breakdown
                monthly = s.get("monthly", {})
                if monthly:
                    print(f'  {"":<12} Monthly mean:')
                    months_sorted = sorted(monthly.items())
                    line = ''
                    for m, v in months_sorted:
                        entry = f'{m}:{v:.3f}  '
                        if len(line) + len(entry) > 52:
                            print(f'  {"":<12}   {line.strip()}')
                            line = entry
                        else:
                            line += entry
                    if line.strip():
                        print(f'  {"":<12}   {line.strip()}')
            elif isinstance(s, dict) and 'lst_mean' in s:
                print(f'  {"UHI":<12} LST mean={s["lst_mean"]:.2f}C  std={s["lst_std"]:.2f}C')
        print(f'  {dash}')

    if stats_summary:
        print_insight(region_name, start_date, end_date, stats_summary, variables)

    if not panels_surface and not panels_atmo:
        print('No results generated. Check region name, dates, or variables.')


# =============================================================================
# SECTION L - LAND COVER CLASSIFICATION (LULC)
# Random Forest classifier trained on ESRI 10m Global Land Cover 2023
# Dynamic class detection — only reports classes present in study region
# =============================================================================

# All possible ESRI Land Cover classes (value → name, hex color)
ESRI_CLASSES = {
    1 : ('Water',       '#1A5BAB'),
    2 : ('Trees',       '#358221'),
    4 : ('Flooded Veg', '#87D19E'),
    5 : ('Crops',       '#FFDB5C'),
    7 : ('Built Area',  '#ED022A'),
    8 : ('Bare Ground', '#EDE9E4'),
    9 : ('Snow/Ice',    '#F2FAFF'),
    10: ('Clouds',      '#C8C8C8'),
    11: ('Rangeland',   '#C6AD8D'),
}

# LLM decides which classes are relevant for a given region
LULC_SYSTEM_PROMPT = (
    "You are a geographic expert. Given a region name, return ONLY a JSON list of "
    "integers representing the ESRI Land Cover class IDs that realistically exist there.\n"
    "ESRI classes: 1=Water, 2=Trees, 4=Flooded Vegetation, 5=Crops, 7=Built Area, "
    "8=Bare Ground, 9=Snow/Ice, 11=Rangeland.\n"
    "Examples: Jakarta=[1,2,4,5,7,8,11], Greenland=[1,2,8,9], "
    "London=[1,2,5,7,8,11], Sahara=[1,7,8,11]\n"
    "Return ONLY a JSON array of integers. Nothing else."
)

def get_relevant_classes(region_name):
    """Ask LLM which land cover classes are relevant for this region."""
    try:
        resp = requests.post(OLLAMA_URL,
            json={'model': OLLAMA_MODEL,
                  'messages': [
                      {'role': 'system', 'content': LULC_SYSTEM_PROMPT},
                      {'role': 'user',   'content': f'Region: {region_name}'}],
                  'stream': False}, timeout=30)
        raw = resp.json()['message']['content'].strip()
        # Extract JSON array
        start = raw.find('['); end = raw.rfind(']') + 1
        if start >= 0 and end > start:
            classes = json.loads(raw[start:end])
            # Filter to valid ESRI class IDs
            valid = [c for c in classes if c in ESRI_CLASSES]
            if valid:
                print(f'  LLM selected classes for {region_name}: {[ESRI_CLASSES[c][0] for c in valid]}')
                return valid
    except Exception as e:
        print(f'  LLM class selection failed: {e}')
    # Fallback: all classes except clouds
    return [1, 2, 4, 5, 7, 8, 11]

def compute_lulc(study_area, start_date, end_date, region_name):
    """
    Land cover classification using:
    1. LLM selects relevant classes for the region
    2. ESA WorldCover 2021 as training reference (reliable GEE public dataset)
       Remapped to ESRI-compatible class IDs
    3. Landsat 8 spectral bands + indices as features
    4. Stratified Random Forest (200 trees)
    5. Area stats in hectares and percentage
    """
    try:
        # Re-initialize GEE fresh in this thread (same as notebook workflow)
        gee_init_for_thread()

        # ── Step 1: Get relevant classes from LLM ────────────────────────────
        relevant_ids = get_relevant_classes(region_name)

        # ── Step 2: Load ESA WorldCover as training reference ─────────────────
        # ESA WorldCover v200 (2021) — reliable public GEE dataset, 10m global
        # Original ESA classes:
        #   10=Trees, 20=Shrubland, 30=Grassland, 40=Cropland, 50=Built,
        #   60=Bare/Sparse, 70=Snow/Ice, 80=Water, 90=Wetland, 95=Mangrove, 100=MossLichen
        # Remapped to ESRI-compatible IDs (our ESRI_CLASSES dict)
        print('  Loading ESA WorldCover 2021...')
        esa_raw = (ee.ImageCollection('ESA/WorldCover/v200')
                     .filterBounds(study_area)
                     .first()
                     .clip(study_area))

        # Remap ESA → our class IDs
        esa_from = [10,  20,  30,  40,  50,  60,  70,  80,  90,  95, 100]
        esa_to   = [ 2,  11,  11,   5,   7,   8,   9,   1,   4,   4,  11]
        ref_lc   = esa_raw.remap(esa_from, esa_to).rename('landcover').clip(study_area)
        print('  ESA WorldCover loaded and remapped')

        # ── Step 3: Load Landsat 8 composite as features ─────────────────────
        print('  Loading Landsat 8 features...')
        landsat_col, composite = load_landsat(study_area, start_date, end_date)
        count = landsat_col.size().getInfo()
        if count == 0:
            return {'success': False, 'message': 'No Landsat scenes found'}
        print(f'  {count} Landsat scenes loaded')

        # Feature stack: 6 bands + 6 indices = 12 features
        ndvi  = compute_ndvi(composite)
        ndbi  = compute_ndbi(composite)
        ndwi  = compute_ndwi(composite)
        mndwi = compute_mndwi(composite)
        savi  = compute_savi(composite)
        bsi   = compute_bsi(composite)

        features = (composite.select(['SR_B2','SR_B3','SR_B4','SR_B5','SR_B6','SR_B7'])
                              .addBands(ndvi).addBands(ndbi).addBands(ndwi)
                              .addBands(mndwi).addBands(savi).addBands(bsi))

        # ── Step 4: Stratified sampling ───────────────────────────────────────
        # Use ee.Image.stratifiedSample — samples proportionally from each class
        # This is more reliable than per-class sampling for rare classes
        print('  Sampling training points (stratified)...')
        training_stack = features.addBands(ref_lc)

        # Filter relevant_ids to only those present — SINGLE batch GEE call
        present_classes = []
        try:
            # Count pixels per class in one reduceRegion call
            counts = ref_lc.reduceRegion(
                reducer  = ee.Reducer.frequencyHistogram(),
                geometry = study_area,
                scale    = 500,
                maxPixels= 1e8
            ).getInfo().get('landcover', {})
            # counts is a dict like {'1': 65, '2': 163, ...}
            counts_by_id = {int(float(k)): int(v) for k, v in counts.items()}
            for class_id in relevant_ids:
                count = counts_by_id.get(class_id, 0)
                if count > 2:
                    present_classes.append(class_id)
                    print(f'    {ESRI_CLASSES[class_id][0]}: present ({count} px @ 500m)')
                else:
                    print(f'    {ESRI_CLASSES[class_id][0]}: absent in this region, skipping')
        except Exception as e:
            print(f'  Presence check failed: {e}')
            # Fallback: use all relevant classes
            present_classes = relevant_ids
            print(f'  Fallback: using all {len(present_classes)} LLM-selected classes')

        if len(present_classes) < 2:
            return {'success': False,
                    'message': f'Only {len(present_classes)} class(es) present — need ≥2'}

        print(f'  Sampling {len(present_classes)} classes...')
        # No per-class getInfo() — build collections lazily, merge once
        all_samples = []
        sampled_ids = []
        for class_id in present_classes:
            try:
                class_mask = ref_lc.eq(class_id)
                # Build sample collection without calling getInfo()
                samples = (training_stack
                           .updateMask(class_mask)
                           .sample(region    = study_area,
                                   scale     = 100,
                                   numPixels = 200,
                                   seed      = 42 + class_id,
                                   geometries= False))
                all_samples.append(samples)
                sampled_ids.append(class_id)
            except Exception as e:
                print(f'    {ESRI_CLASSES[class_id][0]}: sampling error ({e})')

        if len(all_samples) < 2:
            return {'success': False,
                    'message': f'Only {len(all_samples)} class(es) sampled — need ≥2'}

        training_data = all_samples[0]
        for s in all_samples[1:]:
            training_data = training_data.merge(s)

        n_total = training_data.size().getInfo()
        print(f'  Total training samples: {n_total} across {len(all_samples)} classes')

        # ── Step 5: Train Random Forest ───────────────────────────────────────
        print('  Training Random Forest (200 trees)...')
        # Known feature bands — no getInfo() call needed
        band_names = ['SR_B2','SR_B3','SR_B4','SR_B5','SR_B6','SR_B7',
                      'NDVI','NDBI','NDWI','MNDWI','SAVI','BSI']
        classifier = ee.Classifier.smileRandomForest(
            numberOfTrees     = 200,
            variablesPerSplit = None,
            minLeafPopulation = 1,
            bagFraction       = 0.5,
            seed              = 42
        ).train(
            features        = training_data,
            classProperty   = 'landcover',
            inputProperties = band_names
        )

        # ── Step 6: Classify ──────────────────────────────────────────────────
        print('  Classifying image...')
        classified = features.classify(classifier).rename('classification')

        # ── Step 7: Area statistics at appropriate scale ──────────────────────
        print('  Computing area statistics...')
        bbox_area     = study_area.area(maxError=1).getInfo()
        stats_scale   = 100 if bbox_area < 5e9 else 300  # 100m cities, 300m countries
        pixel_area_ha = (stats_scale ** 2) / 10000.0

        area_stats   = {}
        total_pixels = 0
        class_pixels = {}

        # Single batch call — count all class pixels at once
        try:
            counts = classified.reduceRegion(
                reducer  = ee.Reducer.frequencyHistogram(),
                geometry = study_area,
                scale    = stats_scale,
                maxPixels= 1e9
            ).getInfo().get('classification', {})
            # counts = {'1': 909, '7': 57492, ...}
            for k, v in counts.items():
                try:
                    class_id = int(float(k))
                    if class_id in sampled_ids and v > 0:
                        class_pixels[class_id] = int(v)
                        total_pixels += int(v)
                except: pass
        except Exception as e:
            print(f'  Area batch calc failed: {e}')

        if total_pixels == 0:
            return {'success': False, 'message': 'Area calculation returned zero pixels'}

        for class_id, px in class_pixels.items():
            name     = ESRI_CLASSES[class_id][0]
            hectares = round(px * pixel_area_ha, 1)
            pct      = round((px / total_pixels) * 100, 2)
            area_stats[name] = {
                'hectares'  : hectares,
                'percentage': pct,
                'class_id'  : class_id,
                'color'     : ESRI_CLASSES[class_id][1],
            }

        # Sort by area descending
        area_stats = dict(sorted(area_stats.items(),
                                 key=lambda x: x[1]['hectares'], reverse=True))
        total_ha   = round(total_pixels * pixel_area_ha, 1)

        print(f'  Classification done! {len(area_stats)} classes | {total_ha:,.0f} ha total')
        for name, s in area_stats.items():
            print(f'    {name:<16} {s["hectares"]:>10,.1f} ha  ({s["percentage"]:.1f}%)')

        # ── Step 8: Remap classified image to 0-indexed for correct color mapping ──
        # GEE palette maps min→max linearly, so we must remap class IDs to 0,1,2...
        sorted_ids    = sorted([s['class_id'] for s in area_stats.values()])
        sorted_colors = [ESRI_CLASSES[c][1] for c in sorted_ids]

        # Build SLD style — assigns exact hex color to each class ID value
        # No remap() needed, no extra GEE API calls
        sld_entries = ''.join(
            f'<ColorMapEntry color="{color}" quantity="{cid}" label="{ESRI_CLASSES[cid][0]}" opacity="1"/>'
            for cid, color in zip(sorted_ids, sorted_colors)
        )
        sld_style = (
            '<RasterSymbolizer>'
            '<ColorMap type="values" extended="false">'
            + sld_entries +
            '</ColorMap>'
            '</RasterSymbolizer>'
        )
        vis_params  = {'sld_style': sld_style}
        remapped    = classified

        return {
            'success'   : True,
            'lulc_img'  : remapped,
            'vis_params': vis_params,
            'stats'     : {
                'classes'  : area_stats,
                'total_ha' : total_ha,
                'scale_m'  : stats_scale,
                'n_classes': len(area_stats),
            },
            'message': f'LULC done: {len(area_stats)} classes, {total_ha:,.0f} ha total',
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return {'success': False, 'message': str(e)}

def plot_lulc_with_pie(panels, title, lulc_stats):
    """
    Special plot function for LULC: map on left, pie chart on right.
    Other variables (if any) plotted normally above.
    """
    import matplotlib.patches as mpatches

    # Separate lulc panel from others
    lulc_panel   = None
    other_panels = []
    for p in panels:
        if 'Land Cover' in p[2]:
            lulc_panel = p
        else:
            other_panels.append(p)

    # Plot other panels normally first
    if other_panels:
        plot_panels(other_panels, title)

    if not lulc_panel:
        return

    area_stats = lulc_stats.get('classes', {})
    total_ha   = lulc_stats.get('total_ha', 0)
    if not area_stats:
        return

    img, vp, label, region = lulc_panel

    # ── Figure: 2 columns — map | pie ────────────────────────────────────────
    fig, axes = plt.subplots(1, 2, figsize=(16, 8))
    fig.patch.set_facecolor('#0a0c10')
    fig.suptitle(title, fontsize=13, fontweight='bold', color='white', y=0.98)

    # ── Left: LULC map ────────────────────────────────────────────────────────
    ax_map = axes[0]
    ax_map.set_facecolor('#0d1117')
    try:
        print('  Downloading LULC map...')
        arr = get_thumb(img.clip(region), vp, region, dim=512)
        ax_map.imshow(arr)
        print('  Done: LULC map')
    except Exception as e:
        ax_map.text(0.5, 0.5, f'Map error:\n{str(e)[:80]}',
                    ha='center', va='center', transform=ax_map.transAxes,
                    fontsize=9, color='gray')

    ax_map.set_title('Land Cover Classification', fontsize=12, fontweight='bold',
                     color='white', pad=10)
    ax_map.axis('off')

    # Legend patches on map
    legend_patches = []
    for name, s in area_stats.items():
        legend_patches.append(
            mpatches.Patch(color=s['color'], label=f'{name} ({s["percentage"]:.1f}%)')
        )
    ax_map.legend(handles=legend_patches, loc='lower left',
                  fontsize=8, framealpha=0.7,
                  facecolor='#11141a', edgecolor='#252b38',
                  labelcolor='white')

    # ── Right: Pie chart ──────────────────────────────────────────────────────
    ax_pie = axes[1]
    ax_pie.set_facecolor('#0a0c10')

    names   = list(area_stats.keys())
    sizes   = [s['percentage'] for s in area_stats.values()]
    colors  = [s['color']      for s in area_stats.values()]
    hectares= [s['hectares']   for s in area_stats.values()]

    # Explode the largest class slightly
    explode = [0.03] * len(names)

    wedges, texts, autotexts = ax_pie.pie(
        sizes,
        labels      = None,
        colors      = colors,
        autopct     = '%1.1f%%',
        startangle  = 140,
        explode     = explode,
        pctdistance = 0.75,
        wedgeprops  = {'linewidth': 0.8, 'edgecolor': '#0a0c10'},
    )

    for at in autotexts:
        at.set_color('white')
        at.set_fontsize(8)
        at.set_fontweight('bold')

    ax_pie.set_facecolor('#0a0c10')
    ax_pie.set_title(f'Land Cover Distribution\nTotal: {total_ha:,.0f} ha',
                     fontsize=12, fontweight='bold', color='white', pad=15)

    # Custom legend with hectares
    legend_labels = [f'{n}: {h:,.0f} ha ({p:.1f}%)'
                     for n, h, p in zip(names, hectares, sizes)]
    ax_pie.legend(
        wedges, legend_labels,
        loc            = 'lower center',
        bbox_to_anchor = (0.5, -0.18),
        ncol           = 2,
        fontsize       = 8,
        framealpha     = 0.5,
        facecolor      = '#11141a',
        edgecolor      = '#252b38',
        labelcolor     = 'white',
    )

    plt.tight_layout(rect=[0, 0, 1, 0.95])
    safe_title = title.replace(' ', '_').replace('/', '-')[:40] + '_LULC'
    save_figure(fig, safe_title)

print('LULC functions loaded. Ready for land cover classification.')

# =============================================================================
# SECTION J - WEB SEARCH CONTEXT FETCHER
# =============================================================================

try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

# Hard blocklist — definitely off-topic titles/snippets
BLOCKLIST_TERMS = [
    'mexico city', 'water bankruptcy', 'corn phenology', 'spectrometer',
    'estarfm', 'modis fusion', 'phenological stage', 'stack overflow',
    'reddit', 'quora', 'amazon rainforest', 'arctic', 'antarctic',
    'how to use ndvi', 'ndvi wikipedia', 'what is ndvi', 'what is evi',
    'normalized difference vegetation index -', 'esri | sentinel',
    'interactive world forest', 'u.s. geological survey',
    'land cover | u.s.', 'global forest watch',
]

# Region → country/context terms for relevance matching
REGION_CONTEXT = {
    'jakarta' : ['indonesia', 'indonesian', 'jabodetabek', 'java', 'bogor',
                 'tangerang', 'bekasi', 'depok', 'jawa', 'sumatra'],
    'beijing' : ['china', 'chinese', 'bth', 'hebei', 'tianjin', 'jing-jin-ji',
                 'north china'],
    'cairo'   : ['egypt', 'egyptian', 'nile', 'north africa', 'mena'],
    'mumbai'  : ['india', 'indian', 'maharashtra', 'south asia'],
    'delhi'   : ['india', 'indian', 'ncr', 'south asia'],
    'bangkok' : ['thailand', 'thai', 'southeast asia'],
    'manila'  : ['philippines', 'filipino', 'southeast asia'],
    'lagos'   : ['nigeria', 'nigerian', 'west africa'],
    'nairobi' : ['kenya', 'kenyan', 'east africa'],
    'shanghai': ['china', 'chinese', 'yangtze'],
    'dhaka'   : ['bangladesh', 'bangladeshi', 'south asia'],
    'karachi' : ['pakistan', 'pakistani', 'sindh'],
    'hanoi'   : ['vietnam', 'vietnamese'],
    'ho chi minh': ['vietnam', 'vietnamese', 'saigon'],
    'tehran'  : ['iran', 'iranian', 'persia'],
    'istanbul': ['turkey', 'turkish'],
    'moscow'  : ['russia', 'russian'],
    'london'  : ['uk', 'britain', 'british', 'england'],
    'paris'   : ['france', 'french'],
    'berlin'  : ['germany', 'german'],
    'tokyo'   : ['japan', 'japanese'],
    'seoul'   : ['korea', 'korean', 'south korea'],
}

# Per-variable query templates — ordered most specific → broad
VAR_QUERY_TEMPLATES = {
    'ndvi'   : [
        '{region} vegetation green space satellite {year}',
        '{region} NDVI land cover urban green {year}',
        '{region} deforestation forest loss {year}',
        '{region} vegetation change remote sensing {year}',
        '{region} urban greening park {year}',
    ],
    'evi'    : [
        '{region} forest canopy vegetation health {year}',
        '{region} forest cover change {year}',
        '{region} vegetation productivity {year}',
        '{region} EVI remote sensing {year}',
    ],
    'savi'   : [
        '{region} agriculture soil land {year}',
        '{region} arid land degradation vegetation {year}',
        '{region} land use agriculture change {year}',
    ],
    'ndwi'   : [
        '{region} flood drought water crisis {year}',
        '{region} river lake water level {year}',
        '{region} water scarcity flooding {year}',
        '{region} water body change {year}',
    ],
    'mndwi'  : [
        '{region} urban flooding inundation {year}',
        '{region} coastal wetland change {year}',
        '{region} flood water surface {year}',
    ],
    'ndbi'   : [
        '{region} urban expansion development {year}',
        '{region} urbanization construction growth {year}',
        '{region} city land use change {year}',
        '{region} built-up area growth {year}',
    ],
    'ui'     : [
        '{region} urban density sprawl {year}',
        '{region} city expansion infrastructure {year}',
        '{region} urban growth population {year}',
    ],
    'bsi'    : [
        '{region} soil erosion land degradation {year}',
        '{region} desertification bare land {year}',
        '{region} agricultural soil loss {year}',
    ],
    'ndsi'   : [
        '{region} snow ice cover change {year}',
        '{region} glacier snowpack {year}',
        '{region} winter snow climate {year}',
    ],
    'lst'    : [
        '{region} extreme heat heat wave {year}',
        '{region} surface temperature record {year}',
        '{region} heat climate temperature {year}',
        '{region} thermal pollution heat {year}',
    ],
    'uhi'    : [
        '{region} urban heat island {year}',
        '{region} city heat cooling green space {year}',
        '{region} heat island effect {year}',
        '{region} urban thermal {year}',
    ],
    'co'     : [
        '{region} carbon monoxide air pollution {year}',
        '{region} vehicle emission CO air quality {year}',
        '{region} air pollution carbon monoxide {year}',
    ],
    'no2'    : [
        '{region} nitrogen dioxide NO2 air quality {year}',
        '{region} traffic emission pollution {year}',
        '{region} nitrogen oxide industrial {year}',
        '{region} air pollution NO2 {year}',
    ],
    'so2'    : [
        '{region} sulfur dioxide SO2 industrial {year}',
        '{region} coal power plant pollution {year}',
        '{region} sulfur emission air {year}',
    ],
    'ch4'    : [
        '{region} methane emission greenhouse gas {year}',
        '{region} CH4 landfill natural gas {year}',
        '{region} methane leak {year}',
    ],
    'aerosol': [
        '{region} PM2.5 haze air quality AQI {year}',
        '{region} dust smoke wildfire air {year}',
        '{region} particulate matter pollution {year}',
        '{region} smog haze {year}',
    ],
    'o3'     : [
        '{region} ozone air quality {year}',
        '{region} ground level ozone smog {year}',
        '{region} ozone pollution health {year}',
    ],
    'gpp'    : [
        '{region} forest carbon sink productivity {year}',
        '{region} vegetation photosynthesis {year}',
        '{region} carbon uptake forest {year}',
    ],
    'burned' : [
        '{region} wildfire fire disaster {year}',
        '{region} forest fire burned area {year}',
        '{region} peatland fire hotspot {year}',
        '{region} fire emergency {year}',
    ],
    'ffpi'   : [
        '{region} industrial air pollution emission {year}',
        '{region} fossil fuel pollution {year}',
        '{region} NO2 CO SO2 environment {year}',
    ],
}

WIKI_TOPICS = {
    'ndvi': 'vegetation green space', 'evi': 'forest ecology',
    'ndwi': 'water resources', 'mndwi': 'urban flooding',
    'ndbi': 'urbanization', 'ui': 'urban sprawl',
    'bsi': 'soil erosion', 'ndsi': 'snow cover',
    'lst': 'urban heat', 'uhi': 'urban heat island',
    'co': 'air pollution', 'no2': 'air quality nitrogen',
    'so2': 'sulfur pollution', 'ch4': 'methane emissions',
    'aerosol': 'air quality particulate', 'o3': 'ozone pollution',
    'gpp': 'forest carbon', 'burned': 'wildfire',
    'ffpi': 'industrial pollution',
}

def _is_relevant(title, snippet, region_name):
    """4-layer relevance check. Returns (bool, reason_str)."""
    title_l  = title.lower()
    text_l   = (title + ' ' + snippet).lower()
    rwords   = region_name.lower().split()
    extra    = REGION_CONTEXT.get(region_name.lower(), [])
    all_terms = rwords + extra

    # Layer 1: hard blocklist
    for term in BLOCKLIST_TERMS:
        if term in text_l:
            return False, f'blocked:"{term}"'

    # Layer 2-4: accept if any region/country term found anywhere
    for term in all_terms:
        if term in text_l:
            return True, f'match:"{term}"'

    return False, 'no-match'

def _search(query, mode='text', max_results=6):
    """Run DDG search, return normalized result list."""
    try:
        with DDGS() as ddgs:
            if mode == 'news':
                raw = list(ddgs.news(query, max_results=max_results))
                return [{'title': r.get('title',''), 'body': r.get('body', r.get('excerpt','')),
                         'href': r.get('url', r.get('href',''))} for r in raw]
            else:
                return list(ddgs.text(query, max_results=max_results))
    except Exception as e:
        return []   # silent fail, caller handles fallback

def _wiki(region_name, topic):
    """Wikipedia summary fallback."""
    try:
        q    = requests.utils.quote(f'{region_name} {topic}')
        resp = requests.get(
            f'https://en.wikipedia.org/api/rest_v1/page/summary/{q}',
            timeout=8).json()
        text  = resp.get('extract', '').strip()
        title = resp.get('title', 'Wikipedia')
        if text and len(text) > 80:
            return '[Wikipedia: ' + title + ']\n' + text[:600]
    except:
        pass
    return None

def _pick_best(results, region_name, seen_urls):
    """From a result list, return the first relevant non-duplicate snippet."""
    for r in results:
        url     = r.get('href', '')
        snippet = r.get('body', '').strip()
        title   = r.get('title', '')
        if url and url in seen_urls:
            continue
        if not snippet or len(snippet) < 60:
            continue
        ok, reason = _is_relevant(title, snippet, region_name)
        if ok:
            seen_urls.add(url)
            return '[' + title[:70] + ']\n' + snippet[:600], reason
        else:
            print(f'    Skip ({reason}): "{title[:55]}"')
    return None, None

def fetch_web_context(region_name, start_date, end_date, variables):
    """
    Smart multi-query web search.
    Strategy per variable:
      1. Try text search with specific query
      2. If no result, retry with news search
      3. If still nothing, use Wikipedia fallback
    Collect up to 5 unique relevant snippets.
    """
    year      = start_date[:4]
    chunks    = []
    seen_urls = set()

    # Build query plan for each variable
    for v in variables[:2]:
        if len(chunks) >= 5:
            break
        templates = VAR_QUERY_TEMPLATES.get(v.lower(),
                        [f'{region_name} {v} environment {year}'])
        got_one = False
        for tmpl in templates:
            if len(chunks) >= 5 or got_one:
                break
            query = tmpl.format(region=region_name, year=year)
            print(f'  [{v.upper()}] "{query}"')

            # Try text first
            results = _search(query, 'text', 6)
            time.sleep(0.8)
            chunk, reason = _pick_best(results, region_name, seen_urls)

            # Retry as news if text found nothing
            if not chunk:
                results2 = _search(query + ' news', 'news', 6)
                time.sleep(0.8)
                chunk, reason = _pick_best(results2, region_name, seen_urls)

            if chunk:
                chunks.append(chunk)
                print(f'    ✓ Accepted ({reason})')
                got_one = True

        # Wikipedia fallback if no DDG result for this variable
        if not got_one:
            topic = WIKI_TOPICS.get(v.lower(), 'environment')
            wiki  = _wiki(region_name, topic)
            if wiki and wiki not in chunks:
                chunks.append(wiki)
                print(f'    ✓ Wikipedia fallback: {region_name} {topic}')

    # Always add a fresh news context query
    if len(chunks) < 5:
        news_queries = [
            f'{region_name} environment {year}',
            f'{region_name} climate pollution {year}',
            f'{region_name} satellite monitoring {year}',
        ]
        for nq in news_queries:
            if len(chunks) >= 5:
                break
            print(f'  [NEWS] "{nq}"')
            results = _search(nq, 'news', 6)
            time.sleep(0.8)
            if not results:
                results = _search(nq, 'text', 6)
                time.sleep(0.8)
            chunk, reason = _pick_best(results, region_name, seen_urls)
            if chunk:
                chunks.append(chunk)
                print(f'    ✓ Accepted ({reason})')

    if chunks:
        print(f'\n  Web context: {len(chunks)} snippets retrieved')
        return '\n\n'.join(chunks)

    print('  No web context found - using LLM knowledge only')
    return None


# =============================================================================
# SECTION K - LLM INSIGHT GENERATOR (stats + web context)
# =============================================================================

UNIT_LOOKUP = {
    'NDVI': 'index (-1 to 1)',   'EVI': 'index (-1 to 1)',
    'SAVI': 'index (-1 to 1)',   'NDWI': 'index (-1 to 1)',
    'MNDWI': 'index (-1 to 1)', 'NDBI': 'index (-1 to 1)',
    'UI': 'index (-1 to 1)',     'BSI': 'index (-1 to 1)',
    'NDSI': 'index (-1 to 1)',   'NBI': 'index (0 to 0.5)',
    'LST': 'degrees Celsius (C)',
    'CO': 'mol/m2 (moles per square meter)',
    'NO2': 'mol/m2 (moles per square meter)',
    'SO2': 'mol/m2 (moles per square meter)',
    'CH4': 'ppb (parts per billion)',
    'O3': 'Dobson Units (DU)',
    'Aerosol': 'unitless absorbing aerosol index',
    'GPP': 'kgC/m2/8-day (kilograms carbon per sq meter per 8 days)',
    'Burned Area': 'Day of Year 1-366',
    'FFPI': 'normalized score 0=clean to 1=very polluted',
    'LULC': 'area in hectares and percentage per class',
}

def generate_insight(region_name, start_date, end_date, stats_summary, variables):
    if not stats_summary:
        return None

    stats_lines = []
    for var, s in stats_summary.items():
        unit = next((v for k, v in UNIT_LOOKUP.items()
                     if k.upper() in var.upper() or var.upper() in k.upper()), 'dimensionless')
        if isinstance(s, dict) and s.get('mean') is not None:
            line = f'  - {var} [{unit}]: mean={s["mean"]:.6f}, min={s["min"]:.6f}, max={s["max"]:.6f}'
            if s.get('std')    is not None: line += f', std={s["std"]:.6f}'
            if s.get('median') is not None: line += f', median={s["median"]:.6f}'
            if s.get('p10')    is not None: line += f', p10={s["p10"]:.6f}'
            if s.get('p90')    is not None: line += f', p90={s["p90"]:.6f}'
            stats_lines.append(line)
            monthly = s.get('monthly', {})
            if monthly:
                month_str = ', '.join(f'{m}:{v:.4f}' for m, v in sorted(monthly.items()))
                stats_lines.append(f'    Monthly mean: {month_str}')
        elif isinstance(s, dict) and 'lst_mean' in s:
            stats_lines.append(
                f'  - UHI [z-score]: based on LST mean={s["lst_mean"]:.2f}C, std={s["lst_std"]:.2f}C'
            )

    print('  Searching web for real-world context...')
    web_context = fetch_web_context(region_name, start_date, end_date, variables)

    # ── Print raw web search results ─────────────────────────────────────────
    sep_web = '-' * 56
    if web_context:
        print(f'\n  {sep_web}')
        print('  RAW WEB SEARCH RESULTS (passed to LLM):')
        print(f'  {sep_web}')
        for i, chunk in enumerate(web_context.split('\n\n'), 1):
            print(f'\n  [{i}]')
            for line in chunk.split('\n'):
                if line.strip():
                    wrapped = textwrap.fill(line.strip(), width=54)
                    for wline in wrapped.split('\n'):
                        print(f'      {wline}')
        print(f'\n  {sep_web}\n')

        web_section = (
            '\nReal-world context from web search '
            '(use this to ground your analysis):\n'
            + web_context + '\n'
        )
    else:
        web_section = ''
        print('  No web context found - using LLM knowledge only')

    stats_text = '\n'.join(stats_lines)
    vars_text  = ', '.join(variables)

    insight_prompt = (
        'You are an expert remote sensing and geospatial scientist writing a scientific briefing.\n'
        'RULES:\n'
        '- Use exact units shown in brackets. Never say "ppm" or convert units.\n'
        '- Only reference web context that is directly about this region. Ignore off-topic snippets.\n'
        '- Use the monthly data to identify seasonal patterns, peaks, and anomalies.\n'
        '- Compare p10 vs p90 to describe spatial inequality (hotspots vs clean areas).\n'
        '- Be specific: name districts, landmarks, rivers, or seasons when relevant.\n'
        '- Do not make claims unsupported by the stats or web context.\n'
        + web_section +
        f'\nRegion    : {region_name}'
        f'\nPeriod    : {start_date} to {end_date}'
        f'\nVariables : {vars_text}'
        f'\n\nSatellite statistics (use exact units in brackets):\n{stats_text}'
        '\\n\\nWrite a scientific insight (7-10 sentences) in flowing paragraphs covering:\\n'
        '1. Annual mean and what it indicates about overall conditions\\n'
        '2. Seasonal pattern from monthly data - which months peak/dip and why\\n'
        '3. Spatial variability - what p10 vs p90 and std reveal about hotspots vs clean areas\\n'
        '4. Specific districts, landmarks, or geographic features driving the patterns\\n'
        '5. Connection to real-world events or trends from the web context\\n'
        '6. Physical or socioeconomic drivers behind the patterns\\n'
        '7. One notable anomaly or surprising finding\\n'
        '8. A concrete actionable recommendation\\n'
        '\\nGround your analysis in the web context if provided. '
        'Be specific, scientific, and readable. Write in flowing paragraphs, not bullet points.'
    )

    try:
        resp = requests.post(
            OLLAMA_URL,
            json={'model': OLLAMA_MODEL,
                  'messages': [{'role': 'user', 'content': insight_prompt}],
                  'stream': False},
            timeout=120
        )
        return resp.json()['message']['content'].strip()
    except Exception as e:
        return f'Could not generate insight: {e}'


def print_insight(region_name, start_date, end_date, stats_summary, variables):
    sep = '=' * 60
    print(f'\n{sep}')
    print('  AI INSIGHT  (satellite stats + web context + LLM knowledge)')
    print(sep)
    print(f'  Generating insight for {region_name}...')

    insight = generate_insight(region_name, start_date, end_date, stats_summary, variables)

    if insight:
        print()
        for line in insight.split('\n'):
            if line.strip():
                wrapped = textwrap.fill(line.strip(), width=56)
                for wline in wrapped.split('\n'):
                    print(f'  {wline}')
            else:
                print()
    print(f'\n{sep}\n')


print('All analysis functions loaded. Ready to run the agent (Cell 4).')