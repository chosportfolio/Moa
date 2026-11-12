import unittest
from budget_mentions import requirements, update


class BudgetMentionTests(unittest.TestCase):
    def test_group_budget_is_not_per_person(self):
        self.assertEqual(requirements('넷이 다 해서 12만 원까지는 괜찮아'), ['전체 예산 120,000원 언급'])
        self.assertEqual(requirements('한 명당 1만 2천 원 안팎으로 먹자'), ['1인 예산 12,000원 언급'])

    def test_comma_shorthand_and_range_keep_the_amount(self):
        self.assertEqual(requirements('음료는 6,000원씩 생각하자'), ['1인 예산 6,000원 언급'])
        self.assertEqual(requirements('난 만 원 정도면 돼'), ['예산 10,000원 언급'])
        self.assertEqual(requirements('전체 10~12만 원 정도?'), ['전체 예산 100,000~120,000원 언급'])
        self.assertEqual(requirements('1인 1.5만 원'), ['1인 예산 15,000원 언급'])

    def test_scope_does_not_leak_between_amounts(self):
        self.assertEqual(requirements('1인 2만원 전체 8만원'), ['1인 예산 20,000원 언급', '전체 예산 80,000원 언급'])
        self.assertEqual(requirements('한 사람당 음료랑 디저트 합쳐서 1만원'), ['1인 예산 10,000원 언급'])
        self.assertEqual(requirements('돈은 아직 안 정했어'), [])

    def test_written_korean_and_mixed_money_units(self):
        for text, expected in [('1인 만 이천 원까지', '1인 예산 12,000원 언급'),
                               ('만오천 원 정도', '예산 15,000원 언급'),
                               ('삼만오천 원', '예산 35,000원 언급'),
                               ('전체 십이만 원', '전체 예산 120,000원 언급'),
                               ('이십오만 삼천오백 원', '예산 253,500원 언급'),
                               ('2만 오천 원', '예산 25,000원 언급')]:
            with self.subTest(text=text):
                self.assertEqual(requirements(text), [expected])

    def test_scope_can_touch_an_arabic_amount(self):
        self.assertEqual(requirements('인당2만원까지'), ['1인 예산 20,000원 언급'])
        self.assertEqual(requirements('넷이서총12만원안으로'), ['전체 예산 120,000원 언급'])

    def test_written_and_numeric_ranges(self):
        self.assertEqual(requirements('만원~만오천원'), ['예산 10,000~15,000원 언급'])
        self.assertEqual(requirements('12,000~15,000원'), ['예산 12,000~15,000원 언급'])
        self.assertEqual(requirements('1인 1.2~1.5만원'), ['1인 예산 12,000~15,000원 언급'])
        self.assertEqual(requirements('12~10만원'), [])

    def test_non_money_words_do_not_invent_a_budget(self):
        for text in ('카페만 원해', '공원에서 쉬자', '만원해', '돈은 아직 안 정했어', '-1000원', '0원', '이억원'):
            with self.subTest(text=text):
                self.assertEqual(requirements(text), [])

    def test_shared_upper_bound_replaces_a_rough_estimate(self):
        records = []
        update(records, '오늘 밥값은 넷이서 8만 원쯤이면 괜찮죠?', 'driver', 'old')
        update(records, '같이 먹을 거 하나 더 시키고. 그래도 총 10만 원은 넘기지 말자.', 'passenger', 'new')
        self.assertEqual([value['requirement'] for value in records], ['전체 예산 100,000원 언급'])
        self.assertEqual(records[0]['segment_id'], 'new')
        self.assertEqual(records[0]['category'], 'meal')

    def test_same_utterance_shared_estimate_and_cap_use_the_cap(self):
        records = update([], '총 8만원쯤 생각하되 총 10만원은 넘기지 말자.', 'driver', 'a')
        self.assertEqual([value['requirement'] for value in records], ['전체 예산 100,000원 언급'])

    def test_personal_revision_preserves_other_people_and_other_categories(self):
        records = []
        update(records, '식사는 1인 2만원까지', 'driver', 'a')
        update(records, '커피는 1인 6천원 정도', 'driver', 'b')
        update(records, '식사는 1인 1만원까지', 'passenger', 'c')
        update(records, '아니다 커피는 1인 8천원으로 바꾸자', 'driver', 'd')
        self.assertEqual([(value['seat'], value['category'], value['requirement']) for value in records], [
            ('driver', 'meal', '1인 예산 20,000원 언급'),
            ('passenger', 'meal', '1인 예산 10,000원 언급'),
            ('driver', 'drink', '1인 예산 8,000원 언급')])

    def test_different_shared_hard_limits_are_not_assumed_to_be_agreement(self):
        records = []
        update(records, '식사 총 8만원까지', 'driver', 'a')
        update(records, '식사 총 10만원까지', 'passenger', 'b')
        self.assertEqual(len(records), 2)

    def test_unidentified_speakers_do_not_replace_each_others_personal_budget(self):
        records = []
        update(records, '1인 2만원 정도', 'unknown', 'a')
        update(records, '아니다 1인 1만원까지', 'unknown', 'b')
        self.assertEqual(len(records), 2)

    def test_explicit_rejected_amount_is_not_a_current_requirement(self):
        self.assertEqual(requirements('총 8만원 말고 총 10만원으로 바꾸자'), ['전체 예산 100,000원 언급'])
