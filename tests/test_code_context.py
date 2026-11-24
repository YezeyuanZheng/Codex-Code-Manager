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

    def test_attribute_callers_are_discovered_from_index_and_comments_are_excluded(self):
        self.source.write_text('class Validator:\n    def check(self, value):\n        return value > 0\n')
        (self.repo/'handler.py').write_text(
            'def dispatch(validator, value):\n    return validator.check(value)\n\n'
            'def misleading():\n    # validator.check(value)\n    return "check(value)"\n')
        self.graph.index()
        with patch('pathlib.Path.read_text', side_effect=AssertionError('Use indexed bodies')):
            result = self.graph.get_context(symbols=['Validator.check'])
        self.assertEqual({item['name'] for item in result['results']}, {'Validator.check', 'dispatch'})
        self.assertTrue(any(hint['kind']=='text_call_candidate' and hint['id'].endswith(':dispatch')
                            for hint in result['related']))
        self.assertTrue(all(item['next_offset'] is None for item in result['results']))
        self.assert_disjoint(result)

    def test_queries_reserve_space_for_integration_and_refill_without_neighbors(self):
        rows = [dict(self.graph.db.execute('SELECT * FROM nodes WHERE name=?', (name,)).fetchone())
                for name in ('validate', 'persist')]
        candidates = [(row, 'search') for row in rows]
        with patch.object(self.graph, '_search_candidates', return_value=(candidates, None, 'bm25')):
            result = self.graph.get_context(query='validation behavior', limit=2, mode='bm25')
            self.assertEqual({item['name'] for item in result['results']}, {'validate', 'entry'})
            direct = self.graph.get_context(query='validation behavior', limit=2, include_related=False)
            self.assertEqual({item['name'] for item in direct['results']}, {'validate', 'persist'})
            with patch('codemanager.code_context.relationships', return_value=[]):
                fallback = self.graph.get_context(query='validation behavior', limit=2)
            self.assertEqual({item['name'] for item in fallback['results']}, {'validate', 'persist'})
            self.assertFalse(fallback.get('pending'))

    def test_ranking_prioritizes_implementation_and_respects_test_questions(self):
        from codemanager.caller_context import rank_candidates
        candidates = [({'path':'tests/test_api.py', 'kind':'function'}, 'search'),
                      ({'path':'api.py', 'kind':'chunk'}, 'search'),
                      ({'path':'api.py', 'kind':'function'}, 'search')]
        ranked = rank_candidates(candidates, 'Where is request handling implemented?')
        self.assertEqual(ranked[0][0]['path'], 'api.py')
        self.assertEqual(ranked[0][0]['kind'], 'function')
        tests = rank_candidates(candidates, 'Which tests cover request handling?')
        self.assertEqual(tests[0][0]['path'], 'tests/test_api.py')
        self.assertEqual(rank_candidates(candidates, 'Find documentation for request handling'), candidates)

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

    def test_compact_method_hints_offer_exact_followup_without_callee_source(self):
        self.source.write_text(
            'class Manager:\n'
            '    def start(self):\n'
            '        return self._configure()\n'
            '    def _configure(self):\n'
            '        return 42\n'
        )
        self.graph.index()
        result = self.graph.get_context(symbols=['Manager.start'], compact=True, limit=1)
        self.assertEqual(result['called_symbols'], ['app.py:Manager._configure'])
        self.assertEqual(len(result['results']), 1)
        followup = self.graph.get_context(symbols=result['called_symbols'],
                                          context_token=result['context_token'], compact=True)
        self.assertIn('return 42', followup['results'][0]['body'])

        self.source.write_text(self.source.read_text().replace('return self._configure()', 'return 0'))
        self.graph.index()
        self.assertEqual(self.graph.db.execute('SELECT count(*) FROM method_calls').fetchone()[0], 0)

    def test_compact_pending_can_be_sent_back_and_preserves_complete_lines(self):
        source = 'def long_method():\n' + ''.join(
            f'    value_{index} = {index}\n' for index in range(100)
        ) + '    return value_99\n'
        self.source.write_text(source)
        self.graph.index()
        initial = self.graph.get_context(symbols=['long_method'], budget=1000, compact=True,
                                         include_related=False)
        prefix = initial['results'][0]['body']
        self.assertTrue(prefix.endswith('\n') or source[len(prefix)] == '\n')
        followup = self.graph.get_context(pending=initial['pending'], context_token=initial['context_token'],
                                          compact=True, include_related=False)
        self.assertEqual(followup['mode'], 'lookup')
        self.assertEqual(prefix + followup['results'][0]['body'], source.rstrip('\n'))

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

    def test_budget_name_is_consistent_and_legacy_alias_is_accepted(self):
        canonical = self.graph.get_context(symbols=['validate'], budget=3000, include_related=False)
        alias = self.graph.get_context(symbols=['validate'], budget_bytes=3000, include_related=False)
        canonical.pop('context_token'); alias.pop('context_token')
        self.assertEqual(canonical, alias)
        self.assertEqual(alias['budget'], 3000)
        self.assertNotIn('budget_bytes', alias)
        matching = self.graph.get_context(symbols=['validate'], budget=3000, budget_bytes=3000,
                                          include_related=False)
        matching.pop('context_token')
        self.assertEqual(matching, canonical)
        for options in ({'budget':3000,'budget_bytes':4000}, {'budget_bytes':True},
                        {'budget_bytes':255}, {'budget':True,'budget_bytes':3000},
                        {'max_bytes':True}, {'budget_bytes':3000,'max_bytes':4000}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.graph.get_context(symbols=['validate'], **options)

    def test_cli_and_mcp_accept_the_budget_alias(self):
        cli = subprocess.run([sys.executable,'-m','codemanager','context','--repo',str(self.repo),
            '--symbol','validate','--budget-bytes','3000','--no-related'],
            capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(cli.stdout)['budget'],3000)
        request = {'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'get_context',
            'arguments':{'symbols':['validate'],'budget_bytes':3000,'include_related':False,'compact':False}}}
        for alias in ('budget_bytes', 'max_bytes'):
            with self.subTest(alias=alias):
                request['params']['arguments'] = {
                    'symbols':['validate'], alias:3000, 'include_related':False, 'compact':False}
                result = subprocess.run([sys.executable,'-m','codemanager','serve','--repo',str(self.repo)],
                    input=json.dumps(request)+'\n',capture_output=True,text=True,check=True)
                value = json.loads(result.stdout)['result']
                self.assertFalse(value['isError'])
                self.assertEqual(json.loads(value['content'][0]['text'])['budget'],3000)

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
        result = json.loads(value['content'][0]['text'])
        self.assertEqual({item['body'].split('(')[0].strip() for item in result['results']},
                         {'def validate', 'def persist'})
        self.assertTrue(all(set(item) == {'path', 'start', 'end', 'body'}
                            for item in result['results']))

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

    def test_flow_query_includes_direct_called_helper(self):
        self.source.write_text('def fixture_flow(value):\n    return call_fixture_func(value)\n\n'
                               'def call_fixture_func(value):\n    return value\n\n'
                               'def unrelated():\n    return 0\n')
        self.graph.index()
        result = self.graph.get_context(query='Trace fixture_flow through call_fixture_func',
                                        mode='bm25', budget=6000, limit=4)
        names = {item['name'] for item in result['results']}
        self.assertIn('fixture_flow', names)
        self.assertIn('call_fixture_func', names)
        self.assertNotIn('unrelated', names)
        helper = next(item for item in result['results'] if item['name']=='call_fixture_func')
        self.assertTrue(any(site['expression']=='call_fixture_func(value)'
                            for site in helper.get('call_sites', [])))
        self.assert_disjoint(result)

    def test_query_promotes_unique_code_identifier_without_search_hit(self):
        from codemanager.context_symbols import query_identifiers
        self.source.write_text('def call_fixture_func(value):\n    return value\n')
        self.graph.index()
        self.assertEqual(query_identifiers(
            ['Trace call_fixture_func through fixture teardown']), ['call_fixture_func'])
        with patch.object(self.graph, '_search_candidates', return_value=([], None, 'bm25')):
            result = self.graph.get_context(query='Trace call_fixture_func through fixture teardown',
                                            mode='bm25', include_related=False)
        self.assertEqual([item['name'] for item in result['results']], ['call_fixture_func'])

    def test_query_identifier_autopromotion_yields_to_explicit_symbols(self):
        self.source.write_text(
            'def requested(value):\n    return value\n\n'
            'def call_fixture_func(value):\n    return value\n')
        self.graph.index()
        with patch.object(self.graph, '_search_candidates', return_value=([], None, 'bm25')):
            result = self.graph.get_context(query='Inspect call_fixture_func', symbols=['requested'],
                                            mode='bm25', include_related=False)
        self.assertEqual([item['name'] for item in result['results']], ['requested'])

