"""
research_agent.py — Research Agent for GIS AI Agent WebApp
============================================================
Receives structured GIS analysis outputs from the GIS Agent and
generates a downloadable research paper in PDF format.

Architecture:
  GIS Agent → (analysis result dict) → Research Agent → .pdf file

The Research Agent does NOT perform GIS analysis. It only:
  1. Receives analysis outputs (stats, insights, web context, conclusion)
  2. Calls Ollama to generate each paper section
  3. Assembles a DOCX research paper
  4. Returns the file path for download

Usage (called from app.py):
    from research_agent import generate_research_paper
    pdf_path = generate_research_paper(analysis_result, output_dir)
"""

import os
import json
import datetime
import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Optional


# ── Section generators (each calls Ollama for a focused section) ──────────────

def _call_ollama(prompt: str, ollama_url: str, model: str, timeout: int = 90) -> str:
    """Make a single Ollama completion call. Returns the response text."""
    import requests
    try:
        resp = requests.post(
            ollama_url,
            json={
                'model': model,
                'messages': [{'role': 'user', 'content': prompt}],
                'stream': False,
            },
            timeout=timeout,
        )
        return resp.json()['message']['content'].strip()
    except Exception as e:
        return f'[Section unavailable: {e}]'


def _stats_summary(all_stats: dict) -> str:
    """Build a compact plaintext statistics summary for LLM prompts."""
    lines = []
    for var, s in all_stats.items():
        if isinstance(s, dict):
            if s.get('mean') is not None:
                lines.append(
                    f'  {var}: mean={s["mean"]:.4f}, median={s.get("median","N/A")}, '
                    f'std={s.get("std","N/A")}, p10={s.get("p10","N/A")}, p90={s.get("p90","N/A")}'
                )
            elif 'lst_mean' in s:
                lines.append(f'  UHI: LST mean={s["lst_mean"]:.2f}°C, std={s["lst_std"]:.2f}°C')
            elif 'classes' in s:
                top = sorted(s['classes'].items(), key=lambda x: -x[1].get('percentage', 0))[:4]
                top_str = ', '.join(f'{k} {v["percentage"]:.1f}%' for k, v in top)
                lines.append(f'  LULC: top classes — {top_str}')
    return '\n'.join(lines) if lines else 'No statistics available.'


def _format_date_range(start_date: str, end_date: str) -> str:
    try:
        sy, sm, sd = start_date.split('-')
        ey, em, ed = end_date.split('-')
        months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
        sm_name = months[int(sm) - 1]
        em_name = months[int(em) - 1]
        if sy == ey:
            return f'{sm_name}–{em_name} {sy}'
        return f'{sm_name} {sy} – {em_name} {ey}'
    except Exception:
        return f'{start_date} to {end_date}'


# ─────────────────────────────────────────────────────────────────────────────
# SECTION GENERATORS
# ─────────────────────────────────────────────────────────────────────────────

def gen_title(region: str, variables: list, date_range: str,
              ollama_url: str, model: str) -> str:
    var_str = ', '.join(v.upper() for v in variables[:4])
    if len(variables) > 4:
        var_str += ', et al.'
    prompt = (
        f'Write a concise academic research paper title (one sentence, no subtitle) for a '
        f'satellite remote sensing study analyzing {var_str} in {region} during {date_range}. '
        f'Make it specific, scientific, and professional. Return ONLY the title text, no quotes.'
    )
    return _call_ollama(prompt, ollama_url, model)


def gen_abstract(region: str, variables: list, date_range: str,
                 all_stats: dict, conclusion: str,
                 ollama_url: str, model: str) -> str:
    stats_text = _stats_summary(all_stats)
    var_str = ', '.join(v.upper() for v in variables)
    prompt = (
        f'Write an academic abstract (150–200 words) for a satellite remote sensing research paper.\n'
        f'Region: {region} | Period: {date_range} | Variables: {var_str}\n'
        f'Key statistics:\n{stats_text}\n'
        f'Main conclusion: {conclusion[:500] if conclusion else "See analysis results."}\n\n'
        f'The abstract must cover: objectives, data sources (Google Earth Engine + Landsat/Sentinel-5P), '
        f'methodology (satellite indices), key findings (use actual statistics), and implications. '
        f'Write in third person, past tense. No bullet points. No headers. Plain academic prose.'
    )
    return _call_ollama(prompt, ollama_url, model, timeout=120)


def gen_introduction(region: str, variables: list, date_range: str,
                     web_context: str, ollama_url: str, model: str) -> str:
    var_str = ', '.join(v.upper() for v in variables)
    web_section = f'\nBackground context (use sparingly):\n{web_context[:800]}\n' if web_context else ''
    prompt = (
        f'Write an Introduction section (250–300 words) for a satellite remote sensing paper.\n'
        f'Region: {region} | Period: {date_range} | Variables analyzed: {var_str}\n'
        f'{web_section}\n'
        f'Cover: (1) importance of monitoring these variables in this region, '
        f'(2) limitations of ground-based monitoring vs. satellite approaches, '
        f'(3) role of Google Earth Engine for large-scale analysis, '
        f'(4) objectives of this study.\n'
        f'Write flowing academic prose. No bullet points. No markdown. No headers.'
    )
    return _call_ollama(prompt, ollama_url, model, timeout=120)


