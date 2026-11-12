"""Resolve explicit destination intent locally; never send dialogue to a geocoder."""
import re

from recommend import CHANGE_OF_PLAN, NEGATIVE, TOPICS, UNCERTAIN_NEGATION, split_clauses, travel_limit


# Bare city names need a small local vocabulary; named stations, districts and
# beaches are also recognized by their suffix. No network inference is involved.
CITIES = ('서울 부산 대구 인천 광주 대전 울산 세종 제주 서귀포 '
          '수원 성남 용인 고양 부천 안산 안양 남양주 화성 평택 의정부 시흥 파주 김포 광명 '
          '광주 군포 오산 이천 안성 의왕 하남 양주 동두천 구리 포천 여주 과천 '
          '춘천 원주 강릉 동해 속초 삼척 태백 청주 충주 제천 천안 아산 서산 당진 공주 보령 논산 계룡 '
          '전주 군산 익산 정읍 남원 김제 목포 여수 순천 나주 광양 '
          '포항 경주 김천 안동 구미 영주 영천 상주 문경 경산 창원 진주 통영 사천 김해 밀양 거제 양산').split()
# Common bare localities are not cities and must retain their address level.
# Other explicit administrative names still use NAMED_PLACES below.
LOCALITIES = {'판교':2, '분당':2, '일산':2, '동탄':2, '태안':1,
              '가평':1, '양평':1, '홍천':1, '평창':1, '횡성':1, '양양':1,
              '고성':1, '단양':1, '옥천':1, '영동':1, '부여':1, '서천':1,
              '홍성':1, '남해':1, '하동':1, '담양':1, '해남':1}
NAME_END = r'(?=\s|$|[,，.!?]|으?로|에|까지|안에서|쪽|근처|주변|바다|해변|해수욕장|밥|식사|커피|산책|가자|가고|가볼|갈|보러|말고|싫|좋|은|는|도|이나|이랑|이야|이에요|랑|와|과)'
CITY_NAMES = re.compile('(?:'+'|'.join(sorted(set(CITIES),key=len,reverse=True))+r')(?:특별시|광역시|특별자치시|특별자치도|시)?'+NAME_END)
LOCALITY_NAMES = re.compile(r'(?<![가-힣])(?:'+'|'.join(LOCALITIES)+')'+NAME_END)
NAMED_PLACES = re.compile(r'(?<![가-힣])([가-힣]{2,12}(?:해수욕장|해변|공원|시청|구청|특별자치도|광역시|특별시|시|군|구|읍|면|동|역))'+NAME_END)
WANT_TO_GO = re.compile(r'가자|갈까|갈래|가고\s*싶|가고$|가볼|가려|가면|가서|보러|보고\s*싶|놀러|나들이|여행\s*가|추천|찾아|먹자|먹을까|먹으러|먹고\s*싶|식사\s*하자|마시자|마실까|마시러|마시고\s*싶|산책\s*하자|걷자|쉬자')
PAST_OR_HOME = re.compile(r'다녀왔|갔다|갔어|갔었|살았|살고\s*있|살아|사는\s*곳|집(?:이|은|에)|고향')
REJECT_REGION = re.compile(r'^\s*(?:은|는|도|에(?:는|도)?|으로|로|쪽(?:은|으로)?|지역(?:은|는)?)?\s*(?:정말|너무|좀)?\s*(?:싫|별로|말고|빼|제외|안\s*가|못\s*가|가기\s*싫|가고\s*싶지(?:는)?\s*않|가지\s*말)')
NEARBY = re.compile(r'근처|주변|가까운|가까운데|가까이|가까우면')
CONNECTOR = re.compile(r'^\s*(?:이나|또는|혹은|아니면|와|과|랑|이랑|하고|,|·|/)?\s*$')
ALTERNATIVE_CONNECTOR = re.compile(r'^\s*(?:이나|또는|혹은|아니면|와|과|랑|이랑|하고|·|/)\s*$')
ALTERNATIVE_VENUE = re.compile(r'^\s*(?:쪽|근처|주변)?\s*(?:바다|해변|해수욕장|공원|카페|식당|음식점|전시|미술관|박물관)(?:\s|$|[을를은는이가도])')
PROPOSE_REGION = re.compile(r'^\s*(?:은|는|도|으?로|쪽(?:으로)?|에(?:는)?)?\s*(?:어때|어떨까|할까|하자|정할까|정하자|결정하자|좋겠다|좋겠어|괜찮겠다|괜찮겠어|괜찮지\s*않을까(?:요)?(?=\s|$|[?!.])|(?:좋지|좋아|좋네|좋다|괜찮아)(?:요)?(?=\s|$|[?!.]))')
FOOD_ACTIVITY = re.compile(r'먹자|먹을까|먹으러|먹고\s*싶|식사\s*하자|마시자|마실까|마시러|마시고\s*싶')
REGION_LOCATIVE = re.compile(r'^\s*(?:에(?:서)?|안에서|으?로|쪽(?:으로|에서)?|근처|주변)')
LOCAL_VENUE_REQUEST = re.compile(r'(?:다닐|갈|둘러볼|놀|쉴)\s*(?:만한\s*)?(?:데|곳)(?:가|이)?\s*(?:없(?:나|을까)|있(?:나|을까)|찾|보자|추천|어디)')
DIRECT_TRAVEL = re.compile(r'^\s*(?:은|는|도)?\s*(?:가서|가자|갈까|갈래|가고\s*싶)')
SCENERY_TRIP = re.compile(r'(?:바다|해변|해수욕장|공원)(?:를|을)?\s*(?:보러|보고|가서|가자)')
UNKNOWN_PLACE_PROPOSAL = re.compile(r'(?<![가-힣])([가-힣]{2,12})\s*쪽(?:으로)?\s*(?:어때|좋겠다|좋겠어|가자|갈까|할까)')
GENERIC_PLACE_WORDS = {word for words in TOPICS.values() for word in words} | {'바닷가','실내','야외','근처','주변','시내','시외','동네','외곽','도심','반대','주차장','국물','집','길','메뉴','음식','가격','예산','금액','자리','일정','이내','아니','여유','무료','유료'}
CONDITIONAL_STEMS = {'가까우','조용하','넓으','따뜻하','시원하','편하','괜찮으','저렴하','좋으','빠르','깨끗하','짧으','맛있으',
                     '있으','없으','걸으','먹으','마시','앉으','쉬','되','가능하','정도','곳'}
