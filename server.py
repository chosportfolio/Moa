"""Local-only companion server for the Pleos cabin conversation prototype."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import copy
import hashlib
import json
import mimetypes
import math
import os
import threading
import time
import uuid
from urllib.parse import parse_qs, urlparse

from recommend import LABELS, extract, recommend, summarize_conversation, nominatim_search, valid_coordinates, request_profile
from trip_recommendation import recommend_trip, recommend_regions
from speech_engine import SpeechEngine
from journey import JourneyConflict, select_place, start_trip, arrive_trip, decide_trip, cancel_trip
from host_position import HostPosition, VehiclePosition, LocationUnavailable
from search_intent import resolve_search_intent
from demo_conversations import enter_demo, leave_demo
from personal_ratings import rate_visit, public_ratings, personalize
from routing_duration import enrich_travel_times
from native_transcripts import NativeTranscriptConflict, validate_final, stage_final
from spoken_times import normalize_recording_started_at, at_offset, session_recording_origin


def _repair_orphan_selections(state):
    """Repair a reset-era selection only on an owned load/commit copy.

    An absent field is not proof that a conversation is empty. Neither a
    running/completed trip nor its saved visit may be removed by this migration.
    """
    containers = [state]
    backup = state.get('_demo_backup')
    if isinstance(backup, dict):
        containers.append(backup)
    for container in containers:
        trip = container.get('trip')
        if not isinstance(trip, dict) or trip.get('status') != 'selected':
            continue
        if container.get('segments') != [] or container.get('recommendations') != []:
            continue
        # Protect a legacy/inconsistent status if it carries evidence of actual
        # guidance, arrival or a completed save decision.
        if any(trip.get(key) for key in ('started_at', 'arrived_at', 'provider',
                                         'arrival_source', 'decision_at', 'saved_at')):
            continue
        container['trip'] = {}


def load_state(path, defaults):
    """Read compatible fields, including saved visits, without rewriting the file."""
    state = copy.deepcopy(defaults)
    try:
        saved = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(saved, dict):
            # Inspect persisted fields before defaults are filled: missing
            # legacy lists must not be mistaken for explicitly cleared ones.
            _repair_orphan_selections(saved)
            for key, default in state.items():
                if key in saved and isinstance(saved[key], type(default)):
                    state[key] = saved[key]
    except (ValueError, OSError):
        pass
    return state


ROOT = Path(__file__).resolve().parent
DATA = ROOT/'data'
DATA.mkdir(exist_ok=True)
STATE_FILE = DATA/'meeting.json'
ENGINE = SpeechEngine(ROOT/'models',DATA/'profiles.json')
LOCK = threading.RLock()
WORKER = ThreadPoolExecutor(max_workers=1)
JOB_TIMEOUT_SECONDS = 100
JOB_CONTEXT = threading.local()
JOBS = {}
STATE = load_state(STATE_FILE, {'segments':[], 'recommendations':[], 'constraints':[], 'region':'', 'demo':False,
         'notice':'', 'coverage':'', 'lookup_source':'', 'recommendation_summary':{},
         'trip':{}, 'saved_places':[], '_audio_streams':{}, '_speech_streams':{}, 'conversation_id':'',
         'search_mode':'conversation', 'search_location':{}, 'search_intent':{}, 'state_revision':0,
         'demo_scenario':{}, '_demo_backup':{}, '_place_ratings':{}, 'request_profile':{}})
ACTIVE_JOB = None
STREAMS = {}
STREAM_CHUNKS = {}
HOST_POSITION = HostPosition()
VEHICLE_POSITION = VehiclePosition()
ALLOW_HOST_LOCATION = os.environ.get('CABIN_EMULATOR_HOST_LOCATION') == '1'
ORIGIN_CACHE = {}


def route_origin(intent, location=None, demo=False):
    """A destination search center must never become the trip's origin."""
    if intent.get('needs_origin_choice'):
        return None
    region = intent.get('origin_region')
    if region:
        if region in ORIGIN_CACHE:
            return copy.deepcopy(ORIGIN_CACHE[region])
        try:
            coordinates = None
            cached_path=DATA/'places'/(hashlib.sha256(region.encode()).hexdigest()+'.json')
            if cached_path.is_file() and time.time()-cached_path.stat().st_mtime < 7*86400:
                try:
                    cached=json.loads(cached_path.read_text(encoding='utf-8'))
                    center=cached.get('center') or {}
                    if cached.get('region') == region:
                        coordinates=valid_coordinates(center.get('lat'), center.get('lon'))
                except (ValueError, OSError, AttributeError):
                    pass
            if coordinates is None:
                results = nominatim_search({'q':region,'countrycodes':'kr','format':'jsonv2','limit':1,'accept-language':'ko'})
                coordinates = valid_coordinates(results[0].get('lat'), results[0].get('lon')) if results else None
            if coordinates:
                origin = {'lat':coordinates[0], 'lon':coordinates[1], 'label':region+' 도시 기준점'}
                if len(ORIGIN_CACHE) >= 64: ORIGIN_CACHE.pop(next(iter(ORIGIN_CACHE)))
                ORIGIN_CACHE[region] = origin
                return copy.deepcopy(origin)
        except Exception:
            pass
        return None
    # Fictional samples must not borrow the driver's real position as an origin.
    if demo:
        return None
    try:
        position = location or current_position()
        coordinates = valid_coordinates(position.get('latitude'), position.get('longitude'))
        if coordinates:
            return {'lat':coordinates[0], 'lon':coordinates[1], 'label':'차량 현재 위치'}
    except LocationUnavailable:
        pass
    return None


