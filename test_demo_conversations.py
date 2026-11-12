import copy
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from demo_conversations import SCENARIOS, enter_demo, leave_demo, random_scenario, sample_segments
from recommend import extract, request_profile
from search_intent import resolve_search_intent


class DemoConversationTests(unittest.TestCase):
    def rows(self, scenario):
        return [dict(id=str(i), seat=seat, text=text) for i, (seat, text) in enumerate(scenario['dialogue'])]

    def test_every_scenario_has_four_seats_and_the_intended_search_area(self):
        expected = {scenario['id']: None for scenario in SCENARIOS}
        self.assertEqual(len({s['id'] for s in SCENARIOS}), 6)
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario['id']):
                rows = self.rows(scenario)
                self.assertEqual(len(rows), 26)
                self.assertEqual({r['seat'] for r in rows}, {'driver', 'passenger', 'rear1', 'rear2'})
                for seat in ('driver', 'passenger', 'rear1', 'rear2'):
                    self.assertGreaterEqual(sum(r['seat'] == seat for r in rows), 4)
                intent = resolve_search_intent(rows)
                self.assertEqual(intent['region'], expected[scenario['id']])
                self.assertFalse(intent['needs_area_choice'])
                if expected[scenario['id']]:
                    self.assertEqual(intent['area_source'], 'conversation')
                else:
                    self.assertIn(intent['area_source'], ('nearby','unspecified'))

    def test_examples_exercise_important_preference_and_revision_conditions(self):
        profiles = {s['id']: {p['seat']: p for p in extract(self.rows(s))[0]} for s in SCENARIOS}
        self.assertIn('매운 음식', profiles['nearby-lunch']['rear2']['dislikes'])
        self.assertIn('산책', profiles['seoul-rain']['rear1']['dislikes'])
        self.assertIn('카페인 없는 음료 확인', profiles['gangneung-sea']['rear2']['requirements'])
        self.assertIn('카페', profiles['gangneung-sea']['rear2']['likes'])
        self.assertIn('전체 예산 100,000원 언급', profiles['daejeon-family']['passenger']['requirements'])
        self.assertIn('양식', profiles['busan-friends']['passenger']['likes'])
        self.assertNotIn('고기', profiles['busan-friends']['passenger']['likes'])
        self.assertIn('산책', profiles['jeju-walk']['rear1']['likes'])
        self.assertNotIn('전시·문화', profiles['jeju-walk']['rear1']['likes'])

    def test_samples_keep_travel_limits_without_inventing_a_gps_origin(self):
        expected = {'nearby-lunch': ('성수역', 30), 'seoul-rain': ('왕십리역', 40), 'gangneung-sea': ('판교역', 120),
                    'daejeon-family': ('대전역', 30), 'busan-friends': ('부산역', 40),
                    'jeju-walk': ('제주시청', 40)}
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario['id']):
                rows = self.rows(scenario)
                intent = resolve_search_intent(rows)
                origin, minutes = expected[scenario['id']]
                self.assertEqual(intent['origin_region'], origin)
                self.assertEqual(intent['origin_candidates'], [origin] if origin else [])
                self.assertFalse(intent['needs_area_choice'])
                profile = request_profile(extract(rows)[0])
                self.assertEqual(profile['travel_max_minutes'], minutes)
                self.assertFalse(profile['needs_more_preferences'])

    def test_open_destination_sea_example_uses_a_departure_without_preselecting_a_city(self):
        scenario=next(s for s in SCENARIOS if s['id']=='gangneung-sea')
        rows=self.rows(scenario)
        intent=resolve_search_intent(rows)
        profile=request_profile(extract(rows)[0])
        self.assertEqual(intent['origin_region'],'판교역')
        self.assertEqual(intent['search_regions'],[])
        self.assertIsNone(intent['region'])
        self.assertFalse(intent['needs_area_choice'])
        self.assertEqual(profile['travel_max_minutes'],120)
        self.assertTrue(any('해변' in p['likes'] for p in extract(rows)[0]))

    def test_all_examples_leave_destination_open_and_keep_specific_tastes(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario['id']):
                rows=self.rows(scenario)
                intent=resolve_search_intent(rows)
                profile=request_profile(extract(rows)[0])
                self.assertIsNotNone(intent['origin_region'])
                self.assertEqual(intent['search_regions'],[])
                self.assertIsNone(intent['region'])
                self.assertIsNotNone(profile['travel_max_minutes'])
        profiles={s['id']:extract(self.rows(s))[0] for s in SCENARIOS}
        lunch_likes={topic for p in profiles['nearby-lunch'] for topic in p['likes']}
        birthday_likes={topic for p in profiles['busan-friends'] for topic in p['likes']}
        walk_likes={topic for p in profiles['jeju-walk'] for topic in p['likes']}
        self.assertTrue({'칼국수','돈까스','카페'} <= lunch_likes)
        self.assertTrue({'파스타','피자'} <= birthday_likes)
        self.assertTrue({'산책','카페','빵·디저트'} <= walk_likes)

    def test_budget_examples_distinguish_shared_meals_special_occasions_and_no_limit(self):
        profiles = {s['id']: request_profile(extract(self.rows(s))[0]) for s in SCENARIOS}
        self.assertIn('전체 예산 120,000원 언급', profiles['gangneung-sea']['requirements'])
        self.assertNotIn('전체 예산 80,000원 언급', profiles['daejeon-family']['requirements'])
        self.assertIn('전체 예산 100,000원 언급', profiles['daejeon-family']['requirements'])
        self.assertIn('1인 예산 50,000원 언급', profiles['busan-friends']['requirements'])
        self.assertFalse(any('예산 ' in value for value in profiles['jeju-walk']['requirements']))

    def test_replying_to_a_friend_does_not_reverse_the_speakers_own_preference(self):
        profiles = {s['id']: {p['seat']: p for p in extract(self.rows(s))[0]} for s in SCENARIOS}
        self.assertIn('회', profiles['gangneung-sea']['rear2']['likes'])
        self.assertNotIn('회', profiles['gangneung-sea']['rear2']['dislikes'])
        self.assertNotIn('칼국수', profiles['nearby-lunch']['rear2']['dislikes'])
        self.assertNotIn('카페', profiles['nearby-lunch']['rear2']['dislikes'])
        self.assertNotIn('카페', profiles['jeju-walk']['rear2']['dislikes'])

    def test_sample_offsets_follow_continuous_speech_in_seconds(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario['id']):
                segments=sample_segments(scenario['dialogue'])
                self.assertEqual(segments[0]['start'],0)
                self.assertEqual([(s['seat'],s['text']) for s in segments],list(scenario['dialogue']))
                durations=[round(s['end']-s['start'],2) for s in segments]
                self.assertTrue(all(2<=duration<=8 for duration in durations))
                self.assertGreater(len(set(durations)),3)
                for previous,current in zip(segments,segments[1:]):
                    gap=round(current['start']-previous['end'],2)
                    self.assertGreaterEqual(gap,0.3)
                    self.assertLessEqual(gap,0.8)
                self.assertLess(segments[-1]['end'],210)
                self.assertTrue(all(s['seat_source']=='sample' for s in segments))

    def test_longer_sample_lines_take_longer_than_brief_replies(self):
        dialogue=[('driver','좋아.'),('passenger','가게도 안 골랐는데 나눠 먹을 계획은 다 세웠네. 주차장 넓은 식당이면 더 좋겠다.')]
        short,long=sample_segments(dialogue)
        self.assertGreater(long['end']-long['start'],short['end']-short['start'])
        with patch('demo_conversations.random.choice',return_value={'id':'timing','title':'시간 확인','dialogue':dialogue}):
            state=enter_demo({'segments':[]})
        self.assertEqual([(s['start'],s['end']) for s in state['segments']],
                         [(s['start'],s['end']) for s in (short,long)])

    def test_sample_clocks_end_at_requested_time_and_follow_natural_offsets(self):
        ended_at = datetime(2026, 11, 12, 9, 30, 0, 123000, timezone(timedelta(hours=9)))
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario['id']):
                segments = sample_segments(scenario['dialogue'], ended_at=ended_at)
                base = ended_at.astimezone(timezone.utc) - timedelta(seconds=segments[-1]['end'])
                for segment in segments:
                    expected = (base + timedelta(seconds=segment['start'])).isoformat(
                        timespec='milliseconds').replace('+00:00', 'Z')
                    self.assertEqual(segment['spoken_at'], expected)
                    self.assertEqual(segment['time_source'], 'sample')
                    self.assertLessEqual(datetime.fromisoformat(segment['spoken_at'].replace('Z', '+00:00')),
                                         ended_at)
                final_start = datetime.fromisoformat(segments[-1]['spoken_at'].replace('Z', '+00:00'))
                self.assertEqual(final_start + timedelta(seconds=segments[-1]['end'] - segments[-1]['start']),
                                 ended_at)

    def test_default_sample_clock_is_in_the_past_and_survives_copy_and_reload(self):
        before = datetime.now(timezone.utc)
        segments = sample_segments(SCENARIOS[0]['dialogue'])
        after = datetime.now(timezone.utc)
        clocks = [datetime.fromisoformat(segment['spoken_at'].replace('Z', '+00:00')) for segment in segments]
        final_end = clocks[-1] + timedelta(seconds=segments[-1]['end'] - segments[-1]['start'])
        self.assertGreaterEqual(final_end, before - timedelta(milliseconds=1))
        self.assertLessEqual(final_end, after)
        self.assertTrue(all(clock <= after for clock in clocks))
        copied = copy.deepcopy(segments)
        reloaded = json.loads(json.dumps(segments, ensure_ascii=False))
        self.assertEqual(copied, segments)
        self.assertEqual(reloaded, segments)
        copied[0]['spoken_at'] = '2020-01-01T00:00:00.000Z'
        self.assertEqual(segments, reloaded)

    def test_sample_clock_rejects_naive_end_time(self):
        with self.assertRaises(ValueError):
            sample_segments(SCENARIOS[0]['dialogue'], ended_at=datetime(2026, 11, 12, 9, 30))

    def test_random_selection_excludes_previous_but_keeps_all_other_scenarios(self):
        for previous in SCENARIOS:
            with patch('demo_conversations.random.choice', side_effect=lambda options: options[0]) as choose:
                chosen = random_scenario(previous['id'])
                candidates = choose.call_args.args[0]
                self.assertEqual({s['id'] for s in candidates}, {s['id'] for s in SCENARIOS} - {previous['id']})
                self.assertNotEqual(chosen['id'], previous['id'])

    def test_legacy_replace_only_sample_can_exit_into_empty_real_conversation(self):
        original = {'demo': True, 'segments': [{'text': 'old sample'}], 'saved_places': [{'id': 'kept'}]}
        restored = leave_demo(enter_demo(original))
        self.assertFalse(restored['demo'])
        self.assertEqual(restored['segments'], [])
        self.assertEqual(restored['saved_places'], original['saved_places'])
        self.assertFalse(restored['_demo_backup'])

    def test_sample_exit_restores_original_recommendation_profile(self):
        original = {'segments': [{'text': '실제 대화'}], 'request_profile': {'travel_max_minutes': 20},
                    'recommendations': [{'id': 'kept'}], 'saved_places': [{'id': 'visit'}]}
        with patch('demo_conversations.random.choice', return_value=SCENARIOS[2]):
            sample = enter_demo(original)
        sample['request_profile'] = {'travel_max_minutes': 120}
        restored = leave_demo(sample)
        self.assertEqual(restored['request_profile'], original['request_profile'])
        self.assertEqual(restored['saved_places'], original['saved_places'])


if __name__ == '__main__':
    unittest.main()
