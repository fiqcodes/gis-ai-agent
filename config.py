# =============================================================================
# config.py — Configuration for Satellite Analysis AI Agent
# =============================================================================
import os

# ── Google Earth Engine ───────────────────────────────────────────────────────
GEE_PROJECT = 'case-study-360616'

# ── Service Account (preferred — thread-safe, no expiry) ─────────────────────
# Path to your GEE service account JSON key file
GEE_SERVICE_ACCOUNT_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'gee-service-account.json'
)
GEE_SERVICE_ACCOUNT_EMAIL = 'gee-credentials@case-study-360616.iam.gserviceaccount.com'

# ── Ollama (Local LLM) ────────────────────────────────────────────────────────
OLLAMA_URL   = 'http://localhost:11434/api/chat'
OLLAMA_MODEL = 'gemma3:4b'

# ── Output folder ─────────────────────────────────────────────────────────────
OUTPUT_DIR = os.path.expanduser('~/Downloads/satellite_agent_outputs')
os.makedirs(OUTPUT_DIR, exist_ok=True)
