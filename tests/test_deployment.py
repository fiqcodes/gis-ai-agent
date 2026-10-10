import json
from pathlib import Path
import pytest
import app as web
from storage import LocalStore, ArtifactStore
from runtime import _decode_worker_event
from analysis import _parse_intent_json
from validation import validate_roi, validate_analysis

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('GROQ_API_KEY','test-key')
    monkeypatch.setenv('GEE_SERVICE_ACCOUNT_JSON',json.dumps({
        'type':'service_account','project_id':'fixture','private_key':'fixture',
        'client_email':'fixture@example.test','token_uri':'https://oauth2.googleapis.com/token'}))
    web.app.config.update(TESTING=True,SECRET_KEY='s'*48,STORE=LocalStore())
    def runner(payload,store,sid):
        yield {'status':'running','job_id':payload['job_id'],'steps':[]}
        data={'status':'complete','job_id':payload['job_id'],'result':{'type':'analysis','region':'Jakarta'}}
        if payload['kind']=='report':
            data['filename']=payload['job_id']+'.pdf'
            data['title']='Vegetation Patterns in Jakarta (2024)'
            data['download_name']='Vegetation_Patterns_in_Jakarta_2024.pdf'
            store.put(sid,data['filename'],b'%PDF-1.4\nfixture')
        store.put(sid,payload['job_id'],json.dumps(data).encode())
        yield data
    web.app.config['RUN_STREAM']=runner
    return web.app.test_client()

def events(response):return [json.loads(x) for x in response.text.splitlines()]

def test_worker_event_decoder_ignores_stray_stdout():
    event={'status':'running','steps':[]}
    encoded=json.dumps(event)
    assert _decode_worker_event(encoded)==event
    assert _decode_worker_event(encoded.encode())==event
    assert _decode_worker_event('dependency log: '+encoded+' trailing diagnostic')==event
    assert _decode_worker_event('dependency log without an event') is None

def test_intent_parser_ignores_trailing_model_json():
    assert _parse_intent_json('{"intent":"analysis"}\n{"intent":"question"}')=={'intent':'analysis'}
    assert _parse_intent_json('no JSON object')=={}

def test_public_stream_report_and_isolation(client):
    assert client.get('/').status_code==200
    assert client.get('/static/js/app.js').status_code==200
    assert client.get('/assets/orbiview.png').status_code==200
    r=client.post('/api/analyze',json={'message':'NDVI Jakarta 2024'})
    assert r.mimetype=='application/x-ndjson'
    data=events(r); assert [e['status'] for e in data]==['running','complete']
    job_id=data[-1]['job_id']
    assert client.get('/api/job/'+job_id).status_code==200
    other=web.app.test_client()
    assert other.get('/api/job/'+job_id).status_code==404
    assert other.post('/api/generate_report',json={'job_id':job_id}).status_code==404
    report=events(client.post('/api/generate_report',json={'job_id':job_id}))[-1]
    filename=report['filename']
    assert client.get('/api/report/'+filename).data.startswith(b'%PDF')
    assert other.get('/api/report/'+filename).status_code==404
    assert 'attachment' in client.get('/api/report/'+filename+'?download=1').headers['Content-Disposition']
    assert 'Vegetation_Patterns_in_Jakarta_2024.pdf' in client.get('/api/report/'+filename+'?download=1').headers['Content-Disposition']

def test_setup_without_secrets(client,monkeypatch):
    monkeypatch.delenv('GROQ_API_KEY')
    assert client.get('/').status_code==200
    assert client.get('/api/health').json['status']=='setup_required'
    assert client.post('/api/analyze',json={'message':'hi'}).status_code==503

def test_bad_requests_and_origin(client):
    for value in [None,[],{'message':1},{'message':'x'*4001}]:
        assert client.post('/api/analyze',json=value).status_code==400
    assert client.post('/api/analyze',json={'message':'hi'},headers={'Origin':'https://evil.test'}).status_code==403
    assert client.post('/api/generate_report',json={'result':{'fake':'data'}}).status_code==400
    assert client.post('/api/analyze',data='x'*40000,content_type='application/json').status_code==413

def test_shared_ip_budget_cannot_be_bypassed_with_new_cookie(client):
    for _ in range(5):
        assert web.app.test_client().post('/api/analyze',json={'message':'hi'}).status_code==200
    assert web.app.test_client().post('/api/analyze',json={'message':'hi'}).status_code==429

