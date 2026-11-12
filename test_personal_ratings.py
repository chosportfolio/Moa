import copy
import unittest
from personal_ratings import rate_visit, public_ratings, personalize


class RatingTests(unittest.TestCase):
    def setUp(self):
        self.state={'saved_places':[
            {'id':'real','place':{'id':'n1'},'demo':False,'arrived_at':'today'},
            {'id':'sample','place':{'id':'n1'},'demo':True,'arrived_at':'today'}],
            'recommendations':[{'id':'n1'}], 'demo':False}

    def test_sample_feedback_isolated_from_real_visits_and_inputs_unchanged(self):
        original=copy.deepcopy(self.state)
        rated=rate_visit(self.state,{'visit_id':'sample','place_id':'n1','rating':1})
        public=public_ratings(copy.deepcopy(rated))
        self.assertEqual(self.state,original)
        self.assertIsNone(public['recommendations'][0].get('my_rating'))
        self.assertIsNone(public['saved_places'][0]['place'].get('my_rating'))
        self.assertEqual(public['saved_places'][1]['place']['my_rating']['rating'],1)
        self.assertNotIn('_place_ratings', public)

    def test_invalid_or_unvisited_rating_rejected(self):
        for changes in ({'rating':True},{'rating':0},{'rating':6},{'rating':3.5},{'note':'x'*501},{'note':3},{'visit_id':'missing'},{'place_id':'other'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                rate_visit(self.state,{'visit_id':'real','place_id':'n1','rating':5,**changes})
        self.state['saved_places'][0]['arrived_at']=None
        with self.assertRaises(ValueError):
            rate_visit(self.state,{'visit_id':'real','place_id':'n1','rating':5})

    def test_rating_breaks_ties_without_overriding_relevance_or_diversity(self):
        places=[{'id':'best','score':10,'group':'음식점','matched_seats':['driver']},
                {'id':'n1','score':5,'group':'음식점','matched_seats':['driver']},
                {'id':'park','score':2,'group':'산책','matched_seats':['passenger']},
                {'id':'n2','score':5,'group':'음식점','matched_seats':['driver']}]
        result=personalize(places,{'visit:n1':{'rating':1},'visit:n2':{'rating':5}})
        self.assertEqual([p['id'] for p in result],['best','n2','park','n1'])
        self.assertNotIn('my_rating',places[0])


if __name__=='__main__': unittest.main()
