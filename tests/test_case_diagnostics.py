import unittest

from codemanager.case_diagnostics import diagnose


class CaseDiagnosticsTests(unittest.TestCase):
    def test_pipeline_misses_and_partial_source(self):
        rows = [{'id': name, 'path': name + '.py', 'start': 1,
                 'body': 'abcdef\n'} for name in ('absent', 'ranked', 'clipped', 'full')]
        response = {'results': [
            {'id': 'clipped', 'offset': 2, 'body': 'cde'},
            {'id': 'full', 'offset': 0, 'body': 'abcdef\n'}]}
        result = diagnose(rows, ['ranked', 'clipped', 'full'], ['clipped', 'full'],
                          [response], {row['id']: row for row in rows})
        self.assertEqual([item['status'] for item in result],
                         ['not_in_candidates', 'not_selected', 'selected_incomplete', 'covered'])
        self.assertEqual(result[2]['missing_ranges'], [[0, 2], [5, 7]])

    def test_parent_source_covers_child_but_pointer_does_not(self):
        parent = {'id': 'parent', 'path': 'a.py', 'start': 1, 'body': 'header\nchild\n'}
        child = {'id': 'child', 'path': 'a.py', 'start': 2, 'body': 'child\n'}
        pointer = {'references': [{'id': 'child', 'covered_by': ['parent']}]}
        args = ([child], [], [], [pointer], {'parent': parent})
        self.assertEqual(diagnose(*args)[0]['status'], 'not_in_candidates')
        pointer['results'] = [{'id': 'parent', 'offset': 7, 'body': 'child\n'}]
        self.assertEqual(diagnose(*args)[0]['status'], 'covered')
        pointer['results'][0]['body'] = 'stale'
        with self.assertRaises(ValueError):
            diagnose(*args)
