"""Authored, explicitly fictional cabin conversations for a reversible demo."""
import copy
import random
import re
import uuid
from datetime import datetime, timedelta, timezone

from spoken_times import normalize_recording_started_at, at_offset


SCENARIOS = ({'id': 'nearby-lunch',
  'title': '밥부터 먹을까, 커피부터 마실까',
  'dialogue': (('rear1', '나 배에서 소리 나는데 들렸어? 아침도 못 먹었어.'),
               ('passenger', '난 방금 빵 먹고 나왔는데. 커피부터 마실 줄 알았지.'),
               ('rear2', '그건 너만 먹었잖아. 우리 밥부터 먹자.'),
               ('driver', '그래, 성수역에서 출발해서 가까운 데로 가자. 배고픈 사람 데리고 멀리 가면 혼나겠다.'),
               ('rear1', '따뜻한 칼국수 어때? 국물 먹고 싶다.'),
               ('rear2', '좋아. 근데 나 매운 음식은 못 먹어. 빨간 칼국수 말고.'),
               ('passenger', '바지락 들어간 맑은 거? 그럼 나도 조금 먹을래.'),
               ('driver', '주차는 좀 편했으면 좋겠다. 지난번처럼 골목 세 바퀴 돌기 싫어.'),
               ('rear2', '그때 내가 그냥 다른 데 가자고 했잖아.'),
               ('driver', '알았어, 오늘은 주차장부터 볼게. 가까운 공영주차장 있어도 괜찮고.'),
               ('rear1', '얼마 정도 생각해? 나 이번 주에 돈을 너무 썼어.'),
               ('passenger', '점심은 만 원 안팎으로 먹자. 비싸도 1인 만 이천 원까지?'),
               ('rear1', '좋지. 돈까스도 그 정도면 괜찮겠다.'),
               ('rear2', '돈까스 좋아. 둘 중에 덜 기다리는 데로 가자.'),
               ('driver', '가는 데는 30분 넘기지 말자. 앉아서 먹는 시간보다 운전이 길면 억울해.'),
               ('passenger', '밥 먹고 커피는 마시는 거지? 그건 포기 못 해.'),
               ('rear1', '응, 대신 식당 근처로. 밥 먹고 또 차에서 한참 기다리긴 싫어.'),
               ('rear2', '카페는 조용한 데 가자. 우리 지난번에 서로 말 하나도 못 들었어.'),
               ('passenger', '그날 스피커 바로 밑이었잖아. 이번엔 넷이 앉아서 얘기 좀 하자.'),
               ('rear1', '치즈케이크 있으면 한 조각 나눠 먹을래?'),
               ('passenger', '밥값 아낀다더니 디저트는 바로 나오네.'),
               ('rear1', '그건 넷이 나누니까 괜찮아.'),
               ('rear2', '난 커피 말고 과일 음료 마실래. 그런 메뉴도 있겠지?'),
               ('driver', '음료는 한 잔에 6천 원 정도면 좋겠다. 케이크는 따로 나누고.'),
               ('passenger', '좋아. 밥부터 먹자. 카페는 밥 먹고 나와서 정하자.'),
               ('rear1', '응. 지금은 맛있다는 데보다 바로 앉을 수 있는 데가 더 끌려.'))},
 {'id': 'seoul-rain',
  'title': '우산 두 개로 넷이 놀기',
  'dialogue': (('passenger', '우산 접고 들어와, 시트 다 젖겠다.'),
               ('rear1', '이미 신발 안까지 젖었어. 오늘 산책은 진짜 싫어.'),
               ('driver', '실내로 다닐 만한 데 없나? 비 오니까 멀리 운전하기도 피곤하네.'),
               ('rear2', '영화 볼까?'),
               ('passenger', '영화는 어제 봤잖아. 나는 미술관 가고 싶어.'),
               ('rear2', '아, 그럼 사진 전시 같은 건 어때? 그런 건 나도 재밌더라.'),
               ('rear1', '좋은데 하루 종일 서 있는 건 못 해. 한 시간쯤 보고 앉아서 쉬자.'),
               ('driver', '지금 왕십리역에 있으니까 차로 40분 안에 가는 데로 보자. 우산 두 개로 넷이 걷긴 힘들잖아.'),
               ('passenger', '건물에 주차장 붙어 있는 데면 좋겠네.'),
               ('rear1', '엘리베이터도 있으면 좋겠어. 오늘 계단 많이 오르기 싫다.'),
               ('rear2', '근데 전시 오늘 바로 들어갈 수 있어? 예약해야 되는 데도 있잖아.'),
               ('passenger', '맞다. 매진이면 헛걸음이니까 가기 전에 확인해야겠다.'),
               ('driver', '입장료는 어느 정도까지 괜찮아?'),
               ('rear2', '입장료는 한 사람당 만오천 원 정도까지. 네 명이면 표값도 꽤 되네.'),
               ('passenger', '그 안에서 보자. 꼭 유명한 특별전 아니어도 돼. 작은 미술관도 좋아.'),
               ('rear1', '박물관도 괜찮지? 중간에 앉을 데 있는 곳이면 더 좋고.'),
               ('passenger', '응, 실내 전시면 좋아. 작품 보고 이런저런 얘기하는 게 좋은 거라서.'),
               ('rear2', '보고 나서는 카페 가자. 따뜻한 커피랑 케이크 먹고 싶어.'),
               ('driver', '거기서 또 멀리 가야 하는 건 아니겠지?'),
               ('rear2', '근처로 가면 되지. 비 덜 맞는 데로.'),
               ('rear1', '난 차 마실래. 오늘 커피는 이미 두 잔 마셨어.'),
               ('passenger', '창가 자리 있으면 좋겠다. 넷이 앉아서 비 오는 거 구경하자.'),
               ('driver', '나 저녁 약속 있어서 세 시간쯤 뒤에는 돌아와야 해.'),
               ('passenger', '그럼 전시 하나만 보고, 남는 시간에 카페에서 쉬자. 돌아갈 시간은 남겨 두고.'),
               ('rear2', '카페는 너무 시끄럽지만 않으면 돼. 케이크는 내가 살게.'),
               ('rear1', '좋아. 나 젖은 양말 좀 말릴 수 있게 따뜻한 실내로 빨리 가자.'))},
 {'id': 'gangneung-sea',
  'title': '바다는 보고 싶은데, 어디로 갈까',
  'dialogue': (('driver', '벨트 다 했지? 그래서 우리 어디 가는 건데.'),
               ('passenger', '나 오늘 바다 보고 싶어. 계속 건물만 보고 있었더니 답답해.'),
               ('rear1', '바다 하니까 칼국수 당긴다. 파전도 같이 먹고.'),
               ('rear2', '나는 회! 바닷가까지 가는데 회는 먹어야지.'),
               ('driver', '벌써 메뉴가 갈리네. 나는 편도 두 시간 넘게 운전하는 건 힘들어. 돌아올 때도 내가 하잖아.'),
               ('passenger', '지금 판교역에 있으니까 여기서 두 시간 안에 갈 수 있는 바다부터 찾아보자. 어느 지역인지는 상관없어.'),
               ('driver', '좋아. 막히면 더 걸릴 수 있으니까 출발 전에 걸리는 시간부터 보자.'),
               ('rear2', '그럼 점심 먹고 해변 가면 되겠다.'),
               ('rear1', '잠깐, 점심이 회로 결정된 건 아니지? 나는 오늘 익힌 거 먹고 싶어.'),
               ('rear2', '너는 오늘 익힌 걸로 먹고 싶은 거지? 그럼 다른 메뉴도 있는 집으로 찾아보자.'),
               ('rear1', '난 칼국수가 제일 좋아. 파전 하나 시켜서 나눠 먹고.'),
               ('passenger', '횟집에서도 칼국수 파는 데 있나? 같이 있으면 편하겠다.'),
               ('driver', '같이 파는 데가 없으면 메뉴는 다시 정하자. 한 사람만 먹을 게 없는 데는 가지 말고.'),
               ('rear2', '응. 비싼 회 코스까지는 필요 없어. 작은 모둠회면 돼.'),
               ('passenger', '밥값은 넷이서 총 12만 원 안으로 어때? 커피는 나중에 따로 하고.'),
               ('rear1', '그 정도면 괜찮아. 그런 집 찾으면 파전은 한 장 시켜서 나눠 먹자.'),
               ('rear2', '좋아. 칼국수도 주문하게 되면 나도 국물 한 숟갈만 줘.'),
               ('driver', '가게도 안 골랐는데 나눠 먹을 계획은 다 세웠네. 주차장 넓은 식당이면 더 좋겠다.'),
               ('passenger', '식당에서 바다 안 보여도 돼. 밥 먹고 해변 가서 보면 되니까.'),
               ('rear1', '산책은 오래 안 하는 거지? 배부르면 많이 걷기 힘들어.'),
               ('passenger', '30분만 걷자. 사진도 찍고 앉아서 바다도 좀 보고.'),
               ('rear2', '그러고 카페 갈래? 바다 보면서 쉬면 좋겠다.'),
               ('driver', '해변에서 가까우면 좋아. 차 뺐다가 또 한참 가는 건 말고.'),
               ('rear2', '나 카페인은 못 마셔서 과일 음료 있는 데면 좋겠어.'),
               ('rear1', '카페에 빵도 있으면 좋겠다. 아, 나 지금 너무 배고픈가 봐.'),
               ('passenger', '일단 점심부터 고르자. 바다는 밥 먹고 천천히 봐도 되니까.'))},
 {'id': 'daejeon-family',
  'title': '가족 저녁은 다 같이 편한 곳으로',
  'dialogue': (('driver', '다 탔어요? 오늘 저녁은 밖에서 먹고 들어가죠.'),
               ('rear2', '나 돈까스 먹고 싶어.'),
               ('passenger', '나는 밥이랑 반찬 나오는 한식이 좋은데. 점심도 면 먹었잖아.'),
               ('rear1', '난 어느 쪽이든 괜찮아. 대신 매운 음식은 못 먹어.'),
               ('driver', '대전역에서 출발해서 30분 안에 가는 데로 볼게요. 다들 많이 걸었으니까 오늘은 가까운 데서 편하게 먹어요.'),
               ('rear2', '한식이면 불고기는 어때? 그건 나도 좋아.'),
               ('passenger', '불고기 좋지. 돈까스랑 우동 있는 일식집도 괜찮고.'),
               ('rear1', '우동 있으면 나는 그거 먹을래. 속이 좀 허전하네.'),
               ('driver', '주차는 식당에 바로 할 수 있으면 좋겠어요. 멀리 세워 놓고 걸어오는 건 힘들잖아요.'),
               ('rear1', '응. 아까 많이 걸었더니 무릎이 좀 뻐근해.'),
               ('passenger', '의자 있는 자리로 가자. 바닥에 앉으면 일어날 때 힘들어.'),
               ('rear2', '그럼 문 앞에 계단 많은 데도 피해야겠다.'),
               ('driver', '오늘 밥값은 제가 낼게요. 넷이서 8만 원쯤이면 괜찮죠?'),
               ('passenger', '그 정도면 충분하지. 괜히 비싼 코스로 가지 말고.'),
               ('rear2', '고마워. 그럼 나는 맛있는 거 고르는 역할 할게.'),
               ('rear1', '그러다 제일 비싼 거 시키는 거 아니야?'),
               ('rear2', '아니야, 돈까스면 된다니까.'),
               ('passenger', '모자라면 같이 먹을 거 하나 더 시키고. 그래도 총 10만 원은 넘기지 말자.'),
               ('driver', '알겠어요. 가게 정하면 출발 전에 쉬는 시간인지부터 확인할게요.'),
               ('rear1', '줄 길게 서는 데는 말자. 오늘은 바로 앉고 싶어.'),
               ('rear2', '저녁 먹으러 오는 사람 많으려나. 자리 있는지 전화해 볼까?'),
               ('passenger', '그래. 넷이 한 테이블에 앉을 수 있는지도 물어보고.'),
               ('driver', '술집처럼 시끄러운 데보다 얘기하면서 먹을 수 있는 곳이 좋겠어요.'),
               ('passenger', '나도. 밥 먹으면서 오늘 사진도 좀 같이 보자.'),
               ('rear1', '맑은 국이랑 맵지 않은 반찬 있으면 나는 잘 먹어.'),
               ('rear2', '그럼 한식이랑 일식 둘 다 보자. 먹고 나서는 바로 들어가서 쉬고.'))},
 {'id': 'busan-friends',
  'title': '생일 저녁, 고기에서 파스타로',
  'dialogue': (('driver', '생일 주인공, 다들 배고프다는데 저녁 뭐 먹고 싶어?'),
               ('passenger', '고기 먹자. 삼겹살 당기는데.'),
               ('rear1', '또 고기야? 나는 오늘 파스타 먹고 싶었어.'),
               ('rear2', '나도 파스타 한 표. 좀 조용한 데서 생일 얘기라도 하자.'),
               ('passenger', '그럼 파스타 먹자. 고기는 다음에 먹으면 되지.'),
               ('driver', '바뀌는 속도가 빠른데?'),
               ('passenger', '나 사실 메뉴보다 너희가 사 준다는 게 중요해.'),
               ('rear2', '그래서 생일이라고 강조했구나.'),
               ('rear1', '난 토마토 파스타 먹을래. 매운 건 싫고 그냥 토마토 소스로.'),
               ('rear2', '난 크림. 피자도 하나 시켜서 나눠 먹자.'),
               ('passenger', '좋지. 나는 해산물 파스타 있으면 그걸로.'),
               ('driver', '그럼 고깃집은 넘어가고 파스타 잘하는 집 보자. 피자도 같이 되는 데면 더 좋고.'),
               ('rear1', '근데 얼마 정도 생각하고 있어? 생일이라고 너무 무리하지는 말자.'),
               ('rear2', '오늘은 한 사람당 5만 원까지 괜찮아. 평소 밥값이랑 똑같이 잡을 필요는 없지.'),
               ('driver', '나도 괜찮아. 그렇다고 꼭 다 쓰자는 건 아니고.'),
               ('passenger', '알았어. 비싼 코스보다는 각자 파스타 시켜서 나눠 먹는 게 더 좋아.'),
               ('rear1', '그럼 메뉴도 서로 바꿔 먹자. 난 크림 한 입만.'),
               ('rear2', '한 입이 꼭 크더라, 너는.'),
               ('driver', '지금 부산역에 있으니까 식당까지 40분 안으로 가자. 주차장 없으면 가까운 공영주차장 있는 데로.'),
               ('passenger', '바다 보이는 자리 아니어도 돼. 오늘은 맛있는 저녁이면 충분해.'),
               ('rear2', '맞아. 음악 크게 틀어 놓는 데는 피하자. 얘기하다 목 쉬겠어.'),
               ('rear1', '넷이 마주 보고 앉으면 좋겠다. 바 자리로 따로 앉으면 생일 분위기 안 나잖아.'),
               ('driver', '가게 고르면 예약되는지부터 보자. 생일인데 가서 오래 기다리기는 아깝잖아.'),
               ('passenger', '나 배고파지기 시작했어. 메뉴 사진부터 보고 고르자.'),
               ('rear2', '케이크는 밥 먹고 꺼내도 되는지 물어봐야겠다.'),
               ('rear1', '오, 준비했어? 그럼 진짜 조용한 식당으로 가야겠네.'))},
 {'id': 'jeju-walk',
  'title': '제주에서 서두르지 않는 오후',
  'dialogue': (('driver', '오늘 오후에는 뭐 할까? 오전 내내 차만 탄 느낌인데.'),
               ('rear1', '박물관 하나 볼까 했는데.'),
               ('passenger', '나는 밖에 잠깐 나가고 싶어. 햇빛 좋잖아.'),
               ('rear2', '공원 가자. 창문으로만 보지 말고 좀 걷자.'),
               ('rear1', '그러게, 날씨 아깝다. 박물관은 다음에 가고 오늘은 공원 산책하자.'),
               ('driver', '좋아. 대신 등산하는 건 아니지? 신발도 편한 거 안 신었는데.'),
               ('rear2', '나도 등산은 싫어. 평평한 길로 천천히 걷고 싶어.'),
               ('passenger', '30분 정도만 걷고 카페 가면 어때? 창가에 앉아서 쉬고 싶어.'),
               ('rear1', '그거 좋다. 벤치 있으면 중간에 앉아도 되고.'),
               ('driver', '제주시청에서 출발해서 차로 40분 안에 가는 데로 보자. 오늘은 멀리 돌아다니지 말고.'),
               ('rear2', '공원에 화장실도 있으면 좋겠어. 아까 물을 너무 많이 마셨어.'),
               ('driver', '그럼 주차장에서 입구 가까운 데가 편하겠다. 주차장 있는지 같이 보자.'),
               ('passenger', '그늘도 좀 있으면 좋겠네. 볕은 좋은데 계속 쬐면 덥더라.'),
               ('rear1', '공원 입장료는 있나?'),
               ('rear2', '모르겠어. 있어도 괜찮아, 많이 안 걸어도 예쁜 데면.'),
               ('passenger', '오늘은 가격 딱 정하지 말고 편하게 쉬는 쪽으로 고르자. 줄 오래 서는 데만 아니면 돼.'),
               ('driver', '응. 유명하다고 주차 기다리다 오후 다 보내는 건 싫어.'),
               ('rear1', '걷고 나면 출출할 것 같아. 카페에 빵도 있으면 좋겠다.'),
               ('rear2', '아까 점심 먹었는데 벌써?'),
               ('rear1', '걷고 나면 그렇다는 거지, 지금 당장은 아니고.'),
               ('passenger', '나도 디저트 먹을래. 넷이 앉을 만한 테이블 있는 조용한 카페면 좋겠다.'),
               ('rear2', '난 커피 말고 차 마실 거야. 따뜻한 걸로.'),
               ('driver', '공원에서 카페까지는 가까웠으면 해. 차 타자마자 다시 내릴 정도면 더 좋고.'),
               ('passenger', '바람 너무 불면 카페 먼저 가도 되지. 오늘은 시간표대로 움직이기 싫어.'),
               ('rear1', '응. 그래도 산책은 하자. 나 이제 바깥에 나가고 싶어졌어.'),
               ('rear2', '그러고 해 지기 전에 숙소 들어가자. 오늘은 일찍 쉬고 싶다.'))})

