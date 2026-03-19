# =============================================================================
# agent.py — LangGraph Satellite Analysis AI Agent
# 
# Architecture:
#   START → ROUTER
#     ├── surface vars  → SURFACE_WORKER ──┐
#     ├── atmo vars     → ATMO_WORKER ─────┤→ MERGE → CONTEXT → STATS
#     ├── lulc          → LULC_WORKER ─────┘       → INSIGHT → EVALUATOR
#     ├── question      → QA → END                  ↓
#     └── unknown       → UNKNOWN → END         FINAL_OUTPUT → END
#
# Usage:
#   python agent.py
# =============================================================================

import ee
import requests
import json
import os
import textwrap
import operator
from typing import Annotated, Literal, List, Optional
from typing_extensions import TypedDict

from langchain_core.messages import HumanMessage, AIMessage
from langgraph.graph import StateGraph, START, END
from pydantic import BaseModel, Field

from config import GEE_PROJECT, OLLAMA_URL, OLLAMA_MODEL, OUTPUT_DIR
from gis_functions import (
    # Region
    resolve_region,
    # Surface
    load_landsat, compute_ndvi, compute_evi, compute_savi,
    compute_ndwi, compute_mndwi, compute_ndbi, compute_ui,
    compute_nbi, compute_bsi, compute_ndsi, compute_lst,
    compute_uhi, compute_lst_simple,
    # Atmospheric
    compute_co, compute_ch4, compute_no2, compute_so2,
    compute_aerosol, compute_o3, compute_gpp, compute_burned,
    compute_ffpi,
    # LULC
    compute_lulc, plot_lulc_with_pie,
    # Stats + plots
    get_stats, plot_panels,
    # Web + insight
    fetch_web_context, generate_insight,
    # Maps
    SURFACE_INDEX_MAP, ATMO_INDEX_MAP, KEYWORD_MAP, SYSTEM_PROMPT, VIS,
)

# ── Initialize GEE ────────────────────────────────────────────────────────────
ee.Initialize(project=GEE_PROJECT)
print(f'✅ GEE initialized | Model: {OLLAMA_MODEL}')
print(f'✅ Outputs: {OUTPUT_DIR}')

# ── Test Ollama ───────────────────────────────────────────────────────────────
try:
    r = requests.get('http://localhost:11434/api/tags', timeout=3)
    models = [m['name'] for m in r.json().get('models', [])]
    print(f'✅ Ollama connected | Models: {models}')
except Exception as e:
    print(f'⚠️  Ollama not reachable: {e} — run: ollama serve')

# =============================================================================
# LANGGRAPH TOOLS
# =============================================================================
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from typing import List, Optional
import datetime

# ── Input schemas ─────────────────────────────────────────────────────────────

class RegionDateVars(BaseModel):
    region_name : str  = Field(description="City, country or region name")
    start_date  : str  = Field(description="Start date YYYY-MM-DD")
    end_date    : str  = Field(description="End date YYYY-MM-DD")
    variables   : List[str] = Field(description="List of indices e.g. ['ndvi','lst']")

class RegionDate(BaseModel):
    region_name : str  = Field(description="City, country or region name")
    start_date  : str  = Field(description="Start date YYYY-MM-DD")
    end_date    : str  = Field(description="End date YYYY-MM-DD")

# ── Tool 1: resolve region ────────────────────────────────────────────────────

@tool
def tool_resolve_region(region_name: str) -> dict:
    """Resolve a place name to a GEE geometry bounding box.
    Returns a dict with keys: success (bool), bbox (list), message (str)."""
    try:
        geom = resolve_region(region_name)
        coords = geom.bounds().getInfo()['coordinates'][0]
        xs = [c[0] for c in coords]
        ys = [c[1] for c in coords]
        return {
            "success": True,
            "bbox": [min(xs), min(ys), max(xs), max(ys)],
            "message": f"Resolved {region_name} successfully"
        }
    except Exception as e:
        return {"success": False, "bbox": None, "message": str(e)}

# ── Tool 2: run surface analysis ──────────────────────────────────────────────

