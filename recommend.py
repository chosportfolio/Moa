"""Explainable Korean preference extraction and real public OSM place lookup."""
import copy
import hashlib
import json
import math
import os
import re
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree

LABELS = {'driver': '운전자', 'passenger': '동승자', 'rear1': '뒷좌석 1', 'rear2': '뒷좌석 2', 'unknown': '화자 확인 필요'}
TOPICS = {
    '카페': ['커피', '카페', '라떼', '아메리카노'],
    '식사': ['식당', '밥', '식사', '점심', '저녁', '배고', '배가 고'],
    '빵·디저트': ['디저트', '빵', '베이커리', '케이크'],
    '한식': ['한식', '국밥', '찌개', '비빔밥', '백반'],
    '일식': ['일식', '초밥', '돈까스', '돈가스', '돈카츠', '라멘', '스시', '우동'],
    '중식': ['중식', '짜장', '짬뽕', '중국집'],
    '양식': ['양식', '파스타', '피자', '스테이크'],
    '파스타': ['파스타', '스파게티'],
    '피자': ['피자'],
    '돈까스': ['돈까스', '돈가스', '돈카츠'],
    '초밥': ['초밥', '스시'],
    '우동': ['우동'],
    '불고기': ['불고기'],
    '매운 음식': ['매운', '맵', '매워', '매운맛'],
    '고기': ['고기', '삼겹살', '갈비', '바비큐', '바베큐'],
    '칼국수': ['칼국수'],
    '파전': ['파전', '해물파전'],
    '회': ['회', '횟집', '생선회', '활어회', '모둠회', '모듬회', '사시미'],
    '해산물': ['해산물', '해물', '조개', '새우', '대게', '킹크랩'],
    '산책': ['산책', '공원', '걷고', '걷자', '걸을'],
    '전시·문화': ['전시', '미술관', '박물관', '문화'],
    '해변': ['바다', '해변', '해수욕장'],
}
AMENITY_TOPIC = {'cafe': '카페', 'restaurant': '식사', 'fast_food': '식사', 'food_court': '식사', 'ice_cream': '빵·디저트'}
CUISINE_TOPICS = {'korean':'한식', 'japanese':'일식', 'sushi':'초밥', 'chinese':'중식',
                  'italian':'양식', 'pizza':'피자', 'pasta':'파스타', 'western':'양식',
                  'tonkatsu':'돈까스', 'udon':'우동', 'bulgogi':'불고기',
                  '돈까스':'돈까스', '돈가스':'돈까스', '돈카츠':'돈까스', '우동':'우동', '불고기':'불고기',
                  'steak_house':'고기', 'barbecue':'고기', 'seafood':'해산물',
                  'sashimi':'회', 'kalguksu':'칼국수', 'pajeon':'파전'}
DISH_PARENTS = {'파스타':'양식', '피자':'양식', '돈까스':'일식', '초밥':'일식', '우동':'일식', '불고기':'고기'}
MENU_TOPICS = frozenset(('한식','일식','중식','양식','고기','매운 음식','빵·디저트','칼국수','파전','회','해산물', *DISH_PARENTS))
SPECIFIC_MENU_TOPICS = frozenset(('칼국수','파전','회','해산물', *DISH_PARENTS))
# These are fixed catalog expressions, never interpolated user input. Exact
# dishes get their own result allowance instead of being lost after 180 cafes.
MENU_SEARCH_TERMS = {'칼국수':('칼국수','kalguksu'), '파전':('파전','pajeon'),
                     '회':('횟집|생선회|활어회|모둠회|모듬회|사시미|회센터','sashimi'),
                     '해산물':('해물|해산물|조개|대게|새우','seafood'),
                     '한식':('한식|국밥|비빔밥|백반','korean'),
                     '일식':('일식|초밥|돈까스|돈가스|라멘|스시','(japanese|sushi)'),
                     '중식':('중식|짜장|짬뽕|중국집','chinese'),
                     '양식':('파스타|피자|스테이크|이탈리안','(italian|pizza|pasta|western)'),
                     '고기':('고기|삼겹살|갈비|바비큐|바베큐','(steak_house|barbecue)'),
                     '파스타':('파스타|스파게티','pasta'), '피자':('피자','pizza'),
                     '돈까스':('돈까스|돈가스|돈카츠','tonkatsu'), '초밥':('초밥|스시','sushi'),
                     '우동':('우동','udon'), '불고기':('불고기','bulgogi')}
MENU_FALLBACK_TERMS = {'칼국수':('칼국수',), '파전':('파전',), '회':('횟집',),
                       '해산물':('해물',), '한식':('한식','국밥'),
                       '일식':('돈까스','초밥'), '중식':('중국집','짬뽕'),
                       '양식':('파스타','피자'), '고기':('고기','갈비'),
                       '파스타':('파스타','스파게티'), '피자':('피자',),
                       '돈까스':('돈까스','돈가스'), '초밥':('초밥','스시'),
                       '우동':('우동',), '불고기':('불고기',)}
RAW_FISH = re.compile(r'횟집|생선회|활어회|모둠회|모듬회|사시미|회센터|(?<![가-힣])회(?=(?:를|는|가|도|랑|와|만|나|로|요)?(?:\s|$|[,.!?]|먹|좋|싫))')
NEGATIVE = re.compile(r'싫|별로|안\s*먹|못\s*먹|안\s*마|못\s*마|피하|피하고|제외|빼\s*줘|빼고|말고|안\s*가|못\s*가|부담|(?:땡기|당기)지\s*않|싶지(?:는)?\s*않|좋(?:아하)?지(?:는)?\s*않|안\s*좋아|선호하지\s*않|됐어|됐고|알레르기')
POSITIVE = re.compile(r'좋|괜찮|원해|땡겨|당겨|당긴다|땡긴다|가자|갈까|갈래|가면|가고\s*싶|먹자|먹을까|먹을래|먹고\s*싶|마시자|마실까|마실래|마시고\s*싶|보러|보고\s*싶|하고\s*싶|하자|어때|추천|찾아|보자|찍자|고르자|골라|일\s*순위|끌려|시켜서.{0,12}나눠\s*먹')
UNCERTAIN_NEGATION = re.compile(r'싫지(?:는)?\s*않|안\s*싫|안\s*(?:가|먹|마셔)도\s*(?:돼|되)|못\s*(?:먹|마시)는\s*(?:건|것은)\s*아니')
PARKING_OPTIONAL = re.compile(r'주차.{0,8}(?:필요\s*없|상관\s*없|없어도\s*(?:괜찮|돼)|안\s*해도\s*(?:괜찮|돼))')
CHANGE_OF_PLAN = re.compile(r'^(?:아니다|아니야|아니요|아니)(?=\s|$)')
CHOICE_ACTION = re.compile(r'가자|갈까|갈래|가고\s*싶|먹자|먹을까|먹을래|먹고\s*싶|마시자|마실까|마실래|마시고\s*싶|보러|보고\s*싶|하고\s*싶|하자|추천|찾아')
SPICY_MODIFIER = r'(?:매운|매콤한|얼큰한|얼큰|빨간|빨갛게)\s*'
SPICY_DISH = re.compile(SPICY_MODIFIER + r'(?:맛의?\s*)?(?:칼국수|국수|찌개|국물|탕|음식)')
COFFEE_ALTERNATIVE = re.compile(r'(?:커피|아메리카노|라떼)(?:는|를|은|을)?\s*(?:말고|대신)\s*'
                               r'(?:(?:따뜻한|차가운|과일)\s*)?(?:음료|주스|쥬스|티|차)(?=\s|[를는도가로]|마|먹|$)')
NOMINATIM_SEARCH_URL = os.environ.get('CABIN_NOMINATIM_SEARCH_URL','https://nominatim.openstreetmap.org/search')
NOMINATIM_MIN_INTERVAL = 1.1
_NOMINATIM_LOCK = threading.Lock()
_NOMINATIM_LAST_REQUEST = None
_LOCATION_POI_CACHE = OrderedDict()
_LOCATION_POI_CACHE_LOCK = threading.Lock()
_LOCATION_POI_CACHE_TTL = 86400
_LOCATION_POI_CACHE_LIMIT = 32


def split_clauses(text):
    """Separate explicit preference boundaries without splitting '먹고 싶다'."""
    return re.split(
        r'[!?。\n]+|(?<!\d)\.|\.(?!\d)|[,，]|하지만|그런데|그래도|'
        r'(?<=싫고)|(?<=싫지만)|(?<=싫은데)|(?<=말고)(?!도)|(?<=됐고)|'
        r'(?<=좋고)|(?<=좋지만)|(?<=좋은데)|(?<=싶고)|'
        r'(?<=먹고)(?!\s*싶)|(?<=마시고)(?!\s*싶)|(?<=가고)(?!\s*싶)|'
        r'(?<=먹어서)|(?<=마셔서)', text)


