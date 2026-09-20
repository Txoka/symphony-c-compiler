"""CFG liveness and interference analysis for SSA register allocation."""

from ..analysis.cfg import build_cfg


def interference_graph(function):
    """Return ``(graph, live_across_call)`` for values in *function*.

    Control-flow edges do not clobber ordinary registers: branch selection
    owns the dedicated backend scratch register.  Only calls and the runtime
    arithmetic operations lowered as calls require a callee-saved home.
    """
    cfg = build_cfg(function)
    # Entry parameters are initialized by the prologue.  Their textual block
    # may also be a loop header, but revisiting that label does not redefine
    # them, so they must not kill their own live range on a back-edge.
    parameters = {
        item.dst
        for block in cfg.blocks
        for item in block.instructions
        if item.op == "param" and item.dst is not None
    }
    use, define = {}, {}
    for block in cfg.blocks:
        seen = set()
        use[block.label], define[block.label] = set(), set()
        for item in block.instructions:
            for value in item.args:
                if isinstance(value, int) and value not in seen:
                    use[block.label].add(value)
            if item.dst is not None and item.dst not in parameters:
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
                if item.dst not in parameters:
                    live.discard(item.dst)
            live.update(value for value in item.args if isinstance(value, int))
    return graph, across


def copy_coalescing_groups(function, values, graph, pinned=None):
    """Return conservative, non-interfering webs of copy-related values.

    SSA destruction spells phi assignments as ordinary copies.  Keeping their
    source and destination in separate allocation decisions needlessly emits a
    move on every traversed edge.  Merge the two webs when none of their
    members interfere.  This deliberately uses the complete group-to-group
    interference test rather than a local instruction heuristic, so later
    additions to the allocator cannot make an accepted merge unsound.

    ``pinned`` maps values with ABI-mandated homes to their register.  Two
    differently pinned webs must remain distinct even when they do not overlap.
    """
    values = set(values)
    pinned = pinned or {}
    parent = {value: value for value in values}
    members = {value: {value} for value in values}

    def find(value):
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def pinned_register(group):
        homes = {pinned[value] for value in members[group] if value in pinned}
        return next(iter(homes)) if homes else None

    # Repeated copies are more profitable and are considered first.  Stable
    # value ordering makes generated code reproducible across Python versions.
    weights = {}
    for instruction in function.instructions:
        if (
            instruction.op == "copy"
            and instruction.dst in values
            and len(instruction.args) == 1
            and instruction.args[0] in values
        ):
            pair = tuple(sorted((instruction.dst, instruction.args[0])))
            if pair[0] != pair[1]:
                weights[pair] = weights.get(pair, 0) + 1

    for (left, right), _ in sorted(
        weights.items(), key=lambda item: (-item[1], item[0])
    ):
        left, right = find(left), find(right)
        if left == right:
            continue
        left_pin, right_pin = pinned_register(left), pinned_register(right)
        if left_pin is not None and right_pin is not None and left_pin != right_pin:
            continue
        if any(
            other in graph.get(value, ())
            for value in members[left]
            for other in members[right]
        ):
            continue
        # Prefer the pinned root, then the larger web, to limit parent depth.
        if right_pin is not None or (
            left_pin is None and len(members[right]) > len(members[left])
        ):
            left, right = right, left
        parent[right] = left
        members[left].update(members.pop(right))

    return [members[root] for root in sorted(members)]