@tool("run_surface_analysis", args_schema=RegionDateVars)
def tool_surface_analysis(region_name: str, start_date: str,
                           end_date: str, variables: List[str]) -> dict:
    """Run Landsat 8 surface index analysis (NDVI, EVI, LST, UHI, NDBI, etc).
    Returns stats summary dict and saves map to OUTPUT_DIR."""
    surface_keys = list(SURFACE_INDEX_MAP.keys()) + ['lst','uhi','rgb','lulc']
    vars_filtered = [v for v in variables if v.lower() in surface_keys]
    if not vars_filtered:
        return {"success": False, "stats": {}, "message": "No surface variables requested"}

    try:
        study_area = resolve_region(region_name)
        landsat_col, composite = load_landsat(study_area, start_date, end_date)
        count = landsat_col.size().getInfo()
        if count == 0:
            return {"success": False, "stats": {}, "message": "No Landsat scenes found"}

        panels, stats = [], {}
        lst_img = None
        for v in vars_filtered:
            try:
                if v == 'lulc':
                    print(f"  [LULC] Running land cover classification for {region_name}...")
                    try:
                        lulc_result = compute_lulc(study_area, start_date, end_date, region_name)
                        print(f"  [LULC] Result success: {lulc_result.get('success')}")
                        print(f"  [LULC] Message: {lulc_result.get('message')}")
                        if lulc_result['success']:
                            stats['LULC'] = lulc_result['stats']
                            plot_lulc_with_pie(
                                [(lulc_result['lulc_img'], lulc_result['vis_params'],
                                  'Land Cover Classification', study_area)],
                                f'Land Cover - {region_name} | {start_date} to {end_date}',
                                lulc_result['stats']
                            )
                            print(f"  ✓ LULC done: {lulc_result['message']}")
                        else:
                            print(f"  ✗ LULC failed: {lulc_result['message']}")
                    except Exception as lulc_err:
                        import traceback
                        print(f"  ✗ LULC exception: {lulc_err}")
                        traceback.print_exc()
                elif v == 'rgb':
                    panels.append((composite, VIS['rgb'], 'True Color (RGB)', study_area))
                elif v == 'lst':
                    lst_img, _ = compute_lst(composite, study_area)
                    s = get_stats(lst_img, 'LST', study_area, scale=90)
                    s['monthly'] = _monthly_surface(landsat_col, compute_lst_simple, 'LST', study_area, start_date, end_date, 90)
                    stats['LST'] = s
                    panels.append((lst_img, VIS['lst'], 'LST (°C)', study_area))
                elif v == 'uhi':
                    if lst_img is None:
                        lst_img, _ = compute_lst(composite, study_area)
                    uhi_img, lst_mean, lst_std = compute_uhi(lst_img, study_area)
                    stats['UHI'] = {'mean': 0.0, 'lst_mean': lst_mean, 'lst_std': lst_std}
                    panels.append((uhi_img, VIS['uhi'], f'UHI (mean={lst_mean:.1f}°C)', study_area))
                elif v in SURFACE_INDEX_MAP:
                    label, func, vis_key, scale = SURFACE_INDEX_MAP[v]
                    img = func(composite)
                    s = get_stats(img, label, study_area, scale=scale)
                    s['monthly'] = _monthly_surface(landsat_col, func, label, study_area, start_date, end_date, scale)
                    stats[label] = s
                    panels.append((img, VIS[vis_key], label, study_area))
            except Exception as e:
                print(f"  [{v}] failed: {e}")

        if panels:
            title = f'Surface Analysis - {region_name} | {start_date} to {end_date}'
            plot_panels(panels, title)

        return {"success": True, "stats": stats,
                "message": f"Surface analysis done: {list(stats.keys())}"}
    except Exception as e:
        return {"success": False, "stats": {}, "message": str(e)}

# ── Tool 3: run atmospheric analysis ─────────────────────────────────────────

@tool("run_atmo_analysis", args_schema=RegionDateVars)
def tool_atmo_analysis(region_name: str, start_date: str,
                        end_date: str, variables: List[str]) -> dict:
    """Run Sentinel-5P atmospheric analysis (NO2, CO, SO2, CH4, Aerosol, O3, FFPI, etc).
    Returns stats summary dict and saves map to OUTPUT_DIR."""
    atmo_keys = list(ATMO_INDEX_MAP.keys()) + ['ffpi']
    vars_filtered = [v for v in variables if v.lower() in atmo_keys]
    if not vars_filtered:
        return {"success": False, "stats": {}, "message": "No atmospheric variables requested"}

    try:
        study_area = resolve_region(region_name)
        panels, stats = [], {}

        for v in vars_filtered:
            try:
                if v == 'ffpi':
                    ffpi_img, ffpi_class = compute_ffpi(study_area, start_date, end_date)
                    s = get_stats(ffpi_img, 'FFPI', study_area, scale=3500)
                    stats['FFPI'] = s
                    panels.append((ffpi_img,   VIS['ffpi'],       'FFPI Score',           study_area))
                    panels.append((ffpi_class, VIS['ffpi_class'], 'FFPI Pollution Zones',  study_area))
                elif v in ATMO_INDEX_MAP:
                    label, func, vis_key, unit = ATMO_INDEX_MAP[v]
                    img, col = func(study_area, start_date, end_date)
                    count = col.size().getInfo()
                    if count > 0:
                        band_name = img.bandNames().getInfo()[0]
                        s = get_stats(img, band_name, study_area, scale=3500)
                        s['monthly'] = _monthly_atmo(col, band_name, study_area, start_date, end_date)
                        stats[label] = s
                        panels.append((img, VIS[vis_key], f'{label} ({unit})', study_area))
            except Exception as e:
                print(f"  [{v}] failed: {e}")

        if panels:
            title = f'Atmospheric Analysis - {region_name} | {start_date} to {end_date}'
            plot_panels(panels, title)

        return {"success": True, "stats": stats,
                "message": f"Atmospheric analysis done: {list(stats.keys())}"}
    except Exception as e:
        return {"success": False, "stats": {}, "message": str(e)}

# ── Tool 4: fetch web context ─────────────────────────────────────────────────

@tool("fetch_context", args_schema=RegionDateVars)
def tool_fetch_context(region_name: str, start_date: str,
                        end_date: str, variables: List[str]) -> dict:
    """Search the web for real-world context about the region and variables.
    Returns a dict with web_context string."""
    ctx = fetch_web_context(region_name, start_date, end_date, variables)
    return {
        "success": ctx is not None,
        "web_context": ctx or "",
        "message": f"{len(ctx.split(chr(10)+chr(10))) if ctx else 0} snippets retrieved"
    }

