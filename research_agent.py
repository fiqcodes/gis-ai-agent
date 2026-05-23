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
        )
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_JUSTIFY
    except ImportError as e:
        print(f'[research_agent] reportlab import error: {e}')
        print('[research_agent] HINT: pip install reportlab')
        return False

    # ── Page layout ────────────────────────────────────────────────────────────
    PAGE_W = A4[0]
    doc = SimpleDocTemplate(
        output_path,
        pagesize=A4,
        leftMargin=2.2*cm, rightMargin=2.2*cm,
        topMargin=2.8*cm,  bottomMargin=2.5*cm,
        title=sections.get('title', 'Research Paper'),
        author='GIS AI Agent',
    )
    IMG_W_CM = (PAGE_W / cm) - 4.4

    # ── Styles ─────────────────────────────────────────────────────────────────
    BASE  = getSampleStyleSheet()
    DARK  = colors.HexColor('#1a1a2e')
    BLUE  = colors.HexColor('#2563eb')
    GRAY  = colors.HexColor('#6b7280')
    LGRAY = colors.HexColor('#e5e7eb')
    BGRAY = colors.HexColor('#f8f9fa')

    def style(name, **kw):
        return ParagraphStyle(name, parent=BASE['Normal'], **kw)

    S = {
        'title': style('T',
            fontSize=20, leading=26, textColor=DARK,
            fontName='Helvetica-Bold', alignment=TA_CENTER, spaceAfter=8),
        'meta': style('M',
            fontSize=9, leading=13, textColor=GRAY,
            alignment=TA_CENTER, spaceAfter=4),
        'h1': style('H1',
            fontSize=13, leading=17, textColor=BLUE,
            fontName='Helvetica-Bold', spaceBefore=18, spaceAfter=6),
        'h2': style('H2',
            fontSize=11, leading=15, textColor=DARK,
            fontName='Helvetica-Bold', spaceBefore=10, spaceAfter=4),
        'body': style('B',
            fontSize=10, leading=15.5, textColor=DARK,
            alignment=TA_JUSTIFY, spaceAfter=8),
        'caption': style('CAP',
            fontSize=8.5, leading=12, textColor=GRAY,
            alignment=TA_CENTER, spaceAfter=4, spaceBefore=4),
        'fig_note': style('FN',
            fontSize=9, leading=13.5, textColor=colors.HexColor('#374151'),
            alignment=TA_JUSTIFY, spaceAfter=10, spaceBefore=2,
            leftIndent=6, rightIndent=6,
            backColor=BGRAY,
            borderPadding=(6, 8, 6, 8)),
        'ref': style('R',
            fontSize=9, leading=13, textColor=DARK,
            leftIndent=14, firstLineIndent=-14, spaceAfter=4),
        'abstract_box': style('AB',
            fontSize=10, leading=15.5, textColor=DARK,
            alignment=TA_JUSTIFY, spaceAfter=0,
            leftIndent=10, rightIndent=10,
            backColor=colors.HexColor('#eff6ff'),
            borderPadding=(10, 12, 10, 12)),
    }

    def hr(color=LGRAY, thickness=0.5):
        return HRFlowable(width='100%', thickness=thickness,
                          color=color, spaceAfter=8, spaceBefore=3)

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
        # Center the image
        img.hAlign = 'CENTER'
        items = [Spacer(1, 0.3*cm), img]
        if caption:
            items.append(Paragraph(f'<i>{_esc(caption)}</i>', S['caption']))
        return items

    def figure_note(text):
        """A shaded explanation box that appears after a figure."""
        if not text or not text.strip():
            return []
        return [
            Paragraph(_esc(text.strip()), S['fig_note']),
            Spacer(1, 0.25*cm),
        ]

    # ── Figure explanation texts ───────────────────────────────────────────────
    CHART_EXPLANATIONS = {
        'analysis_map': (
            'The map above shows the spatial distribution of {var} values across {region}. '
            'Warmer colors indicate higher values, while cooler colors represent lower values. '
            'Spatial heterogeneity visible in the map reflects the influence of land cover type, '
            'built-up density, and vegetation coverage on the measured variable.'
        ),
        'monthly': (
            'This chart illustrates how {var} values varied month by month throughout the study period. '
            'Peaks and troughs correspond to seasonal changes in weather patterns, vegetation cycles, '
            'and anthropogenic activity. Elevated values during dry months may indicate heat stress '
            'or reduced vegetation cover, while lower values often coincide with wet season conditions.'
        ),
        'class_bar': (
            'The bar chart above shows the areal distribution of {var} across discrete classification '
            'categories. Each bar represents the percentage of the study area falling within a '
            'particular value range. Dominant classes provide insight into the prevailing environmental '
            'conditions and allow comparison with typical reference values for similar urban regions.'
        ),
        'trend': (
            'This trend chart captures the temporal trajectory of {var} over the observation period. '
            'A rising trend may signal progressive environmental degradation or urban expansion, '
            'while a declining trend could reflect mitigation efforts or seasonal recovery. '
            'Significant fluctuations may correspond to episodic events such as heatwaves or heavy rainfall.'
        ),
        'seasonal': (
            'The seasonal pattern chart reveals the intra-annual rhythm of {var} in {region}. '
            'These cycles are driven by monsoon dynamics, solar radiation angle, and land surface '
            'energy balance. Understanding seasonal patterns is essential for designing effective '
            'monitoring protocols and targeted urban heat mitigation interventions.'
        ),
        'histogram': (
            'The histogram displays the statistical frequency distribution of {var} pixel values '
            'across the study area. A narrow, bell-shaped distribution indicates spatial uniformity, '
            'while a broad or skewed distribution suggests strong spatial variability — often linked '
            'to contrasting land cover types such as dense urban core versus peri-urban green spaces.'
        ),
    }

    VAR_MAP_EXPLANATIONS = {
        'lst': 'elevated land surface temperatures confirm a strong urban heat island effect, '
               'with impervious surfaces retaining and re-emitting absorbed solar radiation.',
        'ndvi': 'vegetation density variations reflect the spatial contrast between green corridors, '
                'parks, and bare or built-up surfaces across the study area.',
        'uhi': 'urban heat island intensity highlights thermal disparities between the dense urban '
               'core and surrounding peri-urban or vegetated zones.',
        'lulc': 'land use and land cover patterns reveal the dominant surface types driving '
                'observed thermal and spectral variability in the region.',
        'ndbi': 'built-up index values delineate the extent of urbanized surfaces and their '
                'spatial correlation with elevated surface temperatures.',
        'ndwi': 'water body distribution and moisture content of surface features are captured, '
                'showing cooling influence on adjacent urban areas.',
    }

    def get_map_explanation(var_key, region):
        base = CHART_EXPLANATIONS['analysis_map']
        suffix = VAR_MAP_EXPLANATIONS.get(var_key.lower(), '')
        text = base.format(var=var_key.upper(), region=region)
        if suffix:
            text += f' For {var_key.upper()}, {suffix}'
        return text

    def get_chart_explanation(chart_type, var_key, region):
        tmpl = CHART_EXPLANATIONS.get(chart_type, '')
        if not tmpl:
            return ''
        return tmpl.format(var=var_key.upper(), region=region)

    # ── Figures from the GIS Agent ─────────────────────────────────────────────
    figures  = sections.get('figures', {})
    region   = sections.get('region', '')

    def get_fig(var_key, field):
        v = figures.get(var_key, {})
        return v.get(field) if isinstance(v, dict) else None

    # ── Story ──────────────────────────────────────────────────────────────────
    story = []

    # ── Cover / Title block ────────────────────────────────────────────────────
    story.append(Spacer(1, 0.6*cm))
    story.append(Paragraph(_esc(sections.get('title', 'Research Paper')), S['title']))
    story.append(Spacer(1, 0.2*cm))
    story.append(Paragraph(
        f"Region: <b>{_esc(region)}</b> &nbsp;|&nbsp; "
        f"Period: <b>{_esc(sections.get('date_range',''))}</b> &nbsp;|&nbsp; "
        f"Variables: <b>{_esc(sections.get('variables',''))}</b>",
        S['meta']))
    story.append(Paragraph(
        f"Generated by GIS AI Agent &nbsp;·&nbsp; {_esc(sections.get('generated_date',''))}",
        S['meta']))
    story.append(Spacer(1, 0.3*cm))
    story.append(hr(BLUE, 1.5))
    story.append(Spacer(1, 0.2*cm))

    # ── Abstract (shaded box, no map yet) ──────────────────────────────────────
    story.append(Paragraph('Abstract', S['h1']))
    story.append(hr())
    abstract_text = sections.get('abstract', '').strip()
    for para in abstract_text.split('\n'):
        para = para.strip()
        if para:
            story.append(Paragraph(para, S['abstract_box']))
    story.append(Spacer(1, 0.4*cm))

    # ── Page break → body starts fresh ────────────────────────────────────────
    story.append(PageBreak())

    # ── Study area overview map (now on page 2, properly introduced) ───────────
    first_var = next(iter(figures), None)
    rgb_b64   = get_fig(first_var, 'rgb_overview') if first_var else None
    if rgb_b64:
        story.append(Paragraph('Study Area', S['h1']))
        story.append(hr())
        story.append(Paragraph(
            f'The study area encompasses {_esc(region)}, analyzed using satellite-derived '
            f'data processed on the Google Earth Engine (GEE) cloud computing platform. '
            f'The true color composite below provides spatial context for interpreting '
            f'the analysis results presented in subsequent sections.',
            S['body']))
        story += embed_image(rgb_b64,
            caption=f'Figure 1. Study Area Overview — {_esc(region)} (True Color RGB)',
            max_h=8.0)
        story += figure_note(
            f'Figure 1 shows a true color RGB composite of {_esc(region)} derived from '
            f'Landsat 8 satellite imagery. The image provides a baseline view of the urban '
            f'landscape, vegetation distribution, and water bodies that influence the '
            f'environmental variables analyzed in this study.'
        )
        story.append(Spacer(1, 0.2*cm))

    # ── Introduction ───────────────────────────────────────────────────────────
    story.append(Paragraph('Introduction', S['h1']))
    story.append(hr())
    story += section_body(sections.get('introduction', ''))

    # ── Methodology ────────────────────────────────────────────────────────────
    story.append(Paragraph('Methodology', S['h1']))
    story.append(hr())
    story += section_body(sections.get('methodology', ''))

    # ── Results — text then figures, each figure followed by explanation ────────
    story.append(PageBreak())
    story.append(Paragraph('Results', S['h1']))
    story.append(hr())
    story += section_body(sections.get('results', ''))
    story.append(Spacer(1, 0.3*cm))

    fig_counter = 2
    for var_key, fig_data in figures.items():
        if not isinstance(fig_data, dict):
            continue
        var_label = var_key.upper()

        # Sub-heading per variable
        story.append(Paragraph(f'{var_label} Analysis', S['h2']))

        # Analysis map + explanation
        amap = fig_data.get('analysis_map')
        if amap:
            story += embed_image(amap,
                caption=f'Figure {fig_counter}. {var_label} Analysis Map — {_esc(region)}',
                max_h=8.5)
            story += figure_note(get_map_explanation(var_key, region))
            fig_counter += 1

        # Charts — each with an explanation
        for chart_type, chart_b64 in fig_data.get('charts', []):
            label_map = {
                'monthly'  : f'{var_label} — Monthly Mean',
                'class_bar': f'{var_label} — Class Distribution',
                'trend'    : f'{var_label} — Trend Analysis',
                'seasonal' : f'{var_label} — Seasonal Pattern',
                'histogram': f'{var_label} — Value Distribution',
            }
            caption_text = label_map.get(chart_type,
                f'{var_label} — {chart_type.replace("_"," ").title()}')
            story += embed_image(chart_b64,
                caption=f'Figure {fig_counter}. {caption_text}',
                max_h=6.5)
            explanation = get_chart_explanation(chart_type, var_key, region)
            if explanation:
                story += figure_note(explanation)
            fig_counter += 1

    # ── Discussion ─────────────────────────────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph('Discussion', S['h1']))
    story.append(hr())
    story += section_body(sections.get('discussion', ''))

    # ── Conclusion ─────────────────────────────────────────────────────────────
    story.append(Paragraph('Conclusion', S['h1']))
    story.append(hr())
    story += section_body(sections.get('conclusion_section', ''))

    # ── References ─────────────────────────────────────────────────────────────
    refs = sections.get('references', [])
    if refs:
        story.append(Paragraph('References', S['h1']))
        story.append(hr())
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