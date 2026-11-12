"""Local visit feedback; sample feedback never changes real recommendations."""
import copy
from datetime import datetime, timezone


def rating_key(place_id, demo=False):
    return ('sample:' if demo else 'visit:') + place_id


def rate_visit(state, request):
    place_id, visit_id = request.get('place_id'), request.get('visit_id')
    rating, note = request.get('rating'), request.get('note', '')
    if not isinstance(place_id, str) or not place_id or len(place_id) > 200:
        raise ValueError('평가할 장소를 확인해 주세요.')
    if not isinstance(visit_id, str) or not visit_id:
        raise ValueError('저장한 방문 기록에서 평가해 주세요.')
    if type(rating) is not int or not 1 <= rating <= 5:
        raise ValueError('별점은 1점부터 5점까지 선택해 주세요.')
    if not isinstance(note, str) or len(note) > 500:
        raise ValueError('방문 메모는 500자 이내로 적어 주세요.')
    visit = next((item for item in state.get('saved_places', [])
                  if item.get('id') == visit_id and item.get('place', {}).get('id') == place_id
                  and item.get('arrived_at')), None)
    if visit is None:
        raise ValueError('도착 후 저장한 장소에 평가를 남길 수 있어요.')
    updated = copy.deepcopy(state)
    updated.setdefault('_place_ratings', {})[rating_key(place_id, bool(visit.get('demo')))] = {
        'rating': rating, 'note': note.strip(),
        'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }
    return updated


def annotate_places(places, ratings, demo=False):
    result = copy.deepcopy(places)
    for place in result:
        rating = ratings.get(rating_key(place.get('id', ''), demo))
        if rating:
            place['my_rating'] = copy.deepcopy(rating)
        else:
            place.pop('my_rating', None)
    return result


def personalize(places, ratings, demo=False):
    result = annotate_places(places, ratings, demo)
    # Only swap equally relevant places within the same activity and seat
    # coverage. Keep the menu/seat diversity and negative preferences intact.
    groups = {}
    for index, place in enumerate(result):
        key = (place.get('group'), place.get('score'), tuple(sorted(place.get('matched_seats', []))),
               place.get('travel_constraint', {}).get('within_limit'))
        groups.setdefault(key, []).append(index)
    for indices in groups.values():
        ranked = sorted((result[i] for i in indices), key=lambda p: (p.get('my_rating') or {}).get('rating', 3), reverse=True)
        for i, place in zip(indices, ranked):
            result[i] = place
    for place in result:
        if place.get('my_rating'):
            place['personalization_note'] = '조건이 같은 후보끼리는 이전에 남긴 내 평가를 참고했어요.'
    return result


def public_ratings(state):
    ratings = state.pop('_place_ratings', {})
    state['recommendations'] = annotate_places(state.get('recommendations', []), ratings, bool(state.get('demo')))
    for visit in state.get('saved_places', []):
        if isinstance(visit.get('place'), dict):
            visit['place'] = annotate_places([visit['place']], ratings, bool(visit.get('demo')))[0]
    trip = state.get('trip', {})
    if isinstance(trip.get('place'), dict):
        trip['place'] = annotate_places([trip['place']], ratings, bool(trip.get('demo')))[0]
    return state