# ── Tool 5: generate insight ──────────────────────────────────────────────────

class InsightInput(BaseModel):
    region_name   : str        = Field(description="Region name")
    start_date    : str        = Field(description="Start date YYYY-MM-DD")
    end_date      : str        = Field(description="End date YYYY-MM-DD")
    stats_summary : dict       = Field(description="Stats dict from surface/atmo tools")
    variables     : List[str]  = Field(description="Variables analyzed")
    web_context   : str        = Field(default="", description="Web context string")

@tool("generate_insight_tool", args_schema=InsightInput)
def tool_generate_insight(region_name: str, start_date: str, end_date: str,
                           stats_summary: dict, variables: List[str],
                           web_context: str = "") -> dict:
    """Generate a scientific AI insight from satellite stats + web context.
    Returns insight text."""
    # Inject web_context into the existing generate_insight function
    import types
    insight = generate_insight(region_name, start_date, end_date,
                               stats_summary, variables)
    return {"success": True, "insight": insight or "No insight generated"}

# ── Helper: monthly stats for surface ────────────────────────────────────────

def _monthly_surface(landsat_col, func, label, study_area, start_date, end_date, scale):
    monthly = {}
    start_dt = datetime.datetime.strptime(start_date, "%Y-%m-%d").replace(day=1)
    end_dt   = datetime.datetime.strptime(end_date,   "%Y-%m-%d")
    cur = start_dt
    while cur <= end_dt:
        m_start = cur.strftime("%Y-%m-%d")
        m_end   = (cur.replace(month=cur.month % 12 + 1, day=1) if cur.month < 12
                   else cur.replace(year=cur.year+1, month=1, day=1)).strftime("%Y-%m-%d")
        try:
            scenes = landsat_col.filterDate(m_start, m_end)
            if scenes.size().getInfo() > 0:
                img = func(scenes.median())
                val = img.reduceRegion(ee.Reducer.mean(), study_area, scale, maxPixels=1e9).getInfo()
                v   = val.get(label)
                if v is not None:
                    monthly[cur.strftime("%Y-%m")] = round(v, 6)
        except: pass
        cur = (cur.replace(month=cur.month % 12 + 1, day=1) if cur.month < 12
               else cur.replace(year=cur.year+1, month=1, day=1))
    return monthly

def _monthly_atmo(col, band_name, study_area, start_date, end_date):
    monthly = {}
    start_dt = datetime.datetime.strptime(start_date, "%Y-%m-%d").replace(day=1)
    end_dt   = datetime.datetime.strptime(end_date,   "%Y-%m-%d")
    cur = start_dt
    while cur <= end_dt:
        m_start = cur.strftime("%Y-%m-%d")
        m_end   = (cur.replace(month=cur.month % 12 + 1, day=1) if cur.month < 12
                   else cur.replace(year=cur.year+1, month=1, day=1)).strftime("%Y-%m-%d")
        try:
            sub = col.filterDate(m_start, m_end)
            if sub.size().getInfo() > 0:
                val = sub.mean().reduceRegion(ee.Reducer.mean(), study_area, 3500, maxPixels=1e9).getInfo()
                v   = val.get(band_name)
                if v is not None:
                    monthly[cur.strftime("%Y-%m")] = round(v, 6)
        except: pass
        cur = (cur.replace(month=cur.month % 12 + 1, day=1) if cur.month < 12
               else cur.replace(year=cur.year+1, month=1, day=1))
    return monthly

def compute_lst_simple(composite):
    """LST wrapper that returns a single-band image for monthly stats."""
    lst, _ = compute_lst(composite, composite.geometry())
    return lst


# ── Tool 6: LULC analysis — standalone, independent of surface_analysis ───────

@tool("run_lulc_analysis", args_schema=RegionDateVars)
def tool_lulc_analysis(region_name: str, start_date: str,
                        end_date: str, variables: List[str]) -> dict:
    """Run Land Cover Classification using Random Forest + ESRI training labels.
    Produces a classified map + pie chart saved to OUTPUT_DIR.
    Returns area stats per class in hectares and percentage."""
    try:
        study_area = resolve_region(region_name)
        print(f"  [LULC] Starting classification for {region_name}...")
        result = compute_lulc(study_area, start_date, end_date, region_name)
        print(f"  [LULC] compute_lulc returned success={result.get('success')}")
        print(f"  [LULC] message: {result.get('message')}")

        if not result['success']:
            return {"success": False, "stats": {}, "message": result['message']}

        # Plot map + pie chart
        plot_lulc_with_pie(
            [(result['lulc_img'], result['vis_params'],
              'Land Cover Classification', study_area)],
            f'Land Cover - {region_name} | {start_date} to {end_date}',
            result['stats']
        )

        return {
            "success": True,
            "stats"  : {"LULC": result['stats']},
            "message": result['message'],
        }
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "stats": {}, "message": str(e)}

# Register all tools
ALL_TOOLS = [
    tool_resolve_region,
    tool_surface_analysis,
    tool_atmo_analysis,
    tool_lulc_analysis,
    tool_fetch_context,
    tool_generate_insight,
]

print(f"✅ {len(ALL_TOOLS)} LangGraph tools registered:")
for t in ALL_TOOLS:
    print(f"   • {t.name}: {t.description[:60]}...")