def test_storage_fail_closed(client):
    web.app.config['STORE']=None
    assert client.post('/api/analyze',json={'message':'hi'}).status_code==503

def test_region_bounds():
    valid={'type':'Polygon','coordinates':[[[106,-6],[107,-6],[107,-5],[106,-6]]]}
    validate_roi(valid)
    for bad in [{},[],{'type':'Point','coordinates':[0,0]}, {'type':'Polygon','coordinates':[[[0,0],[50,0],[50,1],[0,0]]]}]:
        with pytest.raises(ValueError):validate_roi(bad)
    validate_analysis('Jakarta','2024-01-01','2024-12-31',['ndvi'])
    for args in [('Jakarta','2024-01-01','2030-01-01',['ndvi']),('Jakarta','2024-01-01','2024-12-31',['madeup'])]:
        with pytest.raises(ValueError):validate_analysis(*args)

def test_groq_transport(monkeypatch):
    import llm
    monkeypatch.setenv('GROQ_API_KEY','test-secret')
    captured={}
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'choices':[{'message':{'content':'answer'}}]}
    def post(url,**kwargs):captured.update(url=url,**kwargs);return Response()
    monkeypatch.setattr(llm.requests,'post',post)
    assert llm.llm_post('http://localhost:11434',json={'messages':[{'role':'user','content':'Hi'}]}).json()['message']['content']=='answer'
    assert captured['url']=='https://api.groq.com/openai/v1/chat/completions'
    assert captured['json']['model']=='openai/gpt-oss-120b'
    assert captured['json']['reasoning_format']=='hidden'
    assert captured['timeout']==(5,30)
    assert captured['allow_redirects'] is False

def test_gee_configuration_rejects_malformed_json(monkeypatch):
    import gee_auth
    info={'type':'service_account','project_id':'fixture','private_key':'fixture',
          'client_email':'fixture@example.test','token_uri':'https://oauth2.googleapis.com/token'}
    monkeypatch.setenv('GEE_SERVICE_ACCOUNT_JSON',json.dumps(info))
    assert gee_auth.configured()
    monkeypatch.setenv('GEE_SERVICE_ACCOUNT_JSON',json.dumps(info)+'\n{"extra":true}')
    assert not gee_auth.configured()

def test_gee_lazy_credentials(monkeypatch):
    import ee,gee_auth
    from google.oauth2 import service_account
    seen=[]
    monkeypatch.setenv('GEE_SERVICE_ACCOUNT_JSON',json.dumps({
        'type':'service_account','project_id':'fixture','private_key':'fixture',
        'client_email':'fixture@example.test','token_uri':'https://oauth2.googleapis.com/token'}))
    monkeypatch.setattr(service_account.Credentials,'from_service_account_info',lambda info,scopes:'credential')
    monkeypatch.setattr(ee,'Initialize',lambda *a,**k:seen.append((a,k)))
    monkeypatch.setattr(ee.data,'setDeadline',lambda value:None)
    monkeypatch.setattr(ee.data,'setMaxRetries',lambda value:None)
    gee_auth._initialized=False
    gee_auth.initialize_gee();gee_auth.initialize_gee()
    assert len(seen)==1 and seen[0][0]==('credential',)
    gee_auth._initialized=False

def test_redis_artifact_chunks_and_owner():
    store=ArtifactStore('https://redis.example','test')
    values={};calls=[]
    def command(op,key,*args):
        calls.append((op,key,args))
        if op=='SET':values[key]=str(args[0]);return 'OK'
        return values.get(key)
    store.command=command
    import os
    data=os.urandom(700000)
    store.put('a','report',data)
    assert store.get('a','report')==data
    assert store.get('b','report') is None
    writes=[args for op,_,args in calls if op=='SET']
    assert len(writes)>2 and all(len(str(args[0]))<=450000 and args[1]=='EX' for args in writes)