def current_position():
    try:
        return VEHICLE_POSITION.for_search()
    except LocationUnavailable:
        if ALLOW_HOST_LOCATION:
            return HOST_POSITION.for_search()
        raise


def public_location():
    vehicle = VEHICLE_POSITION.public()
    if not vehicle['available'] and ALLOW_HOST_LOCATION:
        host = HOST_POSITION.public()
        if host['available']: return host
    return vehicle

def save_state(candidate=None):
    """Caller holds LOCK; publish changes only after their atomic disk commit."""
    job_id = getattr(JOB_CONTEXT, 'identifier', None)
    if job_id and (JOBS.get(job_id, {}).get('status') != 'running'
                   or time.monotonic() >= JOB_CONTEXT.deadline):
        raise TimeoutError('처리 시간이 오래 걸리고 있어요. 인터넷 연결을 확인하고 다시 시도해 주세요.')
    committed = copy.deepcopy(STATE if candidate is None else candidate)
    _repair_orphan_selections(committed)
    previous_revision = STATE.get('state_revision', 0)
    if type(previous_revision) is not int or previous_revision < 0: previous_revision = 0
    committed['state_revision'] = previous_revision + 1
    temp = STATE_FILE.with_suffix('.tmp')
    try:
        with temp.open('w', encoding='utf-8') as output:
            json.dump(committed, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp,STATE_FILE)
    finally:
        temp.unlink(missing_ok=True)
    STATE.clear()
    STATE.update(committed)

def require_idle():
    """Check within the same LOCK section that reserves or changes state."""
    if ACTIVE_JOB and JOBS[ACTIVE_JOB]['status'] in ('queued','running'):
        raise ValueError('진행 중인 처리가 끝날 때까지 기다려 주세요.')


class ConversationConflict(ValueError):
    pass


def conversation_id():
    """Caller holds LOCK. Persist the legacy-state migration before exposing it."""
    identifier = STATE.get('conversation_id')
    if not isinstance(identifier, str) or not identifier:
        updated = copy.deepcopy(STATE)
        identifier = updated['conversation_id'] = uuid.uuid4().hex
        save_state(updated)
    return identifier


def require_conversation(identifier):
    if identifier and identifier != conversation_id():
        raise ConversationConflict('이전 대화에서 녹음한 발언이에요. 현재 대화에는 추가하지 않았어요.')


def valid_region(value):
    if not isinstance(value, str):
        raise ValueError('검색할 지역 이름을 입력해 주세요.')
    region = value.strip()
    if not region or len(region) > 70 or any(ord(char) < 32 for char in region):
        raise ValueError('검색할 시·동·역 이름을 70자 이내로 입력해 주세요.')
    return region

def area_choice_key(segments):
    """Bind an area decision to these utterances, independent of navigation."""
    payload = [conversation_id(), [
        [segment.get('id'), segment.get('seat', 'unknown'), segment.get('text', '')]
        for segment in segments
    ]]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def invalidate_recommendations(candidate, notice=None):
    candidate.update(recommendations=[], recommendation_summary={}, request_profile={}, coverage='', lookup_source='',
                     search_location={}, search_intent={},
                     notice=notice if notice is not None else '대화록을 확인하고 검색 지역을 정한 뒤 장소 추천을 눌러 주세요.')
    candidate['constraints'], _ = extract(candidate['segments'])


def remember_audio_commit(candidate, stream_id, sequence, digest, base, recording_started_at=None):
    """Commit the retry receipt in the same atomic file write as its transcript."""
    sessions=candidate.setdefault('_audio_streams', {})
    session=sessions.setdefault(stream_id, {'base':base, 'receipts':{}})
    if recording_started_at is not None:
        session['recording_started_at'] = recording_started_at
    session.setdefault('receipts', {})[str(sequence)]=digest
    # Each capture lasts at most three minutes; retain recent sessions across
    # process restarts without retaining audio or growing history indefinitely.
    while len(sessions)>64:
        sessions.pop(next(iter(sessions)))

