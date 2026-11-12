"""Evidence-based candidate ordering, never an invented restaurant review.

Scores are internal comparison points, not stars, probabilities, popularity or
proof of taste. Hard exclusions and route eligibility belong to the caller.
The same rules apply to recordings and examples; only personal-rating keys
are isolated between real and sample visits.
"""
from copy import deepcopy
import math

from personal_ratings import rating_key


KNOWN_SEATS = frozenset(('driver', 'passenger', 'rear1', 'rear2'))
QUALITY_NOTE = ('대화 취향과 확인된 장소 정보를 비교한 기준이에요. '
                '맛·분위기·서비스 품질을 검증한 평가는 아니에요.')


def _strings(value):
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {item for item in value if isinstance(item, str) and item}


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _independent_topics(topics):
    # Import lazily so recommend.py can call attach_quality without an import
    # cycle, while keeping dish/category definitions in a single catalog.
    from recommend import DISH_PARENTS, MENU_TOPICS
    topics = set(topics)
    for child, parent in DISH_PARENTS.items():
        if child in topics:
            topics.discard(parent)
    if '회' in topics:
        topics.discard('해산물')
    if topics & (MENU_TOPICS - {'빵·디저트', '매운 음식'}):
        topics.discard('식사')
    return topics


def _factor(key, label, detail, score, basis):
    return {'key': key, 'label': label, 'detail': detail,
            'score': round(score, 2), 'basis': basis}


def _menu_factors(place, matched):
    from recommend import MENU_TOPICS, SPECIFIC_MENU_TOPICS
    classified, hinted = set(), set()
    matches = place.get('menu_matches')
    if not isinstance(matches, (list, tuple)):
        return []
    for match in matches:
        if not isinstance(match, dict):
            continue
        topic = match.get('topic')
        if not isinstance(topic, str) or topic not in matched or topic not in MENU_TOPICS:
            continue
        if match.get('basis') == 'registered':
            classified.add(topic)
        elif match.get('basis') == 'name_hint':
            hinted.add(topic)
    # Duplicate references to a dish never multiply its contribution. Public
    # classification wins if an old record also contains a name-only hint.
    hinted -= classified
    classified = _independent_topics(classified)
    hinted = _independent_topics(hinted)
    result = []
    if classified:
        points = min(40, sum(12 if topic in SPECIFIC_MENU_TOPICS else 7 for topic in classified))
        result.append(_factor('menu_classification', '원하는 메뉴 분류 확인',
            '공개 장소 정보에 ' + '·'.join(sorted(classified)) +
            ' 종류가 등록돼 있어요. 실제 판매 메뉴와 맛은 별도 확인이 필요해요.',
            points, 'public_menu_classification'))
    if hinted:
        points = min(8, sum(2 if topic in SPECIFIC_MENU_TOPICS else 1 for topic in hinted))
        result.append(_factor('menu_name_hint', '가게 이름에 요청 메뉴 포함',
            '이름에 ' + '·'.join(sorted(hinted)) +
            ' 표현이 있어요. 메뉴 분류가 확인된 곳보다 낮게 반영했어요.',
            points, 'name_hint'))
    return result


def _conversation_factors(place, matched):
    topics = _independent_topics(matched)
    factors = []
    if topics:
        label = ('대화 취향 ' + str(len(topics)) + '가지 일치' if len(topics) > 1
                 else next(iter(topics)) + ' 취향 일치')
        factors.append(_factor('conversation_match', label,
            '대화에서 원한 ' + '·'.join(sorted(topics)) + ' 조건과 연결되는 후보예요.',
            min(15, len(topics) * 3), 'conversation'))
    evidence = place.get('evidence')
    identified = set()
    if isinstance(evidence, (list, tuple)):
        for item in evidence:
            if (isinstance(item, dict) and item.get('seat') in KNOWN_SEATS
                    and isinstance(item.get('topic'), str) and item['topic'] in matched
                    and not item.get('negative') and item.get('quote')):
                identified.add(item['seat'])
    identified &= _strings(place.get('matched_seats'))
    if len(identified) > 1:
        factors.append(_factor('identified_preferences',
            f'확인된 {len(identified)}좌석의 취향 반영',
            '좌석 정보와 관련 발언이 있는 의견을 함께 반영했어요.',
            (len(identified) - 1) * 3, 'identified_conversation'))
    return factors


def _travel_factors(place):
    constraint = place.get('travel_constraint')
    if not isinstance(constraint, dict):
        return []
    limit, duration = _number(constraint.get('max_minutes')), _number(constraint.get('duration_minutes'))
    if (constraint.get('verified') is not True or constraint.get('within_limit') is not True
            or limit is None or limit <= 0 or duration is None or not 0 <= duration <= limit):
        return []
    margin = 1 - duration / limit
    # Near the limit there is no meaningful headroom to praise. All route
    # estimates remain qualified; traffic or actual entrance access can differ.
    if margin < .15:
        return []
    return [_factor('travel_headroom', '이동 시간 여유',
        f'허용한 {limit:g}분 중 도로 기준 약 {duration:g}분으로 예상돼요. 실시간 교통은 반영하지 않았어요.',
        10 * margin, 'road_time_estimate')]


def _rating_factors(place, ratings, demo):
    identifier = place.get('id')
    if not isinstance(identifier, str) or not identifier:
        return []
    record = ratings.get(rating_key(identifier, demo))
    if not isinstance(record, dict):
        return []
    rating = record.get('rating')
    if type(rating) is not int or not 1 <= rating <= 5:
        return []
    points = {1: -16, 2: -8, 3: 0, 4: 5, 5: 10}[rating]
    prefix = '샘플에서 ' if demo else ''
    label = (prefix + '내가 이전에 좋게 평가' if rating >= 4 else
             prefix + '내가 이전에 낮게 평가' if rating <= 2 else prefix + '이전 내 평가 참고')
    return [_factor('personal_visit_rating', label,
        f'{"샘플 방문" if demo else "방문"} 후 내가 남긴 {rating}점 평가를 반영했어요. 다른 이용자들의 평균 별점은 아니에요.',
        points, 'sample_visit_rating' if demo else 'personal_visit_rating')]


def attach_quality(places, ratings=None, demo=False):
    """Return independent candidates with explainable comparison metadata.

    ``ratings`` is the trusted local ``_place_ratings`` mapping, keyed through
    personal_ratings.rating_key. Incoming stars, review text, website, contact,
    opening hours and an old ``my_rating`` field never establish good taste.
    Only the matching demo/real namespace is read. This function never reorders
    candidates or changes constraints, eligibility, menus or stored feedback.
    """
    ratings = ratings if isinstance(ratings, dict) else {}
    result = deepcopy(list(places))
    for place in result:
        if not isinstance(place, dict):
            raise TypeError('places must contain dictionaries')
        matched = _strings(place.get('matched_topics'))
        factors = (_menu_factors(place, matched) + _conversation_factors(place, matched)
                   + _travel_factors(place) + _rating_factors(place, ratings, bool(demo)))
        place['quality_score'] = round(sum(factor['score'] for factor in factors), 2)
        place['quality_factors'] = factors
        place['quality_note'] = QUALITY_NOTE
    return result