def test_actual_qa_pipeline_without_gee(monkeypatch):
    import analysis,gee_auth
    from llm import Completion
    monkeypatch.setattr(gee_auth,'initialize_gee',lambda:pytest.fail('QA must not initialize GEE'))
    answers=iter([json.dumps({'intent':'question'}),'NDVI measures vegetation greenness.'])
    monkeypatch.setattr(analysis,'llm_post',lambda *a,**k:Completion(next(answers)))
    job={'steps':[{'status':'pending'} for _ in range(6)]}
    analysis.run_analysis_job(job,'Hello, what can you do?')
    assert job['status']=='complete' and job['result']['type']=='qa'

def test_worker_failure_stream_and_cleanup(tmp_path,monkeypatch):
    import runtime
    # Exercise the real subprocess transport without any provider credentials.
    monkeypatch.delenv('GROQ_API_KEY',raising=False)
    results=list(runtime.run_stream({'kind':'analysis','job_id':'a'*32,'message':'Hello'},LocalStore(),'visitor'))
    assert results[0]['status']=='running'
    assert results[-1]['status']=='error'
    assert 'test-key' not in str(results)

def test_real_pdf_builder(tmp_path,monkeypatch):
    import research_agent
    monkeypatch.setattr(research_agent,'_call_llm',lambda *a,**k:'This is a deterministic test section based on the supplied satellite statistics.')
    result={'type':'analysis','region':'Jakarta','start_date':'2024-01-01','end_date':'2024-12-31',
            'variables':['ndvi'],'stats':{'NDVI':{'mean':0.42,'median':0.4,'std':0.1,'p10':0.1,'p90':0.6}},
            'figures':{},'conclusion':'Measured vegetation index is 0.42.'}
    pdf=research_agent.generate_research_paper(result,str(tmp_path))
    assert pdf and Path(pdf).read_bytes().startswith(b'%PDF')

def test_runtime_success_and_timeout(tmp_path,monkeypatch):
    import runtime,time,os
    worker=tmp_path/'worker_fixture.py'
    worker.write_text('''import json,sys,time,os
from pathlib import Path
p=Path(sys.argv[1]); data=json.loads((p/'input.json').read_text())
print(json.dumps({'status':'running','pid':os.getpid(),'directory':str(p)}),flush=True)
if data.get('slow'): time.sleep(30)
(p/'result.json').write_text(json.dumps({'status':'complete','result':{'type':'qa','answer':'fixture'}}))
''')
    monkeypatch.setattr(runtime,'WORKER_PATH',worker)
    store=LocalStore()
    result=list(runtime.run_stream({'kind':'analysis','job_id':'b'*32},store,'a'))
    assert result[-1]['status']=='complete'
    assert store.get('a','b'*32) and not store.get('b','b'*32)
    directory=next(e['directory'] for e in result if 'directory' in e)
    assert not Path(directory).exists()
    monkeypatch.setattr(runtime,'MAX_SECONDS',0.05)
    stream=runtime.run_stream({'kind':'analysis','job_id':'c'*32,'slow':True},store,'a')
    result=list(stream)
    assert result[-1]['status']=='error' and 'time limit' in result[-1]['error']
    child=next(e for e in result if 'pid' in e)
    with pytest.raises(ProcessLookupError):os.kill(child['pid'],0)
    assert not Path(child['directory']).exists()

def test_runtime_disconnect_kills_worker(tmp_path,monkeypatch):
    import runtime,os
    worker=tmp_path/'worker_fixture.py'
    worker.write_text('import json,os,time\nprint(json.dumps({"status":"running","pid":os.getpid()}),flush=True)\ntime.sleep(30)\n')
    monkeypatch.setattr(runtime,'WORKER_PATH',worker)
    stream=runtime.run_stream({'kind':'analysis','job_id':'d'*32},LocalStore(),'a')
    next(stream)
    child=next(stream)
    assert 'pid' in child
    stream.close()
    with pytest.raises(ProcessLookupError):os.kill(child['pid'],0)

@pytest.mark.parametrize('text,expected', [
    ('Analyze NO₂ air quality over Beijing in 2024', ('no2', 'Beijing')),
    ('Analyze the NDVI of Jakarta in 2024', ('ndvi', 'Jakarta')),
    ('Show me the land cover of London in 2024', ('lulc', 'London')),
])
def test_example_prompt_regions(text, expected):
    from gis_functions import _extract_variables_from_text, _extract_region_and_dates
    assert expected[0] in _extract_variables_from_text(text)
    assert _extract_region_and_dates(text) == (expected[1], '2024-01-01', '2024-12-31')