# =============================================================================
# LANGGRAPH AGENT GRAPH
# =============================================================================
from typing import Annotated, Literal, List, Optional
import operator
from typing_extensions import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.types import Send
from langchain_ollama import ChatOllama
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage
from pydantic import BaseModel, Field

# ── LLM (Ollama) ──────────────────────────────────────────────────────────────
llm = ChatOllama(model=OLLAMA_MODEL, base_url="http://localhost:11434", temperature=0)
llm_with_tools = llm.bind_tools(ALL_TOOLS)

# ── State ─────────────────────────────────────────────────────────────────────
class AgentState(TypedDict):
    # Input
    user_input      : str
    # Parsed intent
    region          : Optional[str]
    start_date      : Optional[str]
    end_date        : Optional[str]
    variables       : List[str]
    intent          : str                    # "analysis" | "question" | "unknown"
    # Worker results (accumulated with operator.add)
    surface_stats   : dict
    atmo_stats      : dict
    web_context     : str
    # Insight + evaluation
    insight         : str
    eval_score      : int                    # 1-10
    eval_feedback   : str
    refine_count    : int
    # Final
    final_response  : str
    messages        : Annotated[list, operator.add]

# ── Pydantic schemas for structured outputs ───────────────────────────────────
class ParsedRequest(BaseModel):
    intent     : Literal["analysis", "question", "unknown"]
    region     : Optional[str]        = None
    start_date : Optional[str]        = None
    end_date   : Optional[str]        = None
    variables  : List[str]            = Field(default_factory=list)
    response   : str                  = ""

class InsightEval(BaseModel):
    score    : int   = Field(ge=1, le=10, description="Quality score 1-10")
    feedback : str   = Field(description="Specific feedback for improvement if score < 7")
    accept   : bool  = Field(description="True if score >= 7 and insight is acceptable")

# ── NODE 1: Router — always use raw JSON fallback for gemma3 ─────────────────
def node_router(state: AgentState) -> dict:
    """Parse user intent using raw JSON (reliable for gemma3:4b)."""
    print("\n[ROUTER] Parsing user request...")
    resp = requests.post(OLLAMA_URL,
        json={"model": OLLAMA_MODEL,
              "messages": [
                  {"role": "system", "content": SYSTEM_PROMPT},
                  {"role": "user",   "content": state["user_input"]}],
              "stream": False}, timeout=60)
    data = resp.json()
    raw  = data.get("message", {}).get("content", "{}").strip()
    if "```" in raw:
        raw = raw.split("```")[1]
        if raw.startswith("json"): raw = raw[4:]
    start = raw.find("{"); end = raw.rfind("}") + 1
    if start >= 0 and end > start: raw = raw[start:end]
    try:
        parsed = json.loads(raw)
    except Exception as e:
        print(f"  JSON parse failed: {e}")
        parsed = {"intent": "unknown", "region": None, "start_date": None,
                  "end_date": None, "variables": [], "response": ""}

    # Normalize variables
    vars_out = parsed.get("variables") or []
    if "all_surface" in vars_out:
        vars_out = list(SURFACE_INDEX_MAP.keys()) + ["lst", "uhi"]
    if "all_atmo" in vars_out:
        vars_out = list(ATMO_INDEX_MAP.keys())
    normalized = []
    for v in vars_out:
        vl = v.lower().strip()
        normalized.append(KEYWORD_MAP.get(vl, vl))
    vars_out = list(dict.fromkeys(normalized))

    print(f"  Intent: {parsed.get('intent')} | Region: {parsed.get('region')} | Vars: {vars_out}")
    return {
        "intent"    : parsed.get("intent", "unknown"),
        "region"    : parsed.get("region"),
        "start_date": parsed.get("start_date"),
        "end_date"  : parsed.get("end_date"),
        "variables" : vars_out,
        "messages"  : [AIMessage(content=parsed.get("response", ""))],
    }

# ── Routing edge — smart branching ───────────────────────────────────────────
def edge_route(state: AgentState) -> list:
    """Return list of next nodes based on what variables were requested."""
    intent = state["intent"]

    if intent == "qa":
        return ["qa"]
    if intent == "unknown":
        return ["unknown"]

    # For analysis: check which worker(s) are needed
    surface_keys = list(SURFACE_INDEX_MAP.keys()) + ["lst", "uhi", "rgb"]
    atmo_keys    = list(ATMO_INDEX_MAP.keys()) + ["ffpi"]
    vars_        = state.get("variables", [])

    needs_surface = any(v in surface_keys for v in vars_)
    needs_atmo    = any(v in atmo_keys    for v in vars_)
    needs_lulc    = "lulc" in vars_

    routes = []
    if needs_surface: routes.append("surface_worker")
    if needs_atmo:    routes.append("atmo_worker")
    if needs_lulc:    routes.append("lulc_worker")
    if not routes:    routes.append("unknown")
    return routes