def mentions_topic(clause, topic):
    if topic == '회':
        # 회의/회사/대회/회관 are not requests for raw fish.
        return bool(RAW_FISH.search(clause))
    if topic == '카페':
        # A caffeine restriction does not reject every cafe, which may have
        # caffeine-free drinks. Keep that as a menu check, not a venue veto.
        clause = clause.replace('카페인', '')
    if topic == '매운 음식' and SPICY_DISH.search(clause):
        return True
    return any(word in clause for word in TOPICS[topic])


def explicitly_names_category(clause, topic):
    return bool(re.search(r'(?<![가-힣])'+re.escape(topic), clause))


def effective_likes(profile):
    """A requested dish does not also request every dish in its cuisine."""
    likes=set(profile['likes'])
    explicit=set(profile.get('explicit_menu_topics',[]))
    return likes-{parent for dish,parent in DISH_PARENTS.items()
                  if dish in likes | set(profile['dislikes']) and parent not in explicit}


def registered_menu_topics(tags):
    topics={CUISINE_TOPICS[cuisine.strip()] for cuisine in
            re.split('[;,]', str(tags.get('cuisine','')).casefold()) if cuisine.strip() in CUISINE_TOPICS}
    topics.update(DISH_PARENTS[topic] for topic in tuple(topics) if topic in DISH_PARENTS)
    return topics


def travel_limit(clause):
    """Extract an explicit maximum, not elapsed time or a driving ETA."""
    numbers = {'한':1,'두':2,'세':3,'네':4,'다섯':5,'여섯':6}
    pattern = r'(?P<amount>\d+(?:\.\d+)?|한|두|세|네|다섯|여섯)\s*(?P<unit>시간|분)(?:\s*(?P<half>반)|\s*(?P<minutes>\d+)\s*분)?'
    limits=[]
    for match in re.finditer(pattern, clause):
        suffix=clause[match.end():match.end()+24]
        prefix=clause[max(0,match.start()-12):match.start()]
        if not (re.search(r'^\s*(?:이내|안(?:에|으로)?(?:\s|$)|까지|(?:은|는|을)?\s*넘(?:기|지)|이상.{0,8}(?:싫|안|힘|부담))',suffix)
                or re.search(r'최대\s*$',prefix)):
            continue
        # Explicit dining/waiting/dwell duration is not a travel constraint.
        if re.search(r'(?:대기|기다리|머무|관람|식사\s*시간|먹는\s*데|먹는데)',clause) and not re.search(r'운전|이동|편도|도착|가는\s*데|가는데',clause):
            continue
        amount=numbers.get(match['amount'])
        if amount is None:
            amount=float(match['amount'])
        minutes=amount*(60 if match['unit']=='시간' else 1)
        if match['half'] and match['unit']=='시간': minutes+=30
        if match['minutes']: minutes+=int(match['minutes'])
        if 0 < minutes <= 1440:
            limits.append(round(minutes))
    return min(limits) if limits else None


def topic_polarity(clause, topic, hungry, utterance=''):
    if (re.search(r'밥값|식비|입장료|예산|음료값|커피값|표값', clause) and '원' in clause
            and not CHOICE_ACTION.search(clause)):
        return None
    # Acknowledging somebody else's restriction does not assign it to the
    # speaker. An explicit "나는/내가 ... 알지?" still describes that speaker.
    if (re.search(r'(?:안|못)\s*(?:먹|마시)는\s*(?:건|거|것).{0,5}(?:알지|알아|알고)', clause)
            and not re.search(r'(?:나는|난|내가|저는|전|제가)(?=\s)', clause)):
        return None
    if (topic == '카페' and COFFEE_ALTERNATIVE.search(utterance)
            and re.search(r'커피|아메리카노|라떼', clause) and re.search(r'말고|대신', clause)
            and not re.search(r'카페(?:는|를)?\s*(?:말고|싫|안\s*좋아)', clause)):
        return None
    if topic in MENU_TOPICS - {'매운 음식'} and NEGATIVE.search(clause):
        modified = SPICY_MODIFIER + r'(?:맛의?\s*)?(?:' + '|'.join(map(re.escape, TOPICS[topic])) + ')'
        if re.search(modified, clause):
            return None
    # These clauses constrain a journey, price or choice breadth, not every
    # venue noun mentioned while describing that constraint.
    if re.search(r'(?:운전|이동|걸|걷|대기|기다리).{0,14}(?:싫|피하|말자|힘들)',clause) and not re.search(r'(?:산책|공원).{0,6}(?:은|는)?\s*싫',clause):
        if topic != '산책': return None
    if re.search(r'만.{0,12}(?:고집|찾지는|찾지|추천하면)|말고도',clause):
        return None
    if topic in {'식사','카페'} and re.search(r'비싼|대기.{0,4}긴|줄.{0,4}긴|주차.{0,5}없|음악.{0,4}큰|매진',clause):
        return None
    if re.search(r'(?:먹자|가자|마시자)는\s*얘기.{0,8}취소|얘기.{0,8}취소|(?:은|는|도)?\s*다음(?:에|번에)',clause):
        return True
    if re.search(r'좋다는\s*이유로|만\s*마시면.{0,14}(?:힘들|곤란|부담)',clause):
        return None
    if re.search(r'매운|맵|매워',clause) and NEGATIVE.search(clause) and topic in {'한식','일식','중식','양식','식사'}:
        # "매운 찌개는 피하자" does not exclude every Korean restaurant.
        return None
    if topic == '매운 음식' and re.search(r'안\s*매운|맵지\s*않|안\s*맵', clause):
        return True
    if topic == '식사' and hungry:
        return False
    # Double negatives and relaxed constraints are not clear preferences.
    if UNCERTAIN_NEGATION.search(clause):
        return None
    if re.search(r'알레르기.{0,4}(?:없|아니)',clause):
        return None
    if NEGATIVE.search(clause):
        return True
    if POSITIVE.search(clause):
        return False
    if re.search(r'먹어야지|마셔야지', clause):
        return False
    short_choice = re.sub(r'^(?:나는|난|저는|전|나도|저도)\s*', '', clause.strip())
    if any(re.fullmatch(re.escape(word)+r'(?:요|로|으로)?', short_choice)
           for word in TOPICS[topic]):
        return False
    # Merely recalling a previous visit is not evidence of liking that venue.
    return None

