"""Discover broadly, verify road times, then select a diverse final list.

Recording and authored examples enter through exactly the same function. The
caller resolves the departure point; this module never invents a GPS fix.
"""
from recommend import (recommend, extract, request_profile, choose_places,
                       geographic_group, first_stop_preferences, valid_coordinates)
from travel_discovery import discover_candidates
from routing_duration import enrich_candidate_travel_times, MAX_CANDIDATE_DESTINATIONS
from place_quality import attach_quality
from region_discovery import discover_region_candidates


def _region_recommendation(segments, region, cache_dir, profile):
    data = discover_region_candidates(region, cache_dir, profile)
    options = {'candidate_data':data} if data is not None else {}
    result = recommend(segments, region, cache_dir, result_limit=None, **options)
    if data is not None:
        result['region_discovery'] = data.get('discovery', {})
    return result


def recommend_regions(segments, regions, cache_dir):
    """An explicit list of alternatives does not require choosing a city first."""
    if not 1 <= len(regions) <= 5:
        raise ValueError('비교할 목적 지역을 한 곳에서 다섯 곳 이내로 정해 주세요.')
    constraints, _ = extract(segments)
    profile = request_profile(constraints)
    batches = [_region_recommendation(segments, region, cache_dir, profile) for region in regions]
    result = batches[0]
    pool = {place['id']: place for batch in batches for place in batch['recommendations']}
    result.update(recommendations=choose_places(list(pool.values()), geographic_diversity=True,
                  first_groups={item['group'] for item in first_stop_preferences(segments)}),
                  coverage=' · '.join(batch['coverage'] for batch in batches), region=' · '.join(regions))
    return result


def recommend_trip(segments, regions, cache_dir, origin, max_minutes, *, ratings=None, demo=False):
    if not origin or valid_coordinates(origin.get('lat'), origin.get('lon')) is None:
        raise ValueError('출발 위치를 확인하지 못했어요. 차량 위치를 확인하거나 대화에서 출발지를 알려 주세요.')
    constraints, _ = extract(segments)
    profile = request_profile(constraints)
    regions = list(dict.fromkeys(regions or []))
    if len(regions) > 5:
        raise ValueError('비교할 목적 지역을 다섯 곳 이내로 좁혀 주세요.')
    if regions:
        batches = [_region_recommendation(segments, region, cache_dir, profile) for region in regions]
        result = batches[0]
        pool = [place for batch in batches for place in batch['recommendations']]
        coverage = ' · '.join(batch['coverage'] for batch in batches)
        discovery = {'mode': 'destination_regions', 'regions': regions,
                     'region_sources':[batch.get('region_discovery', {'mode':'remote_region_search'}) for batch in batches]}
    else:
        data = discover_candidates(origin, max_minutes, profile)
        location = {'latitude': origin['lat'], 'longitude': origin['lon']}
        result = recommend(segments, origin.get('label', '출발 위치'), cache_dir,
                           location=location, candidate_data=data, result_limit=None)
        pool = result['recommendations']
        coverage = data.get('coverage', '')
        if data.get('source_data_at'):
            coverage += ' · 지도 자료 '+str(data['source_data_at'])[:10]+' 기준'
        discovery = data.get('discovery', {})
        for place in pool:
            place['distance_basis'] = 'departure'
    unique = {place['id']: place for place in pool}
    first_groups = {item['group'] for item in first_stop_preferences(segments)}
    # Keep geographically different alternatives before querying routes. The
    # final twelve are deliberately NOT selected until all shortlisted routes
    # have been evaluated. Unknown or over-limit candidates never fill gaps.
    shortlist = choose_places(attach_quality(list(unique.values()), ratings, demo), MAX_CANDIDATE_DESTINATIONS,
                              first_groups=first_groups, geographic_diversity=True)
    checked = enrich_candidate_travel_times(shortlist, origin, max_minutes)
    eligible = [place for place in checked
                if place['travel_constraint'].get('within_limit') is True]
    chosen = choose_places(attach_quality(eligible, ratings, demo), first_groups=first_groups, geographic_diversity=True)
    over = sum(place['travel_constraint'].get('within_limit') is False for place in checked)
    unknown = len(checked) - len(eligible) - over
    report = {'candidate_count': len(unique), 'route_candidate_count': len(checked),
              'route_confirmed_count': sum(p['travel_constraint'].get('verified') is True for p in checked),
              'within_limit_count': len(eligible), 'over_limit_count': over,
              'unverified_count': unknown, 'selected_count': len(chosen),
              'spatial_area_count': len({geographic_group(place) for place in chosen}),
              'live_traffic': False, 'discovery': discovery}
    profile['search_report'] = report
    profile['first_stop_evidence'] = first_stop_preferences(segments)
    # Coverage must describe the final eligible list, not rejected candidates.
    profile['menu_coverage'] = [
        {'topic': topic,
         'registered_count': sum(m['topic'] == topic and m['basis'] == 'registered'
                                 for p in chosen for m in p.get('menu_matches', [])),
         'name_hint_count': sum(m['topic'] == topic and m['basis'] == 'name_hint'
                                for p in chosen for m in p.get('menu_matches', []))}
        for topic in profile['requested_menu_topics']]
    label = origin.get('label', '출발 위치')
    explanation = (f'{label}에서 자동차로 약 {max_minutes:g}분 이내인 후보 중 '
                   '메뉴 근거·대화 취향·이동 부담·내 방문 평가를 비교했어요. 실시간 교통은 반영하지 않았어요.')
    profile['explanation'] = explanation
    if chosen:
        notice = explanation
    elif unknown:
        notice = ('조건에 맞는 공개 장소를 찾지 못했어요. 경로 시간을 확인하지 못한 후보가 있어 '
                  '시간 이내라고 추천하지 않았어요. 잠시 후 다시 추천해 주세요.')
    elif pool:
        notice = (f'조건에 맞는 공개 장소를 찾지 못했어요. 조회한 후보 중 자동차로 {max_minutes:g}분 '
                  '이내인 곳이 없어요. 이동 시간이나 원하는 장소 조건을 조정해 주세요.')
    else:
        notice = ('조건에 맞는 공개 장소를 찾지 못했어요. 넓힌 검색 범위의 공개 정보에서 '
                  '대화 취향과 일치하는 후보를 찾지 못했어요.')
    if unknown and chosen:
        notice += f' 이동 시간 미확인 {unknown}곳은 추천에서 제외했어요.'
    result.update(recommendations=chosen, request_profile=profile, notice=notice,
                  coverage=coverage, region=' · '.join(regions) if regions else f'{label} 출발 · {max_minutes:g}분 이내')
    return result