def gen_methodology(region: str, variables: list, start_date: str, end_date: str,
                    ollama_url: str, model: str) -> str:
    # Categorize variables
    surface_vars = [v for v in variables if v.lower() in
                    ['ndvi','evi','savi','ndwi','mndwi','ndbi','ui','nbi','bsi','ndsi','lst','uhi','rgb']]
    atmo_vars    = [v for v in variables if v.lower() in
                    ['co','ch4','no2','so2','aerosol','o3','gpp','burned','ffpi']]
    lulc_vars    = [v for v in variables if v.lower() == 'lulc']

    data_sources = []
    if surface_vars or lulc_vars:
        data_sources.append('Landsat 8 Collection 2 Level-2 (30 m, 16-day revisit)')
    if atmo_vars:
        data_sources.append('Sentinel-5P TROPOMI (atmospheric trace gases, ~5.5 km resolution)')
    if lulc_vars:
        data_sources.append('ESA WorldCover / ESRI Land Cover for reference classification')

    var_str = ', '.join(v.upper() for v in variables)
    prompt = (
        f'Write a Methodology section (300–350 words) for a satellite remote sensing paper.\n'
        f'Study area: {region} | Analysis period: {start_date} to {end_date}\n'
        f'Variables: {var_str}\n'
        f'Data sources: {"; ".join(data_sources)}\n'
        f'Platform: Google Earth Engine (GEE) cloud computing\n\n'
        f'The methodology must describe:\n'
        f'1. Study area and temporal scope\n'
        f'2. Data sources and satellite sensors used\n'
        f'3. Preprocessing steps (cloud masking, compositing)\n'
        f'4. Index computation formulas (reference the standard formulas for each variable)\n'
        f'5. Statistical analysis approach (mean, percentiles, spatial distribution)\n\n'
        f'Write in academic passive voice. No bullet points. No markdown. No headers. '
        f'Flowing prose paragraphs only.'
    )
    return _call_ollama(prompt, ollama_url, model, timeout=120)


def gen_results(region: str, variables: list, date_range: str,
                all_stats: dict, var_insights: dict,
                ollama_url: str, model: str) -> str:
    stats_text = _stats_summary(all_stats)
    insights_text = '\n'.join(
        f'  {var}: {txt[:300]}' for var, txt in var_insights.items()
    ) if var_insights else ''
    var_str = ', '.join(v.upper() for v in variables)
    prompt = (
        f'Write a Results section (350–450 words) for a satellite remote sensing paper.\n'
        f'Region: {region} | Period: {date_range} | Variables: {var_str}\n\n'
        f'Computed statistics:\n{stats_text}\n\n'
        f'Per-variable insights:\n{insights_text}\n\n'
        f'Present results clearly: for each variable, report the mean value with its unit, '
        f'the spatial range (p10–p90), and what the distribution pattern indicates. '
        f'Reference actual numbers from the statistics above. '
        f'Write in academic past tense. Flowing prose, no bullet points, no markdown, no headers.'
    )
    return _call_ollama(prompt, ollama_url, model, timeout=120)


def gen_discussion(region: str, variables: list, date_range: str,
                   all_stats: dict, web_context: str,
                   ollama_url: str, model: str) -> str:
    stats_text = _stats_summary(all_stats)
    web_section = f'Real-world context:\n{web_context[:600]}\n' if web_context else ''
    var_str = ', '.join(v.upper() for v in variables)
    prompt = (
        f'Write a Discussion section (300–400 words) for a satellite remote sensing paper.\n'
        f'Region: {region} | Period: {date_range} | Variables: {var_str}\n'
        f'{web_section}\n'
        f'Statistics:\n{stats_text}\n\n'
        f'The discussion must:\n'
        f'1. Interpret what the results mean for the environmental/urban conditions of {region}\n'
        f'2. Connect findings across variables (e.g. how LST relates to NDVI or NDBI)\n'
        f'3. Compare results to typical/reference values in literature\n'
        f'4. Acknowledge limitations (cloud cover, temporal compositing, spatial resolution)\n'
        f'5. Suggest monitoring or policy implications\n\n'
        f'Academic prose. No bullet points. No markdown. No headers.'
    )
    return _call_ollama(prompt, ollama_url, model, timeout=120)


def gen_conclusion_section(region: str, variables: list, date_range: str,
                           conclusion: str, ollama_url: str, model: str) -> str:
    var_str = ', '.join(v.upper() for v in variables)
    prompt = (
        f'Write a Conclusion section (150–200 words) for a satellite remote sensing paper.\n'
        f'Region: {region} | Period: {date_range} | Variables: {var_str}\n'
        f'Summary of findings: {conclusion[:600] if conclusion else "See results section."}\n\n'
        f'The conclusion must: summarize the most important findings, state the '
        f'contribution of this satellite-based analysis, and end with one concrete '
        f'recommendation for future monitoring or research. '
        f'Academic prose. No bullet points. No markdown. No headers.'
    )
    return _call_ollama(prompt, ollama_url, model, timeout=90)


def gen_references(variables: list, ollama_url: str, model: str) -> list:
    """Generate a list of plausible but standard references for the variables used."""
    var_str = ', '.join(v.upper() for v in variables)
    prompt = (
        f'List 6–8 standard academic references (APA format) for a satellite remote sensing '
        f'paper analyzing: {var_str}. '
        f'Include: foundational index papers (Rouse 1974 for NDVI, etc.), '
        f'Google Earth Engine platform paper (Gorelick et al. 2017), '
        f'and 1–2 sensor papers (Landsat or Sentinel-5P TROPOMI). '
        f'Return ONLY a numbered list. Each reference on its own line. '
        f'No preamble. No explanation.'
    )
    raw = _call_ollama(prompt, ollama_url, model, timeout=90)
    refs = []
    for line in raw.split('\n'):
        line = line.strip()
        if line and (line[0].isdigit() or line.startswith('-')):
            # Strip leading number/bullet
            clean = line.lstrip('0123456789.-) ').strip()
            if clean:
                refs.append(clean)
    return refs[:10]


