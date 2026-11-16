"""MCP schema and usage guidance for the one-call context operation."""


def context_tool():
    strings = {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1}
    return {
        'name': 'get_context',
        'description': 'Preferred code retrieval: batch independent questions, exact symbols, or node IDs '
        'in one call. Searches, expands bounded one-hop context, and returns deduplicated source under '
        'one shared byte budget. Completes small functions where possible; related containers remain '
        'pointers. Answer directly when sufficient; do not add another expansion call. '
        'Batch follow-up symbols and use per-node offsets for continuations; graph relations are hints.',
        'inputSchema': {'type': 'object', 'properties': {
            'query': {'type': 'string'},
            'queries': {**strings, 'maxItems': 8},
            'symbols': {**strings, 'maxItems': 20,
                        'description': 'Exact names, qualified names, or path.py:symbol'},
            'node_ids': {**strings, 'maxItems': 20},
            'offsets': {'type': 'object', 'additionalProperties': {'type': 'integer', 'minimum': 0}},
            'budget': {'type': 'integer', 'minimum': 256, 'maximum': 100000, 'default': 12000},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 8},
            'mode': {'type': 'string', 'enum': ['hybrid', 'bm25', 'semantic']},
            'model': {'type': 'string'},
            'include_related': {'type': 'boolean', 'default': True}},
            'anyOf': [{'required': [name]} for name in ('query', 'queries', 'symbols', 'node_ids')],
            'additionalProperties': False}}