def extract(segments):
    from budget_mentions import update as update_budgets
    grouped = {}
    evidence = []
    latest_choices = {}
    budgets = []
    for segment in segments:
        text = str(segment.get('text', '')).strip()
        seat = segment.get('seat', 'unknown')
        profile = grouped.setdefault(seat, {'seat':seat, 'label':LABELS.get(seat,seat), 'likes':[], 'dislikes':[], 'requirements':[]})
        # Parse complete utterances so punctuation inside 6,000 or 1.5 never
        # splits a money amount, and explicit corrections retain their context.
        update_budgets(budgets, text, seat, segment.get('id', ''))
        if COFFEE_ALTERNATIVE.search(text) and '커피 외 음료 확인' not in profile['requirements']:
            profile['requirements'].append('커피 외 음료 확인')
        # Keep the cue in its clause, so a rejection does not affect a later preference.
        # "칼국수 말고 돈까스도 괜찮아" offers an additional acceptable dish.
        # Preserve the original utterance as evidence, only normalize the cue.
        parse_text=re.sub(r'말고(?=[^.!?\n]{1,30}(?:도|라도)\s*(?:괜찮|좋))','외에',text)
        clauses = split_clauses(parse_text)
        correction_pending = False
        for clause in clauses:
            clause = clause.strip()
            if not clause:
                continue
            if CHANGE_OF_PLAN.search(clause):
                correction_pending = True
            mentioned = [topic for topic in TOPICS if mentions_topic(clause, topic)]
            hungry = (bool(re.search(r'(밥|점심|저녁|식사).{0,5}(안|못)\s*먹었|배고|배가\s*고', clause))
                      and not re.search(r'고프지(?:는)?\s*않|안\s*고파', clause))
            polarities = {topic:topic_polarity(clause, topic, hungry, text) for topic in mentioned}
            for dish,parent in DISH_PARENTS.items():
                if dish in mentioned and polarities.get(parent) is True and not explicitly_names_category(clause,parent):
                    # Disliking pork cutlet does not reject every Japanese meal.
                    polarities[parent]=None
            choices = {topic for topic,negative in polarities.items() if negative is False and topic != '매운 음식'}
            if correction_pending and any(negative is not None for negative in polarities.values()):
                # Replace only this identified speaker's previous positive
                # choice. Keep exclusions/conditions and other seats' opinions.
                # '그럼/이제/아니면' alone may add a stop rather than cancel one.
                if seat != 'unknown' and choices and CHOICE_ACTION.search(clause):
                    previous = latest_choices.get(seat, set())
                    profile['likes'] = [topic for topic in profile['likes'] if topic not in previous]
                    if 'explicit_menu_topics' in profile:
                        profile['explicit_menu_topics']=[topic for topic in profile['explicit_menu_topics'] if topic not in previous]
                correction_pending = False
            for topic, negative in polarities.items():
                if negative is None:
                    continue
                field = 'dislikes' if negative else 'likes'
                other = 'likes' if negative else 'dislikes'
                if topic not in profile[field]:
                    profile[field].append(topic)
                # The same person's latest clear preference replaces an earlier one.
                # Unattributed statements may be different people; preserve both.
                if seat != 'unknown' and topic in profile[other]:
                    profile[other].remove(topic)
                if topic in DISH_PARENTS.values() and explicitly_names_category(clause,topic):
                    explicit=profile.setdefault('explicit_menu_topics',[])
                    if negative and topic in explicit and seat!='unknown':
                        explicit.remove(topic)
                    elif not negative and topic not in explicit:
                        explicit.append(topic)
                evidence.append({'seat':seat, 'topic':topic, 'negative':negative, 'text':text, 'segment_id':segment.get('id','')})
            if choices:
                latest_choices[seat] = choices
            if polarities.get('해변') is False and '바다' in clause and '바닷가 여부 확인' not in profile['requirements']:
                profile['requirements'].append('바닷가 여부 확인')
            parking_optional = bool(PARKING_OPTIONAL.search(clause))
            if parking_optional and seat != 'unknown' and '주차 정보 확인' in profile['requirements']:
                profile['requirements'].remove('주차 정보 확인')
            # A mention establishes a condition to check, not its polarity or
            # a verified facility. Weather alone does not establish indoor intent.
            for pattern, requirement in [(r'주차', '주차 정보 확인'), (r'조용|시끄럽', '소음 정도 확인'),
                                          (r'멀리|가까|근처', '이동 거리 조건 확인'),
                                          (r'실내|야외', '실내·야외 조건 확인')]:
                if requirement == '주차 정보 확인' and parking_optional:
                    continue
                if re.search(pattern, clause) and requirement not in profile['requirements']:
                    profile['requirements'].append(requirement)
            if '카페인' in clause and NEGATIVE.search(clause):
                if '카페인 없는 음료 확인' not in profile['requirements']:
                    profile['requirements'].append('카페인 없는 음료 확인')
            limit=travel_limit(clause)
            if limit is not None:
                if seat != 'unknown':
                    profile['requirements']=[req for req in profile['requirements'] if not re.fullmatch(r'이동 \d+분 이내',req)]
                else:
                    limit=min(limit,profile.get('travel_max_minutes',limit))
                profile['travel_max_minutes']=limit
                profile['travel_evidence']={'text':text,'segment_id':segment.get('id','')}
                req=f'이동 {limit}분 이내'
                if req not in profile['requirements']: profile['requirements'].append(req)
            elif re.search(r'이동|운전|편도|가는\s*데|가는데',clause) and (match:=re.search(r'(\d+)\s*분\s*정도',clause)):
                req=f'이동 {match[1]}분 정도'
                if req not in profile['requirements']: profile['requirements'].append(req)
    for budget in budgets:
        profile = grouped[budget['seat']]
        if budget['requirement'] not in profile['requirements']:
            profile['requirements'].append(budget['requirement'])
        profile.setdefault('budget_evidence', []).append({key: budget[key] for key in
            ('requirement', 'scope', 'category', 'kind', 'text', 'segment_id')})
    return list(grouped.values()), evidence


def summarize_conversation(segments, constraints=None, evidence=None):
    """Summarize only explicit supported preferences, with source utterances."""
    if constraints is None or evidence is None:
        constraints, evidence = extract(segments)
    likes = list(dict.fromkeys(topic for item in constraints for topic in item['likes']))
    dislikes = list(dict.fromkeys(topic for item in constraints for topic in item['dislikes']))
    requirements = list(dict.fromkeys(req for item in constraints for req in item['requirements']))
    sentences = []
    if likes:
        sentences.append('대화에서는 '+', '.join(likes)+'에 대한 선호가 나왔어요.')
    if dislikes:
        sentences.append(', '.join(dislikes)+' 같은 조건은 피하고 싶다는 의견이 있었어요.')
    if requirements:
        sentences.append('또한 '+', '.join(requirements)+' 같은 조건이 언급됐어요.')
    if not sentences:
        sentences.append('대화에서 뚜렷한 장소 종류나 제외 조건을 찾지 못했어요. 검색 지역의 장소를 비교해 보세요.')
    bullets = []
    for item in constraints:
        parts=[]
        if item['likes']: parts.append('선호: '+', '.join(item['likes']))
        if item['dislikes']: parts.append('제외: '+', '.join(item['dislikes']))
        if item['requirements']: parts.append('확인할 점: '+', '.join(item['requirements']))
        if parts:
            label = '좌석이 확인되지 않은 발언' if item['seat']=='unknown' else item['label']
            bullets.append(label+' — '+' / '.join(parts))
    # Choose the latest supporting quote for each current preference, so a
    # corrected preference does not cite a superseded statement.
    source_ids=set()
    for item in constraints:
        for field, negative in [('likes',False),('dislikes',True)]:
            for topic in item[field]:
                source=next((e for e in reversed(evidence) if e['seat']==item['seat'] and e['topic']==topic and e['negative']==negative),None)
                if source: source_ids.add(source['segment_id'])
        if item.get('travel_evidence'):
            source_ids.add(item['travel_evidence']['segment_id'])
        for budget in item.get('budget_evidence', []):
            source_ids.add(budget['segment_id'])
        other_requirements = [req for req in item['requirements'] if '예산 ' not in req]
        for segment in segments:
            if other_requirements and segment.get('seat','unknown')==item['seat'] and re.search(r'주차|조용|시끄럽|근처|가까|멀리|실내|야외|카페인|커피\s*(?:말고|대신)',segment.get('text','')):
                source_ids.add(segment['id'])
    quotes=[{'seat':s.get('seat','unknown'),'text':s['text'],'segment_id':s['id']}
            for s in segments if s['id'] in source_ids][-8:]
    limitations=[]
    if any(s.get('seat','unknown')=='unknown' for s in segments):
        limitations.append('좌석이 확인되지 않은 발언은 누구의 선호인지 단정하지 않았어요.')
    if set(likes)&set(dislikes):
        limitations.append('선호와 제외 의견이 겹치는 항목은 제외 의견을 우선했어요.')
    return {'title':'대화에서 나온 조건' if likes or dislikes or requirements else '장소를 고를 조건이 더 필요해요','text':' '.join(sentences),
            'bullets':bullets,'evidence':quotes,'limitations':limitations,
            'generated_at':datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'segment_count':len(segments)}


def request_profile(constraints):
    requested=sorted({topic for item in constraints for topic in item['likes']})
    excluded=sorted({topic for item in constraints for topic in item['dislikes']})
    requirements=list(dict.fromkeys(req for item in constraints for req in item['requirements']))
    limits=[item['travel_max_minutes'] for item in constraints if item.get('travel_max_minutes')]
    return {'requested_topics':requested,'excluded_topics':excluded,'requirements':requirements,
            'requested_menu_topics':sorted({topic for item in constraints for topic in effective_likes(item)} & MENU_SEARCH_TERMS.keys()),
            'travel_max_minutes':min(limits) if limits else None,
            'travel_evidence':[{'seat':item['seat'],**item['travel_evidence']} for item in constraints if item.get('travel_evidence')],
            'needs_more_preferences':not bool(requested or excluded or requirements),
            'explanation':'제외 의견을 먼저 반영하고, 구체적인 음식·장소와 각 좌석의 의견을 고르게 비교했어요. 이동 시간은 실제 경로를 조회한 경우에만 확인해요.'}