CONVERSATION_FIELDS = (
    'segments', 'recommendations', 'constraints', 'region', 'demo', 'notice',
    'coverage', 'lookup_source', 'recommendation_summary', 'trip', '_audio_streams',
    'conversation_id', 'search_mode', 'search_location', 'search_intent', 'demo_scenario', 'request_profile',
)


def random_scenario(previous_id=''):
    """Uniformly choose another authored scenario; never repeat immediately."""
    return copy.deepcopy(random.choice([s for s in SCENARIOS if s['id'] != previous_id]))


def sample_segments(dialogue, ended_at=None):
    """Fictional speech offsets in seconds, with brief conversational gaps."""
    segments=[]
    cursor=0.0
    for index,(seat,text) in enumerate(dialogue):
        spoken_length=len(re.sub(r'[\s.,!?…·—:;]','',text))
        duration=round(min(8.0,max(2.0,spoken_length/7.0+0.2)),2)
        if index:
            cursor=round(cursor+(0.35,0.5,0.65,0.45)[index%4],2)
        end=round(cursor+duration,2)
        segments.append({'id':uuid.uuid4().hex,'seat':seat,'text':text,
                         'start':cursor,'end':end,'confidence':None,
                         'cluster_id':'demo-'+seat,'seat_source':'sample'})
        cursor=end
    if segments:
        end_clock = datetime.now(timezone.utc) if ended_at is None else ended_at
        if not isinstance(end_clock, datetime) or end_clock.utcoffset() is None:
            raise ValueError('샘플 시각에는 시간대가 필요해요.')
        origin = normalize_recording_started_at((end_clock - timedelta(seconds=cursor)).isoformat())
        for segment in segments:
            segment['spoken_at'] = at_offset(origin, segment['start'])
            segment['time_source'] = 'sample'
    return segments


