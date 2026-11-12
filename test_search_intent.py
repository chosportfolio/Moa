import unittest

from search_intent import resolve_search_intent


class SearchIntentTests(unittest.TestCase):
    def lines(self,*texts,seat='driver'):
        return [{'id':str(i),'seat':seat,'text':text} for i,text in enumerate(texts)]

    def test_explicit_destination_overrides_nearby_default(self):
        for text in ('부산 바다 보러 가자','부산 바다 보고 싶어','부산바다보러가자'):
            with self.subTest(text=text):
                result=resolve_search_intent(self.lines(text))
                self.assertEqual(result['region'],'부산')
                self.assertEqual(result['area_source'],'conversation')
                self.assertFalse(result['needs_area_choice'])

    def test_past_visits_and_home_are_not_destinations(self):
        for text in ('어제 부산 다녀왔어','내 집은 부산이야','부산에 살고 있어','부산물 버리러 가자'):
            with self.subTest(text=text):
                result=resolve_search_intent(self.lines(text))
                self.assertIsNone(result['region'])
                self.assertEqual(result['area_source'],'unspecified')

    def test_nearby_request_uses_location_as_an_assist(self):
        result=resolve_search_intent(self.lines('가까운데 안 매운 밥 먹자','근처 카페 가자'))
        self.assertIsNone(result['region'])
        self.assertEqual(result['area_source'],'nearby')

    def test_ordinary_conditionals_do_not_create_fictitious_regions(self):
        for text in ('디저트도 있으면 좋겠어', '조금 걸으면 좋겠다', '주차할 수 있는 곳이면 좋겠어',
                     '한 사람당 2만 원 정도면 좋겠어', '줄이 없으면 좋겠다', '실내에서 쉬면 좋겠어'):
            with self.subTest(text=text):
                result = resolve_search_intent(self.lines('서울에서 카페 가자', text))
                self.assertEqual(result['region'], '서울')
                self.assertEqual(result['candidate_regions'], ['서울'])
                self.assertFalse(result['needs_area_choice'])

    def test_real_myeon_names_still_work_as_destinations(self):
        for region in ('서종면', '대소면', '신니면', '안면읍'):
            with self.subTest(region=region):
                result = resolve_search_intent(self.lines(region + '에서 밥 먹자'))
                self.assertEqual(result['region'], region)
                self.assertFalse(result['needs_area_choice'])

    def test_same_seats_explicit_correction_uses_latest_quote(self):
        result=resolve_search_intent(self.lines('부산 가자','아니다, 강릉 가자'))
        self.assertEqual(result['region'],'강릉')
        self.assertEqual([e['segment_id'] for e in result['evidence']],['1'])

    def test_named_rejection_does_not_choose_rejected_region(self):
        result=resolve_search_intent(self.lines('부산 가자','부산 말고 강릉 가자'))
        self.assertEqual(result['region'],'강릉')
        self.assertFalse(result['needs_area_choice'])

    def test_different_seats_cannot_silently_overrule_each_other(self):
        lines=[{'id':'a','seat':'driver','text':'충주 가자'},
               {'id':'b','seat':'passenger','text':'부산 가자'}]
        result=resolve_search_intent(lines)
        self.assertIsNone(result['region'])
        self.assertTrue(result['needs_area_choice'])
        self.assertEqual(set(result['candidate_regions']),{'충주','부산'})

    def test_activity_in_an_explicit_region_is_also_destination_intent(self):
        for request in ('충주에서 밥 먹자','충주에서 커피 마시자','충주에서 산책하자'):
            with self.subTest(request=request):
                lines=[{'id':'a','seat':'driver','text':'부산 바다 보러 가자'},
                       {'id':'b','seat':'passenger','text':request}]
                result=resolve_search_intent(lines)
                self.assertTrue(result['needs_area_choice'])
                self.assertEqual(set(result['candidate_regions']),{'충주','부산'})

    def test_explicit_alternatives_are_searched_together(self):
        result=resolve_search_intent(self.lines('부산이나 강릉 가자'))
        self.assertFalse(result['needs_area_choice'])
        self.assertTrue(result['multi_region'])
        self.assertIsNone(result['region'])
        self.assertEqual(set(result['candidate_regions']),{'부산','강릉'})
        self.assertEqual(set(result['search_regions']),{'부산','강릉'})

    def test_alternative_areas_work_with_topics_and_without_known_speakers(self):
        for text,regions in (
                ('화성이나 인천 쪽 바다',{'화성','인천'}),
                ('태안 또는 서산 바다 보고 싶어',{'태안','서산'}),
                ('양평이나 가평 쪽 카페',{'양평','가평'}),
                ('서울 강남역이나 홍대입구역에서 식사하자',{'서울 강남역','홍대입구역'})):
            for seat in ('driver','unknown'):
                with self.subTest(text=text,seat=seat):
                    result=resolve_search_intent(self.lines(text,seat=seat))
                    self.assertEqual(set(result['search_regions']),regions)
                    self.assertTrue(result['multi_region'])
                    self.assertFalse(result['needs_area_choice'])

    def test_alternative_list_does_not_override_an_objection_or_unresolved_area(self):
        for objection in ('인천은 싫어', '해운대 쪽 좋겠다'):
            with self.subTest(objection=objection):
                lines=[{'id':'a','seat':'driver','text':'화성이나 인천 쪽 바다 보고 싶어'},
                       {'id':'b','seat':'passenger','text':objection}]
                result=resolve_search_intent(lines)
                self.assertTrue(result['needs_area_choice'])
                self.assertFalse(result['multi_region'])
                self.assertEqual(result['search_regions'],[])

    def test_a_new_proposal_outside_an_alternative_list_is_still_unresolved(self):
        lines=[{'id':'a','seat':'driver','text':'화성이나 인천 쪽 바다 보고 싶어'},
               {'id':'b','seat':'passenger','text':'나는 강릉 가고 싶어'}]
        result=resolve_search_intent(lines)
        self.assertTrue(result['needs_area_choice'])
        self.assertFalse(result['multi_region'])

    def test_explicit_correction_can_replace_an_alternative_list(self):
        result=resolve_search_intent(self.lines('화성이나 인천 쪽 바다','아니 태안으로 가자'))
        self.assertEqual(result['region'],'태안')
        self.assertEqual(result['search_regions'],['태안'])
        self.assertFalse(result['multi_region'])

    def test_past_region_does_not_borrow_later_travel_action(self):
        result=resolve_search_intent(self.lines('부산 다녀왔는데 강릉 가자'))
        self.assertEqual(result['region'],'강릉')

    def test_named_station_and_qualified_district_are_supported(self):
        for text,region in (('홍대입구역으로 가자','홍대입구역'),('부산 해운대구 가자','부산 해운대구')):
            with self.subTest(text=text):
                self.assertEqual(resolve_search_intent(self.lines(text))['region'],region)

    def test_explicit_city_district_neighborhood_and_station_form_one_place(self):
        for region in ('서울 강남구 강남역','서울시 강남구 역삼동','성남시 분당구 판교역','성남시 판교역'):
            with self.subTest(region=region):
                result=resolve_search_intent(self.lines(region+' 가자'))
                self.assertEqual(result['region'],region)
                self.assertFalse(result['needs_area_choice'])

    def test_two_distinct_stations_or_cities_are_not_joined_as_an_address(self):
        for text in ('서울 강남역 홍대입구역 가자','서울 부산 가자','서울 강남구 서초구 가자'):
            with self.subTest(text=text):
                result=resolve_search_intent(self.lines(text))
                self.assertTrue(result['needs_area_choice'])
                self.assertGreater(len(result['candidate_regions']),1)

    def test_more_specific_place_can_refine_another_seats_same_city_preference(self):
        lines=[{'id':'a','seat':'driver','text':'서울시 가자'},
               {'id':'b','seat':'passenger','text':'서울 강남역 가자'}]
        result=resolve_search_intent(lines)
        self.assertEqual(result['region'],'서울 강남역')
        self.assertFalse(result['needs_area_choice'])
        self.assertEqual([e['segment_id'] for e in result['evidence']],['a','b'])

    def test_rejecting_a_city_also_conflicts_with_a_place_qualified_by_that_city(self):
        lines=[{'id':'a','seat':'driver','text':'서울시는 싫어'},
               {'id':'b','seat':'passenger','text':'서울 강남역 가자'}]
        result=resolve_search_intent(lines)
        self.assertIsNone(result['region'])
        self.assertTrue(result['needs_area_choice'])
        self.assertEqual([e['segment_id'] for e in result['evidence']],['a','b'])

    def test_same_speaker_can_explicitly_change_a_city_rejection_to_a_specific_place(self):
        result=resolve_search_intent(self.lines('서울은 싫어','아니 서울 강남역 가자'))
        self.assertEqual(result['region'],'서울 강남역')
        self.assertFalse(result['needs_area_choice'])

    def test_latest_city_rejection_removes_the_same_seats_earlier_station_choice(self):
        result=resolve_search_intent(self.lines('서울 강남역 가자','서울시는 싫어'))
        self.assertIsNone(result['region'])
        self.assertTrue(result['needs_area_choice'])
        self.assertEqual(result['candidate_regions'],[])
        self.assertEqual([e['segment_id'] for e in result['evidence']],['1'])

    def test_unknown_seat_correction_preserves_possible_other_speaker(self):
        result=resolve_search_intent(self.lines('부산 가자','아니다 강릉 가자',seat='unknown'))
        self.assertTrue(result['needs_area_choice'])

    def test_rejected_destination_from_another_seat_needs_a_choice(self):
        lines=[{'id':'a','seat':'driver','text':'부산 가자'},
               {'id':'b','seat':'passenger','text':'부산은 싫어'}]
        result=resolve_search_intent(lines)
        self.assertIsNone(result['region'])
        self.assertTrue(result['needs_area_choice'])

    def test_food_restriction_does_not_reject_the_region(self):
        lines=[{'id':'a','seat':'driver','text':'부산 바다 보러 가자'},
               {'id':'b','seat':'passenger','text':'부산에서 고기는 못 먹어'}]
        result=resolve_search_intent(lines)
        self.assertEqual(result['region'],'부산')
        self.assertFalse(result['needs_area_choice'])

    def test_not_wanting_to_visit_is_a_region_rejection(self):
        lines=[{'id':'a','seat':'driver','text':'부산 가자'},
               {'id':'b','seat':'passenger','text':'부산에는 안 가'}]
        self.assertTrue(resolve_search_intent(lines)['needs_area_choice'])

    def test_only_rejected_regions_require_a_choice_even_with_anywhere_or_nearby(self):
        for text in ('서울은 싫어. 다른 데 가자', '충주 말고 어디든',
                     '충주 말고 어디든 상관없어. 근처 가자'):
            with self.subTest(text=text):
                result=resolve_search_intent(self.lines(text))
                self.assertIsNone(result['region'])
                self.assertTrue(result['needs_area_choice'])
                self.assertEqual(result['candidate_regions'],[])
                self.assertIn('가고 싶은 지역',result['message'])
                self.assertEqual(result['evidence'][0]['text'],text)

    def test_later_anywhere_preference_does_not_erase_a_region_rejection(self):
        result=resolve_search_intent(self.lines('충주에는 안 가','어디든 상관없어. 가까운 곳 가자'))
        self.assertTrue(result['needs_area_choice'])
        self.assertIsNone(result['region'])
        self.assertEqual(result['candidate_regions'],[])
        self.assertIn('0',[item['segment_id'] for item in result['evidence']])

    def test_explicit_other_region_can_resolve_a_negative_only_request(self):
        result=resolve_search_intent(self.lines('충주 말고 어디든','그럼 부산 가자'))
        self.assertEqual(result['region'],'부산')
        self.assertFalse(result['needs_area_choice'])

    def test_destination_proposals_do_not_require_the_word_go(self):
        for text,region in (('부산은 어때?','부산'),('부산으로 할까?','부산'),
                            ('서울 강남역 쪽 좋겠다','서울 강남역')):
            with self.subTest(text=text):
                result=resolve_search_intent(self.lines(text))
                self.assertEqual(result['region'],region)
                self.assertFalse(result['needs_area_choice'])

    def test_explicit_correction_between_proposals_uses_only_the_latest_quote(self):
        result=resolve_search_intent(self.lines('부산은 어때?','아니 강릉으로 할까?'))
        self.assertEqual(result['region'],'강릉')
        self.assertEqual([e['segment_id'] for e in result['evidence']],['1'])

    def test_food_named_after_a_city_does_not_set_the_travel_region(self):
        for text in ('부산 돼지국밥 먹자','전주 비빔밥 먹자','춘천 닭갈비 먹고 싶어'):
            with self.subTest(text=text):
                result=resolve_search_intent(self.lines(text))
                self.assertIsNone(result['region'])
                self.assertEqual(result['area_source'],'unspecified')
                self.assertFalse(result['needs_area_choice'])

    def test_explicit_location_still_qualifies_a_food_or_drink_activity(self):
        for text in ('부산에서 돼지국밥 먹자','부산으로 국밥 먹으러 가자',
                     '부산 가서 돼지국밥 먹자','부산 바다 보러 가서 밥 먹자'):
            with self.subTest(text=text):
                self.assertEqual(resolve_search_intent(self.lines(text))['region'],'부산')

    def test_unknown_place_proposal_requires_confirmation_not_a_guessed_city(self):
        result=resolve_search_intent(self.lines('해운대 쪽 좋겠다'))
        self.assertIsNone(result['region'])
        self.assertTrue(result['needs_area_choice'])
        self.assertEqual(result['candidate_regions'],[])
        self.assertEqual(result['area_source'],'conversation')
        self.assertEqual(result['evidence'][0]['text'],'해운대 쪽 좋겠다')

    def test_generic_venue_or_adjective_direction_is_not_an_unknown_place(self):
        for text in ('바다 쪽 좋겠다','한식 쪽 좋겠다','조용한 쪽 좋겠다'):
            with self.subTest(text=text):
                result=resolve_search_intent(self.lines(text))
                self.assertFalse(result['needs_area_choice'])
                self.assertEqual(result['area_source'],'unspecified')

    def test_concrete_later_region_resolves_the_same_seats_unknown_place(self):
        result=resolve_search_intent(self.lines('해운대 쪽 좋겠다','부산 해운대구로 가자'))
        self.assertEqual(result['region'],'부산 해운대구')
        self.assertFalse(result['needs_area_choice'])

    def test_correction_to_an_unknown_place_does_not_keep_the_old_city(self):
        result=resolve_search_intent(self.lines('부산으로 할까?','아니 해운대 쪽 좋겠다'))
        self.assertIsNone(result['region'])
        self.assertTrue(result['needs_area_choice'])
        self.assertEqual(result['candidate_regions'],[])
        self.assertEqual([e['segment_id'] for e in result['evidence']],['1'])

    def test_different_seats_proposals_still_need_a_choice(self):
        lines=[{'id':'a','seat':'driver','text':'부산은 어때?'},
               {'id':'b','seat':'passenger','text':'강릉으로 할까?'}]
        result=resolve_search_intent(lines)
        self.assertTrue(result['needs_area_choice'])
        self.assertEqual(set(result['candidate_regions']),{'부산','강릉'})

    def test_conditional_venue_words_are_not_myeon_place_names(self):
        for text in ('밥 먹을 수 있는 식당이면 좋겠어.', '카페면좋겠어',
                     '실내면좋겠어', '공원이면 좋겠다', '조용하면 좋겠다'):
            with self.subTest(text=text):
                result=resolve_search_intent(self.lines(text))
                self.assertIsNone(result['region'])
                self.assertEqual(result['candidate_regions'],[])
                self.assertFalse(result['needs_area_choice'])
                self.assertEqual(result['area_source'],'unspecified')

    def test_nearby_conditional_is_nearby_not_a_myeon_name(self):
        result=resolve_search_intent(self.lines('가까우면좋겠다'))
        self.assertIsNone(result['region'])
        self.assertFalse(result['needs_area_choice'])
        self.assertEqual(result['area_source'],'nearby')

    def test_named_myeon_still_supports_travel_and_qualified_proposals(self):
        for text,region in (('서종면 가자','서종면'),
                            ('시험군 가상면 쪽 좋겠다','시험군 가상면')):
            with self.subTest(text=text):
                result=resolve_search_intent(self.lines(text))
                self.assertEqual(result['region'],region)
                self.assertFalse(result['needs_area_choice'])

    def test_origin_is_separate_from_the_destination(self):
        for text,origin,destination in (
                ('서울에서 출발해서 인천 바다 보러 가자.','서울','인천'),
                ('서울에서 강릉으로 가자.','서울','강릉'),
                ('출발지는 서울이야. 부산 가자.','서울','부산'),
                ('지금 서울시청에 있어. 인천 바다 보고 싶어.','서울시청','인천'),
                ('서울 강남역에서 출발해서 부산 바다 보러 가자.','서울 강남역','부산')):
            with self.subTest(text=text):
                result=resolve_search_intent(self.lines(text))
                self.assertEqual(result['origin_region'],origin)
                self.assertEqual(result['region'],destination)
                self.assertFalse(result['needs_area_choice'])
                self.assertEqual(result['origin_evidence'][0]['text'],text)

    def test_origin_alone_does_not_fix_the_destination_or_current_gps(self):
        result=resolve_search_intent(self.lines('서울에서 출발해서 2시간 이내로 가자.'))
        self.assertEqual(result['origin_region'],'서울')
        self.assertIsNone(result['region'])
        self.assertFalse(result['needs_area_choice'])
        self.assertEqual(result['area_source'],'unspecified')

    def test_time_budget_after_departure_area_establishes_origin_for_any_activity(self):
        for text,origin in (
                ('판교에서 차로 2시간 이내에 바다를 가고 싶어.','판교'),
                ('대전에서 한 시간 안에 갈 카페 찾아줘.','대전'),
                ('서울에서 운전 30분 이내에 갈 만한 식당 추천해줘.','서울'),
                ('수원에서 차로 40분 이내에 전시 보러 가자.','수원'),
                ('성남시 판교역에서 차로 두 시간 안에 갈 공원 찾아줘.','성남시 판교역'),
                ('판교에서 출발해서 바다 보고 싶어.','판교')):
            for seat in ('driver','unknown'):
                with self.subTest(text=text,seat=seat):
                    result=resolve_search_intent(self.lines(text,seat=seat))
                    self.assertEqual(result['origin_region'],origin)
                    self.assertIsNone(result['region'])
                    self.assertFalse(result['needs_area_choice'])
                    self.assertEqual(result['search_regions'],[])

    def test_departure_area_and_multiple_destinations_are_separate(self):
        result=resolve_search_intent(self.lines('판교에서 출발해서 화성이나 인천 쪽 바다 가자.',seat='unknown'))
        self.assertEqual(result['origin_region'],'판교')
        self.assertEqual(set(result['search_regions']),{'화성','인천'})
        self.assertTrue(result['multi_region'])
        self.assertFalse(result['needs_area_choice'])

    def test_home_history_and_non_travel_duration_do_not_establish_origin(self):
        for text in ('판교에 살아. 차로 두 시간 이내에 바다 가고 싶어.',
                     '어제 판교에서 차로 두 시간 이내에 바다 갔어.',
                     '작년에 대전에서 차로 한 시간 안에 바다 갔어.',
                     '서울에서 대기는 한 시간 이내인 식당 가자.',
                     '서울에서 식사 시간은 한 시간 이내로 하자.'):
            with self.subTest(text=text):
                self.assertIsNone(resolve_search_intent(self.lines(text))['origin_region'])

    def test_budget_word_is_not_mistaken_for_a_county(self):
        result=resolve_search_intent(self.lines('예산은 15만 원 안에서 식당 가자.'))
        self.assertIsNone(result['region'])
        self.assertFalse(result['needs_area_choice'])
        self.assertEqual(resolve_search_intent(self.lines('예산군으로 가자'))['region'],'예산군')

    def test_past_starting_place_is_not_used_as_current_origin(self):
        for text in ('어제 서울에서 출발했어.','지난 여행 때 부산에서 출발했어.',
                     '내 집은 서울이야.','서울에서 출발하지 말자.'):
            with self.subTest(text=text):
                result=resolve_search_intent(self.lines(text))
                self.assertIsNone(result['origin_region'])

    def test_conflicting_origins_are_not_silently_selected(self):
        lines=[{'id':'a','seat':'driver','text':'서울에서 출발하자.'},
               {'id':'b','seat':'passenger','text':'부산에서 출발하자.'},
               {'id':'c','seat':'rear1','text':'강릉 바다 보러 가자.'}]
        result=resolve_search_intent(lines)
        self.assertIsNone(result['origin_region'])
        self.assertTrue(result['needs_origin_choice'])
        self.assertEqual(set(result['origin_candidates']),{'서울','부산'})
        self.assertEqual(result['region'],'강릉')

    def test_latest_known_seat_origin_replaces_the_previous_one(self):
        result=resolve_search_intent(self.lines('서울에서 출발하자.','아니 수원에서 출발하자.','인천 바다 보러 가자.'))
        self.assertEqual(result['origin_region'],'수원')
        self.assertEqual(result['region'],'인천')
        self.assertEqual([e['segment_id'] for e in result['origin_evidence']],['1'])

    def test_long_dialogue_conditions_are_not_administrative_myeon_names(self):
        result=resolve_search_intent(self.lines('원주에서 출발해서 강릉 바다 보러 가자.',
            '한 사람당 2만 5천 원 이내면 좋겠어. 맑은 국물이면 좋겠다.',
            '칼국수 식당 후보도 보여주면 좋겠어. 세트 메뉴 추가하면 얼마인지 보고 싶어.',
            '메뉴가 여러 개인 집이면 좋겠다. 평탄한 길이면 좋겠어. 오래 걷는 일정은 아니면 좋겠어.'))
        self.assertEqual(result['region'],'강릉')
        self.assertEqual(result['origin_region'],'원주')
        self.assertFalse(result['needs_area_choice'])

    def test_origin_destination_and_desired_duration_in_one_message_do_not_conflict(self):
        result=resolve_search_intent(self.lines('서울에서 출발해서 부산으로 가자. 운전은 두 시간 이내였으면 좋겠어.'))
        self.assertEqual(result['origin_region'],'서울')
        self.assertEqual(result['region'],'부산')
        self.assertEqual(result['candidate_regions'],['부산'])
        self.assertFalse(result['needs_area_choice'])

    def test_natural_indoor_area_question_establishes_the_search_region(self):
        for text in ('서울 안에서 실내로 다닐 만한 데 없나?',
                     '서울안에서실내로다닐만한데없나?',
                     '서울에서 갈 만한 곳이 있을까?'):
            with self.subTest(text=text):
                result=resolve_search_intent(self.lines(text))
                self.assertEqual(result['region'],'서울')
                self.assertIsNone(result['origin_region'])

    def test_soft_destination_proposal_keeps_its_separate_departure(self):
        result=resolve_search_intent(self.lines('원주에서 출발하면 강릉은 괜찮지 않을까? 막히는지만 보자.'))
        self.assertEqual(result['origin_region'],'원주')
        self.assertEqual(result['region'],'강릉')
        self.assertFalse(result['needs_area_choice'])
        self.assertEqual(resolve_search_intent(self.lines('강릉 좋지. 두 시간 안에 가는지는 보자.'))['region'],'강릉')

    def test_current_visit_scopes_venues_without_claiming_gps_or_departure(self):
        for text,region in (
            ('대전까지 왔으니까 맛있는 거 먹어야죠. 30분 안에 가는 데로요.','대전'),
            ('생일 주인공, 부산까지 왔는데 저녁 뭐 먹고 싶어?','부산'),
            ('오늘 오후에는 뭐 할까? 제주 와서 계속 차만 탄 느낌인데.','제주')):
            with self.subTest(text=text):
                result=resolve_search_intent(self.lines(text))
                self.assertEqual(result['region'],region)
                self.assertIsNone(result['origin_region'])
                self.assertFalse(result['needs_area_choice'])
                self.assertEqual(result['evidence'][0]['text'],text)

    def test_historical_future_and_other_person_visits_do_not_set_current_area(self):
        for text in ('지난주 제주 갔었는데 좋더라.', '지난주 제주 와서 계속 차만 탔어.',
                     '어제 대전까지 왔는데 맛있는 거 먹자고 했어.',
                     '예전에 서울 안에서 갈 만한 데 없나 물었어.',
                     '다음 여행에 제주 와서 쉬면 좋겠어.',
                     '친구는 부산까지 왔는데 저녁 뭐 먹었대?',
                     '내 집은 부산이야.', '제주에 살고 있어.'):
            with self.subTest(text=text):
                result=resolve_search_intent(self.lines(text))
                self.assertIsNone(result['region'])
                self.assertIsNone(result['origin_region'])
                self.assertFalse(result['needs_area_choice'])

    def test_explicit_destination_has_priority_over_a_current_stop(self):
        result=resolve_search_intent(self.lines('대전까지 왔는데 어디서 먹을까?', '부산 바다 보러 가자.'))
        self.assertEqual(result['region'],'부산')
        self.assertEqual(result['candidate_regions'],['부산'])
        self.assertFalse(result['needs_area_choice'])

    def test_current_visit_ambiguity_and_rejection_are_not_overridden(self):
        lines=[{'id':'a','seat':'driver','text':'부산까지 왔는데 저녁 먹자.'},
               {'id':'b','seat':'passenger','text':'제주 와서 계속 차만 탔네.'}]
        result=resolve_search_intent(lines)
        self.assertTrue(result['needs_area_choice'])
        self.assertEqual(set(result['candidate_regions']),{'부산','제주'})
        # The rejection form already supported by the resolver remains binding.
        result=resolve_search_intent(self.lines('부산까지 왔는데 뭐 먹을까?', '부산에는 안 가.'))
        self.assertTrue(result['needs_area_choice'])
        self.assertIsNone(result['region'])

    def test_positive_question_does_not_turn_negative_approval_into_a_proposal(self):
        for text in ('강릉은 괜찮지 않아.', '부산은 좋지 않아.'):
            with self.subTest(text=text):
                self.assertIsNone(resolve_search_intent(self.lines(text))['region'])
        result=resolve_search_intent(self.lines('서울 안에서 갈 만한 데 없으니 부산 가자.'))
        self.assertEqual(result['region'],'부산')

    def test_all_natural_demo_dialogues_keep_their_intended_region_and_origin(self):
        from demo_conversations import SCENARIOS
        expected={'nearby-lunch':(None,'성수역'), 'seoul-rain':(None,'왕십리역'),
                  'gangneung-sea':(None,'판교역'), 'daejeon-family':(None,'대전역'),
                  'busan-friends':(None,'부산역'), 'jeju-walk':(None,'제주시청')}
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario['id']):
                lines=[{'id':str(i),'seat':seat,'text':text}
                       for i,(seat,text) in enumerate(scenario['dialogue'])]
                result=resolve_search_intent(lines)
                self.assertEqual((result['region'],result['origin_region']),expected[scenario['id']])
                self.assertFalse(result['needs_area_choice'])


if __name__=='__main__':
    unittest.main()