# ─────────────────────────────────────────────────────────────────────────────
# DOCX BUILDER
# ─────────────────────────────────────────────────────────────────────────────


# ── Pure-Python PDF builder (reportlab) — no Node.js required ─────────────────

def _b64_to_image(b64_str: str, max_width_cm: float = 15.0, max_height_cm: float = 10.0):
    """Convert a base64 PNG/JPEG string to a reportlab Image flowable, or None."""
    if not b64_str:
        return None
    try:
        import base64, io
        from reportlab.platypus import Image as RLImage
        from reportlab.lib.units import cm

        # Strip data-URI prefix if present
        if ',' in b64_str:
            b64_str = b64_str.split(',', 1)[1]
        raw = base64.b64decode(b64_str)
        buf = io.BytesIO(raw)

        img = RLImage(buf)
        # Scale to fit within max dimensions keeping aspect ratio
        mw = max_width_cm * cm
        mh = max_height_cm * cm
        w, h = img.imageWidth, img.imageHeight
        scale = min(mw / w, mh / h, 1.0)
        img.drawWidth  = w * scale
        img.drawHeight = h * scale
        return img
    except Exception as e:
        print(f'[research_agent] image embed error: {e}')
        return None


def _build_pdf(sections: dict, output_path: str) -> bool:
    """
    Build a research paper PDF using reportlab, embedding GIS figures.
    Returns True on success, False on failure.
    """
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib import colors
        from reportlab.platypus import (
            SimpleDocTemplate, Paragraph, Spacer,
            HRFlowable, PageBreak, KeepTogether, Image as RLImage,
            Table, TableStyle,
        )
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
    except ImportError as e:
        print(f'[research_agent] reportlab import error: {e}')
        print('[research_agent] HINT: pip install reportlab')
        return False


    # ── Page layout ────────────────────────────────────────────────────────────
    PAGE_W, PAGE_H = A4
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=2.5*cm, rightMargin=2.5*cm,
        topMargin=3.0*cm,  bottomMargin=2.5*cm,
        title=sections.get('title', 'Research Paper'),
        author='GIS AI Agent',
    )
    IMG_W_CM = (PAGE_W / cm) - 5.0

    # ── Styles ─────────────────────────────────────────────────────────────────
    BASE  = getSampleStyleSheet()
    BLACK = colors.HexColor('#111111')
    GRAY  = colors.HexColor('#555555')
    LGRAY = colors.HexColor('#cccccc')

    def style(name, **kw):
        return ParagraphStyle(name, parent=BASE['Normal'], **kw)

    S = {
        'title': style('T',
            fontSize=16, leading=21, textColor=BLACK,
            fontName='Helvetica-Bold', alignment=TA_CENTER, spaceAfter=5),
        'meta': style('M',
            fontSize=8.5, leading=12, textColor=GRAY,
            alignment=TA_CENTER, spaceAfter=2),
        'h1': style('H1',
            fontSize=10.5, leading=14, textColor=BLACK,
            fontName='Helvetica-Bold', spaceBefore=10, spaceAfter=3),
        'h2': style('H2',
            fontSize=10, leading=13, textColor=BLACK,
            fontName='Helvetica-Bold', spaceBefore=7, spaceAfter=2),
        'body': style('B',
            fontSize=9.5, leading=14.5, textColor=BLACK,
            alignment=TA_JUSTIFY, spaceAfter=5),
        'formula': style('F',
            fontSize=9.5, leading=13, textColor=BLACK,
            alignment=TA_CENTER, spaceAfter=2, spaceBefore=3,
            fontName='Helvetica'),
        'formula_label': style('FL',
            fontSize=8.5, leading=12, textColor=GRAY,
            alignment=TA_JUSTIFY, spaceAfter=6, leftIndent=10, rightIndent=10),
        'caption': style('CAP',
            fontSize=8.5, leading=12, textColor=GRAY,
            alignment=TA_CENTER, spaceAfter=2, spaceBefore=2),
        'fig_desc': style('FD',
            fontSize=9, leading=13.5, textColor=GRAY,
            alignment=TA_JUSTIFY, spaceAfter=8, spaceBefore=0),
        'ref': style('R',
            fontSize=8.5, leading=12.5, textColor=BLACK,
            leftIndent=14, firstLineIndent=-14, spaceAfter=2),
    }

    def hr(color=LGRAY, thickness=0.4):
        return HRFlowable(width='100%', thickness=thickness,
                          color=color, spaceAfter=4, spaceBefore=1)

    def section_body(body_text):
        items = []
        for para in body_text.split('\n'):
            para = para.strip()
            if para:
                items.append(Paragraph(para, S['body']))
        return items

    def embed_image(b64, caption='', max_h=9.0):
        img = _b64_to_image(b64, max_width_cm=IMG_W_CM, max_height_cm=max_h)
        if img is None:
            return []
        img.hAlign = 'CENTER'
        items = [Spacer(1, 0.1*cm), img]
        if caption:
            items.append(Paragraph(f'<i>{_esc(caption)}</i>', S['caption']))
        return items

    # ── Formulas hardcoded per variable ───────────────────────────────────────
    FORMULAS = {
        'ndvi': [
            ('NDVI = (NIR - RED) / (NIR + RED)',
             'Eq. (1) -- Normalized Difference Vegetation Index (Rouse et al., 1974). '
             'NIR = Band 5 (0.85-0.88 um); RED = Band 4 (0.64-0.67 um). '
             'Values range from -1 to +1; values above 0.3 typically indicate healthy vegetation cover.'),
        ],
        'evi': [
            ('EVI = G x (NIR - RED) / (NIR + C1 x RED - C2 x BLUE + L)',
             'Eq. (1) -- Enhanced Vegetation Index (Huete et al., 2002). '
             'G=2.5 (gain); C1=6; C2=7.5 (aerosol resistance); L=1 (canopy background). '
             'Corrects for atmospheric distortion and canopy background signal.'),
        ],
        'savi': [
            ('SAVI = [(NIR - RED) / (NIR + RED + L)] x (1 + L)',
             'Eq. (1) -- Soil-Adjusted Vegetation Index (Huete, 1988). L=0.5 (standard soil correction factor). '
             'Minimizes soil brightness influence on vegetation signal in sparsely vegetated areas.'),
        ],
        'lst': [
            ('BT = K2 / ln(K1 / L + 1)',
             'Eq. (1) — At-satellite brightness temperature (K). L = spectral radiance at Band 10; '
             'K1 = 774.89 W/(m2·sr·um), K2 = 1321.08 K (Landsat 8 TIRS Band 10 calibration constants, USGS metadata).'),
            ('LST = BT / [1 + (lam × BT / rho) × ln(eps)]',
             'Eq. (2) — Land Surface Temperature (K), converted to °C by subtracting 273.15. '
             'lam = 10.895 um (effective wavelength of Band 10); rho = 1.438 × 10-2 m·K; '
             'eps = land surface emissivity derived from NDVI-based fractional vegetation cover.'),
            ('FVC = [(NDVI - NDVI_soil) / (NDVI_veg - NDVI_soil)]^2',
             'Eq. (3) — Fractional Vegetation Cover (Carlson & Ripley, 1997). NDVI_soil and NDVI_veg '
             'are the minimum and maximum NDVI values in the scene. Emissivity: '
             'eps = eps_soil × (1 - FVC) + eps_veg × FVC + d_eps, '
             'where eps_soil = 0.966, eps_veg = 0.973 (Sobrino et al., 2004).'),
        ],
        'uhi': [
            ('UHI = LST_urban - LST_rural',
             'Eq. (1) -- Urban Heat Island intensity (Voogt & Oke, 2003). Urban pixels: NDVI < 0.2 and NDBI > 0. Rural reference pixels: NDVI > 0.3. Positive values indicate thermal excess of the urban core over surrounding vegetated areas. Expressed in degrees C.'),
        ],
        'ndwi': [
            ('NDWI = (GREEN - NIR) / (GREEN + NIR)',
             'Eq. (1) -- Normalized Difference Water Index (McFeeters, 1996). GREEN = Band 3 (0.53-0.59 um); NIR = Band 5 (0.85-0.88 um). Positive values delineate open water surfaces; threshold of 0 commonly used for water body extraction.'),
        ],
        'mndwi': [
            ('MNDWI = (GREEN - SWIR) / (GREEN + SWIR)',
             'Eq. (1) -- Modified Normalized Difference Water Index (Xu, 2006). SWIR = Band 6 (1.57-1.65 um). More effective than NDWI for suppressing built-up land and soil noise in dense urban environments.'),
        ],
        'ndbi': [
            ('NDBI = (SWIR - NIR) / (SWIR + NIR)',
             'Eq. (1) -- Normalized Difference Built-up Index (Zha et al., 2003). SWIR = Band 6; NIR = Band 5. Positive values correspond to impervious built-up surfaces and are strongly correlated with elevated land surface temperatures.'),
        ],
        'bsi': [
            ('BSI = [(SWIR + RED) - (NIR + BLUE)] / [(SWIR + RED) + (NIR + BLUE)]',
             'Eq. (1) -- Bare Soil Index (Rikimaru et al., 2002). BLUE = Band 2; RED = Band 4; NIR = Band 5; SWIR = Band 6. Higher values indicate absence of vegetation or surface moisture and are used to delineate exposed bare land.'),
        ],
        'lulc': [
            ('NDVI = (NIR - RED) / (NIR + RED)',
             'Eq. (1) -- NDVI used as auxiliary spectral feature alongside Landsat 8 multispectral bands for LULC classification. Supervised classification (Random Forest or Maximum Likelihood) applied within Google Earth Engine on cloud-masked, median-composited imagery.'),
        ],
    }

    # ── Figure description prose (natural, journal-quality) ───────────────────
    FIG_DESC = {
        'analysis_map': {
            'lst':
                'Spatial distribution of LST (°C) across {region}. Elevated thermal signatures '
                'in densely built-up zones reflect the concentration of impervious surfaces that absorb '
                'and re-emit solar radiation, while cooler pockets correspond to vegetated areas and water '
                'bodies where evapotranspiration moderates surface heating. The spatial pattern is '
                'consistent with a well-developed urban heat island structure.',
            'ndvi':
                'NDVI distribution across {region}. High positive values (green tones) indicate dense '
                'vegetation cover, while low or negative values (orange to red) mark built-up surfaces, '
                'bare soil, and paved areas. Pronounced spatial contrasts between green corridors and the '
                'impervious urban matrix are clearly visible.',
            'uhi':
                'Urban Heat Island intensity distribution for {region}. Positive UHI anomalies are '
                'concentrated in high-density commercial and industrial zones with minimal vegetation cover, '
                'while negative or near-zero values are associated with parks, water bodies, and peri-urban '
                'vegetated fringes that exert a measurable cooling influence.',
            'ndwi':
                'NDWI distribution across {region}. Positive values (blue tones) delineate surface water '
                'bodies and wetland areas; negative values characterize dry urban surfaces. Water features '
                'exert a localized cooling influence on surrounding pixels, moderating thermal extremes.',
            'ndbi':
                'NDBI values across {region}. Positive index values coincide with dense built-up areas '
                'and exhibit a strong positive correlation with elevated LST. Negative values are '
                'concentrated in vegetated zones where NIR reflectance exceeds SWIR.',
            'lulc':
                'LULC classification of {region} derived from Landsat 8 multi-spectral imagery. '
                'The dominant classes reveal the prevailing surface composition — built-up land, '
                'vegetation, bare soil, and water — that drives the observed spatial patterns '
                'of thermal and spectral variables.',
            'default':
                'Spatial distribution of {var} across {region}. Color gradients reflect the range '
                'of retrieved values, highlighting spatial heterogeneity driven by differences in '
                'land cover, surface moisture, and urban morphology.',
        },
        'monthly': {
            'lst':
                'Monthly mean LST (°C) for {region} over the study period. Peak temperatures '
                'in dry-season months reflect reduced soil moisture, sparse vegetation, and '
                'intensified solar radiation loading on impervious surfaces. The wet season brings '
                'a marked thermal depression driven by cloud cover, increased latent heat flux, and '
                'active vegetation. The intra-annual range illustrates the sensitivity of the urban '
                'thermal environment to monsoon-driven hydro-climatic forcing.',
            'ndvi':
                'Monthly mean NDVI for {region}. Vegetation greenness peaks during and immediately '
                'after the wet season when soil moisture is sufficient to sustain photosynthetic activity, '
                'and declines sharply in dry months as plant water stress reduces reflectance in the '
                'near-infrared. The seasonal cycle closely tracks the monsoon rainfall pattern.',
            'uhi':
                'Monthly UHI intensity for {region}. UHI magnitude is greatest during dry, cloud-free '
                'months when differential solar loading between impervious and vegetated surfaces is '
                'maximized. Wet season reductions reflect suppressed daytime radiation and increased '
                'evapotranspiration from vegetation and wet surfaces.',
            'default':
                'Monthly mean {var} values for {region} over the study period. Seasonal peaks and '
                'troughs reflect the combined influence of precipitation patterns, solar radiation '
                'seasonality, and land surface energy balance dynamics on the retrieved index.',
        },
        'class_bar': {
            'lst':
                'Areal distribution of LST across discrete temperature classes for {region}. '
                'The dominance of the Hot (40–45°C) class confirms persistently elevated thermal '
                'conditions across the majority of the urban surface. The Extreme class (>45°C) '
                'represents highly impervious zones — commercial rooftops and industrial areas — '
                'while the Cool class (<30°C) is restricted to water bodies and dense vegetation patches.',
            'lulc':
                'Proportional area coverage of LULC classes within {region}. The distribution '
                'quantifies the relative spatial extent of each surface type and provides a basis '
                'for interpreting patterns of LST and spectral indices across the study area.',
            'default':
                'Proportional distribution of {var} across classification categories for {region}. '
                'Dominant classes indicate prevailing environmental conditions and allow comparison '
                'with reference thresholds from similar urban settings.',
        },
        'histogram': {
            'lst':
                'Frequency distribution of LST pixel values across {region}. The approximately '
                'normal distribution centered near the mean with a moderate right tail reflects '
                'spatially extensive moderate-to-high thermal conditions across the urban surface. '
                'The dashed lines mark the 10th (p10) and 90th (p90) percentile boundaries, '
                'delineating the thermal interquartile range of the study area.',
            'ndvi':
                'Frequency distribution of NDVI pixel values for {region}. A distribution skewed '
                'toward low-to-moderate values reflects the predominance of built-up and bare surfaces '
                'over vegetated land, consistent with a densely urbanized environment.',
            'default':
                'Frequency distribution of {var} pixel values across {region}. Distribution shape '
                'and spread characterize spatial variability and identify the prevalence of extreme '
                'values within the study domain.',
        },
        'trend': {
            'lst':
                'Monthly mean LST (\u00b0C) for {region} over the study period. Peak temperatures '
                'in dry-season months (April\u2013August) reflect reduced soil moisture, sparse '
                'vegetation, and intensified solar radiation loading on impervious surfaces. '
                'The wet season brings a marked thermal depression driven by increased cloud cover, '
                'latent heat flux from active vegetation, and reduced net shortwave radiation. '
                'The pronounced intra-annual range underscores the sensitivity of the urban '
                'thermal environment to monsoon-driven hydro-climatic forcing.',
            'ndvi':
                'Monthly mean NDVI for {region}. Vegetation greenness peaks during and immediately '
                'after the wet season when soil moisture sustains photosynthetic activity, and '
                'declines sharply in dry months as plant water stress suppresses near-infrared '
                'reflectance. The seasonal cycle closely tracks the regional monsoon rainfall pattern.',
            'uhi':
                'Monthly UHI intensity for {region}. UHI magnitude is greatest during dry, '
                'cloud-free months when differential solar loading between impervious and '
                'vegetated surfaces is maximized. Wet season reductions reflect suppressed '
                'daytime radiation and increased evapotranspiration from vegetation and wet surfaces.',
            'default':
                'Monthly mean {var} for {region} over the study period. Seasonal peaks and '
                'troughs reflect the combined influence of precipitation patterns, solar radiation '
                'seasonality, and land surface energy balance dynamics on the retrieved index.',
        },
        'monthly_trend': {
            'lst':
                'Monthly mean LST (\u00b0C) for {region} over the study period. Peak temperatures '
                'in dry-season months (April\u2013August) reflect reduced soil moisture, sparse '
                'vegetation, and intensified solar radiation loading on impervious surfaces. '
                'The wet season brings a marked thermal depression driven by increased cloud cover, '
                'latent heat flux from active vegetation, and reduced net shortwave radiation. '
                'The pronounced intra-annual range underscores the sensitivity of the urban '
                'thermal environment to monsoon-driven hydro-climatic forcing.',
            'ndvi':
                'Monthly mean NDVI for {region}. Vegetation greenness peaks during and immediately '
                'after the wet season when soil moisture sustains photosynthetic activity, and '
                'declines sharply in dry months as plant water stress suppresses near-infrared '
                'reflectance. The seasonal cycle closely tracks the regional monsoon rainfall pattern.',
            'uhi':
                'Monthly UHI intensity for {region}. UHI magnitude is greatest during dry, '
                'cloud-free months when differential solar loading between impervious and '
                'vegetated surfaces is maximized. Wet season reductions reflect suppressed '
                'daytime radiation and increased evapotranspiration from vegetation and wet surfaces.',
            'default':
                'Monthly mean {var} for {region} over the study period. Seasonal peaks and '
                'troughs reflect the combined influence of precipitation patterns, solar radiation '
                'seasonality, and land surface energy balance dynamics on the retrieved index.',
        },
        'seasonal': {
            'default':
                'Intra-annual seasonal pattern of {var} for {region}. The cycle is governed by '
                'monsoon dynamics, solar declination angle, and land surface energy balance interactions.',
        },
    }

    def get_fig_desc(chart_type, var_key, region):
        type_dict = FIG_DESC.get(chart_type, {})
        tmpl = type_dict.get(var_key.lower(), type_dict.get('default', ''))
        if not tmpl:
            return ''
        return tmpl.format(var=var_key.upper(), region=region)

    # ── Figures from GIS Agent ─────────────────────────────────────────────────
    figures = sections.get('figures', {})
    region  = sections.get('region', '')

    def get_fig(var_key, field):
        v = figures.get(var_key, {})
        return v.get(field) if isinstance(v, dict) else None

    # ── STORY ──────────────────────────────────────────────────────────────────
    story = []

    # ── Title block ────────────────────────────────────────────────────────────
    story.append(Spacer(1, 0.4*cm))
    story.append(Paragraph(_esc(sections.get('title', 'Research Paper')), S['title']))
    story.append(Spacer(1, 0.15*cm))
    story.append(hr(BLACK, 0.8))   # single black rule under title, like the reference paper
    story.append(Spacer(1, 0.05*cm))
    story.append(Paragraph(
        f"Region: <b>{_esc(region)}</b> &nbsp;&nbsp;|&nbsp;&nbsp; "
        f"Period: <b>{_esc(sections.get('date_range',''))}</b> &nbsp;&nbsp;|&nbsp;&nbsp; "
        f"Variables: <b>{_esc(sections.get('variables',''))}</b>",
        S['meta']))
    story.append(Paragraph(
        f"Generated by GIS AI Agent &nbsp;&nbsp;&middot;&nbsp;&nbsp; {_esc(sections.get('generated_date',''))}",
        S['meta']))
    story.append(Spacer(1, 0.15*cm))
    story.append(hr())   # thin gray rule

    # ── Abstract ───────────────────────────────────────────────────────────────
    story.append(Paragraph('Abstract', S['h1']))
    for para in sections.get('abstract', '').strip().split('\n'):
        para = para.strip()
        if para:
            story.append(Paragraph(para, S['body']))

    # ── Keywords ───────────────────────────────────────────────────────────────
    vars_list = sections.get('variables', '')
    kw_parts  = ['Remote sensing', 'Google Earth Engine', region] + [v.strip() for v in vars_list.split(',') if v.strip()]
    kw_unique = list(dict.fromkeys(kw_parts))[:7]
    story.append(Paragraph(f'<b>Keywords:</b> {_esc("; ".join(kw_unique))}', S['fig_desc']))
    story.append(hr())

    # ── Page break ─────────────────────────────────────────────────────────────
    story.append(PageBreak())

    # ── 1. Introduction ────────────────────────────────────────────────────────
    story.append(Paragraph('1. Introduction', S['h1']))
    story += section_body(sections.get('introduction', ''))

    # ── 2. Study Area ──────────────────────────────────────────────────────────
    first_var = next(iter(figures), None)
    rgb_b64   = get_fig(first_var, 'rgb_overview') if first_var else None
    if rgb_b64:
        story.append(Paragraph('2. Study Area', S['h1']))
        story.append(Paragraph(
            f'{_esc(region)} was selected as the study area. Satellite imagery from Landsat 8 '
            f'Collection 2 Level-2, processed on Google Earth Engine (GEE), served as the primary '
            f'data source. The true color RGB composite (Fig. 1) illustrates the spatial extent '
            f'and general land cover composition of the region.',
            S['body']))
        story += embed_image(rgb_b64,
            caption=f'Fig. 1. True color RGB composite of {_esc(region)} (Landsat 8, Bands 4-3-2).',
            max_h=8.0)
        story.append(Paragraph(
            f'True color composite of {_esc(region)} delineating the urban extent, major road '
            f'networks, vegetation patches, and water bodies that govern the spatial distribution '
            f'of retrieved spectral and thermal variables.', S['fig_desc']))

    # ── 3. Methodology ─────────────────────────────────────────────────────────
    story.append(Paragraph('3. Methodology', S['h1']))
    story += section_body(sections.get('methodology', ''))

    # ── 3.1 Index formulae + satellite data table ──────────────────────────────
    vars_analyzed = [v.strip().lower() for v in vars_list.split(',') if v.strip()]
    formula_added = False
    for var in vars_analyzed:
        if var in FORMULAS:
            if not formula_added:
                story.append(Paragraph('3.1 Data Sources and Satellite Characteristics', S['h2']))
                # Satellite data table
                tbl_data = [
                    ['Satellite / Sensor', 'Spatial Res.', 'Revisit', 'Bands Used', 'Purpose'],
                    ['Landsat 8 OLI/TIRS\n(Collection 2 Level-2)', '30 m (optical)\n100 m (thermal)', '16 days',
                     'B2-B7 (optical)\nB10 (thermal)', 'Surface indices, LST'],
                    ['Landsat 9 OLI-2/TIRS-2\n(Collection 2 Level-2)', '30 m (optical)\n100 m (thermal)', '16 days',
                     'B2-B7 (optical)\nB10 (thermal)', 'Surface indices, LST'],
                    ['Sentinel-5P TROPOMI', '5.5 × 3.5 km', 'Daily',
                     'UV-NIR-SWIR\nmulti-band', 'Atmospheric variables'],
                    ['Google Earth Engine', 'Cloud platform', 'Real-time',
                     'GEE API', 'Processing & compositing'],
                ]
                col_w = [3.8*cm, 2.5*cm, 1.8*cm, 3.2*cm, 3.5*cm]
                tbl = Table(tbl_data, colWidths=col_w, repeatRows=1)
                tbl.setStyle(TableStyle([
                    ('FONTNAME',    (0,0), (-1,0),  'Helvetica-Bold'),
                    ('FONTSIZE',    (0,0), (-1,-1), 8),
                    ('LEADING',     (0,0), (-1,-1), 11),
                    ('BACKGROUND',  (0,0), (-1,0),  colors.HexColor('#eeeeee')),
                    ('TEXTCOLOR',   (0,0), (-1,-1), colors.HexColor('#111111')),
                    ('ALIGN',       (0,0), (-1,-1), 'LEFT'),
                    ('VALIGN',      (0,0), (-1,-1), 'TOP'),
                    ('GRID',        (0,0), (-1,-1), 0.4, colors.HexColor('#aaaaaa')),
                    ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f7f7f7')]),
                    ('TOPPADDING',  (0,0), (-1,-1), 4),
                    ('BOTTOMPADDING',(0,0), (-1,-1), 4),
                    ('LEFTPADDING', (0,0), (-1,-1), 5),
                    ('RIGHTPADDING',(0,0), (-1,-1), 5),
                ]))
                story.append(Spacer(1, 0.1*cm))
                story.append(tbl)
                story.append(Paragraph(
                    'Table 1. Characteristics of satellite datasets used in this study.',
                    S['caption']))

                # Preprocessing narrative
                story.append(Paragraph('3.2 Preprocessing and Index Computation', S['h2']))
                story.append(Paragraph(
                    'All satellite imagery was accessed and processed within the Google Earth Engine '
                    '(GEE) cloud computing platform. Cloud and cloud-shadow masking was performed '
                    'using the QA_PIXEL band (CFMask algorithm) for Landsat Collection 2 products, '
                    'retaining only pixels with clear-sky confidence. A median composite was '
                    'generated from all valid observations within the study period to produce a '
                    'spatially continuous, cloud-free surface reflectance and brightness temperature '
                    'dataset at 30 m resolution. Radiometric calibration applied the official USGS '
                    'scaling factors (scale = 0.0000275, offset = -0.2) to convert digital numbers '
                    'to surface reflectance. Thermal Band 10 DN values were converted to at-sensor '
                    'spectral radiance using the multiplicative and additive rescaling coefficients '
                    'from the Level-2 metadata file prior to brightness temperature derivation. '
                    'The following spectral indices were computed from the processed composites:',
                    S['body']))
                formula_added = True
            for eq_str, eq_desc in FORMULAS[var]:
                story.append(Paragraph(_esc(eq_str), S['formula']))
                story.append(Paragraph(_esc(eq_desc), S['formula_label']))

    # ── 4. Results ─────────────────────────────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph('4. Results', S['h1']))
    story += section_body(sections.get('results', ''))

    fig_counter = 2
    for idx, (var_key, fig_data) in enumerate(figures.items(), start=1):
        if not isinstance(fig_data, dict):
            continue
        var_label = var_key.upper()
        story.append(Paragraph(f'4.{idx} {var_label} Analysis', S['h2']))

        amap = fig_data.get('analysis_map')
        if amap:
            desc = get_fig_desc('analysis_map', var_key, region)
            story += embed_image(amap,
                caption=f'Fig. {fig_counter}. Spatial distribution of {var_label} across {_esc(region)}.',
                max_h=8.0)
            if desc:
                story.append(Paragraph(_esc(desc), S['fig_desc']))
            fig_counter += 1

        for chart_type, chart_b64 in fig_data.get('charts', []):
            label_map = {
                'monthly_trend': f'Monthly mean {var_label} for {_esc(region)}.',
                'monthly'      : f'Monthly mean {var_label} for {_esc(region)}.',
                'class_bar'    : f'Areal distribution of {var_label} by classification category for {_esc(region)}.',
                'lulc_pie'     : f'LULC class composition for {_esc(region)}.',
                'trend'        : f'Temporal trend of {var_label} for {_esc(region)}.',
                'seasonal'     : f'Seasonal pattern of {var_label} for {_esc(region)}.',
                'histogram'    : f'Frequency distribution of {var_label} pixel values across {_esc(region)}.',
            }
            cap = label_map.get(chart_type, f'{var_label} — {chart_type.replace("_"," ").title()} for {_esc(region)}.')
            story += embed_image(chart_b64,
                caption=f'Fig. {fig_counter}. {cap}',
                max_h=5.8)
            desc = get_fig_desc(chart_type, var_key, region)
            if desc:
                story.append(Paragraph(_esc(desc), S['fig_desc']))
            fig_counter += 1

    # ── 5. Discussion ──────────────────────────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph('5. Discussion', S['h1']))
    story += section_body(sections.get('discussion', ''))

    # ── 6. Conclusion ──────────────────────────────────────────────────────────
    story.append(Paragraph('6. Conclusion', S['h1']))
    story += section_body(sections.get('conclusion_section', ''))

    # ── References ─────────────────────────────────────────────────────────────
    refs = sections.get('references', [])
    if refs:
        story.append(Paragraph('References', S['h1']))
        for i, ref in enumerate(refs, 1):
            story.append(Paragraph(f'[{i}] {_esc(str(ref))}', S['ref']))

    # ── Build ──────────────────────────────────────────────────────────────────
    doc.build(story)
    return True



