"""CLI arguments for the batched context operation."""


def configure(parser):
    parser.add_argument('query', nargs='?', help='One natural-language question')
    parser.add_argument('--query', dest='queries', action='append', help='Repeat for independent questions')
    parser.add_argument('--symbol', dest='symbols', action='append', help='Exact or qualified symbol; repeatable')
    parser.add_argument('--node-id', dest='node_ids', action='append')
    parser.add_argument('--cursor', action='append', default=[], metavar='NODE_ID=OFFSET',
                        help='Continue a node; repeat to batch different character offsets')
    parser.add_argument('--budget', type=int, default=12000)
    parser.add_argument('--limit', type=int, default=8)
    parser.add_argument('--no-related', action='store_true')


def run(graph, args):
    offsets, node_ids = {}, list(args.node_ids or [])
    for cursor in args.cursor:
        try:
            ident, raw_offset = cursor.rsplit('=', 1)
            offsets[ident] = int(raw_offset)
        except ValueError as exc:
            raise ValueError('--cursor must be NODE_ID=OFFSET') from exc
        if ident not in node_ids:
            node_ids.append(ident)
    return graph.get_context(query=args.query, queries=args.queries, symbols=args.symbols,
                             node_ids=node_ids, offsets=offsets, budget=args.budget,
                             limit=args.limit, mode=args.mode, model=args.model,
                             include_related=not args.no_related)
