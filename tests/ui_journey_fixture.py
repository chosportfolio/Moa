"""Isolated UI fixture on port 8766. Never reads or writes the user's meeting."""
import argparse
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import tempfile
from concurrent.futures import ThreadPoolExecutor
import server
from recommend import recommend

class FixtureEngine:
    ready=True
    message='UI 테스트 · 음성 준비 완료'
    def profiles(self):return []

class FixtureHandler(server.Handler):
    def allowed_origin(self):
        return self.headers.get('Origin','') in ('','http://127.0.0.1:8766','http://localhost:8766')

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario',choices=('manual','sea','conflict','excluded','no-location'),default='manual')
    args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='ui-journey-',dir=ROOT/'tests') as directory:
        server.DATA=Path(directory)
        server.STATE_FILE=server.DATA/'meeting.json'
        server.ENGINE=FixtureEngine()
        lines=[('driver','멀리 가기는 싫어. 주차하기 편한 곳으로 가자.'),
               ('passenger','카페에서 커피 마시고 싶어.'),
               ('rear1','점심을 안 먹었어. 한식 먹고 싶어.'),
               ('rear2','매운 음식은 못 먹어.')]
        if args.scenario=='sea':
            lines[0]=('driver','부산 바다 보러 가자. 주차하기 편한 곳이면 좋겠어.')
        elif args.scenario=='conflict':
            lines[0]=('driver','부산 바다 보러 가자.')
            lines[1]=('passenger','강릉 가고 싶어. 커피 마시고 싶어.')
        elif args.scenario=='excluded':
            lines[0]=('driver','부산은 싫어. 가까운 곳으로 가자.')
        segments=[{'id':f'fixture-{i}','seat':seat,'text':text,'start':i*6,'end':i*6+5}
                  for i,(seat,text) in enumerate(lines)]
        result=(recommend(segments,'판교역',ROOT/'data/places') if args.scenario=='manual'
                else {'recommendations':[],'recommendation_summary':{},'region':'','constraints':[]})
        server.STATE={'search_mode':'manual' if args.scenario=='manual' else 'conversation',
                      'segments':segments,'demo':True,'trip':{},'saved_places':[],**result}
        server.JOBS={};server.ACTIVE_JOB=None;server.STREAMS={};server.STREAM_CHUNKS={}
        server.WORKER=ThreadPoolExecutor(max_workers=1)
        with server.LOCK:server.save_state()
        http=server.ThreadingHTTPServer(('127.0.0.1',8766),FixtureHandler)
        print(f'Isolated UI fixture http://127.0.0.1:8766 · {args.scenario} · no user data',flush=True)
        try:http.serve_forever()
        finally:
            http.server_close();server.WORKER.shutdown(wait=True)

if __name__=='__main__':main()
