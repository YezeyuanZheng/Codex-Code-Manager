import unittest
from codemanager.retrieval_metrics import information_metrics


class InformationMetricsTests(unittest.TestCase):
    def test_support_is_not_waste_and_repetition_counts_as_exposure(self):
        result = information_metrics([{'a':[(0,20)]},{'a':[(10,30)]}],
            {'a':[(0,5)]}, {'a':[(3,12)]}, {'a':[(12,18)]}, {'a':[(18,25)]})
        self.assertEqual(result['source_exposure_characters'],40)
        self.assertEqual(result['unique_source_characters'],30)
        self.assertEqual(result['core_characters'],5)
        self.assertEqual(result['supporting_characters'],7)
        self.assertEqual(result['audited_useful_extra_characters'],6)
        self.assertEqual(result['audited_unrelated_characters'],7)
        self.assertEqual(result['uncertain_extra_characters'],5)
        self.assertEqual(result['repeated_source_characters'],10)
        self.assertEqual(result['important_unique_percent'],60)
        self.assertEqual(result['important_per_exposure_percent'],45)

    def test_uncertain_extras_are_not_automatically_called_unrelated(self):
        result = information_metrics([{'a':[(0,10)]}], {'a':[(0,2)]}, {})
        self.assertEqual(result['uncertain_extra_characters'],8)
        self.assertEqual(result['audited_unrelated_characters'],0)
        self.assertIsNone(information_metrics([], {}, {})['important_unique_percent'])

    def test_overlapping_excerpts_and_different_files(self):
        result = information_metrics([{'a':[(0,5),(2,7)],'b':[(0,5)]}],
                                      {'a':[(0,7)]}, {'b':[(0,5)]})
        self.assertEqual(result['repeated_source_characters'],3)
        self.assertEqual(result['unique_source_characters'],12)
        self.assertEqual(result['important_unique_percent'],100)