def get_json(url, data=None, timeout=12):
    request = Request(url, data=data, headers={'User-Agent':'CabinConversationPrototype/1.0 (local Pleos demo)', 'Accept':'application/json'})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def nominatim_search(parameters):
    """One application worker, cached callers, at most one request per second.

    Policy: https://operations.osmfoundation.org/policies/nominatim/
    This is a user-triggered limited search, never an exhaustive POI download.
    """
    global _NOMINATIM_LAST_REQUEST
    with _NOMINATIM_LOCK:
        if _NOMINATIM_LAST_REQUEST is not None:
            delay=NOMINATIM_MIN_INTERVAL-(time.monotonic()-_NOMINATIM_LAST_REQUEST)
            if delay > 0:
                time.sleep(delay)
        _NOMINATIM_LAST_REQUEST=time.monotonic()
        try:
            return get_json(NOMINATIM_SEARCH_URL+'?'+urlencode(parameters))
        except Exception as error:
            raise ValueError('외부 지도 서버에 연결하지 못했어요. 인터넷 연결을 확인하고 잠시 후 다시 추천해 주세요.') from error


def get_nominatim_beaches(latitude, longitude, radius_m):
    """A bounded selection of mapped beaches, not every beach in the area."""
    latitude_span=radius_m/111320
    longitude_span=latitude_span/max(math.cos(math.radians(latitude)),0.1)
    bounds=(longitude-longitude_span,latitude-latitude_span,longitude+longitude_span,latitude+latitude_span)
    places=nominatim_search({'q':'[beach]','format':'jsonv2','countrycodes':'kr',
        'viewbox':','.join(str(value) for value in bounds),'bounded':1,'limit':20,
        'extratags':1,'namedetails':1,'addressdetails':1,'accept-language':'ko'})
    if not isinstance(places,list):
        raise ValueError('해변 검색 결과를 확인하지 못했어요.')
    elements=[]
    for place in places:
        if not isinstance(place,dict) or place.get('category',place.get('class')) != 'natural' or place.get('type') != 'beach':
            continue
        coordinates=valid_coordinates(place.get('lat'),place.get('lon'))
        osm_type,osm_id=place.get('osm_type'),place.get('osm_id')
        if coordinates is None or osm_type not in ('node','way','relation') or isinstance(osm_id,bool) or not isinstance(osm_id,int) or osm_id <= 0:
            continue
        # A bounded rectangle also contains corners beyond the promised radius.
        if straight_line_meters((latitude,longitude),coordinates) > radius_m:
            continue
        names=place.get('namedetails') or {}
        if not isinstance(names,dict):
            names={}
        name=names.get('name:ko') or names.get('name') or place.get('name')
        if not isinstance(name,str) or not name.strip():
            continue
        tags={'name':name.strip(),'natural':'beach'}
        extra=place.get('extratags') or {}
        if not isinstance(extra,dict):
            extra={}
        if isinstance(extra.get('opening_hours'),str):
            tags['opening_hours']=extra['opening_hours']
        address=place.get('address') or {}
        if not isinstance(address,dict):
            address={}
        for target,sources in [('addr:city',('city','town','municipality','county')),
                               ('addr:suburb',('suburb','city_district')),
                               ('addr:street',('road',)),('addr:housenumber',('house_number',))]:
            value=next((address[key] for key in sources if isinstance(address.get(key),str)),None)
            if value:
                tags[target]=value
        elements.append({'type':osm_type,'id':osm_id,'lat':coordinates[0],'lon':coordinates[1],'tags':tags})
    return elements


def mapped_meal_topics(tags):
    """Menu evidence in actual restaurant tags, never from the search query."""
    if tags.get('amenity') not in ('restaurant','fast_food','food_court'):
        return set()
    topics = registered_menu_topics(tags)
    name = tags.get('name:ko') or tags.get('name') or ''
    topics.update(topic for topic in MENU_SEARCH_TERMS if mentions_topic(name, topic))
    topics.update(DISH_PARENTS[topic] for topic in tuple(topics) if topic in DISH_PARENTS)
    if '회' in topics:
        topics.add('해산물')
    return topics


def get_nominatim_food_places(latitude, longitude, radius_m, food_topics, *, report=None):
    """At most two user-triggered menu searches; not an exhaustive POI crawl."""
    wanted = set(food_topics) & MENU_FALLBACK_TERMS.keys()
    term_lists = [MENU_FALLBACK_TERMS[topic] for topic in sorted(wanted)]
    queries = list(dict.fromkeys(terms[index] for index in range(2)
                                 for terms in term_lists if len(terms)>index))[:2]
    latitude_span=radius_m/111320
    longitude_span=latitude_span/max(math.cos(math.radians(latitude)),0.1)
    bounds=(longitude-longitude_span,latitude-latitude_span,longitude+longitude_span,latitude+latitude_span)
    elements, seen, completed = [], set(), 0
    for query in queries:
        try:
            places=nominatim_search({'q':query,'format':'jsonv2','countrycodes':'kr',
                'viewbox':','.join(str(value) for value in bounds),'bounded':1,'limit':20,
                'extratags':1,'namedetails':1,'addressdetails':1,'accept-language':'ko'})
        except Exception:
            continue
        if not isinstance(places,list):
            continue
        completed += 1
        for place in places[:20]:
            if not isinstance(place,dict) or place.get('category',place.get('class'))!='amenity' or place.get('type') not in ('restaurant','fast_food','food_court'):
                continue
            coordinates=valid_coordinates(place.get('lat'),place.get('lon'))
            osm_type,osm_id=place.get('osm_type'),place.get('osm_id')
            if coordinates is None or osm_type not in ('node','way','relation') or isinstance(osm_id,bool) or not isinstance(osm_id,int) or osm_id<=0:
                continue
            if (osm_type,osm_id) in seen or straight_line_meters((latitude,longitude),coordinates)>radius_m:
                continue
            names=place.get('namedetails') if isinstance(place.get('namedetails'),dict) else {}
            name=names.get('name:ko') or names.get('name') or place.get('name')
            if not isinstance(name,str) or not name.strip():
                continue
            tags={'name':name.strip(),'amenity':place['type']}
            extra=place.get('extratags') if isinstance(place.get('extratags'),dict) else {}
            for key in ('cuisine','opening_hours','website'):
                if isinstance(extra.get(key),str):
                    tags[key]=extra[key]
            # A search hit alone does not establish menu availability. Require
            # registered cuisine or an explicit name clue, labelled later.
            if not (mapped_meal_topics(tags) & wanted):
                continue
            address=place.get('address') if isinstance(place.get('address'),dict) else {}
            for target,sources in [('addr:city',('city','town','municipality','county')),
                                   ('addr:suburb',('suburb','city_district')),
                                   ('addr:street',('road',)),('addr:housenumber',('house_number',))]:
                value=next((address[key] for key in sources if isinstance(address.get(key),str)),None)
                if value:
                    tags[target]=value
            elements.append({'type':osm_type,'id':osm_id,'lat':coordinates[0],'lon':coordinates[1],'tags':tags})
            seen.add((osm_type,osm_id))
    if report is not None:
        report['complete'] = completed == len(queries)
    if queries and not completed:
        raise ValueError('음식 종류별 장소 검색을 완료하지 못했어요. 인터넷 연결이나 외부 지도 서버에 문제가 있을 수 있어요. 잠시 후 다시 추천해 주세요.')
    return elements


def get_osm_area_places(latitude, longitude):
    """Read a small public map extract when Overpass is unavailable."""
    latitude_span = 800 / 111320
    longitude_span = latitude_span / max(math.cos(math.radians(latitude)), 0.1)
    bounds = (longitude - longitude_span, latitude - latitude_span,
              longitude + longitude_span, latitude + latitude_span)
    query = urlencode({'bbox': ','.join(f'{value:.6f}' for value in bounds)})
    request = Request('https://www.openstreetmap.org/api/0.6/map?' + query,
                      headers={'User-Agent': 'CabinConversationPrototype/1.0 (local Pleos demo)',
                               'Accept': 'application/xml'})
    maximum_size = 12 * 1024 * 1024
    with urlopen(request, timeout=12) as response:
        raw = response.read(maximum_size + 1)
    if len(raw) > maximum_size:
        raise ValueError('주변 지도 데이터가 너무 많아요. 더 구체적인 역이나 장소 이름을 입력해 주세요.')
    document = ElementTree.fromstring(raw)
    nodes = {item.attrib['id']: (float(item.attrib['lat']), float(item.attrib['lon']))
             for item in document.findall('node')}
    ways = {item.attrib['id']: [point.attrib['ref'] for point in item.findall('nd')]
            for item in document.findall('way')}
    elements = []
    for item in document:
        if item.tag not in ('node', 'way', 'relation'):
            continue
        tags = {tag.attrib['k']: tag.attrib['v'] for tag in item.findall('tag')}
        if not (tags.get('name') or tags.get('name:ko')):
            continue
        relevant = (tags.get('amenity') in AMENITY_TOPIC or tags.get('leisure') == 'park'
                    or tags.get('tourism') in ('museum', 'gallery') or tags.get('shop') == 'bakery'
                    or tags.get('natural') == 'beach')
        if not relevant:
            continue
        if item.tag == 'node':
            points = [nodes[item.attrib['id']]]
        elif item.tag == 'way':
            points = [nodes[ref] for ref in ways[item.attrib['id']] if ref in nodes]
        else:
            references = []
            for member in item.findall('member'):
                if member.attrib.get('type') == 'node':
                    references.append(member.attrib['ref'])
                elif member.attrib.get('type') == 'way':
                    references.extend(ways.get(member.attrib['ref'], []))
            points = [nodes[ref] for ref in set(references) if ref in nodes]
        if not points:
            continue
        place_latitude = sum(point[0] for point in points) / len(points)
        place_longitude = sum(point[1] for point in points) / len(points)
        # Connected OSM ways may extend outside the requested map rectangle.
        if not (bounds[0] <= place_longitude <= bounds[2] and bounds[1] <= place_latitude <= bounds[3]):
            continue
        elements.append({'type': item.tag, 'id': int(item.attrib['id']), 'tags': tags,
                         'lat': place_latitude, 'lon': place_longitude})
    elements.sort(key=lambda place: ((place['lat'] - latitude) / latitude_span) ** 2
                  + ((place['lon'] - longitude) / longitude_span) ** 2)
    return elements[:180]