# ── NODE 2a: Surface Worker ───────────────────────────────────────────────────
def node_surface_worker(state: AgentState) -> dict:
    """Run Landsat 8 surface index analysis."""
    # Guard: skip if router didn't extract required fields
    if not state.get("region") or not state.get("start_date") or not state.get("end_date"):
        print("\n[SURFACE WORKER] Skipped — missing region or dates")
        return {"surface_stats": {}}

    surface_vars = [v for v in state["variables"]
                    if v in list(SURFACE_INDEX_MAP.keys()) + ["lst","uhi","rgb"]]
    if not surface_vars:
        return {"surface_stats": {}}

    print(f"\n[SURFACE WORKER] Running: {surface_vars}")
    result = tool_surface_analysis.invoke({
        "region_name": state["region"],
        "start_date" : state["start_date"],
        "end_date"   : state["end_date"],
        "variables"  : surface_vars,
    })
    stats = result.get("stats", {})
    # For LULC: stats may be empty if compute_lulc ran standalone (plots already saved)
    # Re-check if lulc was requested and add placeholder so pipeline continues
    if "lulc" in surface_vars and not stats:
        stats["LULC_STATUS"] = {"classes": {}, "total_ha": 0, "note": "See saved map in outputs folder"}
    print(f"  ✓ Surface done: {list(stats.keys())}")
    return {"surface_stats": stats}

# ── NODE 2b: Atmospheric Worker ───────────────────────────────────────────────
def node_atmo_worker(state: AgentState) -> dict:
    """Run Sentinel-5P atmospheric analysis."""
    # Guard: skip if router didn't extract required fields
    if not state.get("region") or not state.get("start_date") or not state.get("end_date"):
        print("\n[ATMO WORKER] Skipped — missing region or dates")
        return {"atmo_stats": {}}

    atmo_vars = [v for v in state["variables"]
                 if v in list(ATMO_INDEX_MAP.keys()) + ["ffpi"]]
    if not atmo_vars:
        return {"atmo_stats": {}}

    print(f"\n[ATMO WORKER] Running: {atmo_vars}")
    result = tool_atmo_analysis.invoke({
        "region_name": state["region"],
        "start_date" : state["start_date"],
        "end_date"   : state["end_date"],
        "variables"  : atmo_vars,
    })
    stats = result.get("stats", {})
    print(f"  ✓ Atmo done: {list(stats.keys())}")
    return {"atmo_stats": stats}

# ── NODE 2c: LULC Worker ─────────────────────────────────────────────────────
def node_lulc_worker(state: AgentState) -> dict:
    """Run dedicated Land Cover Classification worker."""
    if not state.get("region") or not state.get("start_date") or not state.get("end_date"):
        print("\n[LULC WORKER] Skipped — missing region or dates")
        return {"surface_stats": {}}

    if "lulc" not in [v.lower() for v in state.get("variables", [])]:
        return {"surface_stats": {}}

    print(f"\n[LULC WORKER] Running land cover classification for {state['region']}...")
    result = tool_lulc_analysis.invoke({
        "region_name": state["region"],
        "start_date" : state["start_date"],
        "end_date"   : state["end_date"],
        "variables"  : state["variables"],
    })
    stats = result.get("stats", {})
    msg   = result.get("message", "")
    if result.get("success"):
        print(f"  ✓ LULC done: {msg}")
    else:
        print(f"  ✗ LULC failed: {msg}")
    return {"surface_stats": stats}

# ── NODE 2d: Web Context Worker ───────────────────────────────────────────────
def node_context_worker(state: AgentState) -> dict:
    """Fetch web context for the region and variables."""
    # Guard: skip if missing required fields
    if not state.get("region") or not state.get("start_date") or not state.get("end_date"):
        print("\n[CONTEXT WORKER] Skipped — missing region or dates")
        return {"web_context": ""}

    print(f"\n[CONTEXT WORKER] Fetching web context for {state['region']}...")
    result = tool_fetch_context.invoke({
        "region_name": state["region"],
        "start_date" : state["start_date"],
        "end_date"   : state["end_date"],
        "variables"  : state["variables"],
    })
    ctx = result.get("web_context", "")
    print(f"  ✓ Context: {result['message']}")
    return {"web_context": ctx}

# ── NODE 3: Stats Printer ─────────────────────────────────────────────────────
def node_print_stats(state: AgentState) -> dict:
    """Print statistics summary to console."""
    all_stats = {**state.get("surface_stats", {}), **state.get("atmo_stats", {})}
    if not all_stats:
        return {}
    sep  = "=" * 60
    dash = "-" * 45
    print(f"\n{sep}")
    print(f"  STATISTICS SUMMARY — {state['region']}")
    print(f"  Period: {state['start_date']} to {state['end_date']}")
    print(f"  {dash}")
    for var, s in all_stats.items():
        if isinstance(s, dict) and s.get("mean") is not None:
            print(f"  {var:<12} mean={s.get('mean',0):.4f}  "
                  f"median={s.get('median',0) or 0:.4f}  std={s.get('std',0) or 0:.4f}")
            print(f"  {'':12} min={s.get('min',0):.4f}   max={s.get('max',0):.4f}")
            print(f"  {'':12} p10={s.get('p10',0) or 0:.4f}   p90={s.get('p90',0) or 0:.4f}")
            monthly = s.get("monthly", {})
            if monthly:
                print(f"  {'':12} Monthly mean:")
                line = ""
                for m, v in sorted(monthly.items()):
                    entry = f"{m}:{v:.3f}  "
                    if len(line) + len(entry) > 52:
                        print(f"  {'':12}   {line.strip()}")
                        line = entry
                    else:
                        line += entry
                if line.strip():
                    print(f"  {'':12}   {line.strip()}")
        elif isinstance(s, dict) and "lst_mean" in s:
            print(f"  {'UHI':<12} LST mean={s['lst_mean']:.2f}°C  std={s['lst_std']:.2f}°C")
        elif isinstance(s, dict) and "classes" in s:
            # LULC stats
            total_ha = s.get('total_ha', 0)
            n_cls    = s.get('n_classes', 0)
            print(f"  {'LULC':<12} {n_cls} classes | Total: {total_ha:,.0f} ha")
            for cls_name, cls_data in s.get('classes', {}).items():
                ha  = cls_data.get('hectares', 0)
                pct = cls_data.get('percentage', 0)
                print(f"  {'':12}   {cls_name:<16} {ha:>10,.1f} ha  ({pct:.1f}%)")
    print(f"  {dash}")
    return {}