def snapshot():
    with LOCK:
        conversation_id()
        state = copy.deepcopy(STATE)
        state.pop('_audio_streams', None)
        state.pop('_speech_streams', None)
        state['demo_session'] = bool(state.pop('_demo_backup', {}))
        state.setdefault('search_mode', 'conversation')
        state.setdefault('search_intent', {})
        state.setdefault('state_revision', 0)
        state.update({'status':'ready' if ENGINE.ready else 'loading','models':{'ready':ENGINE.ready,'message':ENGINE.message},
                      'location':public_location(),
                      'development':{'host_location_enabled':ALLOW_HOST_LOCATION},
                      'seat_input':{'available':False,'message':'현재 마이크의 좌석 정보 없음'},
                      'seat_channels':[], 'job':public_job(JOBS.get(ACTIVE_JOB)), 'errors':[]})
        # Old presentation metadata may mention the development adapter; leave
        # the user's actual utterances and saved memories exactly as recorded.
        for container, key in ((state, 'notice'), (state.get('search_intent', {}), 'message')):
            if isinstance(container.get(key), str):
                container[key] = container[key].replace('컴퓨터 현재 위치', '현재 위치')
        return public_ratings(state)


def public_job(job):
    return {key:copy.deepcopy(value) for key,value in job.items() if not key.startswith('_')} if job else None

def queue_job(action, message):
    global ACTIVE_JOB
    with LOCK:
        require_idle()
        identifier = uuid.uuid4().hex
        JOBS[identifier] = {'id':identifier,'status':'queued','message':message}
        ACTIVE_JOB = identifier
        while len(JOBS) > 40:
            JOBS.pop(next(iter(JOBS)))
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    def expire():
        with LOCK:
            job = JOBS.get(identifier)
            if job and job['status'] in ('queued', 'running'):
                job.update(status='error', message='처리 시간이 오래 걸리고 있어요. 외부 지도 서버 응답이 없거나 인터넷 연결이 불안정할 수 있어요. 다시 시도해 주세요.')
    timer = threading.Timer(JOB_TIMEOUT_SECONDS, expire)
    timer.daemon = True
    timer.start()
    def run():
        with LOCK:
            if time.monotonic() >= deadline:
                expire()
            if identifier not in JOBS or JOBS[identifier]['status'] != 'queued':
                return
            JOBS[identifier]['status'] = 'running'
        JOB_CONTEXT.identifier = identifier
        JOB_CONTEXT.deadline = deadline
        try:
            result = action()
            with LOCK:
                if time.monotonic() >= deadline:
                    expire()
                if JOBS.get(identifier, {}).get('status') == 'running':
                    JOBS[identifier].update(status='complete', message='완료', result=result)
        except Exception as error:
            with LOCK:
                if time.monotonic() >= deadline:
                    expire()
                if JOBS.get(identifier, {}).get('status') == 'running':
                    JOBS[identifier].update(status='error', message=str(error)[:400])
        finally:
            timer.cancel()
            del JOB_CONTEXT.identifier
            del JOB_CONTEXT.deadline
    try:
        WORKER.submit(run)
    except Exception:
        timer.cancel()
        with LOCK: JOBS[identifier].update(status='error',message='작업을 시작하지 못했어요. 다시 시도해 주세요.')
        raise
    return identifier