def location_poi_cache(key, payload=None):
    """Bounded process-only POIs; location-derived keys must never reach disk."""
    now = time.monotonic()
    with _LOCATION_POI_CACHE_LOCK:
        expired = [item for item,(expires,_) in _LOCATION_POI_CACHE.items() if expires <= now]
        for item in expired:
            del _LOCATION_POI_CACHE[item]
        if payload is not None:
            # The exact center belongs only to the request currently being served.
            stored = copy.deepcopy({name:value for name,value in payload.items() if name != 'center'})
            _LOCATION_POI_CACHE[key] = (now + _LOCATION_POI_CACHE_TTL, stored)
            _LOCATION_POI_CACHE.move_to_end(key)
            while len(_LOCATION_POI_CACHE) > _LOCATION_POI_CACHE_LIMIT:
                _LOCATION_POI_CACHE.popitem(last=False)
            return None
        entry = _LOCATION_POI_CACHE.get(key)
        if entry is None:
            return None
        _LOCATION_POI_CACHE.move_to_end(key)
        return copy.deepcopy(entry[1])


def lookup_places(region, cache_dir, *, location=None, radius_m=4500, include_beaches=False, beach_only=False, food_topics=()):
    if isinstance(radius_m,bool) or not isinstance(radius_m,(int,float)) or not math.isfinite(radius_m) or not 100 <= radius_m <= 20000:
        raise ValueError('장소 검색 범위는 100m에서 20km 사이로 지정해 주세요.')
    radius_m=int(radius_m)
    food_topics=tuple(sorted(set(food_topics) & MENU_SEARCH_TERMS.keys()))
    include_beaches=include_beaches or beach_only
    current_coordinates = location_coordinates(location) if location is not None else None
    if current_coordinates is not None:
        region = '현재 위치 주변'
        latitude, longitude = current_coordinates
        # Nearby fixes may reuse public POIs in memory. A deterministic location
        # hash on disk could be reversed by enumerating a small geographic grid.
        cache_key = f'location:{latitude:.4f},{longitude:.4f}'
        center_label = '현재 위치 중심'
    else:
        region = region.strip()
        if not region or len(region) > 70:
            raise ValueError('검색할 지역을 입력해 주세요. 예를 들면 시·구 이름이나 역 이름이에요.')
        cache_key = region
        center_label = '검색 지역 중심'
    if radius_m != 4500 or include_beaches:
        cache_key += f':radius={radius_m}:beach={int(include_beaches)}:only={int(beach_only)}'
    if food_topics:
        # Old 800m fallback entries never attempted bounded menu searches.
        cache_key += ':menu-search-v2:foods='+','.join(food_topics)
    if current_coordinates is not None:
        cached = location_poi_cache(cache_key)
        if cached is not None and (not food_topics or cached.get('menu_search_complete') is True):
            # Distance always uses this fix, not the earlier cache fix.
            cached['center'] = {'lat':latitude, 'lon':longitude}
            cached['region'] = region
            return cached
    else:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache = cache_dir / (hashlib.sha256(cache_key.encode()).hexdigest() + '.json')
        if cache.exists() and time.time()-cache.stat().st_mtime < 86400:
            try:
                cached = json.loads(cache.read_text(encoding='utf-8'))
                if isinstance(cached, dict) and isinstance(cached.get('elements'), list) and (not food_topics or cached.get('menu_search_complete') is True):
                    return cached
            except (OSError, ValueError):
                pass
    if current_coordinates is None:
        locations = nominatim_search({'q':region, 'countrycodes':'kr', 'format':'jsonv2', 'limit':1, 'accept-language':'ko'})
        if not locations:
            raise ValueError('지역을 찾지 못했어요. 시·구 이름이나 역 이름을 더해 주세요.')
        latitude, longitude = float(locations[0]['lat']), float(locations[0]['lon'])
    general_radius=min(radius_m,4500) if include_beaches else radius_m
    # Reuse the same public OSM categories and distance normalization as
    # travel discovery. Keep the snapshot's provenance and limitations visible.
    from poi_index import query_candidates
    groups = ('beach',) if beach_only else ('meal', 'cafe', 'bakery', 'park', 'culture')
    local = query_candidates({'lat':latitude, 'lon':longitude},
                             radius_m if beach_only else general_radius, groups)
    if include_beaches and not beach_only:
        beaches = query_candidates({'lat':latitude, 'lon':longitude}, radius_m, ('beach',))
        if local is not None and beaches is not None:
            local['elements'].extend(beaches['elements'])
            local['search_note'] += beaches['search_note']
    if local is not None and local['elements']:
        payload = local
        payload['region'] = region
        payload['coverage'] = center_label+f' 반경 약 {radius_m/1000:g}km'
        if include_beaches and not beach_only and general_radius != radius_m:
            payload['coverage'] = center_label+f' 해변 반경 약 {radius_m/1000:g}km · 그 외 장소 반경 약 {general_radius/1000:g}km'
        payload['discovery']['candidate_count'] = len(payload['elements'])
        payload['discovery']['requested_groups'] = list(groups) + (['beach'] if include_beaches and not beach_only else [])
    else:
        query='[out:json][timeout:6];'
        if not beach_only:
            query += f'''(
              nwr(around:{general_radius},{latitude},{longitude})["amenity"~"^(cafe|restaurant|fast_food|food_court|ice_cream)$"]["name"];
              nwr(around:{general_radius},{latitude},{longitude})["leisure"="park"]["name"];
              nwr(around:{general_radius},{latitude},{longitude})["tourism"~"^(museum|gallery)$"]["name"];
              nwr(around:{general_radius},{latitude},{longitude})["shop"="bakery"]["name"];
            );out center tags {140 if include_beaches else 180};'''
            if food_topics:
                # Independent output sets reserve space for each requested dish.
                # Bounded, user-triggered queries share the ordinary location cache.
                for topic in food_topics:
                    name_pattern,cuisine=MENU_SEARCH_TERMS[topic]
                    query += f'''(
                      nwr(around:{general_radius},{latitude},{longitude})["amenity"~"^(restaurant|fast_food|food_court)$"]["name"~"{name_pattern}"];
                      nwr(around:{general_radius},{latitude},{longitude})["amenity"~"^(restaurant|fast_food|food_court)$"]["cuisine"~"(^|;){cuisine}(;|$)"]["name"];
                    );out center tags 35;'''
        if include_beaches:
            # Reserve beach results rather than letting cafes consume the limit.
            query += f'nwr(around:{radius_m},{latitude},{longitude})["natural"="beach"]["name"];out center tags {180 if beach_only else 40};'
        search_note=''
        try:
            result = get_json('https://overpass-api.de/api/interpreter?' + urlencode({'data': query}), timeout=8)
            if result.get('remark'):
                raise ValueError('Overpass did not finish the query.')
            elements = result.get('elements', [])
            coverage = center_label+f' 반경 약 {radius_m/1000:g}km'
            if include_beaches and not beach_only and general_radius != radius_m:
                coverage = center_label+f' 해변 반경 약 {radius_m/1000:g}km · 그 외 장소 반경 약 {general_radius/1000:g}km'
            lookup_source = 'OpenStreetMap · Overpass API'
        except Exception:
            if include_beaches:
                try:
                    elements=get_nominatim_beaches(latitude,longitude,radius_m)
                except Exception:
                    raise ValueError('해변 장소 검색을 완료하지 못했어요. 인터넷 연결이나 외부 지도 서버에 문제가 있을 수 있어요. 잠시 후 다시 추천해 주세요.') from None
                coverage=center_label+f' 해변 반경 약 {radius_m/1000:g}km 내 일부 검색 결과'
                lookup_source='OpenStreetMap · Nominatim Search'
                search_note='해변 검색은 일부 일치 장소를 보여 주며, 이 지역의 모든 해변을 포함하지는 않아요.'
                if not elements:
                    search_note+=' 이 범위에서 해변 후보를 찾지 못했어요.'
                if not beach_only:
                    try:
                        nearby=get_osm_area_places(latitude,longitude)
                    except Exception:
                        search_note+=' 다른 종류의 장소 검색은 완료하지 못했어요.'
                    else:
                        elements.extend(place for place in nearby if place.get('tags',{}).get('natural') != 'beach')
                        coverage+=' · 그 외 장소는 중심에서 동서·남북 약 800m 범위'
                        lookup_source+=' + Map API'
            else:
                if radius_m > 4500:
                    raise ValueError('넓은 범위의 장소 검색을 완료하지 못했어요. 인터넷 연결이나 외부 지도 서버에 문제가 있을 수 있어요. 잠시 후 다시 추천해 주세요.') from None
                try:
                    elements = get_osm_area_places(latitude, longitude)
                except Exception as error:
                    if not food_topics:
                        raise ValueError('외부 장소 데이터 서버에 연결하지 못했어요. 인터넷 연결을 확인해 주세요. 대화록은 유지돼요. 잠시 후 추천을 다시 눌러 주세요.') from error
                    elements=[]
                    coverage=center_label+'의 음식 종류별 검색'
                    lookup_source='OpenStreetMap'
                    search_note='일반 주변 장소 검색은 완료하지 못했어요.'
                else:
                    coverage = center_label+'에서 동서·남북 약 800m 범위'
                    lookup_source = 'OpenStreetMap · Map API'
        available_foods=set().union(*(mapped_meal_topics(place.get('tags',{})) for place in elements))
        missing_foods=set(food_topics)-available_foods
        menu_search_complete = True
        if missing_foods and not beach_only:
            menu_report = {}
            try:
                food_places=get_nominatim_food_places(latitude,longitude,general_radius,missing_foods,report=menu_report)
            except Exception:
                menu_search_complete = False
                search_note += ' 음식 종류별 보완 검색을 완료하지 못했어요.'
            else:
                menu_search_complete = menu_report.get('complete',True)
                elements.extend(food_places)
                coverage += f' · 음식 조건 보완 검색은 반경 약 {general_radius/1000:g}km 내 일부 결과'
                lookup_source += ' + Nominatim Search'
                search_note += ' 음식 조건 검색은 공개 정보에 등록된 일부 후보이며, 해당 지역의 모든 식당을 포함하지는 않아요.'
                if not menu_search_complete:
                    search_note += ' 일부 음식 조건 검색은 완료하지 못했어요. 다시 추천하면 재시도해요.'
        payload = {'region':region,'center':{'lat':latitude,'lon':longitude}, 'fetched_at':time.time(),
                   'elements':elements, 'coverage':coverage, 'lookup_source':lookup_source,'search_note':search_note,
                   'menu_search_complete':menu_search_complete}
    if current_coordinates is not None:
        location_poi_cache(cache_key, payload)
        return payload
    # Named-region centers and their POIs are public data, safe to cache on disk.
    with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=cache_dir, suffix='.tmp', delete=False) as output:
        temporary = Path(output.name)
        json.dump(payload, output, ensure_ascii=False)
    try:
        temporary.replace(cache)
    finally:
        temporary.unlink(missing_ok=True)
    return payload

