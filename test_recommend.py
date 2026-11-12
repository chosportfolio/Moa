import unittest
import tempfile
import json
import hashlib
from collections import OrderedDict
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch
from recommend import extract, get_nominatim_beaches, get_nominatim_food_places, lookup_places, nominatim_search, recommend, summarize_conversation


class RecommendationTests(unittest.TestCase):
    def setUp(self):
        for name,value in [('NOMINATIM_MIN_INTERVAL',0),('_NOMINATIM_LAST_REQUEST',None),('_LOCATION_POI_CACHE',OrderedDict())]:
            patcher=patch('recommend.'+name,value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_unattributed_people_do_not_overwrite_each_others_exclusions(self):
        constraints, _ = extract([
            {'id':'one','seat':'unknown','text':'고기 싫어'},
            {'id':'two','seat':'unknown','text':'고기 좋아'},
        ])
        self.assertIn('고기',constraints[0]['dislikes'])
        self.assertIn('고기',constraints[0]['likes'])
    def segment(self, text, seat='driver', identifier='a'):
        return {'id':identifier, 'seat':seat, 'text':text}

    def test_negative_clause_does_not_reject_another_preference(self):
        conditions,_=extract([self.segment('카페는 좋고 고기는 싫어. 점심을 안 먹었어.')])
        self.assertIn('카페',conditions[0]['likes'])
        self.assertIn('식사',conditions[0]['likes'])
        self.assertIn('고기',conditions[0]['dislikes'])
        self.assertNotIn('카페',conditions[0]['dislikes'])

    def test_latest_preference_is_used_for_the_same_person(self):
        conditions,_=extract([self.segment('카페 좋아.'),self.segment('카페는 싫어.',identifier='b')])
        self.assertNotIn('카페',conditions[0]['likes'])
        self.assertIn('카페',conditions[0]['dislikes'])

    def test_explicit_change_of_plan_replaces_that_seats_previous_choice(self):
        lines=[self.segment('카페 가자.'),self.segment('아니다, 밥 먹자.',identifier='b')]
        conditions,_=extract(lines)
        self.assertEqual(conditions[0]['likes'],['식사'])
        self.assertNotIn('카페',conditions[0]['dislikes'])
        summary=summarize_conversation(lines)
        self.assertNotIn('카페',summary['text'])
        self.assertEqual([e['segment_id'] for e in summary['evidence']],['b'])

    def test_same_utterance_correction_preserves_its_complete_source_quote(self):
        line=self.segment('카페 가자. 아니다, 밥 먹자.')
        conditions,_=extract([line])
        self.assertEqual(conditions[0]['likes'],['식사'])
        summary=summarize_conversation([line])
        self.assertEqual(summary['evidence'][0]['text'],line['text'])

    def test_corrected_choice_does_not_erase_exclusions_or_facility_conditions(self):
        lines=[self.segment('카페 가자. 고기는 싫어. 주차하기 편해야 돼.'),
               self.segment('아니, 밥 먹자.',identifier='b')]
        conditions,_=extract(lines)
        self.assertEqual(conditions[0]['likes'],['식사'])
        self.assertEqual(conditions[0]['dislikes'],['고기'])
        self.assertIn('주차 정보 확인',conditions[0]['requirements'])

    def test_one_seats_correction_does_not_erase_another_seats_choice(self):
        lines=[self.segment('카페 가자.'),self.segment('밥 먹자.','passenger','b'),
               self.segment('아니다 공원 가자.',identifier='c')]
        conditions,_=extract(lines)
        self.assertEqual(conditions[0]['likes'],['산책'])
        self.assertEqual(conditions[1]['likes'],['식사'])
        summary=summarize_conversation(lines)
        self.assertEqual([e['segment_id'] for e in summary['evidence']],['b','c'])

    def test_sequence_or_alternative_word_alone_does_not_cancel_a_choice(self):
        for followup in ('그럼 밥 먹자.','이제 밥 먹자.','아니면 밥 먹자.'):
            with self.subTest(followup=followup):
                conditions,_=extract([self.segment('카페 가자.'),self.segment(followup,identifier='b')])
                self.assertIn('카페',conditions[0]['likes'])
                self.assertIn('식사',conditions[0]['likes'])

    def test_unknown_speakers_correction_is_not_assumed_to_be_the_same_person(self):
        lines=[self.segment('카페 가자.','unknown'),self.segment('아니다 밥 먹자.','unknown','b')]
        conditions,_=extract(lines)
        self.assertIn('카페',conditions[0]['likes'])
        self.assertIn('식사',conditions[0]['likes'])
        self.assertEqual([e['segment_id'] for e in summarize_conversation(lines)['evidence']],['a','b'])

    def test_correction_without_a_new_choice_does_not_guess_what_was_cancelled(self):
        conditions,_=extract([self.segment('카페 가자.'),self.segment('아니야.',identifier='b')])
        self.assertEqual(conditions[0]['likes'],['카페'])

    def test_correction_marker_does_not_carry_past_a_fresh_affirmed_preference(self):
        conditions,_=extract([self.segment('카페 가자.'),self.segment('아니 카페는 좋고 밥도 먹자.',identifier='b')])
        self.assertIn('카페',conditions[0]['likes'])
        self.assertIn('식사',conditions[0]['likes'])

    def test_recommendation_uses_the_replacement_choice_and_its_quote(self):
        elements=[{'type':'node','id':1,'lat':37,'lon':127,'tags':{'name':'커피','amenity':'cafe'}},
                  {'type':'node','id':2,'lat':37.001,'lon':127,'tags':{'name':'식당','amenity':'restaurant'}}]
        lines=[self.segment('카페 가자.'),self.segment('아니다 밥 먹자.',identifier='b')]
        result=self.recommend_places(elements,{'lat':37,'lon':127},lines)
        self.assertEqual([place['id'] for place in result],['node/2'])
        self.assertEqual(result[0]['reasons'][0]['quote'],'아니다 밥 먹자.')

    def test_negative_drink_clause_does_not_reject_following_meal(self):
        conditions,_=extract([self.segment('커피는 안 마시고 밥 먹자.')])
        self.assertIn('카페',conditions[0]['dislikes'])
        self.assertIn('식사',conditions[0]['likes'])
        self.assertNotIn('식사',conditions[0]['dislikes'])

    def test_want_to_eat_is_not_split_before_its_negative(self):
        conditions,_=extract([self.segment('고기 먹고 싶지 않아. 카페 가자.')])
        self.assertIn('고기',conditions[0]['dislikes'])
        self.assertNotIn('고기',conditions[0]['likes'])
        self.assertIn('카페',conditions[0]['likes'])

    def test_incidental_visit_and_double_negative_do_not_invent_preferences(self):
        conditions,_=extract([self.segment('아까 카페에서 커피 마셨어. 한식이 싫지는 않아.')])
        self.assertEqual(conditions[0]['likes'],[])
        self.assertEqual(conditions[0]['dislikes'],[])

    def test_caffeine_restriction_does_not_veto_every_cafe(self):
        lines=[self.segment('카페 가자. 카페인은 못 마셔.')]
        conditions,_=extract(lines)
        self.assertIn('카페',conditions[0]['likes'])
        self.assertNotIn('카페',conditions[0]['dislikes'])
        self.assertIn('카페인 없는 음료 확인',conditions[0]['requirements'])
        summary=summarize_conversation(lines)
        self.assertEqual(summary['evidence'][0]['segment_id'],'a')

    def test_changed_preference_is_not_overwritten_by_a_neutral_mention(self):
        conditions,_=extract([self.segment('카페 좋아.'),self.segment('아까 카페 앞을 지나왔어.',identifier='b')])
        self.assertEqual(conditions[0]['likes'],['카페'])

    def test_negated_liking_is_an_exclusion(self):
        for text in ('카페 안 좋아해.', '카페를 좋아하지 않아.', '한식 먹고 싶지는 않아.'):
            with self.subTest(text=text):
                conditions,_=extract([self.segment(text)])
                self.assertFalse(conditions[0]['likes'])
                self.assertTrue(conditions[0]['dislikes'])

    def test_not_hungry_is_not_a_meal_request(self):
        conditions,_=extract([self.segment('배고프지는 않아. 카페 가자.')])
        self.assertNotIn('식사',conditions[0]['likes'])
        self.assertIn('카페',conditions[0]['likes'])

    def test_short_candidates_include_other_passengers_coffee_preference(self):
        places={'region':'테스트역','fetched_at':123,'elements':[
            {'type':'node','id':1,'lat':37,'lon':127,'tags':{'name':'가 한식당','amenity':'restaurant','cuisine':'korean'}},
            {'type':'node','id':2,'lat':37,'lon':127,'tags':{'name':'나 국밥','amenity':'restaurant','cuisine':'korean;barbecue'}},
            {'type':'node','id':3,'lat':37,'lon':127,'tags':{'name':'다 한식당','amenity':'restaurant','cuisine':'korean;japanese'}},
            {'type':'node','id':4,'lat':37,'lon':127,'tags':{'name':'라 커피','amenity':'cafe'}}]}
        with patch('recommend.lookup_places',return_value=places):
            result=recommend([self.segment('밥 먹자. 한식 좋아.'),self.segment('커피 마시고 싶어.','passenger','b')],'테스트역',Path('.'))
        ids=[p['id'] for p in result['recommendations']]
        self.assertEqual(ids[0],'node/1')
        self.assertEqual(ids[1],'node/4')
        self.assertEqual(len(ids),4)
        cafe=result['recommendations'][1]
        self.assertEqual(cafe['reasons'][0]['seat'],'passenger')
        self.assertIn('운전자', ' '.join(cafe['cautions']))

    def test_diversity_never_reintroduces_a_rejected_category(self):
        places={'region':'테스트역','fetched_at':123,'elements':[
            {'type':'node','id':1,'lat':37,'lon':127,'tags':{'name':'한식당','amenity':'restaurant','cuisine':'korean'}},
            {'type':'node','id':2,'lat':37,'lon':127,'tags':{'name':'카페','amenity':'cafe'}}]}
        with patch('recommend.lookup_places',return_value=places):
            result=recommend([self.segment('한식 좋아. 카페 싫어.'),self.segment('카페 좋아.','passenger','b')],'테스트역',Path('.'))
        self.assertEqual([p['id'] for p in result['recommendations']],['node/1'])

    def recommend_places(self, elements, center, segments=None):
        places={'region':'테스트역','fetched_at':123,'center':center,'elements':elements}
        with patch('recommend.lookup_places',return_value=places):
            return recommend(segments or [self.segment('카페 가자.')],'테스트역',Path('.'))['recommendations']

    def test_invalid_or_missing_coordinates_are_not_offered_for_navigation(self):
        invalid=[{}, {'lat':None,'lon':127}, {'lat':37,'lon':float('nan')},
                 {'lat':float('inf'),'lon':127}, {'lat':91,'lon':127},
                 {'lat':37,'lon':181}, {'lat':True,'lon':127},
                 {'lat':37,'lon':'bad'}, {'center':None}, {'center':[]},
                 {'center':{'lat':37}}]
        elements=[{'type':'node','id':i,'tags':{'name':f'카페 {i}','amenity':'cafe'},**coords}
                  for i,coords in enumerate(invalid)]
        elements.append({'type':'way','id':99,'center':{'lat':'37.01','lon':'127'},
                         'tags':{'name':'유효한 카페','amenity':'cafe'}})
        result=self.recommend_places(elements,{'lat':37,'lon':127})
        self.assertEqual([p['id'] for p in result],['way/99'])
        self.assertEqual(result[0]['latitude'],37.01)
        self.assertAlmostEqual(result[0]['distance_m'],1112,delta=1)
        self.assertEqual(result[0]['distance_basis'],'search_center')

    def test_equal_preferences_rank_by_search_center_distance_before_name(self):
        elements=[{'type':'node','id':1,'lat':37.01,'lon':127,'tags':{'name':'가 먼 카페','amenity':'cafe'}},
                  {'type':'node','id':2,'lat':37.001,'lon':127,'tags':{'name':'하 가까운 카페','amenity':'cafe'}},
                  {'type':'node','id':3,'lat':37.005,'lon':127,'tags':{'name':'나 중간 카페','amenity':'cafe'}}]
        result=self.recommend_places(elements,{'lat':37,'lon':127})
        self.assertEqual([p['id'] for p in result],['node/2','node/3','node/1'])
        self.assertEqual([p['distance_basis'] for p in result],['search_center']*3)
        self.assertEqual([p['distance_m'] for p in result],sorted(p['distance_m'] for p in result))

    def test_distance_does_not_replace_preference_quality_or_other_passengers(self):
        elements=[{'type':'node','id':1,'lat':37.02,'lon':127,'tags':{'name':'한식당','amenity':'restaurant','cuisine':'korean'}},
                  {'type':'node','id':2,'lat':37.0001,'lon':127,'tags':{'name':'가까운 식당','amenity':'restaurant'}},
                  {'type':'node','id':3,'lat':37.03,'lon':127,'tags':{'name':'먼 카페','amenity':'cafe'}}]
        result=self.recommend_places(elements,{'lat':37,'lon':127},[
            self.segment('밥 먹자. 한식 좋아.'),self.segment('카페 좋아.','passenger','b')])
        self.assertEqual([p['id'] for p in result],['node/1','node/3'])

    def test_separate_same_brand_branches_are_limited_to_two_alternatives(self):
        elements=[{'type':'node','id':i,'lat':37+i*.003,'lon':127,
                   'tags':{'name':'같은 커피','amenity':'cafe'}} for i in (3,2,1)]
        result=self.recommend_places(elements,{'lat':37,'lon':127})
        self.assertEqual([p['id'] for p in result],['node/1','node/2'])

    def test_duplicate_map_entities_keep_the_nearer_representative(self):
        elements=[{'type':'node','id':1,'lat':37.0002,'lon':127,'tags':{'name':'같은 커피','amenity':'cafe'}},
                  {'type':'way','id':2,'center':{'lat':37.0001,'lon':127},'tags':{'name':'같은 커피','amenity':'cafe'}},
                  {'type':'node','id':3,'lat':37.0001,'lon':127,'tags':{'name':'다른 커피','amenity':'cafe'}}]
        elements.append(elements[1].copy())
        result=self.recommend_places(elements,{'lat':37,'lon':127})
        self.assertEqual({p['id'] for p in result},{'way/2','node/3'})

    def test_missing_or_invalid_search_center_does_not_fabricate_distance(self):
        elements=[{'type':'node','id':1,'lat':37,'lon':127,'tags':{'name':'카페','amenity':'cafe'}}]
        for center in (None,{},[],{'lat':float('nan'),'lon':127},{'lat':37,'lon':True}):
            with self.subTest(center=center):
                result=self.recommend_places(elements,center)
                self.assertEqual(len(result),1)
                self.assertIsNone(result[0]['distance_m'])
                self.assertIsNone(result[0]['distance_basis'])

    def test_rejected_category_is_never_recommended_and_facilities_are_not_invented(self):
        places={'region':'테스트역','fetched_at':123,'elements':[
            {'type':'node','id':1,'lat':37,'lon':127,'tags':{'name':'테스트 카페','amenity':'cafe'}},
            {'type':'node','id':2,'lat':37,'lon':127,'tags':{'name':'테스트 한식당','amenity':'restaurant','cuisine':'korean'}}]}
        with patch('recommend.lookup_places',return_value=places):
            result=recommend([self.segment('카페 싫어. 한식 먹고 싶어. 주차도 편했으면 좋겠어.')],'테스트역',Path('.'))
        self.assertEqual([p['id'] for p in result['recommendations']],['node/2'])
        rec=result['recommendations'][0]
        self.assertIn('주차 정보 확인',rec['cautions'])
        self.assertFalse(rec['verified'])
        self.assertEqual(rec['reasons'][0]['quote'],'카페 싫어. 한식 먹고 싶어. 주차도 편했으면 좋겠어.')
        self.assertIn('한식',rec['reason_summary'])
        self.assertIn('카페',result['recommendation_summary']['text'])

    def test_summary_cites_current_preferences_and_preserves_unknown_conflicts(self):
        lines=[self.segment('카페 좋아.','driver','a'),self.segment('카페 싫어.','driver','b'),
               self.segment('한식 좋아.','unknown','c'),self.segment('한식 싫어.','unknown','d')]
        summary=summarize_conversation(lines)
        self.assertNotIn('a',[e['segment_id'] for e in summary['evidence']])
        self.assertEqual({e['segment_id'] for e in summary['evidence']},{'b','c','d'})
        self.assertIn('제외 의견을 우선',' '.join(summary['limitations']))
        self.assertTrue(all(e['text'] in [s['text'] for s in lines] for e in summary['evidence']))

    def test_summary_does_not_invent_preferences_for_unrelated_conversation(self):
        summary=summarize_conversation([self.segment('오늘 회의는 세 시에 끝났어.')])
        self.assertEqual(summary['bullets'],[])
        self.assertEqual(summary['evidence'],[])
        self.assertIn('찾지 못했어요',summary['text'])

    def test_negated_location_conditions_are_not_reversed_into_preferences(self):
        summary=summarize_conversation([self.segment('실내는 싫어. 가까운 곳 말고 멀리 가자.')])
        self.assertNotIn('실내 활동 선호',summary['text'])
        self.assertNotIn('가까운 장소 선호',summary['text'])
        self.assertIn('실내·야외 조건 확인',summary['text'])
        self.assertIn('이동 거리 조건 확인',summary['text'])

    def test_rain_mention_does_not_invent_an_indoor_preference(self):
        conditions,_=extract([self.segment('비가 안 오니까 공원 가자.')])
        self.assertEqual(conditions[0]['likes'],['산책'])
        self.assertFalse(conditions[0]['requirements'])

    def test_explicitly_unneeded_parking_is_not_presented_as_a_requirement(self):
        conditions,_=extract([self.segment('카페 가자. 주차는 필요 없어.')])
        self.assertEqual(conditions[0]['likes'],['카페'])
        self.assertNotIn('주차 정보 확인',conditions[0]['requirements'])

    def test_empty_search_does_not_claim_places_were_chosen(self):
        places={'region':'테스트역','fetched_at':123,'elements':[]}
        with patch('recommend.lookup_places',return_value=places):
            result=recommend([self.segment('카페 가자.')],'테스트역',Path('.'))
        self.assertEqual(result['recommendations'],[])
        self.assertNotIn('골랐어요',result['recommendation_summary']['title'])
        self.assertNotIn('걸렀어요',result['recommendation_summary']['text'])
        self.assertIn('찾지 못했어요',result['notice'])

    def test_conflicting_seats_with_all_candidates_excluded_do_not_claim_success(self):
        places={'region':'테스트역','fetched_at':123,'elements':[
            {'type':'node','id':1,'lat':37,'lon':127,'tags':{'name':'카페','amenity':'cafe'}}]}
        with patch('recommend.lookup_places',return_value=places):
            result=recommend([self.segment('카페 가자.'),self.segment('카페 싫어.','passenger','b')],'테스트역',Path('.'))
        self.assertFalse(result['recommendations'])
        self.assertNotIn('골랐어요',result['recommendation_summary']['title'])
        self.assertIn('제외 의견을 우선',' '.join(result['recommendation_summary']['limitations']))

    def test_cafe_name_does_not_prove_it_serves_a_korean_meal(self):
        elements=[{'type':'node','id':1,'lat':37,'lon':127,
                   'tags':{'name':'국밥거리 커피','amenity':'cafe'}}]
        result=self.recommend_places(elements,{'lat':37,'lon':127},[self.segment('한식 먹고 싶어.')])
        self.assertEqual(result,[])

    def test_name_based_cuisine_reason_is_identified_as_a_name_clue(self):
        elements=[{'type':'node','id':1,'lat':37,'lon':127,
                   'tags':{'name':'동네 국밥','amenity':'restaurant'}}]
        result=self.recommend_places(elements,{'lat':37,'lon':127},[self.segment('한식 먹고 싶어.')])
        self.assertEqual(len(result),1)
        self.assertIn('이름',result[0]['reason_summary'])
        self.assertIn('메뉴 확인',' '.join(result[0]['cautions']))
        self.assertNotIn('장소 분류가 맞아',result[0]['reason_summary'])

    def test_missing_menu_data_does_not_confirm_a_food_exclusion(self):
        elements=[{'type':'node','id':1,'lat':37,'lon':127,
                   'tags':{'name':'동네 식당','amenity':'restaurant'}}]
        result=self.recommend_places(elements,{'lat':37,'lon':127},[self.segment('고기 싫어.')])
        self.assertEqual(len(result),1)
        self.assertNotIn('제외 조건에 해당하지 않는',result[0]['reason_summary'])
        self.assertIn('고기', ' '.join(result[0]['cautions']))
        self.assertIn('메뉴 확인',' '.join(result[0]['cautions']))

    def test_exact_dishes_and_raw_fish_do_not_match_unrelated_korean_words(self):
        conditions,_=extract([self.segment('칼국수랑 파전 먹고 싶어. 회도 좋아.')])
        self.assertTrue({'칼국수','파전','회'}.issubset(conditions[0]['likes']))
        for text in ('회의 가자.','회사 밥 먹자.','대회가 좋아.','문화회관 가자.'):
            with self.subTest(text=text):
                conditions,_=extract([self.segment(text)])
                self.assertNotIn('회',conditions[0]['likes'])
                self.assertNotIn('회',conditions[0]['dislikes'])

    def test_menu_negation_and_correction_keep_each_seats_actual_choice(self):
        lines=[self.segment('칼국수 먹자. 파전도 좋아.'),
               self.segment('회 먹고 싶어.','rear2','b'),
               self.segment('회는 못 먹고 칼국수는 좋아.','passenger','c'),
               self.segment('아니다 회는 싫어. 파전 먹자.','rear2','d')]
        conditions,evidence=extract(lines)
        profiles={p['seat']:p for p in conditions}
        self.assertEqual(set(profiles['driver']['likes']),{'칼국수','파전'})
        self.assertEqual(profiles['passenger']['likes'],['칼국수'])
        self.assertIn('회',profiles['passenger']['dislikes'])
        self.assertNotIn('회',profiles['rear2']['likes'])
        self.assertIn('파전',profiles['rear2']['likes'])
        self.assertTrue(all(e['text'] in [s['text'] for s in lines] for e in evidence))

    def test_hours_and_minutes_are_explicit_limits_not_elapsed_driving_or_meal_time(self):
        for text,minutes in [('편도 2시간 이내로 갔으면 좋겠어.',120),
                             ('편도 1.5시간 이내로 가자.',90),
                             ('운전은 두 시간 반 안에 끝냈으면 좋겠어.',150),
                             ('이동은 1시간 30분 이내로 하자.',90),
                             ('최대 45분 운전하고 싶어.',45),
                             ('이동은 90분을 넘기지 않았으면 좋겠어.',90)]:
            with self.subTest(text=text):
                conditions,_=extract([self.segment(text)])
                self.assertEqual(conditions[0]['travel_max_minutes'],minutes)
                self.assertIn(f'이동 {minutes}분 이내',conditions[0]['requirements'])
        for text in ('운전을 2시간 했어.','밥 먹는데 30분 이내면 충분해.','2시간 전에 출발했어.'):
            with self.subTest(text=text):
                conditions,_=extract([self.segment(text)])
                self.assertNotIn('travel_max_minutes',conditions[0])

    def test_latest_known_seat_travel_limit_replaces_the_earlier_one(self):
        conditions,_=extract([self.segment('운전은 2시간 이내로 하자.'),
                              self.segment('아니다 90분 이내로 가자.',identifier='b')])
        self.assertEqual(conditions[0]['travel_max_minutes'],90)
        self.assertNotIn('이동 120분 이내',conditions[0]['requirements'])

    def test_specific_dish_beats_generic_korean_category_and_cites_name_uncertainty(self):
        elements=[{'type':'node','id':1,'lat':37,'lon':127,'tags':{'name':'가까운 한식당','amenity':'restaurant','cuisine':'korean'}},
                  {'type':'node','id':2,'lat':37.01,'lon':127,'tags':{'name':'멀리 칼국수','amenity':'restaurant','cuisine':'korean'}},
                  {'type':'node','id':3,'lat':37.02,'lon':127,'tags':{'name':'면가','amenity':'restaurant','cuisine':'kalguksu'}}]
        result=self.recommend_places(elements,{'lat':37,'lon':127},[self.segment('칼국수 먹고 싶어.')])
        self.assertEqual([p['id'] for p in result],['node/3','node/2'])
        self.assertEqual(result[0]['menu_matches'],[{'topic':'칼국수','basis':'registered'}])
        self.assertEqual(result[1]['evidence_strength'],'name_hint')
        self.assertEqual(result[1]['evidence'][0]['quote'],'칼국수 먹고 싶어.')
        self.assertIn('메뉴 확인',' '.join(result[1]['cautions']))

    def test_requested_cuisine_does_not_fill_alternatives_with_generic_restaurants(self):
        elements = [
            {'type':'node','id':1,'lat':37,'lon':127,
             'tags':{'name':'개천식당 (Gaecheonsikdang)','amenity':'restaurant','cuisine':'떡만두국, 만두, 냉면'}},
            {'type':'node','id':2,'lat':37.001,'lon':127,
             'tags':{'name':'테라스키친','amenity':'restaurant','cuisine':'pasta'}},
            {'type':'node','id':3,'lat':37.002,'lon':127,
             'tags':{'name':'양식 가게','amenity':'restaurant','cuisine':'western'}},
            {'type':'node','id':4,'lat':37,'lon':127,
             'tags':{'name':'조용한 카페','amenity':'cafe'}},
        ]
        result = self.recommend_places(elements, {'lat':37,'lon':127}, [
            self.segment('식사하자. 파스타 먹고 싶어.'),
            self.segment('밥 먹고 카페도 가자.', 'passenger', 'b')])
        self.assertEqual({p['id'] for p in result}, {'node/2','node/4'})
        restaurant = next(p for p in result if p['id']=='node/2')
        self.assertIn({'topic':'파스타','basis':'registered'}, restaurant['menu_matches'])
        self.assertNotIn('메뉴 확인 필요', restaurant['category'])

    def test_same_public_place_data_is_reranked_for_each_conversation_without_a_shop_blacklist(self):
        elements = [
            {'type':'node','id':1,'lat':37,'lon':127,
             'tags':{'name':'개천식당 (Gaecheonsikdang)','amenity':'restaurant'}},
            {'type':'node','id':2,'lat':37.001,'lon':127,
             'tags':{'name':'행복칼국수','amenity':'restaurant','cuisine':'korean'}},
            {'type':'node','id':3,'lat':37.002,'lon':127,
             'tags':{'name':'테라스키친','amenity':'restaurant','cuisine':'pasta'}},
        ]
        for text, expected in [('식사하자. 칼국수 먹자.', {'node/2'}),
                               ('식사하자. 파스타 먹자.', {'node/3'}),
                               ('식사하자.', {'node/1','node/2','node/3'})]:
            with self.subTest(text=text):
                result = self.recommend_places(elements, {'lat':37,'lon':127}, [self.segment(text)])
                self.assertEqual({p['id'] for p in result}, expected)

    def test_missing_requested_dish_does_not_turn_generic_meals_into_recommendations(self):
        elements = [{'type':'node','id':1,'lat':37,'lon':127,
                     'tags':{'name':'동네 식당','amenity':'restaurant'}}]
        result = self.recommend_places(elements, {'lat':37,'lon':127}, [self.segment('식사하자. 칼국수 먹자.')])
        self.assertEqual(result, [])

    def test_missing_food_condition_keeps_other_requested_activity_and_explains_empty_food_results(self):
        elements = [{'type':'node','id':1,'lat':37,'lon':127,
                     'tags':{'name':'동네 식당','amenity':'restaurant'}},
                    {'type':'node','id':2,'lat':37,'lon':127,
                     'tags':{'name':'조용한 카페','amenity':'cafe'}}]
        lines = [self.segment('식사하자. 파스타 먹자. 카페도 가자.')]
        with patch('recommend.lookup_places', return_value={
                'elements':elements, 'center':{'lat':37,'lon':127}, 'fetched_at':123}):
            result = recommend(lines, '테스트 지역', Path('.'))
        self.assertEqual([p['id'] for p in result['recommendations']], ['node/2'])
        self.assertIn('파스타 조건에 맞는 음식점', result['notice'])
        self.assertIn('검색 지역이나 범위를 조정', result['notice'])

    def test_specific_pasta_request_requires_pasta_evidence_not_only_an_italian_category(self):
        elements=[{'type':'node','id':1,'lat':37,'lon':127,
                   'tags':{'name':'가까운 이탈리안','amenity':'restaurant','cuisine':'italian'}},
                  {'type':'node','id':2,'lat':37.001,'lon':127,
                   'tags':{'name':'피자 전문점','amenity':'restaurant','cuisine':'pizza'}},
                  {'type':'node','id':3,'lat':37.002,'lon':127,
                   'tags':{'name':'음식점','amenity':'restaurant','cuisine':'pasta'}}]
        result=self.recommend_places(elements,{'lat':37,'lon':127},[self.segment('밥 먹자. 파스타 먹고 싶어.')])
        self.assertEqual([p['id'] for p in result],['node/3'])
        self.assertEqual(result[0]['menu_matches'],[{'topic':'파스타','basis':'registered'}])
        self.assertIn('파스타 종류가 등록',result[0]['reasons'][0]['text'])
        self.assertTrue(all(e['quote']=='밥 먹자. 파스타 먹고 싶어.' for e in result[0]['evidence']))

    def test_disliking_one_japanese_dish_does_not_exclude_another_passengers_sushi(self):
        lines=[self.segment('돈까스는 싫어.'),self.segment('초밥 먹고 싶어.','passenger','b')]
        constraints,_=extract(lines)
        self.assertEqual(constraints[0]['dislikes'],['돈까스'])
        elements=[{'type':'node','id':1,'lat':37,'lon':127,
                   'tags':{'name':'음식점','amenity':'restaurant','cuisine':'sushi'}},
                  {'type':'node','id':2,'lat':37.001,'lon':127,
                   'tags':{'name':'돈까스집','amenity':'restaurant','cuisine':'tonkatsu'}},
                  {'type':'node','id':3,'lat':37.002,'lon':127,
                   'tags':{'name':'일식집','amenity':'restaurant','cuisine':'japanese'}}]
        result=self.recommend_places(elements,{'lat':37,'lon':127},lines)
        self.assertEqual([p['id'] for p in result],['node/1'])
        rejected=self.recommend_places(elements,{'lat':37,'lon':127},[
            self.segment('일식 싫어.'),self.segment('초밥 먹자.','passenger','b')])
        self.assertEqual(rejected,[])

    def test_retracting_a_specific_dish_does_not_leave_an_implicit_cuisine_request(self):
        lines=[self.segment('파스타 먹자.'),self.segment('파스타는 싫어졌어. 카페 가자.',identifier='b')]
        elements=[{'type':'node','id':1,'lat':37,'lon':127,
                   'tags':{'name':'피자 식당','amenity':'restaurant','cuisine':'pizza'}},
                  {'type':'node','id':2,'lat':37,'lon':127,
                   'tags':{'name':'동네 카페','amenity':'cafe'}}]
        result=self.recommend_places(elements,{'lat':37,'lon':127},lines)
        self.assertEqual([p['id'] for p in result],['node/2'])

    def test_explicit_broad_cuisine_choice_is_not_narrowed_to_just_one_also_liked_dish(self):
        lines=[self.segment('양식은 아무거나 좋아. 파스타도 좋아.')]
        elements=[{'type':'node','id':1,'lat':37,'lon':127,
                   'tags':{'name':'이탈리안','amenity':'restaurant','cuisine':'italian'}},
                  {'type':'node','id':2,'lat':37.001,'lon':127,
                   'tags':{'name':'파스타','amenity':'restaurant','cuisine':'pasta'}}]
        self.assertEqual({p['id'] for p in self.recommend_places(elements,{'lat':37,'lon':127},lines)},
                         {'node/1','node/2'})

    def test_exact_menu_revisions_remove_old_dish_and_retain_the_current_quote(self):
        lines=[self.segment('파스타 먹자.'),self.segment('아니다 피자 먹자.',identifier='new')]
        elements=[{'type':'node','id':1,'lat':37,'lon':127,
                   'tags':{'name':'파스타','amenity':'restaurant','cuisine':'pasta'}},
                  {'type':'node','id':2,'lat':37.001,'lon':127,
                   'tags':{'name':'피자','amenity':'restaurant','cuisine':'pizza'}}]
        result=self.recommend_places(elements,{'lat':37,'lon':127},lines)
        self.assertEqual([p['id'] for p in result],['node/2'])
        self.assertEqual(result[0]['evidence'][0]['segment_id'],'new')

    def test_menu_coverage_reports_missing_passenger_menu_without_padding_results(self):
        elements=[{'type':'node','id':1,'lat':37,'lon':127,
                   'tags':{'name':'실제 음식점','amenity':'restaurant','cuisine':'pasta'}}]
        lines=[self.segment('파스타 먹자.'),self.segment('피자도 먹고 싶어.','passenger','b')]
        with patch('recommend.lookup_places',return_value={'elements':elements,'fetched_at':123}) as lookup:
            result=recommend(lines,'시험 지역',Path('.'))
        self.assertEqual(lookup.call_args.kwargs['food_topics'],('파스타','피자'))
        self.assertEqual(result['request_profile']['requested_menu_topics'],['파스타','피자'])
        self.assertEqual(result['request_profile']['menu_coverage'],[
            {'topic':'파스타','registered_count':1,'name_hint_count':0},
            {'topic':'피자','registered_count':0,'name_hint_count':0}])
        self.assertIn('피자 조건에 맞는 음식점 후보는 찾지 못',result['notice'])
        self.assertEqual(len(result['recommendations']),1)

    def test_seafood_exclusion_also_rejects_a_raw_fish_restaurant(self):
        elements=[{'type':'node','id':1,'lat':37,'lon':127,'tags':{'name':'동네 횟집','amenity':'restaurant'}},
                  {'type':'node','id':2,'lat':37,'lon':127,'tags':{'name':'동네 식당','amenity':'restaurant'}}]
        result=self.recommend_places(elements,{'lat':37,'lon':127},[
            self.segment('밥 먹자. 해산물 알레르기 있어.')])
        self.assertEqual([p['id'] for p in result],['node/2'])
        self.assertIn('해산물',' '.join(result[0]['cautions']))

    def test_many_candidates_cover_four_passengers_and_never_invent_drive_time(self):
        lines=[self.segment('편도 2시간 이내로 가고 주차하기 편했으면 좋겠어.'),
               self.segment('바다 보고 싶어.','passenger','b'),
               self.segment('칼국수랑 파전 먹고 싶어.','rear1','c'),
               self.segment('나는 회 먹고 싶어.','rear2','d'),
               self.segment('카페도 가자.','driver','e'),
               self.segment('해변에서 산책하고 싶어.','passenger','f')]
        lines += [self.segment('칼국수는 계속 먹고 싶어.','rear1',str(i)) for i in range(14)]
        elements=[{'type':'node','id':i,'lat':37+i*.001,'lon':127,
                   'tags':{'name':f'칼국수 {i}','amenity':'restaurant'}} for i in range(1,16)]
        elements += [
            {'type':'node','id':21,'lat':37.02,'lon':127,'tags':{'name':'바닷가 파전','amenity':'restaurant'}},
            {'type':'node','id':22,'lat':37.02,'lon':127,'tags':{'name':'항구 횟집','amenity':'restaurant'}},
            {'type':'node','id':23,'lat':37.02,'lon':127,'tags':{'name':'해변','natural':'beach'},
             'coastal_distance_m':50,'coastal_basis':'OpenStreetMap natural=coastline'},
            {'type':'node','id':24,'lat':37.02,'lon':127,'tags':{'name':'커피','amenity':'cafe'}},
            {'type':'node','id':25,'lat':37.02,'lon':127,'tags':{'name':'공원','leisure':'park'}}]
        places={'region':'테스트해변','center':{'lat':37,'lon':127},'fetched_at':123,'elements':elements}
        with patch('recommend.lookup_places',return_value=places):
            result=recommend(lines,'테스트해변',Path('.'))
        self.assertEqual(len(result['recommendations']),9)
        self.assertEqual(result['request_profile']['travel_max_minutes'],120)
        self.assertFalse(result['request_profile']['needs_more_preferences'])
        self.assertTrue({'node/21','node/22','node/23','node/24','node/25'}.issubset({p['id'] for p in result['recommendations']}))
        for place in result['recommendations']:
            self.assertFalse(place['travel_constraint']['verified'])
            self.assertIsNone(place['travel_constraint']['duration_minutes'])
            self.assertIn('경로',place['travel_constraint']['message'])
            self.assertNotIn('rating',place)
            self.assertTrue(all(e['quote'] in [s['text'] for s in lines] for e in place['evidence']))

    def test_targeted_menu_query_is_bounded_reserved_and_separately_cached(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch('recommend.get_json',return_value={'elements':[]}) as get_json, patch('recommend.get_nominatim_food_places',return_value=[]):
            location={'latitude':37,'longitude':127}
            lookup_places('',Path(directory),location=location)
            lookup_places('',Path(directory),location=location,food_topics=('회','칼국수'))
            lookup_places('',Path(directory),location=location,food_topics=('칼국수','회'))
            self.assertEqual(get_json.call_count,2)
        query=parse_qs(urlparse(get_json.call_args.args[0]).query)['data'][0]
        self.assertIn('칼국수',query)
        self.assertIn('sashimi',query)
        self.assertEqual(query.count('out center tags 35'),2)
        self.assertIn('around:4500,37.0,127.0',query)

    def test_menu_fallback_verifies_restaurant_identity_menu_evidence_and_radius(self):
        valid={'category':'amenity','type':'restaurant','osm_type':'node','osm_id':1,
               'name':'실제 음식점','lat':'37.001','lon':'127','extratags':{'cuisine':'pasta'},
               'address':{'city':'시험시','road':'시험로','house_number':'10'}}
        rows=[valid, {**valid,'osm_id':2,'type':'cafe'},
              {**valid,'osm_id':3,'extratags':{'cuisine':'korean'}},
              {**valid,'osm_id':4,'lat':'37.3'},
              {**valid,'osm_id':5,'lat':'nan'},
              {**valid,'osm_id':6,'name':'파스타 가게','extratags':{}},
              {**valid,'osm_id':True}, {**valid,'osm_id':7,'osm_type':'postcode'}]
        with patch('recommend.nominatim_search',return_value=rows) as search:
            result=get_nominatim_food_places(37,127,4500,{'양식'})
        self.assertEqual([p['id'] for p in result],[1,6])
        self.assertEqual(result[0]['tags']['cuisine'],'pasta')
        self.assertEqual(result[0]['tags']['addr:street'],'시험로')
        self.assertNotIn('cuisine',result[1]['tags'])
        self.assertEqual(search.call_count,2)
        self.assertEqual([call.args[0]['q'] for call in search.call_args_list],['파스타','피자'])
        self.assertTrue(all(call.args[0]['bounded']==1 and call.args[0]['limit']==20
                            for call in search.call_args_list))

    def test_menu_search_keeps_verified_first_result_when_secondary_query_fails(self):
        valid={'category':'amenity','type':'restaurant','osm_type':'node','osm_id':1,
               'name':'파스타 식당','lat':'37.001','lon':'127','extratags':{'cuisine':'pasta'}}
        report={}
        with patch('recommend.nominatim_search',side_effect=[[valid],OSError('offline')]):
            self.assertEqual([p['id'] for p in get_nominatim_food_places(37,127,4500,{'양식'},report=report)],[1])
        self.assertFalse(report['complete'])
        with patch('recommend.nominatim_search',side_effect=OSError('offline')) as search:
            with self.assertRaisesRegex(ValueError,'음식 종류별 장소 검색'):
                get_nominatim_food_places(37,127,4500,{'양식','한식','일식'})
            self.assertEqual(search.call_count,2)

    def test_menu_fallback_bypasses_old_small_area_cache_and_caches_verified_results(self):
        food={'type':'node','id':1,'lat':35.19,'lon':129.08,
              'tags':{'name':'실제 파스타집','amenity':'restaurant','cuisine':'pasta'}}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            cache_dir=Path(directory)
            legacy=cache_dir/(hashlib.sha256('부산'.encode()).hexdigest()+'.json')
            legacy.write_text(json.dumps({'region':'부산','elements':[], 'lookup_source':'OpenStreetMap · Map API'}),encoding='utf-8')
            with patch('recommend.nominatim_search',return_value=[{'lat':'35.18','lon':'129.075'}]) as geocode, \
                    patch('recommend.get_json',side_effect=OSError('offline')) as overpass, \
                    patch('recommend.get_osm_area_places',return_value=[]), \
                    patch('recommend.get_nominatim_food_places',return_value=[food]) as menus:
                first=lookup_places('부산',cache_dir,food_topics=('양식',))
                second=lookup_places('부산',cache_dir,food_topics=('양식',))
            geocode.assert_called_once()
            overpass.assert_called_once()
            menus.assert_called_once_with(35.18,129.075,4500,{'양식'},report={})
            self.assertEqual(first['elements'],[food])
            self.assertEqual(second['elements'],[food])
            self.assertIn('Nominatim Search',first['lookup_source'])
            self.assertIn('음식 조건 보완 검색은 반경 약 4.5km 내 일부 결과',first['coverage'])
            self.assertTrue(legacy.exists())

    def test_successful_menu_search_does_not_depend_on_the_general_map_server(self):
        food={'type':'node','id':1,'lat':37.001,'lon':127,
              'tags':{'name':'파스타집','amenity':'restaurant','cuisine':'pasta'}}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, \
                patch('recommend.get_json',side_effect=OSError('offline')), \
                patch('recommend.get_osm_area_places',side_effect=OSError('offline')), \
                patch('recommend.get_nominatim_food_places',return_value=[food]):
            result=lookup_places('',Path(directory),location={'latitude':37,'longitude':127},food_topics=('양식',))
        self.assertEqual(result['elements'],[food])
        self.assertNotIn('800m',result['coverage'])
        self.assertIn('일반 주변 장소 검색은 완료하지 못했어요',result['search_note'])

    def test_partial_menu_failure_does_not_poison_the_next_user_retry_with_empty_cache(self):
        attempts=[]
        food={'type':'node','id':1,'lat':37.001,'lon':127,
              'tags':{'name':'실제 파스타집','amenity':'restaurant','cuisine':'pasta'}}
        def menus(*args,report):
            attempts.append(1)
            report['complete']=len(attempts)>1
            return [food] if report['complete'] else []
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, \
                patch('recommend.get_json',side_effect=OSError('offline')), \
                patch('recommend.get_osm_area_places',side_effect=lambda *args: []), \
                patch('recommend.get_nominatim_food_places',side_effect=menus):
            arguments={'location':{'latitude':37,'longitude':127},'food_topics':('양식',)}
            first=lookup_places('',Path(directory),**arguments)
            second=lookup_places('',Path(directory),**arguments)
            third=lookup_places('',Path(directory),**arguments)
        self.assertFalse(first['menu_search_complete'])
        self.assertEqual(first['elements'],[])
        self.assertEqual(second['elements'],[food])
        self.assertEqual(third['elements'],[food])
        self.assertEqual(len(attempts),2)

    def test_venue_conditions_do_not_become_a_blanket_meal_or_cafe_veto(self):
        lines=[self.segment('밥 먹자. 카페도 가자.'),
               self.segment('식당에서 카페까지 오래 운전하긴 싫어.',identifier='b'),
               self.segment('대기 긴 식당은 다음에 가자.',identifier='c'),
               self.segment('비싼 코스 식당은 부담돼.',identifier='d')]
        conditions,_=extract(lines)
        self.assertEqual(set(conditions[0]['likes']),{'식사','카페'})
        self.assertEqual(conditions[0]['dislikes'],[])

    def test_spicy_dish_and_not_exclusively_korean_do_not_veto_all_korean_food(self):
        conditions,_=extract([self.segment('한식 좋아. 매운 찌개만 있는 집은 피하자. 한식만 찾지는 말고 두 가지를 비교하자.')])
        self.assertIn('한식',conditions[0]['likes'])
        self.assertNotIn('한식',conditions[0]['dislikes'])
        self.assertIn('매운 음식',conditions[0]['dislikes'])

    def test_cancelled_request_is_not_reintroduced_by_quoting_its_old_proposal(self):
        conditions,_=extract([self.segment('고기 먹자.'),
                              self.segment('아니다 파스타 먹자.',identifier='b'),
                              self.segment('고기 먹자는 얘기는 취소야. 파스타 먹자.',identifier='c')])
        self.assertNotIn('고기',conditions[0]['likes'])
        self.assertIn('양식',conditions[0]['likes'])

    def test_additional_menu_choice_does_not_cancel_the_first_dish(self):
        conditions,_=extract([self.segment('칼국수 먹자. 칼국수 말고 돈까스도 괜찮아.')])
        self.assertIn('칼국수',conditions[0]['likes'])
        self.assertIn('일식',conditions[0]['likes'])
        self.assertFalse(conditions[0]['dislikes'])

    def test_compound_won_budget_and_walking_duration_are_not_misread(self):
        conditions,_=extract([self.segment('한 사람당 2만 5천 원 이내면 좋아. 산책은 30분 정도만 하자.')])
        self.assertIn('1인 예산 25,000원 언급',conditions[0]['requirements'])
        self.assertNotIn('예산 5,000원 언급',conditions[0]['requirements'])
        self.assertFalse(any(req.startswith('이동') for req in conditions[0]['requirements']))

    def test_written_money_and_comma_money_survive_clause_splitting(self):
        conditions, _ = extract([self.segment('점심은 1인 만 이천 원까지. 음료는 6,000원씩 생각하자.')])
        self.assertIn('1인 예산 12,000원 언급', conditions[0]['requirements'])
        self.assertIn('1인 예산 6,000원 언급', conditions[0]['requirements'])
        self.assertEqual({value['category'] for value in conditions[0]['budget_evidence']}, {'meal', 'drink'})

    def test_revised_shared_budget_removes_old_estimate_and_uses_current_quote(self):
        lines = [self.segment('오늘 밥값은 넷이서 8만 원쯤이면 괜찮죠?', identifier='old'),
                 self.segment('모자라면 같이 먹을 거 하나 더 시키고. 그래도 총 10만 원은 넘기지 말자.',
                              'passenger', 'current')]
        conditions, _ = extract(lines)
        requirements = [value for profile in conditions for value in profile['requirements']]
        self.assertNotIn('전체 예산 80,000원 언급', requirements)
        self.assertIn('전체 예산 100,000원 언급', requirements)
        summary = summarize_conversation(lines)
        self.assertNotIn('80,000', summary['text'])
        self.assertEqual([entry['segment_id'] for entry in summary['evidence']], ['current'])

    def test_acknowledging_another_person_restriction_does_not_change_speaker_preference(self):
        lines = [self.segment('나는 회! 바닷가까지 가는데 회는 먹어야지.', 'rear2', 'a'),
                 self.segment('회 안 먹는 건 알지. 다른 메뉴도 있는 집 가면 되잖아.', 'rear2', 'b')]
        conditions, _ = extract(lines)
        self.assertIn('회', conditions[0]['likes'])
        self.assertNotIn('회', conditions[0]['dislikes'])
        self.assertEqual([entry['segment_id'] for entry in summarize_conversation(lines)['evidence']], ['a'])

    def test_explicit_first_person_restriction_remains_a_restriction(self):
        conditions, _ = extract([self.segment('내가 회 못 먹는 거 알지?')])
        self.assertIn('회', conditions[0]['dislikes'])

    def test_red_noodle_rejection_does_not_exclude_every_noodle_restaurant(self):
        lines = [self.segment('칼국수 좋아.'), self.segment('빨간 칼국수 말고.', identifier='b')]
        conditions, _ = extract(lines)
        self.assertIn('칼국수', conditions[0]['likes'])
        self.assertNotIn('칼국수', conditions[0]['dislikes'])
        self.assertIn('매운 음식', conditions[0]['dislikes'])
        rejected, _ = extract([self.segment('칼국수 말고 돈까스 먹자.')])
        self.assertIn('칼국수', rejected[0]['dislikes'])

    def test_non_coffee_drink_choice_preserves_cafe_choice_without_guessing_caffeine(self):
        for text in ('난 커피 말고 과일 음료 마실래.', '난 커피 말고 차 마실 거야.',
                     '카페에서는 커피 말고 따뜻한 차 마실래.'):
            with self.subTest(text=text):
                conditions, _ = extract([self.segment('조용한 카페 가자.'), self.segment(text, identifier='b')])
                self.assertIn('카페', conditions[0]['likes'])
                self.assertNotIn('카페', conditions[0]['dislikes'])
                self.assertIn('커피 외 음료 확인', conditions[0]['requirements'])
                self.assertNotIn('카페인 없는 음료 확인', conditions[0]['requirements'])

    def test_rejecting_the_cafe_itself_is_not_relaxed_by_a_drink_alternative(self):
        conditions, _ = extract([self.segment('카페 말고 집에 가서 커피 대신 차 마시자.')])
        self.assertIn('카페', conditions[0]['dislikes'])

    def test_clear_shared_menu_proposals_keep_their_exact_topics(self):
        conditions, _ = extract([self.segment('칼국수 당긴다. 파전 하나 시켜서 나눠 먹고.')])
        self.assertIn('칼국수', conditions[0]['likes'])
        self.assertIn('파전', conditions[0]['likes'])
        rejected, _ = extract([self.segment('칼국수는 당기지 않아.')])
        self.assertIn('칼국수', rejected[0]['dislikes'])

    def test_latest_travel_quote_is_used_after_an_explicit_time_correction(self):
        summary=summarize_conversation([self.segment('운전은 2시간 이내로 하자.'),
                                        self.segment('아니다 90분 이내로 가자.',identifier='b')])
        self.assertEqual([q['segment_id'] for q in summary['evidence']],['b'])

    def test_exhibition_before_coffee_controls_the_first_stop_despite_more_coffee_votes(self):
        elements=[{'type':'node','id':1,'lat':37,'lon':127,'tags':{'name':'커피','amenity':'cafe'}},
                  {'type':'node','id':2,'lat':37.002,'lon':127,'tags':{'name':'전시관','tourism':'museum'}}]
        lines=[self.segment('전시 보고 조용한 카페에 가자.'),
               self.segment('카페 좋아.','passenger','b'),
               self.segment('커피 마시고 싶어.','rear1','c')]
        result=self.recommend_places(elements,{'lat':37,'lon':127},lines)
        self.assertEqual([p['id'] for p in result],['node/2','node/1'])
        self.assertLess(result[0]['score'],result[1]['score'])

    def test_many_coffee_shops_do_not_pad_a_mixed_activity_shortlist(self):
        elements=[{'type':'node','id':i,'lat':37+i*.001,'lon':127,
                   'tags':{'name':f'개성 있는 카페 {i}','amenity':'cafe'}} for i in range(1,14)]
        elements.append({'type':'node','id':20,'lat':37.01,'lon':127,'tags':{'name':'박물관','tourism':'museum'}})
        result=self.recommend_places(elements,{'lat':37,'lon':127},[self.segment('전시 먼저 보고 카페 가자.')])
        self.assertEqual(result[0]['id'],'node/20')
        self.assertEqual(len([p for p in result if p['group']=='카페·디저트']),6)
        self.assertEqual(len(result),7)

    def test_mapped_fast_food_dessert_does_not_halve_cafe_only_choices(self):
        elements=[{'type':'node','id':i,'lat':37+i*.001,'lon':127,
                   'tags':{'name':f'카페 {i}','amenity':'cafe'}} for i in range(1,14)]
        elements.append({'type':'node','id':20,'lat':37.01,'lon':127,
                         'tags':{'name':'도넛','amenity':'fast_food','shop':'bakery'}})
        result=self.recommend_places(elements,{'lat':37,'lon':127},[self.segment('카페 가자. 디저트도 먹자.')])
        self.assertEqual(12,len(result))
        self.assertEqual({'카페·디저트'},{place['group'] for place in result})
        self.assertTrue(any(place['id']=='node/20' for place in result))

    def test_brand_metadata_and_localized_branch_names_share_a_two_branch_cap(self):
        names=['스타벅스 시청점','Starbucks','스타벅스 서울역점','스타벅스','동네 커피','작은 커피']
        elements=[{'type':'node','id':i,'lat':37+i*.001,'lon':127,
                   'tags':{'name':name,'amenity':'cafe',**({'brand':'Starbucks'} if i==1 else {})}}
                  for i,name in enumerate(names)]
        result=self.recommend_places(elements,{'lat':37,'lon':127})
        self.assertEqual(len(result),4)
        self.assertEqual({p['id'] for p in result},{'node/0','node/1','node/4','node/5'})
        self.assertEqual([p['id'] for p in result[:3]],['node/0','node/4','node/5'])

    def test_one_requested_activity_can_still_offer_twelve_distinct_places(self):
        elements=[{'type':'node','id':i,'lat':37+i*.001,'lon':127,
                   'tags':{'name':f'카페 {i}','amenity':'cafe'}} for i in range(1,20)]
        result=self.recommend_places(elements,{'lat':37,'lon':127})
        self.assertEqual(len(result),12)

    def test_current_location_search_skips_region_geocoding(self):
        location={'latitude':37.1234,'longitude':127.2345}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch('recommend.get_json',return_value={'elements':[]}) as get_json:
            result=lookup_places('이전에 입력한 다른 지역',Path(directory),location=location)
            self.assertEqual(list(Path(directory).iterdir()),[])
        get_json.assert_called_once()
        url=get_json.call_args.args[0]
        self.assertEqual(urlparse(url).netloc,'overpass-api.de')
        self.assertIn('around:4500,37.1234,127.2345',parse_qs(urlparse(url).query)['data'][0])
        self.assertEqual(result['center'],{'lat':37.1234,'lon':127.2345})
        self.assertEqual(result['region'],'현재 위치 주변')
        self.assertIn('현재 위치 중심',result['coverage'])

    def test_nearby_fix_cache_keeps_this_fix_as_the_distance_center(self):
        first={'latitude':37.123441,'longitude':127.234441}
        second={'latitude':37.123449,'longitude':127.234449}
        third={'latitude':37.125,'longitude':127.234449}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch('recommend.get_json',return_value={'elements':[]}) as get_json:
            cache_dir=Path(directory)
            lookup_places('무시할 지역',cache_dir,location=first)
            cached=lookup_places('다른 무시할 지역',cache_dir,location=second)
            self.assertEqual(get_json.call_count,1)
            self.assertEqual(cached['center'],{'lat':second['latitude'],'lon':second['longitude']})
            lookup_places('',cache_dir,location=third)
            self.assertEqual(get_json.call_count,2)
            self.assertEqual(list(cache_dir.iterdir()),[])

    def test_location_poi_cache_is_bounded_and_hits_do_not_extend_its_lifetime(self):
        clock=[0]
        locations=[{'latitude':37.1234+offset,'longitude':127.2345} for offset in (0,.001,.002)]
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, \
                patch('recommend.get_json',return_value={'elements':[]}) as get_json, \
                patch('recommend.time.monotonic',side_effect=lambda:clock[0]), \
                patch('recommend._LOCATION_POI_CACHE_LIMIT',2), patch('recommend._LOCATION_POI_CACHE_TTL',60):
            cache_dir=Path(directory)
            lookup_places('',cache_dir,location=locations[0])
            lookup_places('',cache_dir,location=locations[1])
            clock[0]=20
            lookup_places('',cache_dir,location=locations[0])  # most recently used
            lookup_places('',cache_dir,location=locations[2])  # evicts location 1
            self.assertEqual(get_json.call_count,3)
            lookup_places('',cache_dir,location=locations[1])
            self.assertEqual(get_json.call_count,4)
            clock[0]=70
            lookup_places('',cache_dir,location=locations[2])
            self.assertEqual(get_json.call_count,4)
            clock[0]=80
            lookup_places('',cache_dir,location=locations[2])
            self.assertEqual(get_json.call_count,5)
            self.assertEqual(list(cache_dir.iterdir()),[])

    def test_location_cache_returns_independent_data_and_never_retains_exact_center(self):
        memory=OrderedDict()
        first={'latitude':37.123441,'longitude':127.234441}
        second={'latitude':37.123449,'longitude':127.234449}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, \
                patch('recommend._LOCATION_POI_CACHE',memory), \
                patch('recommend.get_json',return_value={'elements':[]}) as get_json:
            original=lookup_places('',Path(directory),location=first)
            original['elements'].append({'injected':'caller mutation'})
            cached=lookup_places('',Path(directory),location=second)
            self.assertEqual(cached['elements'],[])
            self.assertEqual(cached['center'],{'lat':second['latitude'],'lon':second['longitude']})
            self.assertTrue(all('center' not in entry[1] for entry in memory.values()))
            cached['elements'].append({'injected':'cache-hit mutation'})
            self.assertEqual(lookup_places('',Path(directory),location=first)['elements'],[])
            self.assertEqual(get_json.call_count,1)

    def test_location_search_ignores_legacy_disk_cache_and_starts_empty_after_restart(self):
        location={'latitude':37.1234,'longitude':127.2345}
        key='location:37.1234,127.2345'
        legacy_text=json.dumps({'region':'현재 위치 주변','elements':[{'id':'legacy'}]})
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, \
                patch('recommend.get_json',return_value={'elements':[]}) as get_json:
            cache_dir=Path(directory)
            legacy=cache_dir/(hashlib.sha256(key.encode()).hexdigest()+'.json')
            legacy.write_text(legacy_text,encoding='utf-8')
            self.assertEqual(lookup_places('',cache_dir,location=location)['elements'],[])
            with patch('recommend._LOCATION_POI_CACHE',OrderedDict()):
                self.assertEqual(lookup_places('',cache_dir,location=location)['elements'],[])
            self.assertEqual(get_json.call_count,2)
            self.assertEqual(legacy.read_text(encoding='utf-8'),legacy_text)
            self.assertEqual(list(cache_dir.iterdir()),[legacy])

    def test_current_location_fallback_uses_the_same_coordinates(self):
        location={'latitude':37.1234,'longitude':127.2345}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch('recommend.get_json',side_effect=OSError('test offline')), patch('recommend.get_osm_area_places',return_value=[]) as fallback:
            result=lookup_places('',Path(directory),location=location)
        fallback.assert_called_once_with(37.1234,127.2345)
        self.assertIn('현재 위치 중심',result['coverage'])
        self.assertEqual(result['lookup_source'],'OpenStreetMap · Map API')

    def test_current_location_recommendations_use_fix_not_cached_or_named_center(self):
        location={'latitude':37,'longitude':127}
        places={'region':'기존 지역','center':{'lat':0,'lon':0},'fetched_at':123,'elements':[
            {'type':'node','id':1,'lat':37.001,'lon':127,'tags':{'name':'카페','amenity':'cafe'}}]}
        with patch('recommend.lookup_places',return_value=places) as lookup:
            result=recommend([self.segment('카페 좋아.')],'이전 검색 지역',Path('.'),location=location)
        lookup.assert_called_once_with('현재 위치 주변',Path('.'),location=location)
        self.assertEqual(result['region'],'현재 위치 주변')
        self.assertAlmostEqual(result['recommendations'][0]['distance_m'],111,delta=1)
        self.assertEqual(result['recommendations'][0]['distance_basis'],'current_location')

    def test_invalid_current_location_never_falls_back_to_a_named_region(self):
        invalid=[{},[],{'latitude':float('nan'),'longitude':127},
                 {'latitude':True,'longitude':127},{'latitude':91,'longitude':127}]
        for location in invalid:
            with self.subTest(location=location), patch('recommend.lookup_places') as lookup:
                with self.assertRaisesRegex(ValueError,'현재 위치 좌표'):
                    recommend([self.segment('카페 좋아.')],'검색 가능한 지역',Path('.'),location=location)
                lookup.assert_not_called()

    def test_named_region_search_still_geocodes_when_no_fix_is_supplied(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch('recommend.get_json',side_effect=[
                [{'lat':'37','lon':'127'}],{'elements':[]}]) as get_json:
            result=lookup_places('테스트역',Path(directory))
            saved=json.loads(next(Path(directory).glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(get_json.call_count,2)
        self.assertEqual(urlparse(get_json.call_args_list[0].args[0]).netloc,'nominatim.openstreetmap.org')
        self.assertEqual(result['region'],'테스트역')
        self.assertIn('검색 지역 중심',result['coverage'])
        self.assertEqual(saved['center'],{'lat':37,'lon':127})

    def test_beach_request_uses_beaches_not_restaurants_or_cafes(self):
        places={'region':'부산','center':{'lat':35.1,'lon':129},'fetched_at':123,'elements':[
            {'type':'node','id':1,'lat':35.11,'lon':129,'tags':{'name':'바다 커피','amenity':'cafe'}},
            {'type':'node','id':2,'lat':35.12,'lon':129,'tags':{'name':'해변 식당','amenity':'restaurant'}},
            {'type':'way','id':3,'center':{'lat':35.13,'lon':129},'tags':{'name':'시험 해변','natural':'beach'},
             'coastal_distance_m':50,'coastal_basis':'OpenStreetMap natural=coastline'}]}
        with patch('recommend.lookup_places',return_value=places) as lookup:
            result=recommend([self.segment('부산 바다 보고 싶어.')],'부산',Path('.'))
        lookup.assert_called_once_with('부산',Path('.'),include_beaches=True,beach_only=True,radius_m=20000)
        self.assertEqual([p['id'] for p in result['recommendations']],['way/3'])
        self.assertEqual(result['recommendations'][0]['category'],'해변')
        self.assertIn('바닷가 여부 확인',result['recommendations'][0]['cautions'])

    def test_beach_request_with_no_beach_does_not_fall_back_to_food(self):
        elements=[{'type':'node','id':1,'lat':37,'lon':127,'tags':{'name':'바다 식당','amenity':'restaurant'}}]
        result=self.recommend_places(elements,{'lat':37,'lon':127},[self.segment('바다 보러 가자.')])
        self.assertEqual(result,[])

    def test_river_beach_and_unknown_coast_are_not_sea_recommendations(self):
        elements=[{'type':'node','id':1,'lat':37.5,'lon':127,
                   'tags':{'name':'한강 백사장','natural':'beach'}},
                  {'type':'node','id':2,'lat':37.2,'lon':126.5,
                   'tags':{'name':'시험 바닷가','natural':'beach'},
                   'coastal_distance_m':80,'coastal_basis':'OpenStreetMap natural=coastline'}]
        with patch('poi_index.coastal_proximity',return_value=None) as coast:
            result=self.recommend_places(elements,{'lat':37,'lon':127},[self.segment('바다 보러 가자.')])
        self.assertEqual(['node/2'],[place['id'] for place in result])
        self.assertEqual(80,result[0]['coastal_proximity']['distance_m'])
        coast.assert_called_once_with(37.5,127.0)

    def test_explicit_beach_correction_replaces_previous_cafe_choice(self):
        constraints,_=extract([self.segment('카페 가자.'),self.segment('아니다 바다 보고 싶어.',identifier='b')])
        self.assertEqual(constraints[0]['likes'],['해변'])

    def test_mixed_beach_query_reserves_beach_results_and_reports_both_ranges(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch('recommend.get_json',return_value={'elements':[]}) as get_json:
            result=lookup_places('',Path(directory),location={'latitude':37,'longitude':127},radius_m=20000,include_beaches=True)
        query=parse_qs(urlparse(get_json.call_args.args[0]).query)['data'][0]
        self.assertIn('around:4500,37.0,127.0',query)
        self.assertIn('around:20000,37.0,127.0',query)
        self.assertIn('["natural"="beach"]',query)
        self.assertIn('out center tags 140',query)
        self.assertIn('out center tags 40',query)
        self.assertIn('해변 반경 약 20km',result['coverage'])
        self.assertIn('그 외 장소 반경 약 4.5km',result['coverage'])

    def test_wide_search_failure_does_not_claim_a_tiny_fallback_is_citywide(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch('recommend.get_json',side_effect=OSError('test offline')), patch('recommend.get_osm_area_places') as fallback:
            with self.assertRaisesRegex(ValueError,'해변 장소 검색'):
                lookup_places('',Path(directory),location={'latitude':37,'longitude':127},radius_m=20000,beach_only=True)
        fallback.assert_not_called()

    def test_wide_beach_and_regular_place_caches_are_separate(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch('recommend.get_json',return_value={'elements':[]}) as get_json:
            location={'latitude':37,'longitude':127}
            lookup_places('',Path(directory),location=location)
            lookup_places('',Path(directory),location=location,radius_m=20000,beach_only=True)
            self.assertEqual(get_json.call_count,2)

    def test_search_radius_cannot_exceed_twenty_kilometers(self):
        for radius in (True,0,20001,float('inf')):
            with self.subTest(radius=radius), patch('recommend.get_json') as get_json:
                with self.assertRaisesRegex(ValueError,'20km'):
                    lookup_places('',Path('.'),location={'latitude':37,'longitude':127},radius_m=radius)
                get_json.assert_not_called()

    def test_nominatim_beaches_require_real_beach_type_and_in_radius_coordinates(self):
        valid={'category':'natural','type':'beach','osm_type':'way','osm_id':1,
               'name':'Test beach','lat':'37.001','lon':'127',
               'namedetails':{'name:ko':'시험 해변'},'address':{'city':'시험시'}}
        places=[valid,{**valid,'osm_id':2,'category':'amenity','type':'cafe'},
                {**valid,'osm_id':3,'lat':'37.3'},
                {**valid,'osm_id':4,'lat':'nan'},
                {**valid,'osm_id':5,'osm_type':'postcode'},
                {**valid,'osm_id':None}]
        with patch('recommend.nominatim_search',return_value=places) as search:
            result=get_nominatim_beaches(37,127,20000)
        self.assertEqual([p['id'] for p in result],[1])
        self.assertEqual(result[0]['tags'],{'name':'시험 해변','natural':'beach','addr:city':'시험시'})
        self.assertEqual(search.call_args.args[0]['q'],'[beach]')
        self.assertEqual(search.call_args.args[0]['bounded'],1)

    def test_wide_beach_fallback_keeps_twenty_km_beaches_separate_from_small_map_area(self):
        beach={'type':'way','id':1,'lat':37.05,'lon':127,'tags':{'name':'해변','natural':'beach'}}
        cafe={'type':'node','id':2,'lat':37.001,'lon':127,'tags':{'name':'커피','amenity':'cafe'}}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch('recommend.get_json',side_effect=OSError('test offline')), patch('recommend.get_nominatim_beaches',return_value=[beach]) as beaches, patch('recommend.get_osm_area_places',return_value=[cafe]):
            result=lookup_places('',Path(directory),location={'latitude':37,'longitude':127},radius_m=20000,include_beaches=True)
        beaches.assert_called_once_with(37,127,20000)
        self.assertEqual([p['id'] for p in result['elements']],[1,2])
        self.assertIn('해변 반경 약 20km 내 일부 검색 결과',result['coverage'])
        self.assertIn('그 외 장소는 중심에서 동서·남북 약 800m',result['coverage'])
        self.assertNotIn('4.5km',result['coverage'])
        self.assertIn('Nominatim Search + Map API',result['lookup_source'])
        self.assertIn('모든 해변을 포함하지는',result['search_note'])

    def test_beach_only_fallback_is_cached_and_does_not_query_the_small_map_area(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch('recommend.get_json',side_effect=OSError('test offline')) as overpass, patch('recommend.get_nominatim_beaches',return_value=[]) as beaches, patch('recommend.get_osm_area_places') as small_map:
            arguments={'location':{'latitude':37,'longitude':127},'radius_m':20000,'beach_only':True}
            first=lookup_places('',Path(directory),**arguments)
            second=lookup_places('',Path(directory),**arguments)
        overpass.assert_called_once()
        beaches.assert_called_once()
        small_map.assert_not_called()
        self.assertEqual(first['elements'],[])
        self.assertEqual(second['coverage'],first['coverage'])
        self.assertIn('해변 후보를 찾지 못했어요',first['search_note'])

    def test_beach_result_survives_secondary_general_place_failure_with_clear_notice(self):
        beach={'type':'way','id':1,'lat':37.05,'lon':127,'tags':{'name':'해변','natural':'beach'}}
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory, patch('recommend.get_json',side_effect=OSError('test offline')), patch('recommend.get_nominatim_beaches',return_value=[beach]), patch('recommend.get_osm_area_places',side_effect=OSError('test offline')):
            result=lookup_places('',Path(directory),location={'latitude':37,'longitude':127},radius_m=20000,include_beaches=True)
        self.assertEqual(result['elements'],[beach])
        self.assertNotIn('800m',result['coverage'])
        self.assertIn('다른 종류의 장소 검색은 완료하지 못했어요',result['search_note'])

    def test_nominatim_calls_are_serialized_at_more_than_one_second_intervals(self):
        with patch('recommend.NOMINATIM_MIN_INTERVAL',1.1), patch('recommend.time.monotonic',side_effect=[10.0,10.25,11.1]), patch('recommend.time.sleep') as sleep, patch('recommend.get_json',return_value=[]):
            nominatim_search({'q':'시험시'})
            nominatim_search({'q':'[beach]'})
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0],0.85)


if __name__=='__main__':
    unittest.main()
