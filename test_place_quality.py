import copy
import unittest

from place_quality import attach_quality


def restaurant(identifier='pasta', basis='registered'):
    return {'id': identifier, 'group': '음식점', 'name': '예시 식당',
            'matched_topics': ['식사', '양식', '파스타'], 'matched_seats': ['unknown'],
            'menu_matches': [{'topic': '파스타', 'basis': basis}],
            'evidence': [{'seat': 'unknown', 'topic': '파스타', 'quote': '파스타 먹자'}]}


class PlaceQualityTests(unittest.TestCase):
    def test_registered_dish_is_stronger_than_name_hint_and_broad_cuisine(self):
        registered = restaurant()
        hinted = restaurant('hint', 'name_hint')
        broad = {'id': 'broad', 'matched_topics': ['식사', '양식'],
                 'menu_matches': [{'topic': '양식', 'basis': 'registered'}]}
        actual, name_only, cuisine = attach_quality([registered, hinted, broad])
        self.assertGreater(actual['quality_score'], name_only['quality_score'])
        self.assertGreater(actual['quality_score'], cuisine['quality_score'])
        hint_factor = next(f for f in name_only['quality_factors'] if f['key'] == 'menu_name_hint')
        self.assertIn('이름', hint_factor['detail'])
        self.assertIn('낮게', hint_factor['detail'])
        self.assertIn('실제 판매 메뉴와 맛은 별도 확인', actual['quality_factors'][0]['detail'])

    def test_dish_parents_and_duplicate_evidence_do_not_inflate_preferences(self):
        place = restaurant()
        place['matched_topics'] += ['파스타', '식사']
        place['menu_matches'] *= 4
        place['evidence'] *= 5
        result = attach_quality([place])[0]
        simple = attach_quality([restaurant()])[0]
        self.assertEqual(result['quality_score'], simple['quality_score'])
        factor = next(f for f in result['quality_factors'] if f['key'] == 'conversation_match')
        self.assertEqual(factor['label'], '파스타 취향 일치')
        self.assertEqual(factor['score'], 3)

    def test_specific_dishes_are_independent_without_counting_parent_twice(self):
        place = restaurant()
        place['matched_topics'].append('피자')
        factor = next(f for f in attach_quality([place])[0]['quality_factors']
                      if f['key'] == 'conversation_match')
        self.assertEqual(factor['label'], '대화 취향 2가지 일치')
        self.assertEqual(factor['score'], 6)

    def test_unknown_speaker_does_not_become_seat_coverage(self):
        place = restaurant()
        place['evidence'] *= 4
        place['matched_seats'] = ['unknown'] * 4
        result = attach_quality([place])[0]
        self.assertNotIn('identified_preferences', [f['key'] for f in result['quality_factors']])

    def test_seat_coverage_requires_actual_matching_evidence_and_confirmed_seat(self):
        place = restaurant()
        place['matched_seats'] = ['driver', 'passenger', 'rear1', 'rear2']
        place['evidence'] = [
            {'seat': 'driver', 'topic': '파스타', 'quote': '파스타 먹고 싶어'},
            {'seat': 'passenger', 'topic': '파스타', 'quote': '나도 파스타'},
            {'seat': 'rear1', 'topic': '파스타', 'quote': ''},
            {'seat': 'rear2', 'topic': '파스타', 'quote': '파스타는 싫어', 'negative': True}]
        factor = next(f for f in attach_quality([place])[0]['quality_factors']
                      if f['key'] == 'identified_preferences')
        self.assertEqual(factor['label'], '확인된 2좌석의 취향 반영')
        self.assertEqual(factor['score'], 3)

    def test_route_headroom_is_normalized_to_requested_limit(self):
        base = restaurant()
        cases = [dict(base, travel_constraint={'verified': True, 'within_limit': True,
                    'max_minutes': limit, 'duration_minutes': duration})
                 for limit, duration in [(120, 30), (120, 60), (60, 30), (120, 119)]]
        result = attach_quality(cases)
        self.assertGreater(result[0]['quality_score'], result[1]['quality_score'])
        self.assertEqual(result[1]['quality_score'], result[2]['quality_score'])
        self.assertNotIn('travel_headroom', [f['key'] for f in result[3]['quality_factors']])
        detail = next(f['detail'] for f in result[0]['quality_factors'] if f['key'] == 'travel_headroom')
        self.assertIn('실시간 교통은 반영하지 않았어요', detail)

    def test_unknown_over_limit_or_inconsistent_route_gets_no_travel_reward(self):
        constraints = [
            {'verified': False, 'within_limit': True, 'max_minutes': 120, 'duration_minutes': 10},
            {'verified': True, 'within_limit': False, 'max_minutes': 120, 'duration_minutes': 130},
            {'verified': True, 'within_limit': True, 'max_minutes': 120, 'duration_minutes': 130},
            {'verified': True, 'within_limit': True, 'max_minutes': 120, 'duration_minutes': float('nan')},
            {'verified': True, 'within_limit': True, 'max_minutes': True, 'duration_minutes': 0}]
        for constraint in constraints:
            place = dict(restaurant(), travel_constraint=constraint)
            self.assertNotIn('travel_headroom', [f['key'] for f in attach_quality([place])[0]['quality_factors']])

    def test_valid_zero_minute_route_is_not_treated_as_unknown(self):
        place = dict(restaurant(), travel_constraint={'verified': True, 'within_limit': True,
                     'max_minutes': 120, 'duration_minutes': 0})
        factor = next(f for f in attach_quality([place])[0]['quality_factors'] if f['key'] == 'travel_headroom')
        self.assertEqual(factor['score'], 10)

    def test_personal_rating_is_scoped_to_real_or_sample_visit(self):
        ratings = {'visit:pasta': {'rating': 1}, 'sample:pasta': {'rating': 5}}
        real = attach_quality([restaurant()], ratings)[0]
        sample = attach_quality([restaurant()], ratings, demo=True)[0]
        real_factor = next(f for f in real['quality_factors'] if f['key'] == 'personal_visit_rating')
        sample_factor = next(f for f in sample['quality_factors'] if f['key'] == 'personal_visit_rating')
        self.assertLess(real_factor['score'], 0)
        self.assertGreater(sample_factor['score'], 0)
        self.assertEqual(real_factor['basis'], 'personal_visit_rating')
        self.assertEqual(sample_factor['basis'], 'sample_visit_rating')
        self.assertIn('샘플', sample_factor['label'])
        no_real = attach_quality([restaurant()], {'sample:pasta': {'rating': 5}})[0]
        self.assertNotIn('personal_visit_rating', [f['key'] for f in no_real['quality_factors']])

    def test_invalid_ratings_and_embedded_stars_never_become_personal_feedback(self):
        for bad in [True, 0, 6, 4.5, '5', None]:
            place = dict(restaurant(), my_rating={'rating': 5}, stars=5, review_score=4.9)
            result = attach_quality([place], {'visit:pasta': {'rating': bad}})[0]
            self.assertNotIn('personal_visit_rating', [f['key'] for f in result['quality_factors']])

    def test_website_hours_contact_and_unverified_reviews_do_not_boost_taste(self):
        base = restaurant()
        decorated = dict(base, website='https://example.test', opening_hours='24/7',
                         phone='000', rating=5, review_count=10000, reviews=['최고의 맛집'])
        result = attach_quality([base, decorated])
        self.assertEqual(result[0]['quality_score'], result[1]['quality_score'])
        self.assertEqual(result[0]['quality_factors'], result[1]['quality_factors'])

    def test_cafe_category_does_not_claim_quality_of_coffee_or_atmosphere(self):
        cafe = {'id': 'cafe', 'group': '카페·디저트', 'matched_topics': ['카페']}
        result = attach_quality([cafe])[0]
        self.assertEqual(result['quality_score'], 3)
        self.assertEqual(len(result['quality_factors']), 1)
        self.assertEqual(result['quality_factors'][0]['label'], '카페 취향 일치')
        text = str(result['quality_factors'])
        for unsupported in ['원두', '커피 맛', '분위기가 좋은', '맛집']:
            self.assertNotIn(unsupported, text)

    def test_unrelated_menu_metadata_does_not_add_preference_points(self):
        place = {'id': 'cafe', 'matched_topics': ['카페'],
                 'menu_matches': [{'topic': '파스타', 'basis': 'registered'}]}
        result = attach_quality([place])[0]
        self.assertNotIn('menu_classification', [f['key'] for f in result['quality_factors']])

    def test_independent_copies_preserve_order_constraints_and_are_idempotent(self):
        places = [restaurant('b'), restaurant('a')]
        places[0]['cautions'] = ['주차 확인']
        original = copy.deepcopy(places)
        ratings = {'visit:b': {'rating': 4}}
        once = attach_quality(places, ratings)
        twice = attach_quality(once, ratings)
        self.assertEqual(places, original)
        self.assertEqual(once, twice)
        self.assertEqual([p['id'] for p in once], ['b', 'a'])
        once[0]['cautions'].append('changed')
        self.assertEqual(places[0]['cautions'], ['주차 확인'])
        without_ratings = attach_quality(once)
        self.assertNotIn('personal_visit_rating', [f['key'] for f in without_ratings[0]['quality_factors']])


if __name__ == '__main__':
    unittest.main()
