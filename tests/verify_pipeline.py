"""Opt-in smoke test with synthetic speech, isolated state and named-place cache."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import copy,json,tempfile,threading,time
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request,urlopen
from unittest.mock import patch
import server
from speech_engine import SpeechEngine
from recommend import recommend

def main():
    with tempfile.TemporaryDirectory(dir=ROOT/'tests') as directory:
        data=Path(directory)
        engine=SpeechEngine(ROOT/'models',data/'profiles.json')
        engine.load()
        initial={'segments':[], 'recommendations':[], 'constraints':[], 'region':'판교역',
                 'demo':False,'notice':'','coverage':'','lookup_source':''}
        worker=ThreadPoolExecutor(max_workers=1)
        with patch.multiple(server,ENGINE=engine,DATA=data,STATE_FILE=data/'meeting.json',
                            STATE=initial,JOBS={},ACTIVE_JOB=None,WORKER=worker,STREAMS={},STREAM_CHUNKS={}):
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
            thread=threading.Thread(target=http.serve_forever,daemon=True)
            thread.start()
            base='http://127.0.0.1:'+str(http.server_port)
            def request(path,body=None,headers=None):
                with urlopen(Request(base+path,data=body,headers=headers or {}),timeout=20) as response:
                    return json.load(response)
            def wait_job(identifier):
                for _ in range(120):
                    result=request('/api/jobs/'+identifier)
                    if result['status'] in ('complete','error'):
                        assert result['status']=='complete',result
                        return result
                    time.sleep(.25)
                raise AssertionError('Job timed out')
            try:
                audio=(ROOT/'tests/fixtures/synthetic-ko-utterance.wav').read_bytes()
                result=request('/api/transcribe?stream=1',audio,{'Content-Type':'audio/wav',
                    'X-Stream-Id':'pipeline-test','X-Sequence':'0','X-Start-Seconds':'0'})
                wait_job(result['job_id'])
                state=request('/api/state')
                text=' '.join(s['text'] for s in state['segments'])
                assert '커피' in text and '매운' in text,text
                assert all(s['seat']=='unknown' for s in state['segments'])
                with patch('server.recommend',side_effect=lambda segments,region,_:recommend(segments,region,ROOT/'data/places')):
                    result=request('/api/recommend',json.dumps({'region':'판교역'}).encode(),{'Content-Type':'application/json'})
                    wait_job(result['job_id'])
                state=request('/api/state')
                assert state['recommendations'],'No real named-place results'
                print(json.dumps({'transcript':text,'seat':'unknown','places':[r['name'] for r in state['recommendations']],
                    'coverage':state['coverage'],'isolated_state':True},ensure_ascii=False))
            finally:
                worker.shutdown(wait=True)
                http.shutdown();http.server_close();thread.join()

if __name__=='__main__':main()
