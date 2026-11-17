"""MCP schema and usage guidance for the one-call context operation."""


def context_tool():
    strings = {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1}
    return {
        'name': 'get_context',
        'description': 'Preferred code retrieval: batch independent questions, exact symbols, or node IDs '
        'in one call. Searches, expands bounded one-hop context, and returns deduplicated source under '
        'one shared byte budget. Completes small functions where possible; related containers remain '
        'pointers. If a query names code identifiers, also pass those exact names in symbols; keep '
        'ordinary prose in the query. Answer directly when sufficient; do not add another expansion call. '
        'The repository is already bound: omit repo. Example: {"query":"QUESTION","budget":12000}. '
        'Pass context_token from the hook or preceding response to avoid repeats. '
        'Batch follow-up symbols and use per-node offsets for continuations; graph relations are hints.',
        'inputSchema': {'type': 'object', 'properties': {
            'query': {'type': 'string'},
            'queries': {**strings, 'maxItems': 8},
            'symbols': {**strings, 'maxItems': 20,
                        'description': 'Exact names, qualified names, or path.py:symbol'},
            'node_ids': {**strings, 'maxItems': 20},
            'offsets': {'type': 'object', 'additionalProperties': {'type': 'integer', 'minimum': 0}},
            'budget': {'type': 'integer', 'minimum': 256, 'maximum': 100000, 'default': 12000,
                       'description': 'Shared serialized byte budget; also returned as budget in the response.'},
            'budget_bytes': {'type': 'integer', 'minimum': 256, 'maximum': 100000, 'deprecated': True,
                             'description': 'Compatibility alias for budget. Prefer budget; values must match if both are supplied.'},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50,
                      'description': 'Maximum selected nodes. Omit to size selection to the byte budget and explicit requests.'},
            'context_token': {'type':'string', 'pattern':'^[a-f0-9]{32}$',
                              'description':'Prior response or hook token; unchanged source is not repeated.'},
            'include_seen': {'type':'boolean', 'default':False},
            'bm25_only': {'type':'boolean', 'default':False,
                          'description':'Use keyword-only BM25 without initializing or embedding a model.'},
            'mode': {'type': 'string', 'enum': ['hybrid', 'bm25', 'semantic']},
            'model': {'type': 'string'},
            'include_related': {'type': 'boolean', 'default': True}},
            'anyOf': [{'required': [name]} for name in ('query', 'queries', 'symbols', 'node_ids')],
            'additionalProperties': False}}