# ── NODE 4: Insight Generator ─────────────────────────────────────────────────
def node_insight_generator(state: AgentState) -> dict:
    """Generate scientific insight from stats + web context."""
    all_stats = {**state.get("surface_stats", {}), **state.get("atmo_stats", {})}
    feedback  = state.get("eval_feedback", "")
    count     = state.get("refine_count", 0)

    print(f"\n[INSIGHT GENERATOR] Generating insight (attempt {count+1})...")

    # Build stats text
    stats_lines = []
    UNIT_LOOKUP_LOCAL = {
        "NDVI":"index (-1 to 1)", "EVI":"index (-1 to 1)", "SAVI":"index (-1 to 1)",
        "NDWI":"index (-1 to 1)", "MNDWI":"index (-1 to 1)", "NDBI":"index (-1 to 1)",
        "UI":"index (-1 to 1)", "BSI":"index (-1 to 1)", "NDSI":"index (-1 to 1)",
        "NBI":"index (0 to 0.5)", "LST":"degrees Celsius",
        "CO":"mol/m2", "NO2":"mol/m2", "SO2":"mol/m2",
        "CH4":"ppb", "O3":"Dobson Units (DU)", "Aerosol":"unitless AAI",
        "GPP":"kgC/m2/8-day", "Burned Area":"Day of Year 1-366",
        "FFPI":"normalized 0-1",
    }
    for var, s in all_stats.items():
        unit = next((v for k,v in UNIT_LOOKUP_LOCAL.items()
                     if k.upper() in var.upper()), "dimensionless")
        if isinstance(s, dict) and s.get("mean") is not None:
            line = (f"  - {var} [{unit}]: mean={s['mean']:.6f}, "
                    f"min={s.get('min',0):.6f}, max={s.get('max',0):.6f}")
            for k in ["std","median","p10","p90"]:
                if s.get(k) is not None: line += f", {k}={s[k]:.6f}"
            stats_lines.append(line)
            monthly = s.get("monthly", {})
            if monthly:
                mstr = ", ".join(f"{m}:{v:.4f}" for m,v in sorted(monthly.items()))
                stats_lines.append(f"    Monthly: {mstr}")
        elif isinstance(s, dict) and "lst_mean" in s:
            stats_lines.append(f"  - UHI: LST mean={s['lst_mean']:.2f}°C std={s['lst_std']:.2f}°C")
        elif isinstance(s, dict) and "classes" in s:
            # LULC: format class breakdown for LLM
            total_ha = s.get('total_ha', 0)
            cls_lines = []
            for cls_name, cls_data in s.get('classes', {}).items():
                ha  = cls_data.get('hectares', 0)
                pct = cls_data.get('percentage', 0)
                cls_lines.append(f"{cls_name}: {ha:,.0f} ha ({pct:.1f}%)")
            stats_lines.append(
                f"  - LULC [area in hectares and %]: total={total_ha:,.0f} ha | "
                + " | ".join(cls_lines)
            )

    web_ctx  = state.get("web_context", "")
    web_sect = (f"\nReal-world context from web search:\n{web_ctx}\n" if web_ctx else "")
    feedback_sect = (f"\nPREVIOUS FEEDBACK TO ADDRESS:\n{feedback}\n" if feedback else "")

    prompt = (
        "You are an expert remote sensing and geospatial scientist writing a scientific briefing.\n"
        "RULES:\n"
        "- Use exact units shown in brackets. Never say \"ppm\".\n"
        "- p10 = 10% of pixels are BELOW this value (degraded areas).\n"
        "- p90 = 90% of pixels are BELOW this value (only top 10% exceed it).\n"
        "- Use monthly data to identify seasonal patterns, peaks, dips.\n"
        "- For LULC: discuss EACH class by exact name, hectares, and percentage. Do NOT use p10/p90/std for LULC — those are irrelevant. Focus on: dominant class, surprising absences, urban vs natural balance, implications for environment and planning.\n"
        "- Name specific districts, landmarks, rivers when relevant.\n"
        "- Only cite web context directly relevant to this region.\n"
        + feedback_sect + web_sect
        + f"\nRegion  : {state['region']}"
        f"\nPeriod  : {state['start_date']} to {state['end_date']}"
        f"\nVars    : {', '.join(state['variables'])}"
        f"\n\nSatellite statistics:\n{''.join(stats_lines)}"
        "\n\nWrite a scientific insight (7-10 sentences) covering:\n"
        "1. Annual mean interpretation\n"
        "2. Seasonal pattern from monthly data\n"
        "3. Spatial variability (p10/p90/std)\n"
        "4. Specific geographic features driving patterns\n"
        "5. Connection to real-world events (web context)\n"
        "6. Socioeconomic/physical drivers\n"
        "7. One notable anomaly\n"
        "8. Concrete actionable recommendation\n"
        "\nWrite in flowing paragraphs, not bullet points."
    )

    resp = requests.post(
        OLLAMA_URL,
        json={"model": OLLAMA_MODEL,
              "messages": [{"role": "user", "content": prompt}],
              "stream": False},
        timeout=180
    )
    insight = resp.json()["message"]["content"].strip()
    print(f"  ✓ Insight generated ({len(insight)} chars)")
    return {
        "insight"      : insight,
        "refine_count" : count + 1,
    }