def test_context_search_budget(monkeypatch):
    import gis_functions as gis
    clock=[0.0];calls=[]
    monkeypatch.setattr(gis.time, 'monotonic', lambda:clock[0])
    def slow_search(query, mode, count, timeout):
        calls.append(timeout)
        clock[0] += timeout
        return []
    monkeypatch.setattr(gis, '_search', slow_search)
    monkeypatch.setattr(gis, '_wiki', lambda *a,**k: pytest.fail('No requests after budget expires'))
    assert gis.fetch_web_context('Jakarta', '2024-01-01', '2024-12-31', ['ndvi'], max_seconds=7) is None
    assert calls == [3,3,1]


def test_thumbnail_retry(monkeypatch):
    import gis_functions as gis
    from io import BytesIO
    from PIL import Image
    image=BytesIO();Image.new('RGB',(2,2)).save(image,format='PNG');data=image.getvalue()
    calls=[]
    def download(url, timeout):
        calls.append(url)
        if len(calls)==1:raise TimeoutError('fixture')
        return BytesIO(data)
    monkeypatch.setattr(gis.urllib.request, 'urlopen', download)
    source=type('Image',(),{'getThumbURL':lambda self,params:'https://example.test/thumb'})()
    assert gis.get_thumb(source,{},None).shape == (2,2,3)
    assert len(calls)==2


def test_numeric_charts_without_thumbnail():
    from gis_functions import make_stats_charts
    values={'NO2':{'mean':0.0001,'min':0.00001,'max':0.0002,'std':0.00003,
                  'p10':0.00005,'p90':0.00017,'monthly':{'2024-01':0.0001,'2024-02':0.00012},
                  'class_pcts':{'Clean (<8e-5)':{'pct':30},'Moderate (8–15e-5)':{'pct':70}}}}
    charts=make_stats_charts(values,'no2','NO2')
    assert {'monthly_trend','histogram','class_bar'} <= {kind for kind,_ in charts}
    assert all(data.startswith('data:image/png;base64,') for _,data in charts)


def test_tile_preview_preserves_alpha_and_bounds(monkeypatch):
    import tile_preview as preview
    from io import BytesIO
    from PIL import Image
    image=BytesIO();Image.new('RGBA',(256,256),(10,20,30,128)).save(image,format='PNG')
    seen=[]
    class Response:
        content=image.getvalue()
        def raise_for_status(self):pass
    def fetch(url,timeout):
        seen.append(url)
        return Response()
    monkeypatch.setattr(preview.requests,'get',fetch)
    pixels=preview.tile_preview('https://earthengine.googleapis.com/tiles/{z}/{x}/{y}',[115.4,39.4,117.5,41.1])
    assert pixels.shape[1]==512 and pixels.shape[2]==4
    assert len(seen)<=16
    assert (pixels[:,:,:3]==[10,20,30]).all()
    assert (pixels[:,:,3]==128).all()


def test_pdf_unicode_and_descriptive_name(tmp_path, monkeypatch):
    import research_agent as research
    from reportlab.pdfbase import pdfmetrics
    title='Land\u2011Use/Land\u2011Cover Change in London (2024)'
    body='High\u2011resolution multi\u2011sensor observations. NO₂, m², μm, ±, ≥, κ. A & B < C.'
    monkeypatch.setattr(research, '_call_llm', lambda *args, **kwargs: body)
    monkeypatch.setattr(research, 'gen_title', lambda *args: title)
    monkeypatch.setattr(research, 'gen_references', lambda *args: [])
    metadata={}
    path=research.generate_research_paper({'region':'London','variables':['ndvi'],
        'start_date':'2024-01-01','end_date':'2024-12-31','stats':{}}, str(tmp_path), metadata)
    assert Path(path).read_bytes().startswith(b'%PDF')
    assert metadata['title']==title
    assert metadata['download_name']==Path(path).name
    assert 'London_2024' in Path(path).name
    assert research._esc(body).startswith('High-resolution multi-sensor')
    for font in ['Orbiview', 'Orbiview-Bold', 'Orbiview-Oblique']:
        assert all(ord(char) in pdfmetrics.getFont(font).face.charToGlyph for char in 'μ±≥κ')