def valid_coordinates(latitude, longitude):
    # bool is numerically convertible but is never a geographic coordinate.
    if isinstance(latitude, bool) or isinstance(longitude, bool):
        return None
    try:
        latitude, longitude = float(latitude), float(longitude)
    except (TypeError, ValueError, OverflowError):
        return None
    if (not math.isfinite(latitude) or not math.isfinite(longitude)
            or not -90 <= latitude <= 90 or not -180 <= longitude <= 180):
        return None
    return latitude, longitude


def location_coordinates(location):
    coordinates = (valid_coordinates(location.get('latitude'), location.get('longitude'))
                   if isinstance(location, dict) else None)
    if coordinates is None:
        raise ValueError('현재 위치 좌표를 확인하지 못했어요. 위치를 다시 확인해 주세요.')
    return coordinates


def straight_line_meters(first, second):
    lat1, lon1, lat2, lon2 = map(math.radians, (*first, *second))
    haversine = (math.sin((lat2-lat1)/2)**2
                 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2)
    return 6371000 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, haversine))))


def korean_particle(text, consonant, vowel):
    last = ord(text[-1])
    return consonant if 0xAC00 <= last <= 0xD7A3 and (last-0xAC00) % 28 else vowel


def topic_phrase(topics):
    words = list(topics)
    if len(words) < 2:
        return ''.join(words)
    return ', '.join(words[:-1])+korean_particle(words[-2], '과', '와')+' '+words[-1]


def first_stop_preferences(segments):
    """Read explicit first/then language without inventing a full itinerary."""
    patterns={'음식점':r'식사|식당|밥|점심|저녁|칼국수|파전|횟집|파스타|피자|고기',
              '카페·디저트':r'카페(?!인)|커피|디저트|빵|케이크',
              '전시·문화':r'전시|미술관|박물관', '여행·산책':r'산책|공원|해변|바다'}
    by_seat={}
    for segment in segments:
        seat=segment.get('seat','unknown')
        for clause in split_clauses(str(segment.get('text',''))):
            if NEGATIVE.search(clause) or re.search(r'곤란|아쉬|취소',clause):
                continue
            spans=sorted((m.start(),m.end(),group) for group,pattern in patterns.items() for m in re.finditer(pattern,clause))
            for marker in re.finditer(r'먼저|우선|부터|다음|보고(?:\s*나서)?|먹고(?:\s*나서)?|걷고(?:\s*나서)?',clause):
                previous=[span for span in spans if span[1]<=marker.start() and marker.start()-span[1]<=12]
                if not previous:
                    continue
                if marker.group().startswith(('보고','먹고','걷고','다음')) and len({span[2] for span in spans})<2:
                    continue
                group=previous[-1][2]
                proof={'seat':seat,'group':group,'quote':segment.get('text',''),'segment_id':segment.get('id','')}
                if seat=='unknown': by_seat[(seat,group)]=proof
                else: by_seat[seat]=proof
    return list(by_seat.values())


def business_key(tags,name):
    """Prefer mapped brand identity, then normalize ordinary branch labels."""
    brand=tags.get('brand:ko') or tags.get('brand') or ''
    compact=re.sub(r'\s+','',(brand or name).casefold())
    if compact.startswith(('스타벅스','starbucks')):
        return 'brand:starbucks'
    if brand:
        return 'brand:'+compact
    if tags.get('brand:wikidata'):
        return 'brand:'+tags['brand:wikidata']
    normalized=re.sub(r'\s+\S+점$|\s*\([^()]*점\)$','',name.casefold()).strip()
    return 'name:'+re.sub(r'\s+','',normalized or name.casefold())


def geographic_group(place):
    """A spatial bucket for diversity, never an invented administrative name."""
    point = valid_coordinates(place.get('latitude'), place.get('longitude'))
    if point is None:
        return place.get('id', '')
    return (math.floor(point[0] * 111.32 / 20),
            math.floor(point[1] * 88 / 20))