def _esc(text: str) -> str:
    """Escape special XML chars for reportlab Paragraph."""
    return (str(text)
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;'))


# ── Main entry point ───────────────────────────────────────────────────────────

def generate_research_paper(analysis_result: dict, output_dir: str) -> 'Optional[str]':
    """
    Main entry point. Accepts the GIS Agent result dict and generates a PDF.

    Parameters
    ----------
    analysis_result : dict
        The job['result'] dict from app.py.
    output_dir : str
        Directory where the PDF will be saved.

    Returns
    -------
    str or None
        Absolute path to the generated .pdf file, or None on failure.
    """
    try:
        from config import OLLAMA_URL, OLLAMA_MODEL
    except ImportError:
        OLLAMA_URL   = 'http://localhost:11434/api/chat'
        OLLAMA_MODEL = 'llama3'

    region      = analysis_result.get('region', 'Unknown Region')
    start_date  = analysis_result.get('start_date', '')
    end_date    = analysis_result.get('end_date', '')
    variables   = analysis_result.get('variables', [])
    all_stats   = analysis_result.get('stats', {})
    var_insights= analysis_result.get('var_insights', {})
    conclusion  = analysis_result.get('conclusion', '')
    web_context = analysis_result.get('web_context', '')

    date_range  = _format_date_range(start_date, end_date)
    var_str     = ', '.join(v.upper() for v in variables)
    generated   = datetime.datetime.now().strftime('%B %d, %Y')

    print(f'[research_agent] Generating paper for {region} | {date_range}')
    print(f'[research_agent] Variables: {var_str}')
    print(f'[research_agent] Calling Ollama for each section...')

    # ── Generate all sections via Ollama ──────────────────────────────────────
    title = gen_title(region, variables, date_range, OLLAMA_URL, OLLAMA_MODEL)
    print(f'  ✓ Title: {title[:60]}...')

    abstract = gen_abstract(region, variables, date_range, all_stats, conclusion,
                            OLLAMA_URL, OLLAMA_MODEL)
    print(f'  ✓ Abstract ({len(abstract.split())} words)')

    introduction = gen_introduction(region, variables, date_range, web_context,
                                    OLLAMA_URL, OLLAMA_MODEL)
    print(f'  ✓ Introduction ({len(introduction.split())} words)')

    methodology = gen_methodology(region, variables, start_date, end_date,
                                  OLLAMA_URL, OLLAMA_MODEL)
    print(f'  ✓ Methodology ({len(methodology.split())} words)')

    results = gen_results(region, variables, date_range, all_stats, var_insights,
                          OLLAMA_URL, OLLAMA_MODEL)
    print(f'  ✓ Results ({len(results.split())} words)')

    discussion = gen_discussion(region, variables, date_range, all_stats, web_context,
                                OLLAMA_URL, OLLAMA_MODEL)
    print(f'  ✓ Discussion ({len(discussion.split())} words)')

    conclusion_section = gen_conclusion_section(region, variables, date_range, conclusion,
                                                OLLAMA_URL, OLLAMA_MODEL)
    print(f'  ✓ Conclusion ({len(conclusion_section.split())} words)')

    references = gen_references(variables, OLLAMA_URL, OLLAMA_MODEL)
    print(f'  ✓ References ({len(references)} entries)')

    # ── Assemble sections dict (include raw figures for PDF embedding) ───────────
    figures = analysis_result.get('figures', {})
    print(f'[research_agent] Figures available: {list(figures.keys())}')
    sections = {
        'title'             : title,
        'region'            : region,
        'date_range'        : date_range,
        'variables'         : var_str,
        'generated_date'    : generated,
        'abstract'          : abstract,
        'introduction'      : introduction,
        'methodology'       : methodology,
        'results'           : results,
        'discussion'        : discussion,
        'conclusion_section': conclusion_section,
        'references'        : references,
        'figures'           : figures,   # b64 maps + charts from GIS Agent
    }

    # ── Build PDF ─────────────────────────────────────────────────────────────
    os.makedirs(output_dir, exist_ok=True)
    safe_region = region.replace(' ', '_').replace('/', '-')[:30]
    safe_vars   = '_'.join(v.upper() for v in variables[:3])
    timestamp   = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    filename    = f'research_{safe_region}_{safe_vars}_{timestamp}.pdf'
    output_path = str(Path(output_dir) / filename)

    print(f'[research_agent] Building PDF: {filename}')
    success = _build_pdf(sections, output_path)

    if success and Path(output_path).exists():
        size_kb = Path(output_path).stat().st_size // 1024
        print(f'[research_agent] ✓ PDF ready: {output_path} ({size_kb} KB)')
        return output_path
    else:
        print(f'[research_agent] ✗ PDF build failed')
        return None