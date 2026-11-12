"""Isolated manual UI review on :8766; never writes the real conversation."""
import copy
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
import demo_conversations


class PreviewEngine:
    ready = True
    message = '화면 검증'


class PreviewHandler(server.Handler):
    def allowed_origin(self):
        return self.headers.get('Origin', '') in ('', 'http://127.0.0.1:8766', 'http://localhost:8766')


with tempfile.TemporaryDirectory(prefix='ui-review-', dir=Path(__file__).parent) as directory:
    server.DATA = Path(directory)
    server.STATE_FILE = server.DATA/'meeting.json'
    server.ENGINE = PreviewEngine()
    server.STATE = {'segments':[], 'recommendations':[], 'constraints':[], 'region':'', 'demo':False,
                    'notice':'', 'coverage':'', 'lookup_source':'', 'recommendation_summary':{},
                    'trip':{}, 'saved_places':[], '_audio_streams':{}, 'conversation_id':'ui-review',
                    'search_mode':'conversation', 'search_location':{}, 'search_intent':{}, 'state_revision':0,
                    'demo_scenario':{}, '_demo_backup':{}, '_place_ratings':{}, 'request_profile':{}}
    shutil.copytree(server.ROOT/'data'/'places', server.DATA/'places', dirs_exist_ok=True)
    with patch.object(demo_conversations, 'random_scenario', return_value=copy.deepcopy(demo_conversations.SCENARIOS[1])):
        server.STATE = demo_conversations.enter_demo(server.STATE)
    server.save_state()
    print('Isolated UI review http://127.0.0.1:8766', flush=True)
    server.ThreadingHTTPServer(('127.0.0.1',8766), PreviewHandler).serve_forever()
