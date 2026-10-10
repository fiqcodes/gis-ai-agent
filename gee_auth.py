"""Lazy service-account initialization inside each isolated analysis process."""
import json
import os
from pathlib import Path
from config import GEE_PROJECT, GEE_SERVICE_ACCOUNT_FILE

_initialized = False

def _service_account_info():
    raw = os.getenv('GEE_SERVICE_ACCOUNT_JSON')
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    required = ('project_id', 'private_key', 'client_email', 'token_uri')
    if (not isinstance(info, dict) or info.get('type') != 'service_account' or
            any(not isinstance(info.get(key), str) or not info[key].strip() for key in required)):
        return None
    return info

def configured():
    return bool(GEE_PROJECT and (_service_account_info() or
        (not os.getenv('VERCEL') and Path(GEE_SERVICE_ACCOUNT_FILE).is_file())))

def initialize_gee():
    global _initialized
    if _initialized:
        return
    import ee
    from google.oauth2 import service_account
    scopes = ['https://www.googleapis.com/auth/earthengine', 'https://www.googleapis.com/auth/cloud-platform']
    info = _service_account_info()
    if info:
        credentials = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    elif os.getenv('GEE_SERVICE_ACCOUNT_JSON'):
        raise RuntimeError('GEE_SERVICE_ACCOUNT_JSON must contain one valid service-account JSON object.')
    elif not os.getenv('VERCEL') and Path(GEE_SERVICE_ACCOUNT_FILE).is_file():
        credentials = service_account.Credentials.from_service_account_file(GEE_SERVICE_ACCOUNT_FILE, scopes=scopes)
    else:
        raise RuntimeError('Earth Engine service account has not been configured.')
    ee.Initialize(credentials, project=GEE_PROJECT, opt_url='https://earthengine.googleapis.com')
    ee.data.setDeadline(45000)
    ee.data.setMaxRetries(1)
    _initialized = True