ORIGIN_DEPARTURE = re.compile(r'^\s*(?:에서|부터|을|를)?\s*(?:(?:지금|이제|같이|함께)\s*)?출발')
ORIGIN_LABEL = re.compile(r'(?:출발지(?:는|가|를)?|출발(?:은|을))\s*$')
PAST_ORIGIN = re.compile(r'어제|그제|지난|저번|예전|당시|다녀온|여행\s*때')
CURRENT_VISIT = re.compile(r'^\s*(?:에|까지)?\s*(?:와서|왔(?:으니까|으니|는데|잖아))')
FUTURE_VISIT = re.compile(r'내일|내년|다음|나중|언젠가|(?:여름|겨울|봄|가을)(?:에|방학)')
OTHER_VISITOR = re.compile(r'(?:친구|부모님|엄마|아빠|동생|언니|형|누나|오빠|그\s*사람|걔)(?:는|가|은|이)\s*$')


def _is_current_visit(clause,start,end,next_start):
    prefix,tail=clause[:start],clause[end:next_start]
    # A present visit can scope the next venue search, but it is neither a GPS
    # fix nor an origin declaration. Historical/imagined trips must not do so.
    return bool(CURRENT_VISIT.search(tail) and not PAST_ORIGIN.search(prefix)
                and not FUTURE_VISIT.search(prefix) and not OTHER_VISITOR.search(prefix)
                and not PAST_OR_HOME.search(tail)
                and not re.search(r'왔(?:었|던)|와서\s*(?:살|지냈|먹었|놀았|쉬었)|돌아왔|돌아갔',tail))


