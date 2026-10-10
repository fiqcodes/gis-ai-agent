"""Lazy service-account initialization inside each isolated analysis process."""
import json
import os
from pathlib import Path
from config import GEE_PROJECT, GEE_SERVICE_ACCOUNT_FILE

_initialized = False

def configured():
    return bool(GEE_PROJECT and (os.getenv('GEE_SERVICE_ACCOUNT_JSON') or
        (not os.getenv('VERCEL') and Path(GEE_SERVICE_ACCOUNT_FILE).is_file())))

def initialize_gee():
    global _initialized
    if _initialized:
        return
    import ee
    from google.oauth2 import service_account
    scopes = ['https://www.googleapis.com/auth/earthengine', 'https://www.googleapis.com/auth/cloud-platform']
    raw = os.getenv('GEE_SERVICE_ACCOUNT_JSON')
    if raw:
        credentials = service_account.Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
    elif not os.getenv('VERCEL') and Path(GEE_SERVICE_ACCOUNT_FILE).is_file():
        credentials = service_account.Credentials.from_service_account_file(GEE_SERVICE_ACCOUNT_FILE, scopes=scopes)
    else:
        raise RuntimeError('Earth Engine service account has not been configured.')
    ee.Initialize(credentials, project=GEE_PROJECT, opt_url='https://earthengine.googleapis.com')
    ee.data.setDeadline(45000)
    ee.data.setMaxRetries(1)
    _initialized = True
