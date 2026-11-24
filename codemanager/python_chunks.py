"""Contiguous Python module source ranges outside indexed definitions."""

PYTHON_CHUNKS = '2'


def module_ranges(lines, definitions):
    first = 1
    for start, end in [*sorted(definitions), (len(lines) + 1, len(lines))]:
        for offset in range(first, start, 60):
            stop = min(offset + 59, start - 1)
            if any(line.strip() for line in lines[offset-1:stop]):
                yield offset, stop
        first = max(first, end + 1)