def choose_places(places, limit=12, *, first_groups=(), geographic_diversity=False):
    """Select from eligible places; route eligibility must be checked beforehand.

    For candidate screening (limit > 12), do not discard whole business/groups
    before their routes are known. Final output retains category/business caps.
    """
    def road_minutes(place):
        minutes = (place.get('travel_constraint') or {}).get('duration_minutes')
        return minutes if minutes is not None else math.inf
    remaining = list(places)
    remaining.sort(key=lambda p: (-p.get('score', 0), -p.get('quality_score', 0),
        road_minutes(p),
        p.get('distance_m') if p.get('distance_m') is not None else math.inf,
        p.get('name', ''), p.get('id', '')))
    chosen = []
    covered_requests, covered_seats, covered_activities, covered_cuisines = set(), set(), set(), set()
    business_counts, group_counts, area_counts, activity_counts = {}, {}, {}, {}
    group_limit = max(6, math.ceil(limit / 2)) if len({p.get('group') for p in remaining}) > 1 else limit

    def facts(place):
        requests = {(e['seat'], e['topic']) for e in place.get('evidence', [])
                    if 'seat' in e and 'topic' in e}
        seats = set(place.get('matched_seats', []))
        activity = place.get('selection_activity') or place.get('group')
        cuisine = set(place.get('matched_topics', [])) & MENU_TOPICS
        business = place.get('business_group') or place.get('name') or place.get('id')
        return requests, seats, activity, cuisine, business, geographic_group(place)

    while remaining and len(chosen) < limit:
        eligible = [p for p in remaining if limit > 12 or
                    (business_counts.get(facts(p)[4], 0) < 2 and group_counts.get(p.get('group'), 0) < group_limit)]
        if not eligible:
            break
        if not chosen:
            candidate = next((p for p in eligible if p.get('group') in first_groups), eligible[0])
        else:
            def diversity_score(place):
                requests, seats, activity, cuisine, business, area = facts(place)
                return (len(seats - covered_seats), len(requests - covered_requests),
                        activity not in covered_activities, len(cuisine - covered_cuisines),
                        -activity_counts.get(activity, 0),
                        -area_counts.get(area, 0) if geographic_diversity else 0,
                        -business_counts.get(business, 0), -group_counts.get(place.get('group'), 0),
                        place.get('quality_score', 0), place.get('score', 0),
                        -road_minutes(place),
                        -place['distance_m'] if place.get('distance_m') is not None else -math.inf)
            candidate = max(eligible, key=diversity_score)
        chosen.append(candidate)
        remaining.remove(candidate)
        requests, seats, activity, cuisine, business, area = facts(candidate)
        covered_requests.update(requests)
        covered_seats.update(seats)
        covered_activities.add(activity)
        activity_counts[activity] = activity_counts.get(activity, 0) + 1
        covered_cuisines.update(cuisine)
        business_counts[business] = business_counts.get(business, 0) + 1
        group_counts[candidate.get('group')] = group_counts.get(candidate.get('group'), 0) + 1
        area_counts[area] = area_counts.get(area, 0) + 1
    return chosen


def sea_proximity(place, coordinates):
    """A beach tag also describes river sandbanks; require coastline geometry."""
    from poi_index import coastal_proximity, COASTAL_MAX_DISTANCE_M
    distance = place.get('coastal_distance_m')
    if (place.get('coastal_basis') == 'OpenStreetMap natural=coastline'
            and type(distance) in (int, float) and math.isfinite(distance)
            and 0 <= distance <= COASTAL_MAX_DISTANCE_M):
        return {'distance_m': distance, 'basis': place['coastal_basis'],
                'max_distance_m': COASTAL_MAX_DISTANCE_M, 'method': 'center_to_osm_coastline_segments'}
    return coastal_proximity(*coordinates)