def require_demo_switch_allowed(state):
    if (state.get('trip') or {}).get('status') in ('navigating', 'arrived'):
        raise ValueError('길안내를 마치고 도착 기록을 정리한 뒤 샘플을 바꿔 주세요.')


def enter_demo(state):
    require_demo_switch_allowed(state)
    updated = copy.deepcopy(state)
    if not updated.get('_demo_backup'):
        # Persist the backup beside the conversation so app/server restarts do
        # not lose the original. Saved visits stay outside the swap.
        updated['_demo_backup'] = {key: copy.deepcopy(state[key]) for key in CONVERSATION_FIELDS if key in state}
        if state.get('demo'):
            # Old releases had replace-only samples with no restorable original.
            updated['_demo_backup'].update(segments=[], recommendations=[], constraints=[],
                demo=False, demo_scenario={}, trip={}, region='', search_mode='conversation',
                notice='', coverage='', lookup_source='', recommendation_summary={},
                search_location={}, search_intent={}, request_profile={}, _audio_streams={})
    scenario = random_scenario((state.get('demo_scenario') or {}).get('id', ''))
    updated.update(
        segments=sample_segments(scenario['dialogue']),
        demo=True, demo_scenario={'id': scenario['id'], 'title': scenario['title']},
        region='', search_mode='conversation', conversation_id=uuid.uuid4().hex,
        trip={}, _audio_streams={},
    )
    return updated


def leave_demo(state):
    backup = state.get('_demo_backup')
    if not backup:
        return copy.deepcopy(state)
    require_demo_switch_allowed(state)
    updated = copy.deepcopy(state)
    for key in CONVERSATION_FIELDS:
        updated.pop(key, None)
    updated.update(copy.deepcopy(backup))
    updated.update(_demo_backup={}, demo_scenario={}, conversation_id=uuid.uuid4().hex)
    for stream in updated.get('_audio_streams', {}).values():
        stream['closed'] = True
    return updated