# ── NODE 5: Evaluator ─────────────────────────────────────────────────────────
def node_evaluator(state: AgentState) -> dict:
    """Evaluate insight quality using structured LLM output."""
    print(f"\n[EVALUATOR] Evaluating insight quality...")
    evaluator_llm = llm.with_structured_output(InsightEval)

    all_stats = {**state.get("surface_stats",{}), **state.get("atmo_stats",{})}
    stats_summary = {k: {"mean": v.get("mean"), "monthly": v.get("monthly",{})}
                     for k, v in all_stats.items() if isinstance(v, dict)}

    eval_prompt = (
        "You are a senior geospatial scientist evaluating a satellite analysis insight.\n\n"
        f"REGION: {state['region']} | PERIOD: {state['start_date']} to {state['end_date']}\n"
        f"VARIABLES: {', '.join(state['variables'])}\n\n"
        f"STATISTICS SUMMARY: {json.dumps(stats_summary, indent=2)[:2000]}\n\n"
        f"INSIGHT TO EVALUATE:\n{state['insight']}\n\n"
        "Score this insight 1-10 on:\n"
        "- Scientific accuracy (units correct, no hallucinated values)\n"
        "- Use of seasonal/monthly data\n"
        "- Correct interpretation of p10/p90\n"
        "- Specificity (named places, real events)\n"
        "- Actionability of recommendation\n"
        "Accept (score >= 7) or reject with specific feedback."
    )

    result = evaluator_llm.invoke(eval_prompt)
    print(f"  ✓ Eval score: {result.score}/10 | Accept: {result.accept}")
    if not result.accept:
        print(f"  ✗ Feedback: {result.feedback[:100]}...")
    return {
        "eval_score"   : result.score,
        "eval_feedback": result.feedback,
    }

# ── Routing edge: accept or refine ────────────────────────────────────────────
def edge_eval(state: AgentState) -> str:
    if state.get("eval_score", 0) >= 7 or state.get("refine_count", 0) >= 3:
        return "accept"
    return "refine"

# ── NODE 6: Final output printer ──────────────────────────────────────────────
def node_final_output(state: AgentState) -> dict:
    """Print final insight to console."""
    sep = "=" * 60
    print(f"\n{sep}")
    print(f"  AI INSIGHT  (score: {state.get('eval_score','?')}/10)")
    print(f"  Region: {state['region']} | {state['start_date']} → {state['end_date']}")
    print(sep)
    insight = state.get("insight", "No insight generated.")
    print()
    for line in insight.split("\n"):
        if line.strip():
            for wline in textwrap.wrap(line.strip(), width=56):
                print(f"  {wline}")
        else:
            print()
    print(f"\n{sep}\n")
    return {"final_response": insight}

# ── NODE 7: QA node ───────────────────────────────────────────────────────────
def node_qa(state: AgentState) -> dict:
    """Answer general questions about satellite remote sensing."""
    print("\n[QA] Answering question...")
    resp = requests.post(OLLAMA_URL,
        json={"model": OLLAMA_MODEL,
              "messages": [
                  {"role": "system", "content": "You are an expert in satellite remote sensing and GIS."},
                  {"role": "user",   "content": state["user_input"]}],
              "stream": False}, timeout=60)
    answer = resp.json()["message"]["content"].strip()
    print(f"  ✓ Answer generated")
    return {"final_response": answer,
            "messages": [AIMessage(content=answer)]}

# ── NODE 8: Unknown handler ───────────────────────────────────────────────────
def node_unknown(state: AgentState) -> dict:
    msg = "I need more information. Please specify: region, date range, and what to analyze (e.g. NDVI, NO2, LST)."
    return {"final_response": msg, "messages": [AIMessage(content=msg)]}

# ── Add a merge node (waits for both workers if both ran) ────────────────────
def node_merge(state: AgentState) -> dict:
    """Merge point after parallel workers complete."""
    s_stats = state.get("surface_stats", {})
    a_stats = state.get("atmo_stats", {})
    total   = len(s_stats) + len(a_stats)
    print(f"\n[MERGE] Workers done — {total} variables ready")
    return {}

# ── Build the graph ───────────────────────────────────────────────────────────
builder = StateGraph(AgentState)

builder.add_node("router",            node_router)
builder.add_node("surface_worker",    node_surface_worker)
builder.add_node("atmo_worker",       node_atmo_worker)
builder.add_node("lulc_worker",       node_lulc_worker)
builder.add_node("merge",             node_merge)
builder.add_node("context_worker",    node_context_worker)
builder.add_node("print_stats",       node_print_stats)
builder.add_node("insight_generator", node_insight_generator)
builder.add_node("evaluator",         node_evaluator)
builder.add_node("final_output",      node_final_output)
builder.add_node("qa",                node_qa)
builder.add_node("unknown",           node_unknown)

