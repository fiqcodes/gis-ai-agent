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
                lines.append(f'  UHI: LST mean={s["lst_mean"]:.2f}deg C, std={s["lst_std"]:.2f}deg C')
            elif 'classes' in s:
                top = sorted(s['classes'].items(), key=lambda x: -x[1].get('percentage', 0))[:4]
                top_str = ', '.join(f'{k} {v["percentage"]:.1f}%' for k, v in top)
                lines.append(f'  LULC: top classes - {top_str}')
    return '\n'.join(lines) if lines else 'No statistics available.'


def _format_date_range(start_date: str, end_date: str) -> str:
    try:
        sy, sm, sd = start_date.split('-')
        ey, em, ed = end_date.split('-')
        months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
        sm_name = months[int(sm) - 1]
        em_name = months[int(em) - 1]
        if sy == ey:
            return f'{sm_name}-{em_name} {sy}'
        return f'{sm_name} {sy} - {em_name} {ey}'
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
        data_sources.append('ESA WorldCover 2021 and ESRI Land Cover 2023 for stratified reference sampling')

    var_str = ', '.join(v.upper() for v in variables)

    # ── LULC-specific methodology prompt ──────────────────────────────────────
    if lulc_vars and not surface_vars and not atmo_vars:
        prompt = (
            f'Write a detailed Methodology section (500-550 words) for a satellite remote sensing '
            f'LULC classification paper.\n'
            f'Study area: {region} | Analysis period: {start_date} to {end_date}\n'
            f'Data sources: {"; ".join(data_sources)}\n'
            f'Platform: Google Earth Engine (GEE) cloud computing\n\n'
            f'The methodology MUST cover these steps in flowing prose paragraphs:\n'
            f'1. Study area and temporal scope\n'
            f'2. Data acquisition: Landsat 8 and Landsat 9 Collection 2 Level-2 multi-spectral bands '
            f'(B2-B7) merged into a single collection; reference ground-truth from ESA WorldCover 2021 '
            f'(10 m global) remapped from original ESA class IDs (10=Trees, 20=Shrubland, 30=Grassland, '
            f'40=Cropland, 50=Built-up, 60=Bare/Sparse, 70=Snow/Ice, 80=Water, 90=Wetland, '
            f'95=Mangroves, 100=MossLichen) to ESRI-compatible class IDs '
            f'(1=Water, 2=Trees, 4=Flooded Veg, 5=Crops, 7=Built Area, 8=Bare Ground, 11=Rangeland)\n'
            f'3. Preprocessing: cloud and cloud-shadow masking using QA_PIXEL CFMask algorithm, '
            f'median compositing, radiometric scaling (scale=0.0000275, offset=-0.2)\n'
            f'4. Feature engineering: a 13-feature input stack combining 6 raw Landsat spectral bands '
            f'(SR_B2 Blue, SR_B3 Green, SR_B4 Red, SR_B5 NIR, SR_B6 SWIR1, SR_B7 SWIR2) with 7 derived '
            f'spectral indices: NDVI=(NIR-RED)/(NIR+RED), NDBI=(SWIR1-NIR)/(SWIR1+NIR), '
            f'NDWI=(Green-NIR)/(Green+NIR), MNDWI=(Green-SWIR1)/(Green+SWIR1), '
            f'SAVI=[(NIR-RED)/(NIR+RED+0.5)]*1.5, '
            f'BSI=[(SWIR1+RED)-(NIR+Blue)]/[(SWIR1+RED)+(NIR+Blue)], '
            f'and SWIR_ratio=SR_B7/SR_B6. The SWIR2/SWIR1 ratio is a key discriminator: '
            f'bare soil and exposed surfaces have a distinctly higher SWIR2/SWIR1 ratio than '
            f'impervious built surfaces, enabling separation of Bare Ground from Built Area\n'
            f'5. Training sample generation: a pixel-presence check at 100 m scale (minimum 10 pixels) '
            f'was applied to exclude phantom classes; 500 samples per class were drawn at 30 m scale '
            f'via stratified random sampling from the ESA WorldCover reference layer; samples from all '
            f'classes were merged and split 80/20 into training (~396 samples) and test (~99 samples) sets\n'
            f'6. Random Forest classifier: 200 decision trees, bag fraction 0.5, minimum leaf population 1, '
            f'Gini impurity as splitting criterion, trained within GEE; majority-vote class assignment '
            f'across the full 13-feature input stack\n'
            f'7. Post-classification spectral override: pixels classified as Built Area but satisfying '
            f'brightness > 0.25, NDVI < 0.05, BSI > 0.05, and NIR < 0.20 were reclassified as Bare Ground '
            f'to correct for ESA WorldCover labelling of bright exposed surfaces as built-up\n'
            f'8. Accuracy assessment: overall accuracy, Cohen\'s Kappa, per-class precision '
            f'(user\'s accuracy), recall (producer\'s accuracy), F1 score, AUC, and confusion matrix\n\n'
            f'Academic passive voice. No bullet points. No markdown. No headers. Flowing prose only.'
        )
    else:
        prompt = (
            f'Write a Methodology section (300-350 words) for a satellite remote sensing paper.\n'
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

    lulc_vars = [v for v in variables if v.lower() == 'lulc']

    # ── LULC-specific results prompt ──────────────────────────────────────────
    if lulc_vars and not [v for v in variables if v.lower() != 'lulc']:
        lulc_stats   = all_stats.get('LULC', {})
        ml_metrics   = lulc_stats.get('ml_metrics', {})
        classes_data = lulc_stats.get('classes', {})

        # Summarize class areas
        class_lines = []
        for cls, info in classes_data.items():
            pct  = info.get('percentage', 0)
            ha   = info.get('area_ha', 0)
            class_lines.append(f'    {cls}: {pct:.1f}% ({ha:,.0f} ha)')
        class_summary = '\n'.join(class_lines) if class_lines else 'No class data.'

        # Summarize overall ML metrics
        oa      = ml_metrics.get('overall_accuracy', None)
        kap     = ml_metrics.get('kappa', None)
        apr     = ml_metrics.get('avg_precision', None)
        arc     = ml_metrics.get('avg_recall', None)
        af1     = ml_metrics.get('avg_f1', None)
        auc_val = ml_metrics.get('auc_approx', None)
        n_train = ml_metrics.get('n_train', None)
        n_test  = ml_metrics.get('n_test', None)
        n_total = ml_metrics.get('n_total', None)

        per_class_data = ml_metrics.get('per_class', {})
        per_class_lines = []
        avg_fpr_vals = []
        for cls_name, cls_m in per_class_data.items():
            p   = cls_m.get('precision', 0)
            r   = cls_m.get('recall', 0)
            f1  = cls_m.get('f1', 0)
            fpr = cls_m.get('fpr', 0)
            acc = cls_m.get('accuracy', 0)
            avg_fpr_vals.append(fpr)
            per_class_lines.append(
                f'    {cls_name}: Precision={p*100:.1f}%, Recall={r*100:.1f}%, '
                f'F1={f1*100:.1f}%, Accuracy={acc*100:.1f}%, FPR={fpr*100:.1f}%'
            )
        per_class_summary = '\n'.join(per_class_lines) if per_class_lines else 'No per-class data.'
        avg_fpr_computed = round(sum(avg_fpr_vals) / len(avg_fpr_vals), 4) if avg_fpr_vals else None

        ml_summary = ''
        if oa is not None:
            ml_summary = (
                f'  Overall accuracy: {oa*100:.1f}%, Kappa: {kap:.3f}, '
                f'Macro precision: {apr*100:.1f}%, Macro recall: {arc*100:.1f}%, '
                f'Macro F1: {af1*100:.1f}%'
            )
            if auc_val:
                ml_summary += f', AUC (approx.): {auc_val:.3f}'
            if avg_fpr_computed:
                ml_summary += f', Avg FPR: {avg_fpr_computed*100:.1f}%'
            if n_total:
                ml_summary += (
                    f'\n  Training samples: ~{n_train} (80%), Test samples: ~{n_test} (20%), '
                    f'Total: {n_total}'
                )

        prompt = (
            f'Write a Results section (550-650 words) for a satellite LULC classification paper.\n'
            f'Region: {region} | Period: {date_range}\n\n'
            f'LULC class composition:\n{class_summary}\n\n'
            f'Overall classification model performance:\n{ml_summary if ml_summary else "Metrics not available."}\n\n'
            f'Per-class performance:\n{per_class_summary}\n\n'
            f'Present results in two clearly separated parts:\n\n'
            f'(1) LULC composition (2-3 paragraphs): for each class report its percentage coverage '
            f'and exact area in hectares, describe its spatial distribution across {region}, and note '
            f'urban planning or environmental implications. Use all actual numbers.\n\n'
            f'(2) Classification accuracy assessment (2-3 paragraphs):\n'
            f'  - State the total sample count, training/test split, and number of classes.\n'
            f'  - Interpret the overall accuracy and Kappa coefficient with reference to published '
            f'    benchmarks (e.g. OA >85%% is considered good; Kappa >0.60 = substantial agreement).\n'
            f'  - Explain macro precision, recall, and F1 as overall classifier tendencies.\n'
            f'  - For EACH class, discuss its precision, recall, and F1 individually. Explain why '
            f'    classes with low precision but high recall suffer from commission errors, and why '
            f'    minority classes (Crops, Bare Ground) are harder to classify.\n'
            f'  - Mention the confusion matrix and what the largest off-diagonal misclassifications reveal.\n'
            f'  - Report AUC and what the average false positive rate implies for map reliability.\n\n'
            f'Reference ALL actual statistics above. Use exact numbers. Academic past tense. '
            f'Flowing prose. No bullet points. No headers. No placeholder text.'
        )
    else:
        prompt = (
            f'Write a Results section (350-450 words) for a satellite remote sensing paper.\n'
            f'Region: {region} | Period: {date_range} | Variables: {var_str}\n\n'
            f'Computed statistics:\n{stats_text}\n\n'
            f'Per-variable insights:\n{insights_text}\n\n'
            f'Present results clearly: for each variable, report the mean value with its unit, '
            f'the spatial range (p10-p90), and what the distribution pattern indicates. '
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

    lulc_vars = [v for v in variables if v.lower() == 'lulc']

    # ── LULC-specific discussion prompt ──────────────────────────────────────
    if lulc_vars and not [v for v in variables if v.lower() != 'lulc']:
        lulc_stats   = all_stats.get('LULC', {})
        ml_metrics   = lulc_stats.get('ml_metrics', {})
        classes_data = lulc_stats.get('classes', {})

        class_lines = []
        for cls, info in classes_data.items():
            pct = info.get('percentage', 0)
            ha  = info.get('area_ha', 0)
            class_lines.append(f'    {cls}: {pct:.1f}% ({ha:,.0f} ha)')
        class_summary = '\n'.join(class_lines) if class_lines else 'No class data.'

        oa      = ml_metrics.get('overall_accuracy', None)
        kap     = ml_metrics.get('kappa', None)
        apr     = ml_metrics.get('avg_precision', None)
        arc     = ml_metrics.get('avg_recall', None)
        af1     = ml_metrics.get('avg_f1', None)
        auc_val = ml_metrics.get('auc_approx', None)
        n_train = ml_metrics.get('n_train', None)
        n_test  = ml_metrics.get('n_test', None)

        per_class_data = ml_metrics.get('per_class', {})
        per_class_lines = []
        avg_fpr_vals = []
        for cls_name, cls_m in per_class_data.items():
            p   = cls_m.get('precision', 0)
            r   = cls_m.get('recall', 0)
            f1  = cls_m.get('f1', 0)
            fpr = cls_m.get('fpr', 0)
            avg_fpr_vals.append(fpr)
            per_class_lines.append(
                f'    {cls_name}: Precision={p*100:.1f}%, Recall={r*100:.1f}%, F1={f1*100:.1f}%, FPR={fpr*100:.1f}%'
            )
        per_class_summary = '\n'.join(per_class_lines) if per_class_lines else 'No per-class data.'
        avg_fpr_computed = round(sum(avg_fpr_vals) / len(avg_fpr_vals), 4) if avg_fpr_vals else None

        ml_summary = ''
        if oa is not None:
            ml_summary = (
                f'OA={oa*100:.1f}%, Kappa={kap:.3f}, Macro-P={apr*100:.1f}%, '
                f'Macro-R={arc*100:.1f}%, Macro-F1={af1*100:.1f}%'
            )
            if auc_val:
                ml_summary += f', AUC≈{auc_val:.3f}'
            if avg_fpr_computed:
                ml_summary += f', Avg FPR={avg_fpr_computed*100:.1f}%'
            if n_train:
                ml_summary += f', n_train≈{n_train}, n_test≈{n_test}'

        prompt = (
            f'Write a Discussion section (400-500 words) for a satellite LULC classification paper.\n'
            f'Region: {region} | Period: {date_range}\n'
            f'{web_section}\n'
            f'LULC class composition:\n{class_summary}\n\n'
            f'Overall model performance: {ml_summary if ml_summary else "Not available."}\n'
            f'Per-class performance:\n{per_class_summary}\n\n'
            f'The discussion must address these points in flowing academic prose:\n'
            f'1. Interpret the overall accuracy and Kappa in the context of typical LULC studies '
            f'(e.g., how does OA > 85% and Kappa > 0.75 compare to published benchmarks?)\n'
            f'2. Discuss the gap between macro precision and macro recall — high recall but low '
            f'precision often indicates over-prediction of minority classes (commission errors)\n'
            f'3. Identify the best-performing and worst-performing classes by F1 score and explain '
            f'why certain classes (e.g., Water, Bare Ground, Crops) may suffer from low precision '
            f'despite high recall — link to spectral confusion, class imbalance, or small sample sizes\n'
            f'4. Discuss what the AUC and average FPR imply for map reliability and practical usability\n'
            f'5. Discuss the LULC composition in terms of urban planning implications for {region} '
            f'— e.g., built area dominance, green space coverage, water body constraints\n'
            f'6. Acknowledge limitations: cloud cover effects on compositing, 30 m spatial resolution '
            f'limits fine-scale mapping, stratified sampling may underrepresent rare classes\n'
            f'7. Suggest future improvements: higher-resolution imagery, additional spectral features '
            f'(e.g., SAR), temporal classification to capture seasonal dynamics\n\n'
            f'Academic prose. No bullet points. No markdown. No headers.'
        )
    else:
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


def _generate_roc_curve_b64(ml_metrics: dict) -> 'Optional[str]':
    """
    Generate a ROC curve plot from per-class FPR/Recall data.
    Returns base64-encoded PNG string, or None on failure.
    """
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import numpy as np
        import io, base64

        per_class = ml_metrics.get('per_class', {})
        if not per_class:
            return None

        auc_val = ml_metrics.get('auc_approx', None)

        # Class color mapping (fallback to tab10)
        tab10 = plt.cm.get_cmap('tab10')
        class_colors_default = [tab10(i) for i in range(len(per_class))]

        fig, ax = plt.subplots(figsize=(6, 5))
        ax.set_facecolor('#f8f8f8')
        fig.patch.set_facecolor('white')

        # Diagonal reference line
        ax.plot([0, 1], [0, 1], 'k--', lw=0.8, alpha=0.5, label='Random classifier')

        legend_handles = []
        for idx, (cls_name, cls_m) in enumerate(per_class.items()):
            fpr  = cls_m.get('fpr', 0.0)
            rec  = cls_m.get('recall', 0.0)
            f1   = cls_m.get('f1', 0.0)
            prec = cls_m.get('precision', 0.0)

            # Get stored color or fallback
            raw_color = cls_m.get('color', None)
            if raw_color and isinstance(raw_color, str) and raw_color.startswith('#'):
                c = raw_color
            else:
                c = class_colors_default[idx % len(class_colors_default)]

            # Plot point on ROC space
            ax.scatter(fpr, rec, color=c, s=90, zorder=5, edgecolors='white', linewidths=0.8)

            # Draw a schematic curve from (0,0) → point → (1,1) using a smooth arc
            # Use a simple 2-point curve: origin → point, then point → (1,1)
            xs = np.linspace(0, fpr, 30)
            ys = np.linspace(0, rec, 30) ** 0.8  # slight bow
            ax.plot(xs, ys, color=c, lw=1.5, alpha=0.75)
            xs2 = np.linspace(fpr, 1, 30)
            ys2 = rec + (1 - rec) * ((xs2 - fpr) / (1 - fpr + 1e-9)) ** 1.2
            ax.plot(xs2, ys2, color=c, lw=1.5, alpha=0.75)

            patch = mpatches.Patch(
                color=c,
                label=f'{cls_name}  (P={prec*100:.1f}%, R={rec*100:.1f}%, F1={f1*100:.1f}%)'
            )
            legend_handles.append(patch)

        ax.set_xlim(-0.02, 1.02)
        ax.set_ylim(-0.02, 1.05)
        ax.set_xlabel('False Positive Rate (1 - Specificity)', fontsize=9)
        ax.set_ylabel('True Positive Rate (Recall / Sensitivity)', fontsize=9)
        title_str = 'ROC Curve — Per-class Classification Performance'
        if auc_val:
            title_str += f'\nAUC (approx.) = {auc_val:.3f}'
        ax.set_title(title_str, fontsize=9.5, fontweight='bold', pad=8)
        ax.tick_params(labelsize=8)
        ax.grid(True, alpha=0.3, lw=0.5)

        ax.legend(handles=legend_handles, fontsize=7.5, loc='lower right',
                  framealpha=0.9, edgecolor='#cccccc', borderpad=0.6)

        plt.tight_layout(pad=1.2)
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=150, bbox_inches='tight')
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode('utf-8')
    except Exception as e:
        print(f'[research_agent] ROC curve generation failed: {e}')
        return None


def _generate_confusion_matrix_b64(ml_metrics: dict) -> 'Optional[str]':
    """Render a confusion matrix heatmap from ml_metrics. Returns base64 PNG or None."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.colors as mcolors
        import numpy as np
        import io, base64

        conf_matrix = ml_metrics.get('confusion_matrix', None)
        class_names = ml_metrics.get('class_names', [])
        oa  = ml_metrics.get('overall_accuracy', None)
        kap = ml_metrics.get('kappa', None)
        if conf_matrix is None or not class_names:
            return None

        cm_arr = np.array(conf_matrix, dtype=float)
        n = len(class_names)
        row_sums = cm_arr.sum(axis=1, keepdims=True)
        row_sums[row_sums == 0] = 1
        cm_norm = cm_arr / row_sums

        cmap = mcolors.LinearSegmentedColormap.from_list(
            'rb', ['#1565C0', '#1976D2', '#BBDEFB', '#ffffff', '#FFCDD2', '#E53935', '#B71C1C']
        )
        fig_size = max(5.5, n * 1.15)
        fig, ax = plt.subplots(figsize=(fig_size + 1.5, fig_size))
        ax.set_facecolor('white'); fig.patch.set_facecolor('white')
        im = ax.imshow(cm_norm, cmap=cmap, vmin=0, vmax=1, aspect='auto')
        cbar = fig.colorbar(im, ax=ax, fraction=0.035, pad=0.02)
        cbar.set_label('Proportion', fontsize=9); cbar.ax.tick_params(labelsize=8)
        cbar.set_ticks([0, 0.2, 0.4, 0.6, 0.8, 1.0])
        for i in range(n):
            for j in range(n):
                val = int(cm_arr[i, j])
                bg = cm_norm[i, j]
                txt_color = 'white' if (bg > 0.65 or bg < 0.15) else 'black'
                ax.text(j, i, str(val), ha='center', va='center',
                        fontsize=9, fontweight='bold', color=txt_color)
        ax.set_xticks(range(n)); ax.set_yticks(range(n))
        ax.set_xticklabels(class_names, rotation=30, ha='right', fontsize=9)
        ax.set_yticklabels(class_names, fontsize=9)
        ax.set_xlabel('Predicted', fontsize=10, labelpad=6)
        ax.set_ylabel('Actual', fontsize=10, labelpad=6)
        title = 'Confusion Matrix'
        if oa is not None and kap is not None:
            title += f'\nOverall Accuracy: {oa*100:.1f}%  |  Kappa: {kap:.3f}'
        ax.set_title(title, fontsize=10.5, fontweight='bold', pad=10)
        plt.tight_layout(pad=1.2)
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=160, bbox_inches='tight')
        plt.close(fig); buf.seek(0)
        return base64.b64encode(buf.read()).decode('utf-8')
    except Exception as e:
        print(f'[research_agent] confusion matrix chart failed: {e}')
        return None


def _generate_metrics_panel_b64(ml_metrics: dict) -> 'Optional[str]':
    """
    Generate a per-class performance metrics panel (like the GIS Agent UI screenshot).
    Returns base64-encoded PNG string, or None on failure.
    """
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches
        import io, base64

        per_class = ml_metrics.get('per_class', {})
        if not per_class:
            return None

        oa      = ml_metrics.get('overall_accuracy', 0)
        kap     = ml_metrics.get('kappa', 0)
        apr     = ml_metrics.get('avg_precision', 0)
        arc     = ml_metrics.get('avg_recall', 0)
        af1     = ml_metrics.get('avg_f1', 0)
        auc_val = ml_metrics.get('auc_approx', None)

        fpr_vals = [v.get('fpr', 0) for v in per_class.values()]
        avg_fpr  = sum(fpr_vals) / len(fpr_vals) if fpr_vals else 0

        n_classes = len(per_class)
        # Figure height: header + per-class rows + overall section
        row_h   = 0.52
        panel_h = 1.0 + n_classes * row_h + 1.8
        fig_w   = 8.5

        fig, ax = plt.subplots(figsize=(fig_w, panel_h))
        ax.set_facecolor('#1a1a2e')
        fig.patch.set_facecolor('#1a1a2e')
        ax.axis('off')

        WHITE   = '#ffffff'
        LGRAY   = '#aaaaaa'
        MGRAY   = '#555577'
        BG2     = '#16213e'
        BOLD    = {'fontweight': 'bold'}

        y = 1.0  # start from top (axes coords via transform)
        total_h = panel_h

        # Use figure coordinates (inches from bottom-left)
        def fig_y(logical_y):
            """Convert top-down logical y to figure fraction."""
            return 1.0 - logical_y / total_h

        # ── Header ───────────────────────────────────────────────────────────────
        fig.text(0.03, fig_y(0.15), 'Per-class Performance',
                 color=WHITE, fontsize=11, **BOLD,
                 transform=fig.transFigure)

        # Column headers
        cols_x = [0.03, 0.28, 0.42, 0.56, 0.70, 0.84]
        headers = ['Class', 'Accuracy', 'Precision', 'Recall', 'F1 Score', 'FPR']
        header_y = fig_y(0.45)
        for hx, ht in zip(cols_x, headers):
            fig.text(hx, header_y, ht, color=LGRAY, fontsize=8.5, **BOLD,
                     transform=fig.transFigure)

        tab10 = plt.cm.get_cmap('tab10')

        for i, (cls_name, cls_m) in enumerate(per_class.items()):
            row_y  = fig_y(0.65 + i * row_h)
            prec   = cls_m.get('precision', 0)
            rec    = cls_m.get('recall', 0)
            f1     = cls_m.get('f1', 0)
            acc    = cls_m.get('accuracy', 0)
            fpr    = cls_m.get('fpr', 0)

            raw_color = cls_m.get('color', None)
            if raw_color and isinstance(raw_color, str) and raw_color.startswith('#'):
                dot_c = raw_color
            else:
                rgba  = tab10(i % 10)
                dot_c = '#{:02x}{:02x}{:02x}'.format(
                    int(rgba[0]*255), int(rgba[1]*255), int(rgba[2]*255))

            # Colored dot
            fig.text(cols_x[0] - 0.005, row_y, '●', color=dot_c,
                     fontsize=11, transform=fig.transFigure, va='center')
            fig.text(cols_x[0] + 0.025, row_y, cls_name, color=WHITE,
                     fontsize=9, **BOLD, transform=fig.transFigure, va='center')

            vals = [f'{acc*100:.1f}%', f'{prec*100:.1f}%',
                    f'{rec*100:.1f}%', f'{f1*100:.1f}%', f'{fpr*100:.1f}%']
            for xi, (col_x, val) in enumerate(zip(cols_x[1:], vals)):
                # Color the value: green if good, yellow if moderate, red if poor
                if xi in (1, 2, 3):  # precision, recall, f1
                    v = [prec, rec, f1][xi - 1 if xi > 0 else 0]
                    vc = '#4ade80' if v >= 0.7 else '#facc15' if v >= 0.4 else '#f87171'
                else:
                    vc = WHITE
                fig.text(col_x, row_y, val, color=vc,
                         fontsize=9, transform=fig.transFigure, va='center')

        # ── Separator line ────────────────────────────────────────────────────────
        sep_y = fig_y(0.65 + n_classes * row_h + 0.15)
        fig.add_artist(plt.Line2D([0.03, 0.97], [sep_y, sep_y],
                                  color=MGRAY, lw=0.6, transform=fig.transFigure))

        # ── Overall model metrics ─────────────────────────────────────────────────
        overall_y = fig_y(0.65 + n_classes * row_h + 0.45)
        fig.text(0.03, overall_y, 'Overall Model Metrics',
                 color=WHITE, fontsize=10, **BOLD, transform=fig.transFigure)

        overall_items = [
            ('Overall Accuracy', f'{oa*100:.1f}%'),
            ('Kappa Coefficient', f'{kap:.3f}'),
            ('Macro Precision',   f'{apr*100:.1f}%'),
            ('Macro Recall',      f'{arc*100:.1f}%'),
            ('Macro F1 Score',    f'{af1*100:.1f}%'),
            ('Avg False Positive Rate', f'{avg_fpr*100:.1f}%'),
        ]
        if auc_val:
            overall_items.append(('AUC (approx.)', f'{auc_val:.3f}'))

        # Two-column layout for overall metrics
        col1_x, col2_x = 0.03, 0.52
        for idx, (label, val) in enumerate(overall_items):
            xpos   = col1_x if idx % 2 == 0 else col2_x
            row_fy = fig_y(0.65 + n_classes * row_h + 0.85 + (idx // 2) * 0.42)
            fig.text(xpos, row_fy, f'• {label}:',
                     color=LGRAY, fontsize=8.5, transform=fig.transFigure)
            fig.text(xpos + 0.22, row_fy, val,
                     color=WHITE, fontsize=8.5, **BOLD, transform=fig.transFigure)

        plt.tight_layout(pad=0.1)
        buf = io.BytesIO()
        fig.savefig(buf, format='png', dpi=150, bbox_inches='tight',
                    facecolor='#1a1a2e')
        plt.close(fig)
        buf.seek(0)
        return base64.b64encode(buf.read()).decode('utf-8')
    except Exception as e:
        print(f'[research_agent] metrics panel generation failed: {e}')
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
             'Eq. (1) - At-satellite brightness temperature (K). L = spectral radiance at Band 10; '
             'K1 = 774.89 W/(m2.sr.um), K2 = 1321.08 K (Landsat 8 TIRS Band 10 calibration constants, USGS metadata).'),
            ('LST = BT / [1 + (lam x BT / rho) x ln(eps)]',
             'Eq. (2) - Land Surface Temperature (K), converted to degrees C by subtracting 273.15. '
             'lam = 10.895 um (effective wavelength of Band 10); rho = 1.438 x 10^-2 m.K; '
             'eps = land surface emissivity derived from NDVI-based fractional vegetation cover.'),
            ('FVC = [(NDVI - NDVI_soil) / (NDVI_veg - NDVI_soil)]^2',
             'Eq. (3) - Fractional Vegetation Cover (Carlson & Ripley, 1997). NDVI_soil and NDVI_veg '
             'are the minimum and maximum NDVI values in the scene. Emissivity: '
             'eps = eps_soil x (1 - FVC) + eps_veg x FVC + d_eps, '
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
             'Eq. (1) -- NDVI used as primary spectral feature for LULC classification. '
             'NIR = Band 5 (0.85-0.88 um); RED = Band 4 (0.64-0.67 um). '
             'Computed from cloud-masked Landsat 8 median composite alongside all six optical bands (B2-B7) '
             'to form the multi-feature input stack for the classifier.'),
            ('f(x) = argmax_k [ (1/T) * SUM_{t=1}^{T} I(h_t(x) = k) ]',
             'Eq. (2) -- Random Forest classification decision rule (Breiman, 2001). '
             'f(x) = predicted class label for pixel x; T = number of decision trees (default 100 in GEE); '
             'h_t(x) = prediction of the t-th tree; I() = indicator function. '
             'Each tree is trained on a bootstrap sample using a random subset of sqrt(n_features) '
             'candidate features at each node split. The final class is assigned by majority vote.'),
            ('Gini(t) = 1 - SUM_k p(k|t)^2',
             'Eq. (3) -- Gini impurity used as the node-splitting criterion in each decision tree. '
             'p(k|t) = proportion of samples of class k at node t. '
             'A split is chosen to maximize the decrease in Gini impurity (information gain). '
             'Lower Gini values indicate purer nodes with more homogeneous class membership.'),
            ('OA = (SUM_i n_ii) / N',
             'Eq. (4) -- Overall Accuracy (Story & Congalton, 1986). '
             'n_ii = correctly classified pixels for class i (diagonal of confusion matrix); '
             'N = total number of validation pixels. '
             'Reported as a percentage; values above 85% indicate good classification performance.'),
            ('Kappa = (OA - p_e) / (1 - p_e)',
             'Eq. (5) -- Cohen\'s Kappa coefficient (Cohen, 1960). '
             'p_e = expected agreement by chance = SUM_i (row_i x col_i) / N^2; '
             'accounts for the possibility of agreement occurring purely by random chance. '
             'Kappa > 0.8 = excellent; 0.6-0.8 = substantial; 0.4-0.6 = moderate agreement.'),
            ('Precision_k = TP_k / (TP_k + FP_k)',
             'Eq. (6) -- Per-class Precision (User\'s Accuracy). '
             'TP_k = true positives for class k; FP_k = pixels of other classes incorrectly assigned to class k. '
             'Measures the reliability of the classifier\'s positive predictions for each class.'),
            ('Recall_k = TP_k / (TP_k + FN_k)',
             'Eq. (7) -- Per-class Recall (Producer\'s Accuracy). '
             'FN_k = pixels of class k incorrectly assigned to other classes. '
             'Measures the classifier\'s ability to correctly identify all pixels belonging to class k.'),
            ('F1_k = 2 * (Precision_k * Recall_k) / (Precision_k + Recall_k)',
             'Eq. (8) -- Per-class F1 score (harmonic mean of Precision and Recall). '
             'Balances the trade-off between precision and recall; '
             'particularly important for imbalanced class distributions where one metric alone is insufficient.'),
        ],
        'ui': [
            ('UI = (SWIR - NIR) / (SWIR + NIR)',
             'Eq. (1) -- Urban Index (Kawamura et al., 1996). SWIR = Band 6 (1.57-1.65 um); NIR = Band 5 (0.85-0.88 um). '
             'Positive values highlight impervious urban surfaces; functionally similar to NDBI but tuned to urban fabric discrimination in tropical environments.'),
        ],
        'nbi': [
            ('NBI = (RED x SWIR) / NIR',
             'Eq. (1) -- New Built-up Index (Jieli et al., 2010). RED = Band 4; SWIR = Band 6; NIR = Band 5. '
             'Designed to improve separation of built-up land from bare soil and bright surfaces by exploiting the ratio of red-SWIR product to NIR reflectance.'),
        ],
        'ndsi': [
            ('NDSI = (GREEN - SWIR) / (GREEN + SWIR)',
             'Eq. (1) -- Normalized Difference Snow Index (Hall et al., 1995). GREEN = Band 3 (0.53-0.59 um); SWIR = Band 6 (1.57-1.65 um). '
             'Positive values (> 0.4) indicate snow or ice cover; distinguishes snow from cloud cover using SWIR absorption characteristics.'),
        ],
        'co': [
            ('XCO = SUM(w_i x L_i) / SUM(w_i)',
             'Eq. (1) -- Column-averaged dry-air mole fraction of CO (mol/mol) retrieved from Sentinel-5P TROPOMI '
             'using the SWIR spectral band (2305-2385 nm) via full-physics inverse radiative transfer (Borsdorff et al., 2018). '
             'w_i = layer-specific averaging kernel weights; L_i = partial column amounts. '
             'Values typically range from 20-500 ppb; elevated concentrations mark combustion source regions.'),
        ],
        'no2': [
            ('VCD_NO2 = ICA / AMF',
             'Eq. (1) -- Tropospheric NO2 vertical column density (mol/m^2) from Sentinel-5P TROPOMI '
             '(van Geffen et al., 2022). ICA = integrated column amount from spectral fitting (DOAS method, 405-465 nm); '
             'AMF = air mass factor derived from TM5-MP chemistry transport model. '
             'Tropospheric column isolates boundary-layer pollution by subtracting the stratospheric contribution.'),
        ],
        'so2': [
            ('VCD_SO2 = SCD_SO2 / AMF_SO2',
             'Eq. (1) -- SO2 total vertical column density (DU) from Sentinel-5P TROPOMI (Theys et al., 2017). '
             'SCD_SO2 = slant column density retrieved via DOAS fitting in the UV band (312-326 nm); '
             'AMF_SO2 = scene-dependent air mass factor. '
             'A threshold of 0.5 DU is commonly used to distinguish anthropogenic emission plumes from background noise.'),
        ],
        'ch4': [
            ('XCH4 = SUM(h_i x x_i)',
             'Eq. (1) -- Column-averaged dry-air mole fraction of CH4 (ppb) from Sentinel-5P TROPOMI '
             '(Hu et al., 2018). Retrieved via proxy method using SWIR bands (2305-2385 nm for CH4, 2265-2300 nm for CO2). '
             'h_i = pressure weighting function; x_i = partial column profile. '
             'Background tropospheric CH4 is approximately 1900 ppb; values above 1950 ppb indicate significant local sources.'),
        ],
        'o3': [
            ('VCD_O3 = SCD_O3 / AMF_O3',
             'Eq. (1) -- Total ozone column (DU) from Sentinel-5P TROPOMI retrieved using the DOAS method '
             'in the UV Huggins band (325-335 nm) (Veefkind et al., 2012). '
             'SCD_O3 = measured slant column; AMF_O3 = ozone-profile-dependent air mass factor. '
             'Typical tropical background values range from 240-300 DU; depletion events fall below 220 DU.'),
        ],
        'aerosol': [
            ('AOD = -ln(I / I0) / (AMF)',
             'Eq. (1) -- Aerosol Optical Depth at 550 nm retrieved from Sentinel-5P TROPOMI using '
             'the UVAI and reflectance inversion method (Torres et al., 2018). '
             'I = measured top-of-atmosphere radiance; I0 = modeled clear-sky radiance; AMF = geometric air mass factor. '
             'AOD > 0.4 typically indicates heavy aerosol loading from industrial emissions, biomass burning, or dust.'),
        ],
        'gpp': [
            ('GPP = APAR x LUE',
             'Eq. (1) -- Gross Primary Production (g C/m^2/day) estimated via the light use efficiency model '
             '(Monteith, 1972). APAR = absorbed photosynthetically active radiation = PAR x FPAR; '
             'FPAR = fraction of PAR derived from NDVI (FPAR = 1.164 x NDVI - 0.143); '
             'LUE = biome-specific light use efficiency (g C / MJ APAR), '
             'typically 0.5-1.0 for tropical vegetation. Derived from Landsat 8 bands and MODIS PAR climatology.'),
        ],
        'burned': [
            ('NBR = (NIR - SWIR2) / (NIR + SWIR2)',
             'Eq. (1) -- Normalized Burn Ratio (Key & Benson, 2006). NIR = Band 5 (0.85-0.88 um); '
             'SWIR2 = Band 7 (2.11-2.29 um). Burned areas exhibit strongly negative NBR values (<-0.1) '
             'due to reduced NIR reflectance and increased SWIR reflectance of charred surfaces.'),
            ('dNBR = NBR_pre - NBR_post',
             'Eq. (2) -- Differenced NBR (burn severity index). Pre-fire and post-fire NBR composites '
             'are subtracted to isolate burn extent and severity. USGS severity classes: '
             'dNBR < 0.1 = unburned; 0.1-0.27 = low severity; 0.27-0.66 = moderate; > 0.66 = high severity.'),
        ],
        'ffpi': [
            ('FFPI = w_slope x S_norm + w_soil x K_norm + w_veg x (1 - NDVI_norm) + w_stream x D_norm',
             'Eq. (1) -- Flash Flood Potential Index (Mogil et al., 2010 adaptation). '
             'S_norm = normalized slope (from DEM); K_norm = normalized soil erodibility (K-factor from HWSD); '
             'NDVI_norm = normalized vegetation index (lower NDVI raises flood potential); '
             'D_norm = normalized proximity to stream network. '
             'Weights: w_slope=0.30, w_soil=0.25, w_veg=0.25, w_stream=0.20 (equal-weight variant). '
             'Final FFPI is rescaled 0-10; values above 7 indicate high flash flood susceptibility.'),
        ],
    }

    # ── Figure description prose (natural, journal-quality) ───────────────────
    FIG_DESC = {
        'analysis_map': {
            'lst':
                'Spatial distribution of LST (deg C) across {region}. Elevated thermal signatures '
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
                'The dominant classes reveal the prevailing surface composition - built-up land, '
                'vegetation, bare soil, and water - that drives the observed spatial patterns '
                'of thermal and spectral variables.',
            'default':
                'Spatial distribution of {var} across {region}. Color gradients reflect the range '
                'of retrieved values, highlighting spatial heterogeneity driven by differences in '
                'land cover, surface moisture, and urban morphology.',
        },
        'monthly': {
            'lst':
                'Monthly mean LST (deg C) for {region} over the study period. Peak temperatures '
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
        'lulc_pie': {
            'lulc':
                'Proportional composition of LULC classes for {region}, expressed as a percentage of '
                'total classified area. Built Area dominates the landscape, underscoring the intensity '
                'of urban development across the metropolitan area. Tree cover constitutes the second '
                'largest class, reflecting the combined contribution of parks, street trees, and suburban '
                'gardens. Rangeland is predominantly concentrated within the Green Belt and peri-urban '
                'fringe, while Water and Bare Ground together account for a minor fraction of the total '
                'area, consistent with the hydrological characteristics of a large temperate city.',
            'default':
                'Proportional composition of {var} classes for {region}. Each segment represents the '
                'percentage of total classified area assigned to a given LULC category, providing a '
                'concise summary of the dominant surface types within the study domain.',
        },
        'class_bar': {
            'lst':
                'Areal distribution of LST across discrete temperature classes for {region}. '
                'The dominance of the Hot (40-45 deg C) class confirms persistently elevated thermal '
                'conditions across the majority of the urban surface. The Extreme class (>45 deg C) '
                'represents highly impervious zones - commercial rooftops and industrial areas - '
                'while the Cool class (<30 deg C) is restricted to water bodies and dense vegetation patches.',
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
        # ── per-variable analysis_map descriptions ────────────────────────────
    }

    # Inject additional per-variable FIG_DESC entries for new variables
    _EXTRA_ANALYSIS_MAP = {
        'ndvi':
            'NDVI distribution across {region}. High positive values (green tones) indicate dense '
            'vegetation cover, while low or negative values (orange to red) mark built-up surfaces, '
            'bare soil, and paved areas. Pronounced spatial contrasts between green corridors and the '
            'impervious urban matrix are clearly visible.',
        'ui':
            'Urban Index (UI) distribution across {region}. Positive values delineate impervious urban '
            'fabric including commercial cores, road networks, and industrial zones. The spatial pattern '
            'closely mirrors the built-up extent and correlates positively with elevated LST hotspots.',
        'nbi':
            'New Built-up Index (NBI) values across {region}. High NBI values identify dense built-up '
            'surfaces by exploiting the contrast between red-SWIR product and NIR reflectance, '
            'providing improved discrimination of urban land from bare soil and bright non-urban surfaces.',
        'ndsi':
            'NDSI distribution across {region}. Positive values exceeding 0.4 mark snow or ice-covered '
            'surfaces, while urban and vegetated areas remain near zero or negative. The index is '
            'particularly useful for monitoring seasonal snowpack dynamics and glacial extent.',
        'co':
            'Spatial distribution of CO column concentration (mol/m^2) across {region} derived from '
            'Sentinel-5P TROPOMI. Elevated values trace combustion-related emission hotspots including '
            'traffic corridors, industrial zones, and biomass burning areas. The spatial gradient '
            'reflects the dominant wind direction and proximity to major emission sources.',
        'no2':
            'Tropospheric NO2 vertical column density (mol/m^2) across {region} from Sentinel-5P TROPOMI. '
            'Concentration peaks are spatially collocated with major road networks, industrial facilities, '
            'and dense urban cores where vehicle exhaust and fossil fuel combustion dominate emission inventories.',
        'so2':
            'SO2 vertical column density (DU) across {region} from Sentinel-5P TROPOMI. '
            'Elevated plumes identify point sources such as power plants, smelters, and volcanic vents. '
            'The spatial footprint of the plume reflects prevailing wind transport and atmospheric dispersion.',
        'ch4':
            'CH4 column-averaged mole fraction (ppb) across {region} from Sentinel-5P TROPOMI. '
            'Anomalies above the background (~1900 ppb) indicate enteric fermentation from livestock, '
            'landfill emissions, wetland sources, or fossil fuel leakage concentrated in identifiable spatial clusters.',
        'o3':
            'Total ozone column (DU) distribution across {region} from Sentinel-5P TROPOMI. '
            'Spatial gradients reflect the interplay of stratospheric dynamics, tropospheric photochemistry, '
            'and NOx-driven ozone production in urban and industrial source regions.',
        'aerosol':
            'Aerosol Optical Depth (AOD at 550 nm) across {region} from Sentinel-5P TROPOMI. '
            'High AOD values correspond to episodes of biomass burning smoke, industrial particulate emissions, '
            'or dust transport. The spatial pattern delineates the leading edge of aerosol plumes '
            'and the relative contribution of local versus regional sources.',
        'gpp':
            'Gross Primary Production (g C/m^2/day) distribution across {region}. High GPP values '
            'coincide with dense vegetated areas where photosynthetically active radiation is efficiently '
            'absorbed. Urban cores and bare surfaces exhibit near-zero GPP, highlighting the stark '
            'contrast between the urban matrix and surrounding green infrastructure.',
        'burned':
            'Burn severity distribution (dNBR) across {region}. Areas with high dNBR values (warm tones) '
            'experienced severe fire impacts with near-complete canopy removal and charring of surface fuels. '
            'Low-severity patches (cool tones) reflect partial scorch events where the forest structure '
            'remained partially intact. Unburned reference areas anchor the severity classification.',
        'ffpi':
            'Flash Flood Potential Index (FFPI) across {region}. High-risk zones (values 7-10) concentrate '
            'in areas combining steep slopes, low vegetation cover, high soil erodibility, and proximity '
            'to drainage networks. Dense urban areas with high impervious surface fraction contribute '
            'elevated runoff coefficients that amplify flood susceptibility in low-lying downstream reaches.',
    }
    _EXTRA_MONTHLY = {
        'ndvi':
            'Monthly mean NDVI for {region}. Vegetation greenness peaks during and immediately '
            'after the wet season when soil moisture is sufficient to sustain photosynthetic activity, '
            'and declines sharply in dry months as plant water stress reduces near-infrared reflectance. '
            'The seasonal cycle closely tracks the monsoon rainfall pattern.',
        'co':
            'Monthly mean CO column concentration for {region}. Peaks during dry-season months '
            'reflect intensified biomass burning and reduced atmospheric mixing heights that trap '
            'pollutants near the surface. Wet-season dilution and washout suppress concentrations.',
        'no2':
            'Monthly mean tropospheric NO2 column for {region}. Elevated dry-season values reflect '
            'reduced photolytic destruction under lower humidity and increased temperature inversions '
            'trapping vehicular and industrial emissions. Wet-season rainfall accelerates NO2 removal.',
        'aerosol':
            'Monthly mean AOD for {region}. Aerosol loading peaks in the dry season driven by '
            'biomass burning and reduced wet scavenging, while monsoon precipitation efficiently '
            'removes aerosols and suppresses dust resuspension.',
        'gpp':
            'Monthly mean GPP for {region}. Productivity peaks during the wet season when soil '
            'moisture and solar radiation jointly optimize photosynthesis. Dry-season water stress '
            'and cloud-reduced PAR suppress GPP to seasonal minima.',
        'ffpi':
            'Monthly FFPI for {region}. Flood potential peaks during wet-season months when '
            'antecedent soil saturation, reduced infiltration capacity, and high-intensity convective '
            'precipitation events combine to drive rapid surface runoff and flash flooding.',
    }
    _EXTRA_HISTOGRAM = {
        'co':
            'Frequency distribution of CO column values across {region}. A right-skewed distribution '
            'with a pronounced tail reflects localized high-concentration emission plumes superimposed '
            'on a relatively homogeneous background field.',
        'no2':
            'Frequency distribution of tropospheric NO2 column values across {region}. The positive '
            'skew reflects the localized nature of traffic and industrial emission hotspots embedded '
            'within a lower-concentration urban background.',
        'ffpi':
            'Frequency distribution of FFPI values across {region}. The distribution shape reflects '
            'the relative proportion of the study area under different flood susceptibility classes, '
            'with tails indicating the most vulnerable and most protected terrain units.',
        'gpp':
            'Frequency distribution of GPP pixel values across {region}. Bimodal or skewed '
            'distributions typically reflect the coexistence of productive vegetated patches and '
            'low-productivity urban or bare surfaces within the study domain.',
    }
    # Merge extra entries into FIG_DESC
    for k, v in _EXTRA_ANALYSIS_MAP.items():
        FIG_DESC['analysis_map'][k] = v
    for k, v in _EXTRA_MONTHLY.items():
        FIG_DESC.setdefault('monthly', {})[k] = v
        FIG_DESC.setdefault('monthly_trend', {})[k] = v
        FIG_DESC.setdefault('trend', {})[k] = v
    for k, v in _EXTRA_HISTOGRAM.items():
        FIG_DESC.setdefault('histogram', {})[k] = v

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
    methodology_paras = section_body(sections.get('methodology', ''))
    if methodology_paras:
        # Keep the header glued to the first paragraph so it never orphans at a page break
        story.append(KeepTogether([Paragraph('3. Methodology', S['h1']), methodology_paras[0]]))
        story += methodology_paras[1:]
    else:
        story.append(Paragraph('3. Methodology', S['h1']))

    # ── 3.1 Index formulae + satellite data table ──────────────────────────────
    vars_analyzed = [v.strip().lower() for v in vars_list.split(',') if v.strip()]
    formula_added = False
    for var in vars_analyzed:
        if var in FORMULAS:
            if not formula_added:
                h31 = Paragraph('3.1 Data Sources and Satellite Characteristics', S['h2'])
                # Satellite data table
                tbl_data = [
                    ['Satellite / Sensor', 'Spatial Res.', 'Revisit', 'Bands Used', 'Purpose'],
                    ['Landsat 8 OLI/TIRS\n(Collection 2 Level-2)', '30 m (optical)\n100 m (thermal)', '16 days',
                     'B2-B7 (optical)\nB10 (thermal)', 'Surface indices, LST'],
                    ['Landsat 9 OLI-2/TIRS-2\n(Collection 2 Level-2)', '30 m (optical)\n100 m (thermal)', '16 days',
                     'B2-B7 (optical)\nB10 (thermal)', 'Surface indices, LST'],
                    ['Sentinel-5P TROPOMI', '5.5 x 3.5 km', 'Daily',
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
                story.append(KeepTogether([h31, Spacer(1, 0.1*cm), tbl]))
                story.append(Paragraph(
                    'Table 1. Characteristics of satellite datasets used in this study.',
                    S['caption']))

                # Preprocessing narrative
                preprocessing_text = (
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
                    'The following spectral indices were computed from the processed composites:'
                )
                preprocessing_para = Paragraph(preprocessing_text, S['body'])
                story.append(KeepTogether([
                    Paragraph('3.2 Preprocessing and Index Computation', S['h2']),
                    preprocessing_para,
                ]))
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
            # Skip confusion matrix and metrics panel in 4.1 — they appear exclusively in 4.2
            if chart_type in ('confusion_matrix', 'metrics_panel'):
                continue
            label_map = {
                'monthly_trend'   : f'Monthly mean {var_label} for {_esc(region)}.',
                'monthly'         : f'Monthly mean {var_label} for {_esc(region)}.',
                'class_bar'       : f'Areal distribution of {var_label} by classification category for {_esc(region)}.',
                'lulc_pie'        : f'LULC class composition for {_esc(region)}.',
                'trend'           : f'Temporal trend of {var_label} for {_esc(region)}.',
                'seasonal'        : f'Seasonal pattern of {var_label} for {_esc(region)}.',
                'histogram'       : f'Frequency distribution of {var_label} pixel values across {_esc(region)}.',
            }
            cap = label_map.get(chart_type, f'{var_label} - {chart_type.replace("_"," ").title()} for {_esc(region)}.')
            story += embed_image(chart_b64,
                caption=f'Fig. {fig_counter}. {cap}',
                max_h=5.8)
            desc = get_fig_desc(chart_type, var_key, region)
            if desc:
                story.append(Paragraph(_esc(desc), S['fig_desc']))
            fig_counter += 1

        # ── LULC-specific: classification accuracy tables + charts ────────────
        if var_key.lower() == 'lulc':
            ml  = sections.get('lulc_ml_metrics', {})
            print(f'[research_agent] _build_pdf: lulc_ml_metrics keys = {list(ml.keys()) if ml else "EMPTY"}')
            if ml:
                oa_check = ml.get('overall_accuracy', None)
                cm_check = ml.get('confusion_matrix', None)
                pc_check = ml.get('per_class', {})
                cn_check = ml.get('class_names', [])
                print(f'[research_agent] _build_pdf: OA={oa_check}, confusion_matrix present={cm_check is not None}, '
                      f'per_class classes={list(pc_check.keys())}, class_names={cn_check}')
                story.append(Paragraph('4.2 Classification Accuracy Assessment', S['h2']))

                n_train_v = ml.get('n_train', None)
                n_test_v  = ml.get('n_test', None)
                n_total_v = ml.get('n_total', None)
                sample_str = (
                    f' A total of {n_total_v} reference samples were used, split 80/20 into '
                    f'{n_train_v} training and {n_test_v} test pixels via stratified random sampling.'
                    if n_total_v else ''
                )
                story.append(Paragraph(
                    f'The Random Forest classifier was evaluated using standard accuracy metrics derived from '
                    f'the confusion matrix.{sample_str} The confusion matrix heatmap and per-class performance '
                    f'panel are shown in the figures below. Overall accuracy, '
                    f"Cohen's Kappa, macro-averaged precision, recall, F1 score, AUC, and average false "
                    f'positive rate are summarised in Table 2. Per-class breakdown is provided in Table 3.',
                    S['body']))

                # ── Confusion matrix heatmap ─────────────────────────────────
                cm_b64 = _generate_confusion_matrix_b64(ml)
                if cm_b64:
                    story += embed_image(
                        cm_b64,
                        caption=f'Fig. {fig_counter}. Confusion matrix of the Random Forest LULC classification '
                                f'(row-normalized by actual class). Red tones = correctly classified; '
                                f'blue tones = misclassified.',
                        max_h=8.0)
                    story.append(Paragraph(
                        'Each row represents the actual reference class; each column the predicted class. '
                        'Cell values show raw sample counts. Row-wise normalization allows direct comparison '
                        'of recall across classes with different sample sizes. Diagonal dominance (red) '
                        'indicates strong recall; elevated off-diagonal column values indicate commission '
                        'errors that suppress per-class precision.',
                        S['fig_desc']))
                    fig_counter += 1

                # ── Per-class metrics panel (visual, like GIS Agent UI) ───────
                panel_b64 = _generate_metrics_panel_b64(ml)
                if panel_b64:
                    story += embed_image(
                        panel_b64,
                        caption=f'Fig. {fig_counter}. Per-class and overall classification performance metrics.',
                        max_h=6.0)
                    story.append(Paragraph(
                        'Summary of classification performance metrics for each LULC class and overall model. '
                        'Precision reflects user\'s accuracy (reliability of predicted labels); Recall reflects '
                        'producer\'s accuracy (completeness of class detection); F1 is the harmonic mean. '
                        'Green = strong (>70%); yellow = moderate (40-70%); red = weak (<40%).',
                        S['fig_desc']))
                    fig_counter += 1

                # ── Overall metrics table ─────────────────────────────────────
                oa  = ml.get('overall_accuracy', 0)
                kap = ml.get('kappa', 0)
                apr = ml.get('avg_precision', 0)
                arc = ml.get('avg_recall', 0)
                af1 = ml.get('avg_f1', 0)
                auc = ml.get('auc_approx', None)   # fixed key

                # Compute avg_fpr from per_class (not stored at top level)
                per_class_tmp = ml.get('per_class', {})
                fpr_vals_tmp  = [v.get('fpr', 0) for v in per_class_tmp.values() if isinstance(v, dict)]
                fpr_avg       = round(sum(fpr_vals_tmp) / len(fpr_vals_tmp), 4) if fpr_vals_tmp else None

                overall_rows = [['Metric', 'Value', 'Interpretation']]
                if n_total_v:
                    overall_rows.append([
                        'Training / Test Samples',
                        f'{n_train_v} / {n_test_v}',
                        f'80/20 stratified split (total: {n_total_v})',
                    ])
                overall_rows += [
                    ['Overall Accuracy (OA)',
                     f'{oa*100:.1f}%',
                     'Excellent (>85%)' if oa >= 0.85 else 'Good (75-85%)' if oa >= 0.75 else 'Moderate (<75%)'],
                    ["Cohen's Kappa",
                     f'{kap:.3f}',
                     'Excellent (>0.80)' if kap >= 0.80 else 'Substantial (0.60-0.80)' if kap >= 0.60 else 'Moderate (0.40-0.60)' if kap >= 0.40 else 'Fair (<0.40)'],
                    ['Macro Precision', f'{apr*100:.1f}%' if apr else 'N/A', 'Avg. user\'s accuracy across classes'],
                    ['Macro Recall',    f'{arc*100:.1f}%' if arc else 'N/A', 'Avg. producer\'s accuracy across classes'],
                    ['Macro F1 Score',  f'{af1*100:.1f}%' if af1 else 'N/A', 'Harmonic mean of precision and recall'],
                ]
                if fpr_avg is not None:
                    overall_rows.append(['Avg. False Positive Rate', f'{fpr_avg*100:.1f}%', 'Mean commission error across classes'])
                if auc is not None:
                    overall_rows.append(['AUC (approx.)', f'{auc:.3f}', 'Area under ROC curve (1.0 = perfect)'])

                col_w2 = [5.5*cm, 2.5*cm, 6.3*cm]
                tbl2 = Table(overall_rows, colWidths=col_w2, repeatRows=1)
                tbl2.setStyle(TableStyle([
                    ('FONTNAME',      (0,0), (-1,0),  'Helvetica-Bold'),
                    ('FONTSIZE',      (0,0), (-1,-1), 8),
                    ('LEADING',       (0,0), (-1,-1), 11),
                    ('BACKGROUND',    (0,0), (-1,0),  colors.HexColor('#eeeeee')),
                    ('TEXTCOLOR',     (0,0), (-1,-1), colors.HexColor('#111111')),
                    ('ALIGN',         (0,0), (0,-1),  'LEFT'),
                    ('ALIGN',         (1,0), (1,-1),  'CENTER'),
                    ('ALIGN',         (2,0), (2,-1),  'LEFT'),
                    ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
                    ('GRID',          (0,0), (-1,-1), 0.4, colors.HexColor('#aaaaaa')),
                    ('ROWBACKGROUNDS',(0,1), (-1,-1), [colors.white, colors.HexColor('#f7f7f7')]),
                    ('TOPPADDING',    (0,0), (-1,-1), 4),
                    ('BOTTOMPADDING', (0,0), (-1,-1), 4),
                    ('LEFTPADDING',   (0,0), (-1,-1), 5),
                    ('RIGHTPADDING',  (0,0), (-1,-1), 5),
                ]))
                story.append(Spacer(1, 0.15*cm))
                story.append(KeepTogether([
                    Paragraph('Table 2. Overall classification model performance metrics.', S['caption']),
                    Spacer(1, 0.05*cm),
                    tbl2,
                ]))
                story.append(Spacer(1, 0.1*cm))

                # ── Per-class metrics table ───────────────────────────────────
                per_class = ml.get('per_class', {})
                if per_class:
                    pc_rows = [['Class', 'Precision', 'Recall', 'F1 Score', 'Accuracy', 'FPR']]
                    for cls_name, cls_m in per_class.items():
                        p   = cls_m.get('precision', 0)
                        r   = cls_m.get('recall', 0)
                        f1  = cls_m.get('f1', 0)
                        acc = cls_m.get('accuracy', 0)
                        fpr = cls_m.get('fpr', 0)
                        pc_rows.append([
                            _esc(str(cls_name)),
                            f'{p*100:.1f}%',
                            f'{r*100:.1f}%',
                            f'{f1*100:.1f}%',
                            f'{acc*100:.1f}%',
                            f'{fpr*100:.1f}%',
                        ])
                    col_w3 = [3.8*cm, 2.2*cm, 2.2*cm, 2.2*cm, 2.2*cm, 1.8*cm]
                    tbl3 = Table(pc_rows, colWidths=col_w3, repeatRows=1)
                    tbl3.setStyle(TableStyle([
                        ('FONTNAME',      (0,0), (-1,0),  'Helvetica-Bold'),
                        ('FONTSIZE',      (0,0), (-1,-1), 8),
                        ('LEADING',       (0,0), (-1,-1), 11),
                        ('BACKGROUND',    (0,0), (-1,0),  colors.HexColor('#eeeeee')),
                        ('TEXTCOLOR',     (0,0), (-1,-1), colors.HexColor('#111111')),
                        ('ALIGN',         (0,0), (0,-1),  'LEFT'),
                        ('ALIGN',         (1,0), (-1,-1), 'CENTER'),
                        ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
                        ('GRID',          (0,0), (-1,-1), 0.4, colors.HexColor('#aaaaaa')),
                        ('ROWBACKGROUNDS',(0,1), (-1,-1), [colors.white, colors.HexColor('#f7f7f7')]),
                        ('TOPPADDING',    (0,0), (-1,-1), 4),
                        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
                        ('LEFTPADDING',   (0,0), (-1,-1), 5),
                        ('RIGHTPADDING',  (0,0), (-1,-1), 5),
                    ]))
                    story.append(KeepTogether([
                        Paragraph('Table 3. Per-class classification performance metrics.', S['caption']),
                        Spacer(1, 0.05*cm),
                        tbl3,
                    ]))
                    story.append(Spacer(1, 0.1*cm))

                # ── ROC Curve ─────────────────────────────────────────────────
                roc_b64 = _generate_roc_curve_b64(ml)
                if roc_b64:
                    story.append(PageBreak())
                    story.append(Paragraph('4.3 ROC Curve Analysis', S['h2']))
                    story.append(Paragraph(
                        'The Receiver Operating Characteristic (ROC) curve plots each class\'s True '
                        'Positive Rate (Recall) against its False Positive Rate across the classification '
                        'threshold. A point closer to the upper-left corner indicates a better trade-off '
                        'between sensitivity and specificity. The approximate AUC is computed as the '
                        'complement of the mean FPR weighted by recall across all classes.',
                        S['body']))
                    story += embed_image(
                        roc_b64,
                        caption=f'Fig. {fig_counter}. ROC curve for each LULC class. '
                                f'Points above the diagonal indicate better-than-random discrimination.',
                        max_h=8.5)
                    story.append(Paragraph(
                        'Classes appearing in the upper-left region of the ROC space (high TPR, low FPR) '
                        'exhibit strong discriminability, while classes near the diagonal indicate '
                        'performance approaching random chance, often caused by spectral confusion with '
                        'adjacent cover types or severe class imbalance in the training sample.',
                        S['fig_desc']))
                    fig_counter += 1

                # ── Confusion matrix ──────────────────────────────────────────
                conf_matrix = ml.get('confusion_matrix', None)
                class_names = ml.get('class_names', list(per_class.keys()) if per_class else [])
                if conf_matrix and class_names:
                    story.append(Spacer(1, 0.2*cm))
                    n = len(class_names)
                    cm_header = ['Actual \\ Predicted'] + [_esc(str(c)) for c in class_names]
                    cm_rows   = [cm_header]
                    for i, row in enumerate(conf_matrix):
                        lbl = _esc(str(class_names[i])) if i < len(class_names) else f'Class {i}'
                        cm_rows.append([lbl] + [str(int(v)) for v in row])

                    label_col_w = 3.5*cm
                    avail_w = (PAGE_W / cm - 5.0) * cm - label_col_w
                    data_col_w = avail_w / max(n, 1)
                    col_w4 = [label_col_w] + [data_col_w] * n

                    cm_style = [
                        ('FONTNAME',      (0,0), (-1,0),  'Helvetica-Bold'),
                        ('FONTNAME',      (0,0), (0,-1),  'Helvetica-Bold'),
                        ('FONTSIZE',      (0,0), (-1,-1), 7.5),
                        ('LEADING',       (0,0), (-1,-1), 10),
                        ('BACKGROUND',    (0,0), (-1,0),  colors.HexColor('#eeeeee')),
                        ('BACKGROUND',    (0,0), (0,-1),  colors.HexColor('#eeeeee')),
                        ('TEXTCOLOR',     (0,0), (-1,-1), colors.HexColor('#111111')),
                        ('ALIGN',         (0,0), (-1,-1), 'CENTER'),
                        ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
                        ('GRID',          (0,0), (-1,-1), 0.4, colors.HexColor('#aaaaaa')),
                        ('TOPPADDING',    (0,0), (-1,-1), 3),
                        ('BOTTOMPADDING', (0,0), (-1,-1), 3),
                        ('LEFTPADDING',   (0,0), (-1,-1), 3),
                        ('RIGHTPADDING',  (0,0), (-1,-1), 3),
                    ]
                    for i in range(n):
                        cm_style.append(('BACKGROUND', (i+1, i+1), (i+1, i+1),
                                         colors.HexColor('#c8e6c9')))

                    tbl4 = Table(cm_rows, colWidths=col_w4, repeatRows=1)
                    tbl4.setStyle(TableStyle(cm_style))
                    story.append(KeepTogether([
                        Paragraph('Table 4. Confusion matrix of the Random Forest classification.', S['caption']),
                        Spacer(1, 0.05*cm),
                        tbl4,
                    ]))
                    story.append(Paragraph(
                        'Rows represent actual reference classes; columns represent predicted classes. '
                        'Diagonal cells (highlighted in green) indicate correctly classified pixels. '
                        'Off-diagonal values represent classification errors — row-wise off-diagonals '
                        'are omission errors (missed detections); column-wise off-diagonals are '
                        'commission errors (false alarms).',
                        S['fig_desc']))
                    story.append(Spacer(1, 0.1*cm))

    # ── 5. Discussion ──────────────────────────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph('5. Discussion', S['h1']))
    story += section_body(sections.get('discussion', ''))

    # ── 6. Conclusion ──────────────────────────────────────────────────────────
    conclusion_paras = section_body(sections.get('conclusion_section', ''))
    if conclusion_paras:
        story.append(KeepTogether([Paragraph('6. Conclusion', S['h1']), conclusion_paras[0]]))
        story += conclusion_paras[1:]
    else:
        story.append(Paragraph('6. Conclusion', S['h1']))

    # ── References ─────────────────────────────────────────────────────────────
    refs = sections.get('references', [])
    if refs:
        story.append(PageBreak())
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
    # Extract LULC ml_metrics — try all possible storage locations
    lulc_ml_metrics = (
        all_stats.get('LULC', {}).get('ml_metrics', {}) or
        analysis_result.get('lulc_ml_metrics', {}) or
        figures.get('LULC', {}).get('ml_metrics', {}) or  # stored in figures by app.py
        {}
    )
    lulc_classes = (
        all_stats.get('LULC', {}).get('classes', {}) or
        analysis_result.get('lulc_classes', {}) or
        {}
    )
    print(f'[research_agent] Figures available: {list(figures.keys())}')
    if lulc_ml_metrics:
        oa = lulc_ml_metrics.get('overall_accuracy', 0)
        cm = lulc_ml_metrics.get('confusion_matrix', None)
        cn = lulc_ml_metrics.get('class_names', [])
        pc = lulc_ml_metrics.get('per_class', {})
        print(f'[research_agent] LULC ml_metrics: OA={oa:.3f}, confusion_matrix={cm is not None}, '
              f'class_names={cn}, per_class_keys={list(pc.keys())}')
    else:
        print(f'[research_agent] WARNING: lulc_ml_metrics is EMPTY — tables will not render!')
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
        'figures'           : figures,        # b64 maps + charts from GIS Agent
        'lulc_ml_metrics'   : lulc_ml_metrics,
        'lulc_classes'      : lulc_classes,
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