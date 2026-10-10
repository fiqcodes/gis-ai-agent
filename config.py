"""Server configuration. Secrets come from environment variables, never the browser."""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv('.env.local')
GEE_PROJECT = os.getenv('GEE_PROJECT', 'gis-ai-agent')
GEE_SERVICE_ACCOUNT_FILE = os.getenv('GEE_SERVICE_ACCOUNT_FILE', str(Path(__file__).parent / 'gee-creds.json'))
GROQ_URL = 'https://api.groq.com/openai/v1/chat/completions'
GROQ_MODEL = os.getenv('GROQ_MODEL', 'openai/gpt-oss-120b')
OUTPUT_DIR = os.getenv('OUTPUT_DIR', '/tmp/orbiview')
os.environ.setdefault('MPLCONFIGDIR', '/tmp/orbiview-matplotlib')
os.environ.setdefault('MPLBACKEND', 'Agg')
