"""Offline diagnostics for evidence from one frozen retrieval evaluation run."""
from .source_coverage import SourceCoverage


def diagnose(expected, candidates, selected, responses, source_rows):
    """Classify misses using candidate IDs, selected IDs, and emitted source.

    source_rows maps response IDs to indexed rows from the frozen revision.
    Include hook and MCP responses together; audit shell reads separately.
    Pointers never count as source. Parent excerpts may cover child definitions.
    Incomplete selections can reflect budgets, excerpt caps, or prior receipts;
    this status alone does not establish which mechanism caused the omission.
    """
    candidates, selected = set(candidates), set(selected)
    coverage = SourceCoverage()
    for response in responses:
        for item in response.get('results', []):
            if not item.get('body'):
                continue
            row = source_rows[item['id']]
            offset = item['offset']
            if offset < 0 or row['body'][offset:offset + len(item['body'])] != item['body']:
                raise ValueError('Response source does not match frozen indexed row')
            coverage.add(row, offset, len(item['body']))
    findings = []
    for row in expected:
        missing = list(coverage.uncovered(row))
        if not missing:
            status = 'covered'
        elif row['id'] in selected:
            status = 'selected_incomplete'
        elif row['id'] in candidates:
            status = 'not_selected'
        else:
            status = 'not_in_candidates'
        findings.append({'id': row['id'], 'status': status,
                         'missing_ranges': [list(pair) for pair in missing]})
    return findings
