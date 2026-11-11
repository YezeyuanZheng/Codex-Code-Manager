import argparse
import json
import sys
from .graph import Graph


def serve(graph):
    """Newline-delimited JSON-RPC over stdio; stdout is protocol-only."""
    tools = [
        {'name': 'search_code', 'description': 'Search the indexed repository before reading whole files. Returns budgeted source snippets and candidate graph neighbors. Run index_repository after edits.', 'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string'}, 'budget': {'type': 'integer', 'minimum': 256, 'maximum': 100000, 'default': 6000}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 8}}, 'required': ['query'], 'additionalProperties': False}},
        {'name': 'index_repository', 'description': 'Incrementally refresh the local code graph after repository edits. Does not execute repository code.', 'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
        {'name': 'graph_stats', 'description': 'Get counts of indexed files, code nodes, and edges.', 'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    ]
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError('Expected JSON-RPC object')
            if 'id' not in request:
                continue
            method = request.get('method')
            params = request.get('params', {})
            if method == 'initialize':
                result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'codemanager', 'version': '0.1.0'}}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': tools}
            elif method == 'tools/call':
                name = params.get('name')
                args = params.get('arguments', {})
                try:
                    if name == 'search_code':
                        value = graph.search(**args)
                    elif name == 'index_repository' and not args:
                        value = graph.index()
                    elif name == 'graph_stats' and not args:
                        value = graph.stats()
                    else:
                        raise ValueError('Unknown tool or invalid arguments')
                    result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}], 'isError': False}
                except (ValueError, TypeError, OSError) as exc:
                    result = {'content': [{'type': 'text', 'text': str(exc)}], 'isError': True}
            else:
                print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'error': {'code': -32601, 'message': 'Method not found'}}), flush=True)
                continue
            response = {'jsonrpc': '2.0', 'id': request['id'], 'result': result}
        except (ValueError, TypeError, KeyError) as exc:
            response = {'jsonrpc': '2.0', 'id': request.get('id') if isinstance(request, dict) else None, 'error': {'code': -32600, 'message': str(exc)}}
        print(json.dumps(response), flush=True)


def main():
    parser = argparse.ArgumentParser(description='Local repository graph and budgeted code search')
    sub = parser.add_subparsers(dest='command', required=True)
    for command in ('index', 'export', 'search', 'stats', 'serve'):
        p = sub.add_parser(command)
        p.add_argument('--repo', required=True, help='Path to a local checkout')
        if command == 'search':
            p.add_argument('query')
            p.add_argument('--budget', type=int, default=6000, help='Maximum serialized result bytes')
            p.add_argument('--limit', type=int, default=8)
    args = parser.parse_args()
    graph = None
    try:
        graph = Graph(args.repo)
        if args.command == 'serve':
            serve(graph)
            return
        if args.command == 'index':
            value = graph.index()
        elif args.command == 'export':
            value = graph.export()
        elif args.command == 'stats':
            value = graph.stats()
        else:
            value = graph.search(args.query, args.budget, args.limit)
        print(json.dumps(value, ensure_ascii=False))
    except (ValueError, OSError) as exc:
        parser.exit(1, f'codemanager: {exc}\n')
    finally:
        if graph:
            graph.close()


if __name__ == '__main__':
    main()