def load_engine():
    try: ENGINE.load()
    except Exception as error: ENGINE.message='음성 모델을 준비하지 못했어요. '+str(error)[:250]

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def reply(self,data,status=200,content_type='application/json; charset=utf-8'):
        if isinstance(data,(dict,list)): data=json.dumps(data,ensure_ascii=False).encode('utf-8')
        elif isinstance(data,str): data=data.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(data)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.end_headers()
        self.wfile.write(data)

    def body(self,limit=64*1024*1024):
        length=int(self.headers.get('Content-Length','0'))
        if length <= 0 or length > limit: raise ValueError('파일이 비었거나 너무 커요. 최대 64MB까지 가능해요.')
        return self.rfile.read(length)

    def json_body(self):
        value = json.loads(self.body(128*1024))
        if not isinstance(value, dict):
            raise ValueError('요청 내용을 확인해 주세요.')
        return value

    def allowed_origin(self):
        origin=self.headers.get('Origin','')
        return not origin or origin in ('http://127.0.0.1:8765','http://localhost:8765')

    def do_GET(self):
        path=urlparse(self.path).path
        if path=='/api/state': return self.reply(snapshot())
        if path.startswith('/api/jobs/'):
            with LOCK: result=public_job(JOBS.get(path.rsplit('/',1)[-1]))
            return self.reply(result or {'error':'처리 내역을 찾을 수 없어요.'},200 if result else 404)
        if path=='/api/export':
            s=snapshot()
            lines=['# 모아 · 탑승자 대화록', '', '내보낸 시각: '+datetime.now().astimezone().isoformat(timespec='seconds'),'']
            if s['demo']: lines+=['샘플 대화로 작성한 예시입니다.','']
            for seg in s['segments']:
                recorded_time = None
                try:
                    instant = normalize_recording_started_at(seg.get('spoken_at'))
                    if instant:
                        recorded_time = datetime.fromisoformat(instant.replace('Z', '+00:00')).astimezone().strftime('%Y-%m-%d %H:%M:%S %z')
                except (ValueError, TypeError, OverflowError):
                    pass
                clock_label = '기록 시각 '+recorded_time if recorded_time else '시각 미상'
                lines.append(f"- {clock_label} **{LABELS.get(seg['seat'],'화자 확인 필요')}**  {seg['text']}")
            lines+=['','## 추천 장소','']
            if s['notice']: lines += [s['notice'], '']
            for rec in s['recommendations']:
                lines += [f"- [{rec['name']}]({rec['url']}) · {rec['address']}"]
                lines += ['  - '+r['text'] for r in rec['reasons']]
                lines += ['  - 확인 필요 · '+r for r in rec['cautions']]
            return self.reply('\n'.join(lines),content_type='text/markdown; charset=utf-8')
        relative='index.html' if path=='/' else path.lstrip('/')
        file=(ROOT/'web'/relative).resolve()
        if not file.is_relative_to((ROOT/'web').resolve()) or not file.is_file():
            return self.reply({'error':'페이지를 찾을 수 없어요.'},404)
        return self.reply(file.read_bytes(),content_type=(mimetypes.guess_type(file.name)[0] or 'application/octet-stream')+('; charset=utf-8' if file.suffix in ('.html','.css','.js') else ''))

    def do_POST(self):
        if not self.allowed_origin(): return self.reply({'error':'허용되지 않은 요청이에요.'},403)
        parsed=urlparse(self.path)
        try:
            if parsed.path in ('/api/trip/select', '/api/trip/start', '/api/trip/arrive', '/api/trip/decision', '/api/trip/cancel'):
                options = self.json_body()
                with LOCK:
                    if parsed.path == '/api/trip/select':
                        require_idle()
                        updated = select_place(STATE, options.get('place_id'))
                    elif parsed.path == '/api/trip/start':
                        updated = start_trip(STATE, options.get('trip_id'), options.get('provider'))
                    elif parsed.path == '/api/trip/arrive':
                        updated = arrive_trip(STATE, options.get('trip_id'), options.get('source'))
                    elif parsed.path == '/api/trip/cancel':
                        updated = cancel_trip(STATE, options.get('trip_id'), options.get('source'))
                    else:
                        updated = decide_trip(STATE, options.get('trip_id'), options.get('save'))
                    # A native event and its WebView callback can deliver the
                    # same transition; already committed requests are no-ops.
                    if updated != STATE:
                        save_state(updated)
                return self.reply(snapshot())
            if parsed.path=='/api/enroll':
                return self.reply({'error':'이 앱은 목소리 등록을 사용하지 않아요.'},410)
            if parsed.path=='/api/host-location':
                if not ALLOW_HOST_LOCATION:
                    return self.reply({'error':'개발용 위치 입력이 꺼져 있어요.'},403)
                return self.reply(HOST_POSITION.update(self.json_body()))
            if parsed.path=='/api/vehicle-location':
                return self.reply(VEHICLE_POSITION.update(self.json_body()))
            if parsed.path=='/api/speech/final':
                event = validate_final(self.json_body())
                with LOCK:
                    conversation_id()
                    updated = stage_final(STATE, event)
                    if updated is not None:
                        require_idle()
                        invalidate_recommendations(updated)
                        save_state(updated)
                    # Reply from the same critical section: a reset cannot
                    # replace the acknowledged conversation before its snapshot.
                    result = {'committed': True, 'state': snapshot()}
                return self.reply(result)
            if parsed.path=='/api/transcribe':
                supplied_conversation = self.headers.get('X-Conversation-Id', '')
                # Capture before reading a possibly slow body. Even legacy
                # uploads must not cross a reset that occurs during transfer.
                with LOCK:
                    captured_conversation = conversation_id()
                    if STATE.get('_demo_backup'):
                        require_conversation(supplied_conversation)
                        raise ValueError('샘플 종료를 누른 뒤 실제 대화를 녹음해 주세요.')
                stream = parse_qs(parsed.query).get('stream',['0'])[0] == '1'
                stream_id = self.headers.get('X-Stream-Id','') if stream else ''
                sequence = int(self.headers.get('X-Sequence','-1')) if stream else -1
                started = float(self.headers.get('X-Start-Seconds','0')) if stream else 0
                supplied_recording_origin = normalize_recording_started_at(self.headers.get('X-Recording-Started-At'))
                if stream and (not stream_id or len(stream_id)>80 or not all(c.isalnum() or c in '-_' for c in stream_id)
                               or not 0<=sequence<=1000 or not math.isfinite(started) or not 0<=started<=180):
                    raise ValueError('음성 스트림 정보가 올바르지 않아요.')
                payload=self.body()
                chunk_key=(stream_id,sequence)
                digest=hashlib.sha256(payload).hexdigest() if stream else ''
                with LOCK:
                    recording_origin = session_recording_origin(
                        STATE.get('_audio_streams', {}).get(stream_id) if stream else None, supplied_recording_origin)
                    receipt=STATE.get('_audio_streams', {}).get(stream_id, {}).get('receipts', {}).get(str(sequence)) if stream else None
                    if receipt is not None:
                        if receipt != digest: raise ValueError('같은 발언 번호에 다른 녹음이 전달됐어요. 발언을 확인해 주세요.')
                        return self.reply({'committed':True, 'duplicate':True, 'segments':[]})
                    require_conversation(supplied_conversation or captured_conversation)
                    if stream and STATE.get('_audio_streams', {}).get(stream_id, {}).get('closed'):
                        raise ValueError('이미 마친 대화의 녹음이에요. 현재 대화에는 추가하지 않았어요.')
                    previous=STREAM_CHUNKS.get(chunk_key) if stream else None
                    if previous in JOBS and JOBS[previous]['status'] != 'error':
                        if JOBS[previous].get('_audio_digest', digest) != digest: raise ValueError('같은 발언 번호에 다른 녹음이 전달됐어요. 발언을 확인해 주세요.')
                        session_recording_origin({'recording_started_at':JOBS[previous].get('_recording_started_at')}, recording_origin)
                        return self.reply({'job_id':previous},202)
                    if not ENGINE.ready: raise ValueError(ENGINE.message)
                    require_idle()
                file=DATA/(uuid.uuid4().hex+'.audio')
                try:
                    file.write_bytes(payload)
                except Exception:
                    file.unlink(missing_ok=True)
                    raise
                def action():
                    try:
                        segments=ENGINE.transcribe(file,allow_enrolled=False)
                        if not segments:
                            if not stream: raise ValueError('인식할 발언을 찾지 못했어요. 마이크를 확인하고 다시 말해 주세요. 기존 대화록은 유지돼요.')
                        with LOCK:
                            require_conversation(captured_conversation)
                            updated = copy.deepcopy(STATE)
                            if segments:
                                if updated['demo']:
                                    updated['segments']=[]
                                offset=stream_base+started if stream else max((s['end'] for s in updated['segments']),default=0)
                                for seg in segments:
                                    spoken_at = at_offset(recording_origin, started + seg['start'])
                                    if spoken_at is not None:
                                        seg['spoken_at'] = spoken_at
                                        seg['time_source'] = 'capture'
                                    seg['start']+=offset; seg['end']+=offset
                                    seg['seat']='unknown'; seg['seat_source']='unavailable'
                                updated['segments'].extend(segments)
                                updated['demo']=False
                                invalidate_recommendations(updated)
                            if stream: remember_audio_commit(updated,stream_id,sequence,digest,stream_base,recording_origin)
                            save_state(updated)
                        return {'segments':segments, 'committed':True}
                    finally: file.unlink(missing_ok=True)
                try:
                    with LOCK:
                        recording_origin = session_recording_origin(
                            STATE.get('_audio_streams', {}).get(stream_id) if stream else None, supplied_recording_origin)
                        receipt=STATE.get('_audio_streams', {}).get(stream_id, {}).get('receipts', {}).get(str(sequence)) if stream else None
                        if receipt is not None:
                            if receipt != digest: raise ValueError('같은 발언 번호에 다른 녹음이 전달됐어요. 발언을 확인해 주세요.')
                            file.unlink(missing_ok=True)
                            return self.reply({'committed':True, 'duplicate':True, 'segments':[]})
                        require_conversation(supplied_conversation or captured_conversation)
                        require_conversation(captured_conversation)
                        if stream and STATE.get('_audio_streams', {}).get(stream_id, {}).get('closed'):
                            raise ValueError('이미 마친 대화의 녹음이에요. 현재 대화에는 추가하지 않았어요.')
                        previous=STREAM_CHUNKS.get(chunk_key) if stream else None
                        if previous in JOBS and JOBS[previous]['status'] != 'error':
                            if JOBS[previous].get('_audio_digest', digest) != digest: raise ValueError('같은 발언 번호에 다른 녹음이 전달됐어요. 발언을 확인해 주세요.')
                            session_recording_origin({'recording_started_at':JOBS[previous].get('_recording_started_at')}, recording_origin)
                            file.unlink(missing_ok=True)
                            return self.reply({'job_id':previous},202)
                        require_idle()
                        stream_base=0
                        if stream:
                            if stream_id not in STREAMS:
                                persisted=STATE.get('_audio_streams', {}).get(stream_id, {})
                                STREAMS[stream_id]=persisted.get('base', 0 if STATE['demo'] else max((s['end'] for s in STATE['segments']),default=0))
                            stream_base=STREAMS[stream_id]
                        job_id=queue_job(action,'발언을 텍스트로 바꾸고 있어요.')
                        if stream:
                            STREAM_CHUNKS[chunk_key]=job_id
                            JOBS[job_id]['_audio_digest']=digest
                            JOBS[job_id]['_recording_started_at']=recording_origin
                        while len(STREAMS)>64: STREAMS.pop(next(iter(STREAMS)))
                        while len(STREAM_CHUNKS)>200: STREAM_CHUNKS.pop(next(iter(STREAM_CHUNKS)))
                except Exception:
                    file.unlink(missing_ok=True)
                    raise
                return self.reply({'job_id':job_id},202)
            if parsed.path=='/api/region':
                region=valid_region(self.json_body().get('region'))
                with LOCK:
                    require_idle()
                    if STATE.get('region') != region or STATE.get('search_mode') != 'manual':
                        updated=copy.deepcopy(STATE)
                        updated['region']=region
                        updated['search_mode']='manual'
                        invalidate_recommendations(updated, '검색 지역을 바꿨어요. 대화 내용으로 장소를 다시 찾아보세요.')
                        save_state(updated)
                return self.reply(snapshot())
            if parsed.path=='/api/search-mode':
                mode=self.json_body().get('mode')
                if mode not in ('conversation','current'):
                    raise ValueError('검색 기준을 확인해 주세요.')
                with LOCK:
                    require_idle()
                    updated=copy.deepcopy(STATE)
                    updated.update(search_mode=mode,region='현재 위치 주변' if mode=='current' else '')
                    if updated != STATE:
                        invalidate_recommendations(updated, '현재 위치를 기준으로 장소를 찾아요.' if mode=='current' else '대화에서 나온 조건과 지역을 바탕으로 장소를 찾아요.')
                        save_state(updated)
                return self.reply(snapshot())
            if parsed.path=='/api/recommend':
                options=self.json_body()
                with LOCK:
                    require_idle()
                    mode=options.get('mode', 'manual' if 'region' in options else STATE.get('search_mode','conversation'))
                    if mode not in ('conversation','current','manual'): raise ValueError('검색 기준을 확인해 주세요.')
                    segments=copy.deepcopy(STATE['segments'])
                    intent=resolve_search_intent(segments) if mode=='conversation' else {}
                    proposed_choice=options.get('area_choice')
                    choice_key=area_choice_key(segments) if mode=='conversation' else None
                    if proposed_choice is not None and (mode!='conversation' or STATE.get('search_mode','conversation')!='conversation'):
                        return self.reply({'error':'검색 기준이 바뀌었어요. 현재 기준으로 장소 추천을 다시 눌러 주세요.'},409)
                    if proposed_choice is not None and (not isinstance(proposed_choice,dict) or proposed_choice.get('key')!=choice_key):
                        return self.reply({'error':'대화 내용이 바뀌었어요. 현재 대화로 장소 추천을 다시 눌러 주세요.'},409)
                    if intent.get('needs_area_choice'):
                        remembered=STATE.get('search_intent',{}).get('area_choice')
                        choice=proposed_choice if proposed_choice is not None else remembered
                        valid_choice=isinstance(choice,dict) and choice.get('key')==choice_key
                        custom=valid_choice and choice.get('custom') is True
                        chosen_region=valid_region(choice.get('region')) if custom else choice.get('region') if valid_choice else None
                        if valid_choice and (custom or chosen_region in intent.get('candidate_regions',[])):
                            accepted={'region':chosen_region,'key':choice_key}
                            if custom: accepted['custom']=True
                            intent.update(region=chosen_region,search_regions=[chosen_region],multi_region=False,needs_area_choice=False,area_choice=accepted,
                                          message='대화를 확인하고 선택한 '+chosen_region+' 주변을 찾아요.')
                        else:
                            return self.reply({'error':intent.get('message') or '대화에 여러 지역이 나왔어요. 이번에 갈 지역을 골라 주세요.',
                                               'needs_area_choice':True, 'area_choices':intent.get('candidate_regions',[]),
                                               'area_choice_key':choice_key,'search_intent':intent},409)
                    elif proposed_choice is not None:
                        return self.reply({'error':'지역 선택이 필요했던 대화와 달라졌어요. 장소 추천을 다시 눌러 주세요.'},409)
                    constraints,evidence=extract(segments)
                    time_limit=request_profile(constraints)['travel_max_minutes']
                    origin_intent=resolve_search_intent(segments)
                    destination_regions=(intent.get('search_regions') or ([intent['region']] if intent.get('region') else [])) if mode=='conversation' else []
                    location=None
                    if mode=='conversation' and destination_regions:
                        region=' · '.join(valid_region(name) for name in destination_regions)
                    elif mode=='conversation' and origin_intent.get('origin_region'):
                        # An explicitly stated departure needs no unrelated GPS
                        # fix, and must not silently become the destination.
                        region=origin_intent['origin_region']
                    elif mode in ('conversation','current'):
                        if time_limit and STATE.get('demo'):
                            region='샘플 출발 위치'
                        else:
                            location=current_position()
                            region='현재 위치 주변'
                    else:
                        region=valid_region(options.get('region'))
                        destination_regions=[region]
                    if time_limit and origin_intent.get('needs_origin_choice'):
                        raise ValueError('출발지 의견이 여러 개예요. 이번에 출발할 위치를 대화에서 정해 주세요.')
                    captured_conversation=conversation_id()
                    rating_demo=bool(STATE.get('demo'))
                    captured_ratings=copy.deepcopy(STATE.get('_place_ratings', {}))

                    def action():
                        if time_limit:
                            origin=route_origin(origin_intent, location, rating_demo)
                            result=recommend_trip(segments,destination_regions,DATA/'places',origin,time_limit,
                                                  ratings=captured_ratings,demo=rating_demo)
                            result_intent=copy.deepcopy(intent)
                            result_intent['message']=((' · '.join(destination_regions)+' 후보를 함께 비교해요. ') if destination_regions else
                                                       '목적 지역을 정하지 않아 출발지에서 여러 지역의 후보를 찾아요. ')+f'자동차 이동 {time_limit}분 이내 조건을 적용했어요.'
                        else:
                            if len(destination_regions)>1:
                                result=recommend_regions(segments,destination_regions,DATA/'places')
                            else:
                                result=recommend(segments,region,DATA/'places',location=location) if location else recommend(segments,region,DATA/'places')
                            max_minutes=result.get('request_profile', {}).get('travel_max_minutes')
                            origin=route_origin(origin_intent, location, rating_demo) if max_minutes else None
                            result['recommendations']=enrich_travel_times(result.get('recommendations', []), origin, max_minutes)
                            result['recommendations'].sort(key=lambda p: 2 if p['travel_constraint'].get('within_limit') is False else 0 if p['travel_constraint'].get('within_limit') is True else 1)
                            result['region']=region
                            result_intent=intent
                        result.update(search_mode=mode,search_intent=result_intent,
                                      search_location={key:location[key] for key in ('accuracy_m','source','updated_at')} if location else {})
                        details = [result.get('coverage', ''), result.get('lookup_source', '')]
                        result['notice'] = ' · '.join(part for part in [result.get('notice', ''), *details] if part)
                        with LOCK:
                            require_conversation(captured_conversation)
                            result['recommendations'] = personalize(result.get('recommendations', []), STATE.get('_place_ratings', {}), rating_demo)
                            updated = copy.deepcopy(STATE)
                            updated.update(result)
                            save_state(updated)
                        return result

                    summary=summarize_conversation(segments,constraints,evidence)
                    summary['title']='대화에서 나온 조건'
                    updated=copy.deepcopy(STATE)
                    invalidate_recommendations(updated, '대화에서 나온 조건을 먼저 정리했어요.')
                    updated.update(region=region,search_mode=mode,search_intent=intent,
                                   constraints=constraints,recommendation_summary=summary,
                                   search_location={key:location[key] for key in ('accuracy_m','source','updated_at')} if location else {})
                    save_state(updated)
                    job_id=queue_job(action,'대화 조건을 정리하고 실제 장소를 찾고 있어요.')
                    queued_state=snapshot()
                return self.reply({'job_id':job_id,'state':queued_state},202)
            if parsed.path=='/api/reset':
                with LOCK:
                    require_idle()
                    updated = copy.deepcopy(STATE)
                    updated.update(segments=[],demo=False,conversation_id=uuid.uuid4().hex,
                                   search_mode='conversation',region='',demo_scenario={},_demo_backup={})
                    if (updated.get('trip') or {}).get('status') == 'selected':
                        updated['trip'] = {}
                    for audio_session in updated.get('_audio_streams', {}).values(): audio_session['closed']=True
                    invalidate_recommendations(updated, '')
                    save_state(updated)
                    STREAMS.clear(); STREAM_CHUNKS.clear()
                return self.reply(snapshot())
            if parsed.path=='/api/ratings':
                request=self.json_body()
                with LOCK:
                    save_state(rate_visit(STATE, request))
                return self.reply(snapshot())
            if parsed.path=='/api/demo':
                with LOCK:
                    require_idle()
                    updated = enter_demo(STATE)
                    invalidate_recommendations(updated, '샘플 대화예요. 실제 음성 인식 결과가 아니에요.')
                    save_state(updated)
                    STREAMS.clear(); STREAM_CHUNKS.clear()
                return self.reply(snapshot())
            if parsed.path=='/api/demo/exit':
                with LOCK:
                    require_idle()
                    save_state(leave_demo(STATE))
                    STREAMS.clear(); STREAM_CHUNKS.clear()
                return self.reply(snapshot())
            if parsed.path=='/api/segments':
                entry=self.json_body()
                seat=entry.get('seat','unknown'); text=str(entry.get('text','')).strip()
                if seat not in LABELS or not text or len(text)>4000: raise ValueError('좌석과 대화 내용을 확인해 주세요.')
                with LOCK:
                    require_idle()
                    updated = copy.deepcopy(STATE)
                    start=max((s['end'] for s in updated['segments']),default=0)
                    updated['segments'].append({'id':uuid.uuid4().hex,'seat':seat,'text':text,'start':start,'end':start,'confidence':None,'manual':True})
                    invalidate_recommendations(updated)
                    save_state(updated)
                return self.reply(snapshot())
            return self.reply({'error':'지원하지 않는 요청이에요.'},404)
        except (JourneyConflict, ConversationConflict, NativeTranscriptConflict) as error:
            return self.reply({'error':str(error)},409)
        except (ValueError,KeyError,TypeError) as error:
            return self.reply({'error':str(error)},400)
        except Exception:
            return self.reply({'error':'요청을 처리하지 못했어요. 다시 시도해 주세요.'},500)

    def do_PATCH(self):
        if not self.allowed_origin(): return self.reply({'error':'허용되지 않은 요청이에요.'},403)
        try:
            path=urlparse(self.path).path
            if not path.startswith('/api/segments/'):
                return self.reply({'error':'지원하지 않는 요청이에요.'},404)
            identifier=path.rsplit('/',1)[-1]
            patch=self.json_body()
            if patch.get('seat','unknown') not in LABELS: raise ValueError('좌석을 확인해 주세요.')
            with LOCK:
                require_idle()
                updated = copy.deepcopy(STATE)
                found=next((s for s in updated['segments'] if s['id']==identifier),None)
                if found is None: return self.reply({'error':'발언을 찾을 수 없어요.'},404)
                if 'text' in patch:
                    text=str(patch['text']).strip()
                    if not text or len(text)>4000: raise ValueError('발언 내용을 확인해 주세요.')
                    found['text']=text
                if 'seat' in patch:
                    found.update(seat=patch['seat'],confidence=None,corrected=True)
                invalidate_recommendations(updated)
                save_state(updated)
            return self.reply(snapshot())
        except (ValueError,TypeError) as error: return self.reply({'error':str(error)},400)
        except Exception: return self.reply({'error':'수정 내용을 저장하지 못했어요. 기존 대화록은 유지돼요.'},500)

    def do_DELETE(self):
        if not self.allowed_origin(): return self.reply({'error':'허용되지 않은 요청이에요.'},403)
        path=urlparse(self.path).path
        if not path.startswith('/api/profiles/'):
            return self.reply({'error':'지원하지 않는 요청이에요.'},404)
        seat=path.rsplit('/',1)[-1]
        if seat not in LABELS.keys()-{'unknown'}: return self.reply({'error':'좌석을 확인해 주세요.'},400)
        try:
            with LOCK:
                if ACTIVE_JOB and JOBS[ACTIVE_JOB]['status'] in ('queued','running'): return self.reply({'error':'처리 후 다시 시도해 주세요.'},409)
                ENGINE.delete_profile(seat)
        except Exception:
            return self.reply({'error':'목소리 등록 정보를 삭제하지 못했어요. 다시 시도해 주세요.'},500)
        return self.reply(snapshot())

if __name__=='__main__':
    threading.Thread(target=load_engine,daemon=True).start()
    print('Cabin conversation listening on http://127.0.0.1:8765',flush=True)
    ThreadingHTTPServer(('127.0.0.1',8765),Handler).serve_forever()
