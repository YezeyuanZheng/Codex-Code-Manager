"""Select disjoint source excerpts using positions in indexed node bodies."""
from bisect import bisect_right
import json


class SourceCoverage:
    """Per-response coverage; never assume code from an earlier call is current."""

    def __init__(self):
        self.ranges = {}

    @staticmethod
    def positions(row):
        return [0] + [index + 1 for index, char in enumerate(row['body']) if char == '\n']

    @staticmethod
    def position(row, starts, offset):
        line = bisect_right(starts, offset) - 1
        return row['start'] + line, offset - starts[line]

    def uncovered(self, row, offset=0, length=None):
        """Yield node-relative character ranges, excluding only emitted source."""
        body = row['body']
        if offset > len(body):
            raise ValueError('offset exceeds node source length')
        end = len(body) if length is None else min(len(body), offset + length)
        starts = self.positions(row)
        first = self.position(row, starts, offset)
        last = self.position(row, starts, end)
        remaining = [(first, last)]
        for covered_start, covered_end, _ in sorted(self.ranges.get(row['path'], [])):
            pieces = []
            for start, stop in remaining:
                if covered_end <= start or covered_start >= stop:
                    pieces.append((start, stop))
                else:
                    if start < covered_start:
                        pieces.append((start, covered_start))
                    if covered_end < stop:
                        pieces.append((covered_end, stop))
            remaining = pieces
        for start, stop in remaining:
            begin = starts[start[0] - row['start']] + start[1]
            finish = starts[stop[0] - row['start']] + stop[1]
            if finish > begin:
                yield begin, finish

    def add(self, row, offset, length):
        """Record the actual emitted characters, after serialized-budget clipping."""
        if not length:
            return
        starts = self.positions(row)
        start = self.position(row, starts, offset)
        end = self.position(row, starts, offset + length)
        self.ranges.setdefault(row['path'], []).append((start, end, row['id']))

    def covered_by(self, row, offset=0, length=None):
        starts = self.positions(row)
        end = len(row['body']) if length is None else min(len(row['body']), offset + length)
        first = self.position(row, starts, offset)
        last = self.position(row, starts, end)
        return sorted({ident for start, stop, ident in self.ranges.get(row['path'], [])
                       if start < last and stop > first})


def source_item(row, offset, stop):
    """Keep node identity and continuation coordinates when an excerpt is split."""
    body = row['body'][offset:stop]
    start = row['start'] + row['body'][:offset].count('\n')
    end = start + body.rstrip('\n').count('\n')
    return {'start': start, 'end': min(row['end'], end), 'body': body,
            'offset': offset, 'next_offset': stop if stop < len(row['body']) else None}


def add_reference(output, row, reason, coverage, budget, offset=0, length=None, via=None):
    """Preserve covered-node identity and graph hints without repeating its body."""
    covered = coverage.covered_by(row, offset, length)
    if not covered:
        return
    item = {'id': row['id'], 'reason': reason, 'covered_by': covered}
    if via is not None:
        item['via'] = via
    mapping = json.loads(row['request_mapping'])
    if mapping:
        item['request_mapping'] = mapping
    output.setdefault('references', []).append(item)
    if len(json.dumps(output, ensure_ascii=False).encode()) > budget:
        output['references'].pop()
        if not output['references']:
            del output['references']
        output['truncated'] = True
