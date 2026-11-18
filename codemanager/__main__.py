import argparse
import json
import os
import queue
import sqlite3
import subprocess
import threading
import time
import sys
from .graph import Graph
from .mcp_tools import tool_catalog
from . import file_limits
from . import context_cli
from .codex_hooks import install as install_codex_hook, stop_hook
from .context import prompt_hook
from .requests import map_request
from .purposes import get_file_purpose, set_file_purpose
from .review import begin_task, map_change, review, configure, commit_check
from .config import model_settings
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


def serve(graph, interval=1, mode=None, bm25_only=False):
    """Newline-delimited JSON-RPC over stdio; stdout is protocol-only."""
    tools = tool_catalog()
    if bm25_only:
        for tool in tools:
            if tool['name'] == 'get_context':
                tool['inputSchema']['properties']['mode']['enum'] = ['bm25']
                tool['description'] += ' This server is configured for BM25 only; embeddings are disabled.'
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
                    if name in ('search_code', 'retrieve_nodes'):
                        raise ValueError('This retrieval tool was removed; use get_context with query, symbols, or node_ids')
                    refreshed = graph.export()
                    if name == 'get_context':
                        args = dict(args)
                        if bm25_only:
                            if args.get('mode') not in (None, 'bm25'):
                                raise ValueError('This server is BM25-only; use mode=bm25')
                            args['mode'] = 'bm25'
                            args['bm25_only'] = True
                        if not args.get('bm25_only'):
                            args.setdefault('mode', mode)
                        args.setdefault('compact', True)
                        value = graph.get_context(**args)
                    elif name == 'get_file_purpose':
                        value = get_file_purpose(graph, **args)
                    elif name == 'set_file_purpose':
                        value = set_file_purpose(graph, **args)
                    elif name == 'begin_task':
                        value = begin_task(graph, **args)
                    elif name == 'complete_task' and not args:
                        value = review(graph.repo)
                    elif name == 'map_change':
                        value = map_change(graph, **args)
                    elif name == 'map_request':
                        value = map_request(graph, **args)
                    elif name == 'index_repository' and not args:
                        value = refreshed['refreshed']
                    elif name == 'graph_stats' and not args:
                        value = graph.stats()
                    else:
                        raise ValueError('Unknown tool or invalid arguments')
                    separators = (',', ':') if name == 'get_context' and args.get('compact') else None
                    result = {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False, separators=separators)}], 'isError': False}
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
    for command in ('index', 'export', 'search', 'context', 'stats', 'serve', 'watch', 'check', 'install-hook', 'embed', 'model', 'map-request', 'review-config', 'begin-task', 'complete-task', 'review', 'map-change', 'commit-check', 'file-purpose', 'set-file-purpose', 'file-limits', 'check-length', 'codex-length-hook', 'install-codex-hook', 'codex-context-hook', 'retrieval-mode', 'node-context', 'review-evidence'):
        p = sub.add_parser(command)
        p.add_argument('--repo', required=True, help='Path to a local checkout')
        if command in ('watch', 'serve'):
            p.add_argument('--interval', type=interval_seconds, default=1, help='Refresh interval in seconds (0.1..60)')
        if command == 'context':
            context_cli.configure(p)
        if command in ('search', 'context', 'serve', 'embed', 'check'):
            p.add_argument('--model', help='FastEmbed model ID; overrides the environment and repository default')
        if command in ('file-purpose', 'set-file-purpose'):
            p.add_argument('--path', required=True)
        if command == 'set-file-purpose':
            p.add_argument('--purpose', required=True)
            p.add_argument('--request', required=True)
            p.add_argument('--requirement', action='append', default=[])
            p.add_argument('--remove-requirement', action='append', default=[])
            p.add_argument('--removal-request')
        if command == 'review-config':
            choices = p.add_mutually_exclusive_group()
            choices.add_argument('--enable', dest='enabled', action='store_const', const=True, default=None)
            choices.add_argument('--disable', dest='enabled', action='store_const', const=False)
            p.add_argument('--reviewer-model')
        if command == 'node-context':
            p.add_argument('--node-id', dest='node_ids', action='append', required=True)
            p.add_argument('--budget', type=int, default=6000)
            p.add_argument('--limit', type=int, default=8)
            p.add_argument('--no-related', action='store_true')
            p.add_argument('--related-only', action='store_true')
            p.add_argument('--offset', type=int, default=0)
        if command == 'review-evidence':
            p.add_argument('--cache', required=True)
            p.add_argument('--related-only', action='store_true')
            action = p.add_mutually_exclusive_group(required=True)
            action.add_argument('--query')
            action.add_argument('--node-id', dest='node_ids', action='append')
            action.add_argument('--diff-path')
            p.add_argument('--budget', type=int, default=6000)
            p.add_argument('--limit', type=int, default=8)
            p.add_argument('--offset', type=int, default=0)
        if command == 'file-limits':
            p.add_argument('--max-lines', type=int)
            p.add_argument('--max-chars', type=int)
        if command == 'check-length':
            p.add_argument('--staged', action='store_true')
            p.add_argument('--all', dest='all_files', action='store_true')
        if command == 'begin-task':
            p.add_argument('--request', required=True)
        if command == 'review':
            p.add_argument('--staged', action='store_true')
        if command == 'map-change':
            p.add_argument('--path', required=True)
            p.add_argument('--request', required=True)
            p.add_argument('--fulfills', required=True)
        if command == 'map-request':
            p.add_argument('--node-id', required=True)
            p.add_argument('--request', required=True)
            p.add_argument('--fulfills', required=True)
        if command == 'model':
            choices = p.add_mutually_exclusive_group()
            choices.add_argument('--set', dest='save_model', metavar='MODEL', help='Save the repository default to codemanager.json')
            choices.add_argument('--reset', action='store_true', help='Remove the saved repository model')
        if command in ('search', 'context', 'serve', 'codex-context-hook'):
            p.add_argument('--mode', choices=('hybrid', 'bm25', 'semantic'))
            p.add_argument('--bm25-only', action='store_true', help='Keyword-only retrieval; never initialize embeddings')
        if command == 'embed':
            p.add_argument('--progress', action='store_true', help='Print embedding progress to stderr')
        if command == 'retrieval-mode':
            p.add_argument('--set', dest='retrieval_mode', choices=('hybrid', 'bm25', 'semantic'))
        if command in ('index', 'export', 'check'):
            p.add_argument('--full', action='store_true', help='Rebuild or audit from all source files instead of reusing the cache')
        if command == 'check':
            p.add_argument('--staged', action='store_true', help='Validate the Git index instead of the working tree')
            p.add_argument('--embeddings', action='store_true', help='Refresh staged embeddings in the local cache; blocks on failures (requires --staged)')
        if command == 'search':
            p.add_argument('query')
            p.add_argument('--budget', type=int, default=6000, help='Maximum serialized result bytes')
            p.add_argument('--limit', type=int, default=8)
    args = parser.parse_args()
    if hasattr(args, 'bm25_only') and args.bm25_only:
        if args.mode not in (None, 'bm25'):
            parser.error('--bm25-only conflicts with --mode')
        args.mode = 'bm25'
    graph = None
    try:
        if args.command == 'review-evidence':
            from .review_evidence import read_evidence
            print(json.dumps(read_evidence(args.repo, args.cache, query=args.query, node_ids=args.node_ids,
                diff_path=args.diff_path, budget=args.budget, limit=args.limit, offset=args.offset, related_only=args.related_only), ensure_ascii=False))
            return
        if args.command == 'file-limits':
            print(json.dumps(file_limits.configure(args.repo, args.max_lines, args.max_chars)))
            return
        if args.command == 'install-codex-hook':
            print(json.dumps(install_codex_hook(args.repo)))
            return
        if args.command == 'codex-context-hook':
            print(json.dumps(prompt_hook(args.repo, mode=args.mode), ensure_ascii=False))
            return
        if args.command == 'codex-length-hook':
            print(json.dumps(stop_hook(args.repo)))
            return
        if args.command == 'check-length':
            value = file_limits.check(args.repo, staged=args.staged, all_files=args.all_files)
            print(json.dumps(value, ensure_ascii=False))
            if not value['passed']:
                parser.exit(1)
            return
        if args.command == 'review-config':
            print(json.dumps(configure(args.repo, args.enabled, args.reviewer_model)))
            return
        if args.command == 'commit-check':
            print(json.dumps(commit_check(args.repo)))
            return
        if args.command in ('complete-task', 'review'):
            value = review(args.repo, staged=getattr(args, 'staged', False))
            print(json.dumps(value, ensure_ascii=False))
            if value.get('status') == 'failed':
                parser.exit(1)
            return
        if args.command == 'retrieval-mode':
            from .retrieval_settings import configure
            print(json.dumps(configure(args.repo, args.retrieval_mode)))
            return
        if args.command == 'model':
            print(json.dumps(model_settings(args.repo, save=args.save_model, reset=args.reset), ensure_ascii=False))
            return
        if args.command in ('check', 'install-hook'):
            value = check_snapshot(args.repo, args.staged, full=args.full, embeddings=args.embeddings, model=args.model) if args.command == 'check' else install_hook(args.repo)
            print(json.dumps(value))
            return
        graph = Graph(args.repo, load_snapshot=True,
                      embedding_model=getattr(args, 'model', None))
        if args.command == 'serve':
            serve(graph, args.interval, args.mode, args.bm25_only)
            return
        if args.command == 'watch':
            watch(graph, args.interval)
            return
        if args.command == 'node-context':
            graph.export()
            value = graph.retrieve_nodes(args.node_ids, args.budget, args.limit, not args.no_related, args.offset, not args.related_only)
        elif args.command == 'context':
            graph.export()
            value = context_cli.run(graph, args)
        elif args.command == 'file-purpose':
            graph.export()
            value = get_file_purpose(graph, args.path)
        elif args.command == 'set-file-purpose':
            value = set_file_purpose(graph, args.path, args.purpose, args.requirement, args.request, args.remove_requirement, args.removal_request)
        elif args.command == 'begin-task':
            value = begin_task(graph, args.request)
        elif args.command == 'map-change':
            value = map_change(graph, args.path, args.request, args.fulfills)
        elif args.command == 'map-request':
            value = map_request(graph, args.node_id, args.request, args.fulfills)
        elif args.command == 'index':
            value = graph.export(full=args.full)['refreshed']
        elif args.command == 'export':
            value = graph.export(full=args.full)
        elif args.command == 'embed':
            graph.export()
            progress = (lambda value: print(json.dumps({'embedding_progress':value}), file=sys.stderr, flush=True)) if args.progress else None
            value = graph.embed(progress=progress)
        elif args.command == 'stats':
            graph.export()
            value = graph.stats()
        else:
            graph.export()
            value = graph.search(args.query, args.budget, args.limit, args.mode)
        print(json.dumps(value, ensure_ascii=False))
    except (ValueError, OSError, sqlite3.Error, subprocess.CalledProcessError) as exc:
        parser.exit(1, f'codemanager: {exc}\n')
    finally:
        if graph:
            graph.close()


if __name__ == '__main__':
    main()
