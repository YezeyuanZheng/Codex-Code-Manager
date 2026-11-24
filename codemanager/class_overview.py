"""Expose method signatures alongside retrieved classes."""
import io
import textwrap
import tokenize

from .source_coverage import SourceCoverage, line_starts


def signature_stop(body):
    """Return the original offset just after the function header colon."""
    dedented = textwrap.dedent(body)
    original_lines = body.split('\n')
    lines = dedented.split('\n')
    depth = 0
    in_header = False
    for token in tokenize.generate_tokens(io.StringIO(dedented).readline):
        if token.type == tokenize.NAME and token.string == 'def':
            in_header = True
        if not in_header or token.type != tokenize.OP:
            continue
        if token.string in ('(', '[', '{'):
            depth += 1
        elif token.string in (')', ']', '}'):
            depth -= 1
        elif token.string == ':' and depth == 0:
            line, column = token.end
            return line_starts(body)[line - 1] + column + len(original_lines[line - 1]) - len(lines[line - 1])


def method_signatures(graph, classes, results, seen):
    from .context_receipts import coverage as prior_coverage
    classes = dict.fromkeys(item['id'] for item in results if item['id'] in classes)
    if not classes:
        return
    coverage = prior_coverage(seen)
    for item in results:
        coverage.add(item, 0, len(item['body']))
    for ident in classes:
        methods = graph.db.execute("""SELECT nodes.* FROM edges JOIN nodes ON nodes.id=edges.target
            WHERE edges.source=? AND edges.kind='contains' AND nodes.kind='function'
            ORDER BY nodes.start, nodes.id""", (ident,))
        for method in methods:
            try:
                stop = signature_stop(method['body'])
            except (IndentationError, tokenize.TokenError):
                continue
            if not stop:
                continue
            for start, end in coverage.uncovered(method, 0, stop):
                body = method['body'][start:end]
                if not body.strip():
                    continue
                line, _ = SourceCoverage.position(method, line_starts(method['body']), start)
                yield {'name': method['name'], 'path': method['path'], 'start': line, 'signature': body}