# START → ROUTER
builder.add_edge(START, "router")

# ROUTER → smart branch (can fan out to multiple nodes in parallel)
builder.add_conditional_edges(
    "router",
    edge_route,
    ["surface_worker", "atmo_worker", "lulc_worker", "qa", "unknown"]
)

# All workers → merge
builder.add_edge("surface_worker", "merge")
builder.add_edge("atmo_worker",    "merge")
builder.add_edge("lulc_worker",    "merge")

# merge → context → stats → insight → evaluator loop
builder.add_edge("merge",             "context_worker")
builder.add_edge("context_worker",    "print_stats")
builder.add_edge("print_stats",       "insight_generator")
builder.add_edge("insight_generator", "evaluator")

builder.add_conditional_edges("evaluator", edge_eval, {
    "accept": "final_output",
    "refine": "insight_generator",
})

builder.add_edge("final_output", END)
builder.add_edge("qa",           END)
builder.add_edge("unknown",      END)

satellite_graph = builder.compile()
print("✅ LangGraph satellite agent compiled!")

# Graph visualization (uncomment in Jupyter)
# try:
#     from IPython.display import display, Image as IPImage
#     display(IPImage(satellite_graph.get_graph().draw_mermaid_png()))
# except Exception as e:
#     print(satellite_graph.get_graph().draw_mermaid())


# =============================================================================
# CHAT LOOP
# =============================================================================
VAR_DESCRIPTIONS = {
    "ndvi":"NDVI","evi":"EVI","savi":"SAVI","ndwi":"NDWI","mndwi":"MNDWI",
    "ndbi":"NDBI","ui":"UI","nbi":"NBI","bsi":"BSI","ndsi":"NDSI",
    "lst":"LST","uhi":"UHI","rgb":"True Color",
    "co":"CO","ch4":"CH4","no2":"NO2","so2":"SO2","aerosol":"Aerosol",
    "o3":"O3","gpp":"GPP","burned":"Burned Area","ffpi":"FFPI",
    "lulc":"Land Cover Classification (LULC)",
}
MONTHS = {"01":"Jan","02":"Feb","03":"Mar","04":"Apr","05":"May","06":"Jun",
          "07":"Jul","08":"Aug","09":"Sep","10":"Oct","11":"Nov","12":"Dec"}

def fmt_vars(variables):
    return ", ".join(VAR_DESCRIPTIONS.get(v.lower(), v.upper()) for v in variables)

def fmt_date(start, end):
    sy,sm = start[:4],start[5:7]
    ey,em = end[:4],  end[5:7]
    if sy == ey: return f"{sy} ({MONTHS.get(sm,sm)}–{MONTHS.get(em,em)})"
    return f"{MONTHS.get(sm,sm)} {sy} – {MONTHS.get(em,em)} {ey}"

print("╔══════════════════════════════════════════════════════╗")
print("║  🛰️  SATELLITE ANALYSIS AI AGENT  (LangGraph)       ║")
print("║  Router → Workers → Evaluator → Insight Loop        ║")
print("╚══════════════════════════════════════════════════════╝")
print(f"  Model   : {OLLAMA_MODEL}")
print(f"  Outputs : {OUTPUT_DIR}")
print()
print("  Examples:")
print("  → Show NDVI in Jakarta from 2023-01-01 to 2023-12-31")
print("  → Analyze NO2 and CO pollution in Beijing in 2022")
print("  → LST and UHI in Cairo, summer 2021")
print("  → What does NDVI measure?")
print("  → Type \"exit\" to quit")
print("─" * 56)

while True:
    try:
        user_input = input("\nYou: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n👋 Stopped.")
        break

    if not user_input:
        continue
    if user_input.lower() in ["exit","quit","bye","stop"]:
        print(f"👋 Goodbye! Outputs saved to: {OUTPUT_DIR}")
        break

    # ── Run the graph ─────────────────────────────────────────────────────────
    print("\n🤖 Agent running...")
    try:
        init_state = {
            "user_input"   : user_input,
            "region"       : None,
            "start_date"   : None,
            "end_date"     : None,
            "variables"    : [],
            "intent"       : "unknown",
            "surface_stats": {},
            "atmo_stats"   : {},
            "web_context"  : "",
            "insight"      : "",
            "eval_score"   : 0,
            "eval_feedback": "",
            "refine_count" : 0,
            "final_response": "",
            "messages"     : [HumanMessage(content=user_input)],
        }

        result = satellite_graph.invoke(init_state)

        # Print summary for analysis intent
        if result.get("intent") == "analysis":
            vars_str  = fmt_vars(result.get("variables", []))
            date_str  = fmt_date(result["start_date"], result["end_date"])
            print(f"\n🤖 Analyzing {vars_str} in {result['region']} ({date_str})")
            print(f"   Eval score: {result.get('eval_score','?')}/10 | "
                  f"Refinements: {result.get('refine_count',0)-1}")
        elif result.get("intent") == "question":
            print(f"\n🤖 {result.get('final_response','')}")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

    print("─" * 56)


if __name__ == '__main__':
    pass  # Chat loop runs automatically above
