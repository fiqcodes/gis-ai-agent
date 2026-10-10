"""Process boundary for Earth Engine and matplotlib; never outlives its request."""
import contextlib
import json
import os
import sys
from pathlib import Path

STEPS=['Initializing agent','Parsing request','Geolocating region','Running GEE analysis',
       'Processing output layers','Generating AI insight']

def main(directory):
    folder=Path(directory)
    data=json.loads((folder/'input.json').read_text())
    import config
    config.OUTPUT_DIR=str(folder)
    def emit(value):
        print(json.dumps(value),flush=True,file=sys.__stdout__)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            if data['kind']=='analysis':
                from analysis import run_analysis_job
                result={'status':'running','result':None,'error':None,'parsed':{},'geo':{},
                        'steps':[{'label':x,'status':'pending','progress':0} for x in STEPS]}
                run_analysis_job(result,data['message'],data.get('roi'),progress=emit)
            else:
                from research_agent import generate_research_paper
                metadata={}
                path=generate_research_paper(data['result'],str(folder),metadata=metadata)
                if not path or not Path(path).is_file():
                    raise RuntimeError('Report generation failed')
                Path(path).replace(folder/'report.pdf')
                result={'status':'complete','filename':data['job_id']+'.pdf',**metadata}
        (folder/'result.json').write_text(json.dumps(result))
    except Exception as exc:
        print('Worker failed: '+type(exc).__name__,file=sys.stderr,flush=True)
        (folder/'result.json').write_text(json.dumps({'status':'error','error':
            str(exc) if isinstance(exc,ValueError) else 'Unable to complete this request. Try a smaller region or shorter period.'}))
    emit({'status':'finished'})

if __name__=='__main__':
    main(sys.argv[1])
