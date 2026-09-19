"""CFG liveness and interference analysis for SSA register allocation."""

from ..analysis.cfg import build_cfg


def interference_graph(function):
    """Return ``(graph, live_across_call)`` for SSA values in *function*."""
    cfg = build_cfg(function)
    use, define = {}, {}
    for block in cfg.blocks:
        seen = set()
        use[block.label], define[block.label] = set(), set()
        for item in block.instructions:
            for value in item.args:
                if isinstance(value, int) and value not in seen:
                    use[block.label].add(value)
            if item.dst is not None:
                seen.add(item.dst); define[block.label].add(item.dst)
    live_in = {block.label: set() for block in cfg.blocks}
    live_out = {block.label: set() for block in cfg.blocks}
    changed = True
    while changed:
        changed = False
        for block in reversed(cfg.blocks):
            out = set().union(*(live_in[s] for s in block.successors))
            incoming = use[block.label] | (out - define[block.label])
            if out != live_out[block.label] or incoming != live_in[block.label]:
                live_out[block.label], live_in[block.label], changed = out, incoming, True
    graph, across = {}, set()
    for block in cfg.blocks:
        live = set(live_out[block.label])
        for item in reversed(block.instructions):
            if item.op in ("call", "direct_call") or (item.op == "binary" and item.extra in ("*", "/", "%")):
                across.update(live)
            if item.dst is not None:
                graph.setdefault(item.dst, set()).update(live - {item.dst})
                for value in live - {item.dst}: graph.setdefault(value, set()).add(item.dst)
                live.discard(item.dst)
            live.update(value for value in item.args if isinstance(value, int))
    return graph, across
