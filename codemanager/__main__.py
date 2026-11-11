import argparse
import json
import queue
import sqlite3
import subprocess
import threading
import time
import sys
from .graph import Graph
from .automation import check_snapshot, install_hook, interval_seconds, watch


def incoming(graph, interval):
    """Keep SQLite on one thread; the reader thread only queues protocol input."""
    messages = queue.Queue()

    def read():
        try:
            for line in sys.stdin:
                messages.put(line)
        finally:
            messages.put(None)

    threading.Thread(target=read, daemon=True).start()
    deadline = 0
    while True:
        now = time.monotonic()
        if now >= deadline:
            try:
                graph.export()
            except (ValueError, OSError, sqlite3.Error) as exc:
                print(f'codemanager refresh: {exc}', file=sys.stderr, flush=True)
            deadline = time.monotonic() + interval
        try:
            line = messages.get(timeout=max(0, deadline - time.monotonic()))
        except queue.Empty:
            continue
        if line is None:
            return
        yield line


def serve(graph, interval=1):
    """Newline-delimited JSON-RPC over stdio; stdout is protocol-only."""
    tools = [
        {'name': 'search_code', 'description': 'Search the indexed repository before reading whole files. Returns budgeted source snippets and candidate graph neighbors. Automatically refreshes and exports the graph before searching.', 'inputSchema': {'type': 'object', 'properties': {'query': {'type': 'string'}, 'budget': {'type': 'integer', 'minimum': 256, 'maximum': 100000, 'default': 6000}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50, 'default': 8}}, 'required': ['query'], 'additionalProperties': False}},
        {'name': 'index_repository', 'description': 'Incrementally refresh and export the code graph after repository edits. The server also polls for edits automatically. Does not execute repository code.', 'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
        {'name': 'graph_stats', 'description': 'Get counts of indexed files, code nodes, and edges.', 'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    ]
    for line in incoming(graph, interval_seconds(interval)):
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
                    refreshed = graph.export()
                    if name == 'search_code':
                        value = graph.search(**args)
                    elif name == 'index_repository' and not args:
                        value = refreshed['refreshed']
                    elif name == 'graph_stats' and not args:
                        value = graph.stats()
                    else:
                        raise ValueError('Unknown tool or invalid arguments')
                    result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}], 'isError': False}
                except (ValueError, TypeError, OSError, sqlite3.Error) as exc:
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
    for command in ('index', 'export', 'search', 'stats', 'serve', 'watch', 'check', 'install-hook'):
        p = sub.add_parser(command)
        p.add_argument('--repo', required=True, help='Path to a local checkout')
        if command in ('watch', 'serve'):
            p.add_argument('--interval', type=interval_seconds, default=1, help='Refresh interval in seconds (0.1..60)')
        if command == 'check':
            p.add_argument('--staged', action='store_true', help='Validate the Git index instead of the working tree')
        if command == 'search':
            p.add_argument('query')
            p.add_argument('--budget', type=int, default=6000, help='Maximum serialized result bytes')
            p.add_argument('--limit', type=int, default=8)
    args = parser.parse_args()
    graph = None
    try:
        if args.command in ('check', 'install-hook'):
            value = check_snapshot(args.repo, args.staged) if args.command == 'check' else install_hook(args.repo)
            print(json.dumps(value))
            return
        graph = Graph(args.repo, load_snapshot=args.command not in ('index', 'export', 'watch'))
        if args.command == 'serve':
            serve(graph, args.interval)
            return
        if args.command == 'watch':
            watch(graph, args.interval)
            return
        if args.command == 'index':
            value = graph.export()['refreshed']
        elif args.command == 'export':
            value = graph.export()
        elif args.command == 'stats':
            graph.export()
            value = graph.stats()
        else:
            graph.export()
            value = graph.search(args.query, args.budget, args.limit)
        print(json.dumps(value, ensure_ascii=False))
    except (ValueError, OSError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'codemanager: {exc}\n')
    finally:
        if graph:
            graph.close()


if __name__ == '__main__':
    main()
