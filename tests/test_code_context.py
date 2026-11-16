import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codemanager.graph import Graph
from codemanager.source_coverage import SourceCoverage


class CodeContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.source = self.repo / 'app.py'
        self.source.write_text('def entry(value):\n    return validate(value) + persist(value)\n\n'
                               'def validate(value):\n    return value > 0\n\n'
                               'def persist(value):\n    return str(value)\n')
        self.graph = Graph(self.repo)
        self.addCleanup(self.graph.close)
        self.graph.export()

    def assert_disjoint(self, result):
        coverage = SourceCoverage()
        for item in result['results']:
            row = dict(self.graph.db.execute('SELECT * FROM nodes WHERE id=?', (item['id'],)).fetchone())
            start, length = item['offset'], len(item['body'])
            self.assertEqual(list(coverage.uncovered(row, start, length)), [(start, start+length)])
            self.assertEqual(row['body'][start:start+length], item['body'])
            coverage.add(row, start, length)

    def test_one_symbol_batch_returns_complete_functions_without_embedding(self):
        self.source.write_text('def validate(value):\n'+''.join(
            f'    value += {number}\n' for number in range(90))+'    return value\n\n'
            'def persist(value):\n    return str(value)\n')
        self.graph.index()
        with patch.object(self.graph, 'embedding_provider', side_effect=AssertionError('No model needed')):
            result = self.graph.get_context(symbols=['validate','persist'], include_related=False)
        self.assertEqual(result['mode'], 'lookup')
        self.assertEqual({item['name'] for item in result['results']}, {'validate','persist'})
        validate = next(item for item in result['results'] if item['name']=='validate')
        self.assertGreater(len(validate['body']), 1200)
        self.assertIn('return value', validate['body'])
        self.assertTrue(all(item['next_offset'] is None for item in result['results']))

    def test_selected_node_expands_callers_and_callees_without_a_second_call(self):
        result = self.graph.get_context(node_ids=['app.py:1:entry'])
        self.assertEqual({item['name'] for item in result['results']}, {'entry','validate','persist'})
        self.assertTrue(any(hint['kind']=='calls_candidate' for hint in result['related']))
        self.assert_disjoint(result)

    def test_independent_queries_share_one_context_request(self):
        with patch.object(self.graph, '_search_candidates', wraps=self.graph._search_candidates) as search:
            result = self.graph.get_context(queries=['validate','persist'], mode='bm25', include_related=False)
        self.assertEqual(search.call_count, 2)
        self.assertTrue({'validate','persist'} <= {item['name'] for item in result['results']})
        self.assertIn('def entry(value):', ''.join(item['body'] for item in result['results']))
        self.assert_disjoint(result)

    def test_containers_stay_pointers_and_covered_nodes_keep_identity(self):
        self.source.write_text('class Box:\n    def small(self):\n        return 17\n\n'
                               '    def big(self):\n        return "'+'x'*8000+'"\n')
        self.graph.index()
        result = self.graph.get_context(symbols=['Box.small'])
        self.assertEqual([item['name'] for item in result['results']], ['Box.small'])
        self.assertTrue(any(hint['id']=='app.py:1:Box' for hint in result['related']))
        both = self.graph.get_context(symbols=['Box.small','Box'], include_related=False)
        self.assert_disjoint(both)
        self.assertEqual(''.join(item['body'] for item in both['results']).count('return 17'), 1)

    def test_per_node_continuations_are_batched_and_unicode_budgets_hold(self):
        self.source.write_text('def first():\n    return "'+'é'*3000+'"\n\n'
                               'def second():\n    return "'+'ø'*3000+'"\n')
        self.graph.index()
        for budget in (256, 400, 1000, 3000, 6000, 12000):
            result = self.graph.get_context(symbols=['first','second'], budget=budget, include_related=False)
            self.assertLessEqual(len(json.dumps(result,ensure_ascii=False).encode()), budget)
            self.assert_disjoint(result)
        initial = self.graph.get_context(symbols=['first','second'], budget=3000, include_related=False)
        self.assertEqual(len(initial['results']), 2)
        cursors = {item['id']:item['next_offset'] for item in initial['results']}
        self.assertTrue(all(value is not None for value in cursors.values()))
        followup = self.graph.get_context(node_ids=list(cursors), offsets=cursors,
                                          budget=3000, include_related=False)
        for item in followup['results']:
            self.assertEqual(item['offset'], cursors[item['id']])
        self.assert_disjoint(followup)

    def test_missing_nodes_and_ambiguous_symbols_do_not_discard_other_results(self):
        (self.repo/'other.py').write_text('def validate(value):\n    return bool(value)\n')
        self.graph.index()
        result = self.graph.get_context(symbols=['validate','missing'],node_ids=['app.py:99:old'],
                                         include_related=False)
        self.assertEqual({item['path'] for item in result['results']}, {'app.py','other.py'})
        self.assertEqual({item['reason'] for item in result['unresolved']}, {'not_found','stale_or_unknown'})
        qualified = self.graph.get_context(symbols=['other.py:validate'], include_related=False)
        self.assertEqual([item['path'] for item in qualified['results']], ['other.py'])

    def test_limit_exposes_deferred_nodes_and_rendering_does_not_mutate_metadata(self):
        result = self.graph.get_context(symbols=['entry','validate','persist'], limit=1, include_related=False)
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(len(result['pending']), 2)
        self.assertEqual(len({item['id'] for item in result['pending']}), 2)
        self.assertTrue(result['truncated'])

    def test_shared_source_does_not_request_redundant_continuations(self):
        self.source.write_text('class Box:\n    def small(self):\n        return 17\n')
        self.graph.index()
        result = self.graph.get_context(symbols=['Box.small','Box'], include_related=False)
        self.assert_disjoint(result)
        self.assertFalse(result['truncated'])
        self.assertNotIn('pending', result)
        self.assertTrue(all(item['next_offset'] is None for item in result['results']))

    def test_module_qualified_lookup_and_bounded_ambiguity(self):
        (self.repo/'package').mkdir()
        (self.repo/'package/extra.py').write_text('class Handler:\n    def handle(self):\n        return 1\n')
        for number in range(5):
            (self.repo/f'copy_{number}.py').write_text('def validate(value):\n    return value\n')
        self.graph.index()
        qualified = self.graph.get_context(symbols=['package.extra.Handler.handle'], include_related=False)
        self.assertEqual([item['name'] for item in qualified['results']], ['Handler.handle'])
        ambiguous = self.graph.get_context(symbols=['validate'], include_related=False)
        self.assertEqual(ambiguous['unresolved'][0]['reason'], 'ambiguous_more_matches')

    def test_invalid_inputs_and_offsets_are_rejected(self):
        requests = [{}, {'query':''}, {'queries':'validate'}, {'symbols':[None]},
                    {'symbols':['entry'],'budget':True}, {'symbols':['entry'],'limit':0},
                    {'symbols':['entry'],'mode':'wrong'}, {'symbols':['entry'],'include_related':1},
                    {'node_ids':['app.py:1:entry'],'offsets':{'wrong':0}},
                    {'node_ids':['app.py:1:entry'],'offsets':{'app.py:1:entry':-1}},
                    {'node_ids':['app.py:1:entry'],'offsets':{'app.py:1:entry':99999}}]
        for request in requests:
            with self.subTest(request=request), self.assertRaises(ValueError):
                self.graph.get_context(**request)

    def test_cli_and_mcp_each_return_a_symbol_batch_in_one_invocation(self):
        env = {**os.environ,'CODEMANAGER_SEARCH_MODE':'bm25'}
        command = [sys.executable,'-m','codemanager']
        cli = subprocess.run(command+['context','--repo',str(self.repo),'--symbol','validate',
            '--symbol','persist','--no-related'],capture_output=True,text=True,check=True,env=env)
        self.assertEqual({item['name'] for item in json.loads(cli.stdout)['results']}, {'validate','persist'})
        request = {'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'get_context',
            'arguments':{'symbols':['validate','persist'],'include_related':False}}}
        mcp = subprocess.run(command+['serve','--repo',str(self.repo)],input=json.dumps(request)+'\n',
            capture_output=True,text=True,check=True,env=env)
        value = json.loads(mcp.stdout)['result']
        self.assertFalse(value['isError'])
        self.assertEqual({item['name'] for item in json.loads(value['content'][0]['text'])['results']},
                         {'validate','persist'})

    def test_removed_mcp_tools_are_rejected_with_the_replacement_name(self):
        requests = [{'jsonrpc':'2.0','id':number,'method':'tools/call',
                     'params':{'name':name,'arguments':{}}}
                    for number,name in enumerate(('search_code','retrieve_nodes'),1)]
        result = subprocess.run([sys.executable,'-m','codemanager','serve','--repo',str(self.repo)],
            input='\n'.join(map(json.dumps,requests))+'\n',capture_output=True,text=True,check=True)
        for line in result.stdout.splitlines():
            value = json.loads(line)['result']
            self.assertTrue(value['isError'])
            self.assertIn('use get_context',value['content'][0]['text'])