def _is_origin(clause,start,end,next_start):
    prefix,tail=clause[:start],clause[end:next_start]
    if PAST_ORIGIN.search(prefix) or PAST_OR_HOME.search(tail):
        return False
    if ORIGIN_LABEL.search(prefix) and not re.search(r'아니|말고|싫',tail):
        return True
    if ORIGIN_DEPARTURE.search(tail) and not re.search(r'안\s*하|못\s*하|하지\s*않|하지\s*말',tail):
        return True
    # The two places in "서울에서 강릉으로 가자" have different roles.
    if next_start < len(clause) and re.fullmatch(r'\s*(?:에서|부터)\s*',tail):
        return True
    # With a travel budget, "판교에서 차로 두 시간 안에 갈 바다" names
    # an origin, even though the destination has not been chosen yet.
    # A meal/wait duration and "서울에서 밥 먹자" still describe local activity.
    if (re.match(r'^\s*(?:에서|부터)\s*',tail) and travel_limit(tail) is not None
            and re.search(r'차로|운전|이동|편도|도착|갈\s|갈$|가(?:는|고|볼|자|려)',tail)
            and not re.search(r'가지\s*말|안\s*가|못\s*가|가고\s*싶지',tail)):
        return True
    return bool(re.search(r'지금|현재|여기',prefix) and re.search(r'^\s*(?:에|에서)\s*(?:있어|있고|있으|있는)',tail))


def _conditional_phrase(name):
    # The administrative suffix 면 must not consume the conditional ending
    # in '식당이면/카페면/가까우면'. Unknown proper names such as 서종면 remain.
    if name.endswith(('으면','다면','라면','려면')):
        return True
    ending='이면' if name.endswith('이면') else '면' if name.endswith('면') else None
    if ending is None:
        return False
    stem=name[:-len(ending)]
    return (stem in GENERIC_PLACE_WORDS or stem in CONDITIONAL_STEMS
            or bool(re.search(r'(?:하|주|되|보|않|같|있|없|어|아)$',stem)))


def _suggests_destination(tail):
    if PROPOSE_REGION.search(tail):
        return True
    if REGION_LOCATIVE.search(tail) and LOCAL_VENUE_REQUEST.search(tail):
        return True
    if not WANT_TO_GO.search(tail):
        return False
    # '부산 돼지국밥 먹자' can describe a dish, unlike '부산에서 먹자'.
    # An independently stated trip/scenery proposal still establishes the area.
    if FOOD_ACTIVITY.search(tail):
        return bool(REGION_LOCATIVE.search(tail) or DIRECT_TRAVEL.search(tail) or SCENERY_TRIP.search(tail))
    return True


def _city_name(name):
    if name in CITIES or name in ('광주시','제주시'):
        return name
    for suffix in ('특별자치시','특별자치도','특별시','광역시','시'):
        if name.endswith(suffix) and name[:-len(suffix)] in CITIES:
            return name[:-len(suffix)]
    return None


def _region_key(name):
    words=name.split()
    return ' '.join([_city_name(words[0]) or words[0],*words[1:]])


def _contains_region(broader, narrower):
    broader,narrower=_region_key(broader),_region_key(narrower)
    return narrower == broader or narrower.startswith(broader+' ')


def _place_level(name):
    if _city_name(name) is not None:
        return 0
    if name in LOCALITIES:
        return LOCALITIES[name]
    if re.search(r'(구|군)$',name):
        return 1
    if re.search(r'(읍|면|동)$',name):
        return 2
    if re.search(r'(역|해변|해수욕장|공원|시청|구청)$',name):
        return 3
    return None


def _names(clause):
    spans = [(m.start(),m.end(),m.group()) for pattern in (CITY_NAMES,LOCALITY_NAMES,NAMED_PLACES)
             for m in pattern.finditer(clause) if not _conditional_phrase(m.group())]
    selected=[]
    for start,end,name in sorted(spans,key=lambda item:(item[0],-(item[1]-item[0]))):
        if any(start < previous[1] and end > previous[0] for previous in selected):
            continue
        selected.append((start,end,name))
    # Keep an explicit city -> district -> neighbourhood -> station chain
    # together. Two stations/districts or two cities remain distinct choices.
    combined=[]
    for item in selected:
        if combined:
            previous=combined[-1]
            previous_level=_place_level(previous[2].split()[-1])
            next_level=_place_level(item[2])
            city=_city_name(previous[2].split()[0])
            other_city=city and any(item[2].startswith(other) for other in CITIES if other != city)
            if (previous_level is not None and next_level is not None and previous_level < next_level
                    and clause[previous[1]:item[0]].isspace() and not other_city):
                combined[-1]=(previous[0],item[1],previous[2]+' '+item[2])
                continue
        combined.append(item)
    return combined


