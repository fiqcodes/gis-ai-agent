"""Public Orbiview app with bounded, request-owned workers and private Redis artifacts."""
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
from urllib.parse import urlsplit
from flask import Flask, Response, jsonify, render_template, request, session, send_from_directory, stream_with_context
from werkzeug.exceptions import HTTPException
import config
from gee_auth import configured as gee_configured
from storage import configured_store, StorageUnavailable, ConversationBusy
from validation import validate_roi
from runtime import run_stream

app=Flask(__name__,static_folder='public/static')
production=bool(os.getenv('VERCEL'))
app.config.update(SECRET_KEY=os.getenv('APP_SECRET_KEY') or (None if production else secrets.token_hex(32)),
    SESSION_COOKIE_NAME='orbiview_session',SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SECURE=production,
    SESSION_COOKIE_SAMESITE='Strict',MAX_CONTENT_LENGTH=32_768,STORE=configured_store())
ID=re.compile(r'^[a-f0-9]{32}(?:\.pdf)?$')

def ready():
    return bool(app.secret_key and len(app.secret_key)>=32 and app.config['STORE'] and os.getenv('GROQ_API_KEY') and gee_configured())

def error(message,status):
    response=jsonify(error=message);response.status_code=status
    if status==429: response.headers['Retry-After']='60'
    return response

@app.before_request
def protect():
    if request.path.startswith(('/assets/','/static/')) or request.path=='/api/health':return
    if request.method=='POST':
        origin=request.headers.get('Origin')
        if request.headers.get('Sec-Fetch-Site')=='cross-site' or (origin and urlsplit(origin).netloc!=request.host):
            return error('Cross-site requests are not allowed.',403)
    if request.path.startswith('/api/') and not ready():
        return error('The owner is still configuring this demo. Please try again later.',503)
    if app.secret_key and 'sid' not in session:
        session['sid']=secrets.token_hex(32);session.permanent=True

@app.after_request
def headers(response):
    if not request.path.startswith(('/assets/','/static/')):response.headers['Cache-Control']='no-store'
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['X-Frame-Options']='SAMEORIGIN'
    response.headers['Referrer-Policy']='same-origin'
    return response

@app.get('/')
def index():return render_template('index.html')

@app.get('/assets/<path:filename>')
def assets(filename):return send_from_directory('public/assets',filename)

@app.get('/api/health')
def health():
    return jsonify(status='ready' if ready() else 'setup_required',flask=True,
        groq=bool(os.getenv('GROQ_API_KEY')),gee=gee_configured()),200 if ready() else 503

def budget(kind):
    store=app.config['STORE'];sid=session['sid']
    ip=request.headers.get('x-vercel-forwarded-for') if production else request.remote_addr
    ip=hmac.new(app.secret_key.encode(),(ip or 'unknown').split(',')[0].strip().encode(),hashlib.sha256).hexdigest()
    daily=int(os.getenv('DAILY_REPORT_LIMIT' if kind=='report' else 'DAILY_ANALYSIS_LIMIT','10' if kind=='report' else '50'))
    return all(store.allow(k,n,t) for k,n,t in [('session:'+sid,3,60),('ip:'+ip,5,60),(kind+':daily',daily,86400)])

def stream(payload):
    sid=session['sid'];store=app.config['STORE']
    if not budget(payload['kind']):return error('Demo usage limit reached. Please try again later.',429)
    def generate():
        try:
            for event in app.config.get('RUN_STREAM',run_stream)(payload,store,sid):
                yield json.dumps(event,separators=(',',':'))+'\n'
        except (ValueError,ConversationBusy) as exc:
            yield json.dumps({'status':'error','error':str(exc)})+'\n'
        except Exception as exc:
            logging.error('Stream failed: %s',type(exc).__name__)
            yield json.dumps({'status':'error','error':'Shared storage or the analysis service is unavailable. Please try again later.'})+'\n'
    return Response(stream_with_context(generate()),mimetype='application/x-ndjson',headers={'X-Accel-Buffering':'no'})

@app.post('/api/analyze')
def analyze():
    body=request.get_json(silent=True)
    if not isinstance(body,dict):return error('A JSON object is required.',400)
    message=body.get('message')
    if not isinstance(message,str) or not 1<=len(message.strip())<=4000:return error('Enter 1–4000 characters.',400)
    validate_roi(body.get('roi'))
    return stream(dict(kind='analysis',job_id=secrets.token_hex(16),message=message.strip(),roi=body.get('roi')))

def stored(artifact_id):
    if not ID.fullmatch(artifact_id):return None
    return app.config['STORE'].get(session['sid'],artifact_id)

@app.get('/api/job/<job_id>')
def job(job_id):
    raw=stored(job_id)
    return Response(raw,mimetype='application/json') if raw else error('Result expired or not found in this browser session.',404)

@app.post('/api/generate_report')
def report():
    body=request.get_json(silent=True)
    if not isinstance(body,dict):return error('A JSON object is required.',400)
    job_id=body.get('job_id')
    if not isinstance(job_id,str):return error('Run an analysis first, then generate its report.',400)
    raw=stored(job_id)
    if not raw:return error('Analysis expired or belongs to another session. Please run it again.',404)
    result=json.loads(raw).get('result',{})
    if result.get('type')!='analysis':return error('Reports require a completed satellite analysis.',400)
    return stream(dict(kind='report',job_id=secrets.token_hex(16),result=result))

@app.get('/api/report/<filename>')
def download(filename):
    raw=stored(filename) if filename.endswith('.pdf') else None
    if raw is None:return error('Report expired or not found in this browser session.',404)
    disposition='attachment' if request.args.get('download')=='1' else 'inline'
    return Response((raw[i:i+65536] for i in range(0,len(raw),65536)),mimetype='application/pdf',
                    headers={'Content-Disposition':f'{disposition}; filename="{filename}"'})

@app.errorhandler(ValueError)
def bad_input(exc):return error(str(exc),400)
@app.errorhandler(StorageUnavailable)
def unavailable(exc):return error('Shared storage is unavailable.',503)
@app.errorhandler(HTTPException)
def http_error(exc):return error(exc.name,exc.code)
@app.errorhandler(Exception)
def internal_error(exc):
    logging.error('Request failed: %s',type(exc).__name__)
    return error('Unable to complete the request.',500)

if __name__=='__main__':app.run(host='127.0.0.1',port=8000,debug=False)
