"""Measure required information, supporting context, uncertain extras, and repeated exposure.

Inputs are source-character ranges, not tool metadata or estimates from answer length.
Off-reference source remains uncertain until an explicit relevance audit classifies it.
"""


def merge(ranges):
    result = []
    for start, stop in sorted(ranges):
        if type(start) is not int or type(stop) is not int or start < 0 or stop < start:
            raise ValueError('Source ranges must be nonnegative half-open integer intervals')
        if start == stop:
            continue
        if result and start <= result[-1][1]:
            result[-1] = result[-1][0], max(stop, result[-1][1])
        else:
            result.append((start,stop))
    return result


def subtract(ranges, covered):
    result = merge(ranges)
    for left, right in merge(covered):
        output = []
        for start, stop in result:
            if right <= start or left >= stop:
                output.append((start,stop))
            else:
                if start < left: output.append((start,left))
                if right < stop: output.append((right,stop))
        result = output
    return result


def intersect(left, right):
    return sum(max(0,min(b,d)-max(a,c)) for a,b in merge(left) for c,d in merge(right))


def information_metrics(exposures, core, supporting, useful_extra=None, unrelated=None):
    seen, total = {}, 0
    for response in exposures:
        for path, ranges in response.items():
            # Every emitted source character counts, including duplicates in one response.
            merge(ranges)  # Validate before accumulating raw lengths.
            total += sum(stop-start for start,stop in ranges)
            seen.setdefault(path, []).extend(ranges)
    labels = {'core':core, 'supporting':supporting, 'useful_extra':useful_extra or {},
              'unrelated':unrelated or {}}
    counts = {name:0 for name in labels}
    unique = sum(sum(b-a for a,b in merge(ranges)) for ranges in seen.values())
    # Priority makes categories disjoint: support overlapping core is counted only as core.
    for path, ranges in seen.items():
        used = []
        for name, annotation in labels.items():
            classified = subtract(annotation.get(path, []), used)
            counts[name] += intersect(ranges, classified)
            used.extend(classified)
    uncertain = unique-sum(counts.values())
    important = counts['core']+counts['supporting']+counts['useful_extra']
    percentage = lambda value, denominator: round(100*value/denominator,2) if denominator else None
    return {'source_exposure_characters':total, 'unique_source_characters':unique,
            'repeated_source_characters':total-unique,
            'core_characters':counts['core'], 'supporting_characters':counts['supporting'],
            'audited_useful_extra_characters':counts['useful_extra'],
            'audited_unrelated_characters':counts['unrelated'], 'uncertain_extra_characters':uncertain,
            'extra_beyond_core_characters':unique-counts['core'],
            'important_unique_characters':important,
            'important_unique_percent':percentage(important,unique),
            'important_per_exposure_percent':percentage(important,total),
            'repeated_exposure_percent':percentage(total-unique,total),
            'unrelated_unique_percent':percentage(counts['unrelated'],unique),
            'uncertain_unique_percent':percentage(uncertain,unique)}
