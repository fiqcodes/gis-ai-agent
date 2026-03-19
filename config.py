# =============================================================================
# config.py — Configuration for Satellite Analysis AI Agent
# =============================================================================
import os

# ── Google Earth Engine ───────────────────────────────────────────────────────
GEE_PROJECT = 'case-study-360616'   # ← change to your GEE project ID

# ── Ollama (Local LLM) ────────────────────────────────────────────────────────
OLLAMA_URL   = 'http://localhost:11434/api/chat'
OLLAMA_MODEL = 'gemma3:4b'          # ← change to your local model

# ── Output folder ─────────────────────────────────────────────────────────────
OUTPUT_DIR = os.path.expanduser('~/Downloads/satellite_agent_outputs')
os.makedirs(OUTPUT_DIR, exist_ok=True)