def resolve_search_intent(segments):
    """Return destination search regions, an origin, or an unresolved conflict.

    Unknown seats may be multiple people, so one such utterance cannot silently
    cancel another. Only explicit correction cancels another destination topic.
    """
    profiles={}
    for segment in segments:
        seat=segment.get('seat','unknown')
        profile=profiles.setdefault(seat,{'wanted':{},'avoided':{},'nearby':None,'unresolved':[],'origins':{},'visits':{},'alternatives':[]})
        evidence={'segment_id':segment.get('id'),'seat':seat,'text':str(segment.get('text',''))}
        correction=False
        # A past/home clause must not borrow a later destination's travel verb.
        text=re.sub(r'(다녀왔는데|갔었는데|갔는데|살았는데|사는데|살고)',r'\1.',evidence['text'])
        for raw_clause in split_clauses(text):
            clause=raw_clause.strip()
            if not clause:
                continue
            if CHANGE_OF_PLAN.search(clause):
                correction=True
            names=_names(clause)
            explicit_alternatives = any(ALTERNATIVE_CONNECTOR.fullmatch(clause[left[1]:right[0]])
                                        for left,right in zip(names,names[1:]))
            positives=[]
            visits=[]
            for index,(start,end,name) in enumerate(names):
                next_start=names[index+1][0] if index+1<len(names) else len(clause)
                tail=clause[end:next_start]
                if _is_origin(clause,start,end,next_start):
                    if seat != 'unknown': profile['origins'].clear()
                    profile['origins'][name]=evidence
                    continue
                if CURRENT_VISIT.search(tail):
                    if _is_current_visit(clause,start,end,next_start):
                        visits.append(name)
                    # A historical visit cannot borrow an activity or proposal
                    # later in the same phrase to become a current destination.
                    continue
                # Alternatives share their final action: '부산이나 강릉 가자'.
                if index+1<len(names) and CONNECTOR.fullmatch(tail):
                    tail=clause[names[-1][1]:]
                if PAST_OR_HOME.search(tail) or UNCERTAIN_NEGATION.search(tail):
                    continue
                if (PAST_ORIGIN.search(clause[:start]) and REGION_LOCATIVE.search(tail)
                        and LOCAL_VENUE_REQUEST.search(tail)):
                    continue
                if REJECT_REGION.search(tail):
                    if seat != 'unknown':
                        profile['wanted']={region:proof for region,proof in profile['wanted'].items() if not _contains_region(name,region)}
                    profile['avoided'][name]=evidence
                elif not NEGATIVE.search(tail) and (_suggests_destination(tail)
                        or (explicit_alternatives and ALTERNATIVE_VENUE.search(tail))):
                    positives.append(name)
            unresolved=False
            if not names and not PAST_OR_HOME.search(clause) and not NEGATIVE.search(clause):
                hint=UNKNOWN_PLACE_PROPOSAL.search(clause)
                if hint and hint[1] not in GENERIC_PLACE_WORDS and not re.search(r'(은|는|한|운|할|던)$',hint[1]):
                    unresolved=True
            nearby=bool(NEARBY.search(clause) and not names and not PAST_OR_HOME.search(clause) and not NEGATIVE.search(clause))
            if correction and (positives or visits or nearby or unresolved):
                if seat != 'unknown':
                    profile['wanted'].clear()
                    profile['nearby']=None
                    profile['unresolved'].clear()
                    profile['visits'].clear()
                    profile['alternatives'].clear()
                correction=False
            for name in positives:
                if seat != 'unknown':
                    profile['avoided']={region:proof for region,proof in profile['avoided'].items() if not _contains_region(region,name)}
                    profile['unresolved'].clear()
                profile['wanted'][name]=evidence
            if explicit_alternatives and len(positives)>1:
                profile['alternatives'].append(set(positives))
            for name in visits:
                if seat != 'unknown': profile['visits'].clear()
                profile['visits'][name]=evidence
            if unresolved:
                profile['unresolved'].append(evidence)
            if nearby:
                profile['nearby']=evidence

    wanted={name:proof for profile in profiles.values() for name,proof in profile['wanted'].items()}
    # An explicit destination such as "강릉 가자" takes precedence over a
    # current stop such as "원주까지 왔는데". Otherwise retain visit ambiguity.
    if not wanted:
        wanted={name:proof for profile in profiles.values() for name,proof in profile['visits'].items()}
    avoided={name:proof for profile in profiles.values() for name,proof in profile['avoided'].items()}
    nearby=[profile['nearby'] for profile in profiles.values() if profile['nearby']]
    unresolved=[proof for profile in profiles.values() for proof in profile['unresolved']]
    origins={name:proof for profile in profiles.values() for name,proof in profile['origins'].items()}
    origin_unique={_region_key(name):name for name in origins}
    origin_candidates=[name for key,name in origin_unique.items() if not any(other.startswith(key+' ') for other in origin_unique)]
    origin_region=origin_candidates[0] if len(origin_candidates)==1 else None
    unique={_region_key(name):name for name in wanted}
    candidates=[name for key,name in unique.items() if not any(other.startswith(key+' ') for other in unique)]
    opposed={blocked for blocked in avoided if any(_contains_region(blocked,target) for target in wanted)}
    # An explicit list is a request to compare alternatives, not an unresolved
    # disagreement. Separate speakers' independent proposals remain a conflict.
    # Unknown-seat recordings can still preserve an alternative list verbatim.
    candidate_keys={_region_key(name) for name in candidates}
    alternative_list = len(candidates)>1 and any(
        candidate_keys <= {_region_key(name) for name in group}
        for profile in profiles.values() for group in profile['alternatives'])
    # Without reverse geocoding, "nearby/anywhere" cannot establish that the
    # computer is outside an explicitly rejected region. Ask for a destination.
    needs_choice=(len(candidates)>1 and not alternative_list) or bool(opposed) or bool(unresolved) or (bool(avoided) and not candidates)
    region=candidates[0] if len(candidates)==1 and not needs_choice else None
    multi_region=bool(alternative_list and not needs_choice)
    source='conversation' if candidates or avoided or unresolved else 'nearby' if nearby else 'unspecified'
    sources=[*wanted.values(),*(avoided[name] for name in avoided if name in opposed or not candidates),*nearby,*unresolved]
    source_ids={item['segment_id'] for item in sources}
    quotes=[{'segment_id':s.get('id'),'seat':s.get('seat','unknown'),'text':str(s.get('text',''))}
            for s in segments if s.get('id') in source_ids]
    if unresolved:
        message='말씀하신 장소를 어느 지역에서 찾을지 확인해 주세요.'
    elif needs_choice:
        message='가고 싶은 지역에 대한 의견이 달라요. 검색할 지역을 골라 주세요.' if candidates else '피하고 싶은 지역 외에 가고 싶은 지역을 골라 주세요.'
    elif multi_region:
        message='대화에서 제안한 '+', '.join(candidates)+' 지역의 장소를 함께 비교해요.'
    elif region:
        message='대화에서 제안한 '+region+' 주변을 찾아요.'
    elif nearby:
        message='가까운 곳을 원하는 의견에 따라 현재 위치 주변에서 찾아요.'
    else:
        message='대화에 지역이 정해지지 않아 현재 위치 주변에서 찾아요.'
    return {'region':region,'area_source':source,'candidate_regions':candidates,
            'search_regions':candidates if not needs_choice else [],'multi_region':multi_region,
            'needs_area_choice':needs_choice,'evidence':quotes,'message':message,
            'origin_region':origin_region,'origin_candidates':origin_candidates,
            'needs_origin_choice':len(origin_candidates)>1,'origin_evidence':list(origins.values())}