def recommend(segments, region, cache_dir, *, location=None, radius_m=None,
              candidate_data=None, result_limit=12):
    constraints, evidence = extract(segments)
    profile=request_profile(constraints)
    profile['first_stop_evidence']=first_stop_preferences(segments)
    first_groups={proof['group'] for proof in profile['first_stop_evidence']}
    if not segments:
        raise ValueError('대화를 녹음하거나 대화록을 입력한 뒤 추천해 주세요.')
    current_coordinates = location_coordinates(location) if location is not None else None
    all_likes={topic for item in constraints for topic in effective_likes(item)}
    all_dislikes={topic for item in constraints for topic in item['dislikes']}
    beach_requested='해변' in all_likes and '해변' not in all_dislikes
    lookup_options={}
    if beach_requested:
        lookup_options.update(include_beaches=True,beach_only=not bool(all_likes-{'해변'}))
        lookup_options['radius_m']=radius_m if radius_m is not None else 20000 if current_coordinates is None else 4500
    elif radius_m is not None:
        lookup_options['radius_m']=radius_m
    requested_foods=(all_likes-all_dislikes) & MENU_SEARCH_TERMS.keys()
    if requested_foods:
        lookup_options['food_topics']=tuple(sorted(requested_foods))
    if candidate_data is not None:
        data = candidate_data
    elif current_coordinates is not None:
        region = '현재 위치 주변'
        data = lookup_places(region, cache_dir, location=location, **lookup_options)
    else:
        data = lookup_places(region, cache_dir, **lookup_options)
    center = data.get('center') or {}
    search_center = current_coordinates or (valid_coordinates(center.get('lat'), center.get('lon')) if isinstance(center, dict) else None)
    distance_basis = 'current_location' if current_coordinates is not None else 'search_center' if search_center else None
    nearby_label = region if region.endswith('주변') else region+' 주변'
    results = []
    represented = {}
    activities = {}
    cuisines = {}
    businesses = {}
    seen = set()
    unconfirmed_sea_count = 0
    for place in data['elements']:
        tags = place.get('tags', {})
        name = tags.get('name:ko') or tags.get('name')
        identifier = f"{place['type']}/{place['id']}"
        if not name or identifier in seen:
            continue
        place_center = place.get('center') or {}
        if not isinstance(place_center, dict):
            place_center = {}
        coordinates = valid_coordinates(place.get('lat', place_center.get('lat')),
                                        place.get('lon', place_center.get('lon')))
        if coordinates is None:
            continue
        coastal_proof = None
        if tags.get('natural') == 'beach':
            coastal_proof = sea_proximity(place, coordinates)
            if coastal_proof is None:
                unconfirmed_sea_count += 1
                continue
        seen.add(identifier)
        lat, lon = coordinates
        distance = round(straight_line_meters(search_center, coordinates)) if search_center else None
        topics = set()
        kind = AMENITY_TOPIC.get(tags.get('amenity'))
        if kind: topics.add(kind)
        if tags.get('leisure') == 'park': topics.add('산책')
        if tags.get('tourism') in ('museum','gallery'): topics.add('전시·문화')
        if tags.get('shop') == 'bakery': topics.add('빵·디저트')
        if tags.get('natural') == 'beach': topics.add('해변')
        topics.update(registered_menu_topics(tags))
        if '회' in topics: topics.add('해산물')
        classified_topics = topics.copy()
        # Food words in a gallery or cafe name do not establish meal service.
        # Even for restaurants, distinguish a name clue from a registered type.
        for topic in MENU_TOPICS:
            if kind != '식사' and not (topic == '빵·디저트' and (kind in ('카페', '빵·디저트') or tags.get('shop') == 'bakery')):
                continue
            if mentions_topic(name,topic): topics.add(topic)
        topics.update(DISH_PARENTS[topic] for topic in tuple(topics) if topic in DISH_PARENTS)
        if '회' in topics: topics.add('해산물')
        name_topics = topics - classified_topics
        dislikes = set(item for c in constraints for item in c['dislikes'])
        if topics & dislikes:
            continue
        reasons, cautions, score = [], [], 0
        score_breakdown, source_evidence = [], []
        matched_requests = set()
        for c in constraints:
            matched = topics & effective_likes(c)
            if matched:
                # Reward explicit dishes more than a broad restaurant category;
                # a registered cuisine is stronger than a clue in a shop name.
                contribution=8
                for topic in sorted(matched):
                    contribution += (12 if topic in classified_topics else 8) if topic in SPECIFIC_MENU_TOPICS else (4 if topic in classified_topics else 2)
                    source=next((e for e in reversed(evidence) if e['seat']==c['seat'] and e['topic']==topic and not e['negative']),None)
                    if source:
                        source_evidence.append({'seat':c['seat'],'topic':topic,'quote':source['text'],
                                                'segment_id':source['segment_id'],
                                                'basis':'name_hint' if topic in name_topics else 'registered'})
                score += contribution
                matched_requests.update((c['seat'], topic) for topic in matched)
                source = next((e['text'] for e in reversed(evidence) if e['seat']==c['seat'] and e['topic'] in matched and not e['negative']), '')
                reason_text = f"{c['label']}의 {topic_phrase(sorted(matched))} 선호를 반영했어요."
                if matched & SPECIFIC_MENU_TOPICS & classified_topics:
                    reason_text += ' 공개 장소 정보에 '+topic_phrase(sorted(matched & SPECIFIC_MENU_TOPICS & classified_topics))+' 종류가 등록돼 있어요.'
                if matched & name_topics:
                    reason_text += ' '+topic_phrase(sorted(matched & name_topics))+' 메뉴는 이름으로만 예상했어요.'
                reasons.append({'seat':c['seat'], 'text':reason_text, 'quote':source})
                score_breakdown.append({'label':c['label']+'의 '+topic_phrase(sorted(matched)),
                                        'value':contribution,'detail':reason_text})
            unmatched=effective_likes(c)-matched
            if unmatched:
                cautions.append(c['label']+'의 '+topic_phrase(sorted(unmatched))+' 조건은 이 장소에서 확인되지 않았어요.')
            for req in c['requirements']:
                if req not in cautions: cautions.append(req)
        menu_exclusions = dislikes & MENU_TOPICS
        if menu_exclusions and topics & {'식사','카페','빵·디저트'}:
            cautions.append('피하고 싶은 '+topic_phrase(sorted(menu_exclusions))+' 관련 메뉴 확인이 필요해요.')
        if name_topics:
            cautions.append('이름상 '+topic_phrase(sorted(name_topics))+' 메뉴가 예상되지만, 실제 메뉴 확인이 필요해요.')
        if score == 0 and any(c['likes'] for c in constraints):
            continue
        if tags.get('opening_hours'):
            cautions.append('안내된 영업시간 '+tags['opening_hours']+' · 지금 영업 중인지는 확인해 주세요.')
        else:
            cautions.append('이용 시간·요금·주차 여유는 확인해 주세요.' if '해변' in topics or '산책' in topics else '영업 여부·가격·주차 여유는 확인해 주세요.')
        address = ' '.join(tags.get(k,'') for k in ('addr:city','addr:suburb','addr:street','addr:housenumber')).strip()
        matched_topics=sorted(topics & all_likes)
        if matched_topics:
            wanted = topic_phrase(matched_topics)
            reason_summary = wanted+korean_particle(wanted, '을', '를')+' 원하는 의견을 반영했어요.'
            if set(matched_topics) & name_topics:
                reason_summary += ' 이름상 '+topic_phrase(sorted(set(matched_topics) & name_topics))+' 메뉴가 예상되지만, 실제 메뉴는 확인해 주세요.'
        elif dislikes:
            reason_summary = '주변에서 찾은 비교 후보예요. 피하고 싶은 조건에 해당하는지는 확인해 주세요.'
        else:
            reason_summary = '대화에서 원하는 장소를 찾지 못해 주변 장소를 보여드려요.'
        represented[identifier] = matched_requests
        activities[identifier] = kind or ('해변' if '해변' in topics else '산책' if '산책' in topics else '전시·문화' if '전시·문화' in topics else '빵·디저트')
        cuisines[identifier]=topics & MENU_TOPICS
        businesses[identifier]=business_key(tags,name)
        group=('음식점' if kind=='식사' else '카페·디저트' if topics & {'카페','빵·디저트'} else '전시·문화' if '전시·문화' in topics else '여행·산책')
        # A mapped fast-food doughnut shop is a dessert option in a coffee/
        # dessert conversation, not a second requested activity. Otherwise
        # one such shop would halve the cafe allowance for the entire list.
        if kind == '식사' and '식사' not in all_likes and '빵·디저트' in matched_topics:
            group = '카페·디저트'
            activities[identifier] = '빵·디저트'
        matched_name=set(matched_topics) & name_topics
        matched_registered=set(matched_topics) & classified_topics
        travel_constraint=({'max_minutes':profile['travel_max_minutes'],'verified':False,'duration_minutes':None,
                            'message':f"이동 {profile['travel_max_minutes']}분 이내 조건은 경로 소요 시간을 확인해야 해요."}
                           if profile['travel_max_minutes'] else None)
        if travel_constraint:
            cautions.append(travel_constraint['message'])
        results.append({'id':identifier, 'name':name, 'category':' · '.join(topic+'(메뉴 확인 필요)' if topic in name_topics else topic for topic in sorted(topics)),
                        'description':('함께 나온 의견을 바탕으로 살펴본 장소예요.' if matched_topics or dislikes else '주변에서 찾은 장소예요.'), 'reasons':reasons,
                        'reason_summary':reason_summary,
                        'cautions':list(dict.fromkeys(cautions)), 'address':address or nearby_label+' · 상세 주소 확인 필요',
                        'url':f"https://www.openstreetmap.org/{place['type']}/{place['id']}", 'verified':False,
                        'score':score, 'latitude':lat, 'longitude':lon,
                        'group':group,'matched_topics':matched_topics,
                        'selection_activity':activities[identifier], 'business_group':businesses[identifier],
                        'matched_seats':list(dict.fromkeys(c['seat'] for c in constraints if any(seat==c['seat'] for seat,_ in matched_requests))),
                        'score_breakdown':score_breakdown,'evidence':source_evidence,
                        'menu_matches':[{'topic':topic,'basis':'name_hint' if topic in name_topics else 'registered'} for topic in matched_topics if topic in MENU_TOPICS],
                        'evidence_strength':'mixed' if matched_name and matched_registered else 'name_hint' if matched_name else 'registered',
                        'travel_constraint':travel_constraint,
                        'distance_m':distance, 'distance_basis':distance_basis,
                        'coastal_proximity':coastal_proof,
                        'source':'OpenStreetMap', 'data_checked_at':data['fetched_at']})
    # Distance only breaks ties after preference quality and never represents
    # driving route distance. Its basis distinguishes a fix from a named region.
    requested_meal_topics = (all_likes-all_dislikes) & (MENU_TOPICS-{'매운 음식','빵·디저트'})
    matched_meal_ids = {place['id'] for place in results if place['group']=='음식점'
                        and set(place['matched_topics']) & requested_meal_topics}
    if requested_meal_topics:
        # "Let's eat pasta" also contributes a broad meal preference. That
        # must not turn every restaurant into a suitable pasta alternative.
        # Keep other requested activities. Missing menu data is not evidence
        # that an unrelated restaurant can satisfy the requested cuisine.
        results = [place for place in results if place['group']!='음식점'
                   or place['id'] in matched_meal_ids]
    results.sort(key=lambda r: (-r['score'], r['distance_m'] if r['distance_m'] is not None else math.inf,
                                r['name'], r['id']))
    unique_results, nearby_names = [], {}
    for result in results:
        key = (' '.join(result['name'].casefold().split()), activities[result['id']])
        coordinates = result['latitude'], result['longitude']
        duplicates = nearby_names.setdefault(key, [])
        # OSM can describe one shop as both a node and a building. Limit
        # deduplication to 20m; a same-brand branch elsewhere remains eligible.
        if any(straight_line_meters(coordinates, other) <= 20 for other in duplicates):
            continue
        duplicates.append(coordinates)
        unique_results.append(result)
    from place_quality import attach_quality
    unique_results = attach_quality(unique_results)
    chosen = unique_results if result_limit is None else choose_places(unique_results, result_limit, first_groups=first_groups)
    menu_coverage=[]
    for topic in sorted(requested_foods):
        matches=[menu for place in chosen for menu in place['menu_matches'] if menu['topic']==topic]
        menu_coverage.append({'topic':topic,
                              'registered_count':sum(menu['basis']=='registered' for menu in matches),
                              'name_hint_count':sum(menu['basis']=='name_hint' for menu in matches)})
    profile['menu_coverage']=menu_coverage
    unmatched_menus=[item['topic'] for item in menu_coverage if not item['registered_count'] and not item['name_hint_count']]
    partial_menu_notice = (' '+topic_phrase(unmatched_menus)+' 조건에 맞는 음식점 후보는 찾지 못했어요.'
                           if unmatched_menus and matched_meal_ids else '')
    missing_meal_notice = (('조건에 맞는 공개 장소를 찾지 못했어요. ' if not chosen else '')+
                           topic_phrase(sorted(requested_meal_topics))+
                           ' 조건에 맞는 음식점을 현재 검색 범위의 공개 정보에서 찾지 못했어요. '
                           '검색 지역이나 범위를 조정해 주세요.'
                           if requested_meal_topics and not matched_meal_ids else '')
    return {'constraints':constraints,'recommendations':chosen,'region':region,
            'request_profile':profile,
            'recommendation_summary':summarize_conversation(segments,constraints,evidence),
            'coverage':data.get('coverage', '현재 위치 주변' if current_coordinates is not None else '검색 지역 주변'),
            'lookup_source':data.get('lookup_source', 'OpenStreetMap'),
            'notice':((missing_meal_notice or ('조건에 맞는 공개 장소를 찾지 못했어요. 지역이나 조건을 바꿔 주세요.' if not chosen else
                      '공개 장소 정보와 발언의 키워드·제외 조건으로 추천했어요. 실시간 영업·주차 정보는 확인이 필요해요.')
                      )+partial_menu_notice+(' '+data['search_note'] if data.get('search_note') else '')+
                      (' 해안선 자료로 바닷가인지 확인되지 않은 해변 후보는 제외했어요.'
                       if beach_requested and unconfirmed_sea_count else ''))}
