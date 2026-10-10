"""Stream progress while supervising a bounded child process and its temporary files."""
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time

MAX_SECONDS=260
WORKER_PATH=Path(__file__).with_name("worker.py")

def _decode_worker_event(line):
    """Decode one worker event while tolerating stray stdout from dependencies.

    Vercel streams a child process pipe. A dependency may write a diagnostic to
    stdout alongside our newline-delimited JSON; parse the first complete JSON
    value and ignore any trailing text instead of failing the whole analysis.
    """
    if isinstance(line,bytes):
        line=line.decode('utf-8',errors='replace')
    decoder=json.JSONDecoder()
    start=line.find('{')
    if start<0:
        return None
    try:
        event,_=decoder.raw_decode(line,start)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event,dict) and isinstance(event.get('status'),str) else None

def run_stream(payload,store,sid):
    job_id=payload['job_id']
    with store.lock(sid), tempfile.TemporaryDirectory(prefix='orbiview-') as directory:
        folder=Path(directory)
        (folder/'input.json').write_text(json.dumps(payload))
        process=subprocess.Popen([sys.executable,str(WORKER_PATH),directory],
            stdout=subprocess.PIPE,stderr=None,start_new_session=True,bufsize=0,
            env={**os.environ,'PYTHONPATH':os.pathsep.join(sys.path)})
        started=time.monotonic();pending=b''
        try:
            yield {'status':'running','job_id':job_id,'steps':[]}
            while process.poll() is None:
                if time.monotonic()-started>MAX_SECONDS:
                    yield {'status':'error','error':'This analysis exceeded the demo time limit. Try one index, a smaller region, or a shorter date range.'}
                    return
                readable,_,_=select.select([process.stdout],[],[],1)
                if readable:
                    chunk=os.read(process.stdout.fileno(),65536)
                    pending+=chunk
                    while b'\n' in pending:
                        line,pending=pending.split(b'\n',1)
                        event=_decode_worker_event(line)
                        if event is None:
                            continue
                        if event.get('status')!='finished':
                            event['job_id']=job_id
                            yield event
                else:
                    yield {'status':'heartbeat','job_id':job_id}
            result_path=folder/'result.json'
            if process.returncode or not result_path.is_file():
                yield {'status':'error','error':'The analysis worker stopped. Please try a smaller request.'}
                return
            if result_path.stat().st_size>20_000_000:
                raise ValueError('Result is too large. Choose fewer indices.')
            result=json.loads(result_path.read_text());result['job_id']=job_id
            if result.get('status')=='complete':
                if payload['kind']=='report':
                    pdf=(folder/'report.pdf').read_bytes()
                    if len(pdf)>20_000_000: raise ValueError('Report is too large.')
                    store.put(sid,job_id+'.pdf',pdf)
                store.put(sid,job_id,json.dumps(result).encode())
            yield result
        finally:
            if process.poll() is None:
                os.killpg(process.pid,signal.SIGTERM)
                try: process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=3)
            process.stdout.close()
