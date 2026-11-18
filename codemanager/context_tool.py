"""MCP schema and usage guidance for the one-call context operation."""


def context_tool():
    strings = {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1}
    return {
        'name': 'get_context',
        'description': 'Retrieve repository source with citations under one byte budget. '
        'Batch focused queries for missing workflow steps and pass known exact names in symbols. '
        'Resume truncated source with context_token and the needed entries copied from pending. '
        'Set compact=false for graph diagnostics. The repository is already bound.',
        'inputSchema': {'type': 'object', 'properties': {
            'query': {'type': 'string'},
            'queries': {**strings, 'maxItems': 8},
            'symbols': {**strings, 'maxItems': 20,
                        'description': 'Exact names, qualified names, or path.py:symbol'},
            'node_ids': {**strings, 'maxItems': 20},
            'offsets': {'type': 'object', 'additionalProperties': {'type': 'integer', 'minimum': 0}},
            'pending': {'type': 'array', 'minItems': 1, 'maxItems': 20,
                        'items': {'type': 'object', 'properties': {
                            'id': {'type': 'string'}, 'offset': {'type': 'integer', 'minimum': 0}},
                            'required': ['id', 'offset'], 'additionalProperties': False}},
            'budget': {'type': 'integer', 'minimum': 256, 'maximum': 100000, 'default': 12000,
                       'description': 'Maximum serialized response bytes.'},
            'budget_bytes': {'type': 'integer', 'minimum': 256, 'maximum': 100000, 'deprecated': True,
                             'description': 'Compatibility alias for budget. Prefer budget; values must match if both are supplied.'},
            'max_bytes': {'type': 'integer', 'minimum': 256, 'maximum': 100000, 'deprecated': True,
                          'description': 'Compatibility alias for budget. Prefer budget; values must match if both are supplied.'},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50,
                      'description': 'Maximum selected nodes. Omit to size selection to the byte budget and explicit requests.'},
            'context_token': {'type':'string', 'pattern':'^[a-f0-9]{32}$',
                              'description':'Prior response or hook token; unchanged source is not repeated.'},
            'compact': {'type': 'boolean', 'default': True},
            'include_seen': {'type':'boolean', 'default':False},
            'bm25_only': {'type':'boolean', 'default':False,
                          'description':'Use keyword-only BM25 without initializing or embedding a model.'},
            'mode': {'type': 'string', 'enum': ['hybrid', 'bm25', 'semantic']},
            'model': {'type': 'string'},
            'include_related': {'type': 'boolean', 'default': True}},
            'anyOf': [{'required': [name]} for name in ('query', 'queries', 'symbols', 'node_ids', 'pending')],
            'additionalProperties': False}}
