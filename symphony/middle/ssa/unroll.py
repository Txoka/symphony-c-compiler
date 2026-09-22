"""Bounded full unrolling for canonical constant-trip natural loops."""

from ..analysis.cfg import build_cfg
from ..analysis.dominance import build_dominator_tree, find_natural_loops
from ..ir import BasicBlock, Instruction
from .evaluate import _safe_binary
from .sccp import _fold_unary, _normalize


UNKNOWN = object()


def unroll_known_trip_loops(function, iteration_limit=4):
    """Fully unroll statically traced loops with at most ``iteration_limit`` trips."""
    changed = False
    while _unroll_one(function, iteration_limit):
        changed = True
    return changed


def _unroll_one(function, iteration_limit):
    cfg = build_cfg(function)
    loops = sorted(
        find_natural_loops(build_dominator_tree(cfg)), key=lambda loop: len(loop.blocks)
    )
    definitions = {
        item.dst: item
        for block in cfg.blocks
        for item in block.instructions
        if item.dst is not None
    }
    def_block = {
        item.dst: block.label
        for block in cfg.blocks
        for item in block.instructions
        if item.dst is not None
    }

    for loop in loops:
        header_label = loop.header
        header = cfg.by_label[header_label]
        outside = [value for value in header.predecessors if value not in loop.blocks]
        exits = {
            successor
            for label in loop.blocks
            for successor in cfg.by_label[label].successors
            if successor not in loop.blocks
        }
        if len(outside) != 1 or not exits:
            continue
        preheader = outside[0]
        preheader_block = cfg.by_label[preheader]
        if len(preheader_block.successors) != 1 or header_label not in preheader_block.successors:
            continue
        phis = [item for item in header.instructions if item.op == "phi"]
        if not phis or any(
            set(source for source, _ in phi.extra) != set(header.predecessors)
            for phi in phis
        ):
            continue
        if any(
            item.op not in (
                "label", "phi", "const", "copy", "cast", "unary", "binary",
                "branch_if", "cbranch_if", "jump",
            )
            for item in header.instructions
        ):
            continue
        trace_result = _iteration_traces(
            cfg, loop, phis, preheader, definitions, def_block, iteration_limit
        )
        if trace_result is None:
            plan = _fixed_trip_plan(
                cfg, loop, phis, preheader, definitions, def_block, iteration_limit
            )
            if plan is not None and _clone_loop_cfg(
                function, cfg, loop, phis, preheader, plan, definitions
            ):
                return True
            continue
        traces, exit_trace, exit_source, exit_label = trace_result

        current = {phi.dst: dict(phi.extra)[preheader] for phi in phis}
        final_map = dict(current)
        expanded = []
        for trace in traces:
            iteration = dict(current)
            predecessor = header_label
            for label in trace:
                for item in cfg.by_label[label].instructions:
                    if item.op == "label" or item is cfg.by_label[label].terminator():
                        continue
                    if item.op == "phi":
                        incoming = dict(item.extra).get(predecessor)
                        if incoming is None:
                            expanded = None
                            break
                        iteration[item.dst] = iteration.get(incoming, incoming)
                        continue
                    if item.op in ("branch_if", "cbranch_if", "jump", "return"):
                        expanded = None
                        break
                    args = tuple(iteration.get(value, value) for value in item.args)
                    destination = item.dst
                    if destination is not None:
                        destination = function.values
                        function.values += 1
                        iteration[item.dst] = destination
                    expanded.append(
                        Instruction(item.op, destination, args, item.type, item.extra)
                    )
                if expanded is None:
                    break
                predecessor = label
            if expanded is None:
                break
            current = {
                phi.dst: iteration.get(
                    dict(phi.extra)[trace[-1]], dict(phi.extra)[trace[-1]]
                )
                for phi in phis
            }
            final_map = {**iteration, **current}
        if expanded is None:
            continue

        iteration = dict(current)
        predecessor = header_label
        for label in exit_trace:
            for item in cfg.by_label[label].instructions:
                if item.op == "label" or item is cfg.by_label[label].terminator():
                    continue
                if item.op == "phi":
                    incoming = dict(item.extra).get(predecessor)
                    if incoming is None:
                        expanded = None
                        break
                    iteration[item.dst] = iteration.get(incoming, incoming)
                    continue
                if item.op in ("branch_if", "cbranch_if", "jump", "return"):
                    expanded = None
                    break
                args = tuple(iteration.get(value, value) for value in item.args)
                destination = item.dst
                if destination is not None:
                    destination = function.values
                    function.values += 1
                    iteration[item.dst] = destination
                expanded.append(
                    Instruction(item.op, destination, args, item.type, item.extra)
                )
            if expanded is None:
                break
            predecessor = label
        if expanded is None:
            continue
        final_map = {**final_map, **iteration}

        if preheader_block.terminator() is not None:
            terminator = preheader_block.instructions[-1]
            if terminator.op != "jump" or cfg.label_blocks.get(
                terminator.extra, terminator.extra
            ) != header_label:
                continue
            preheader_block.instructions.pop()
        preheader_block.instructions.extend(expanded)
        preheader_block.instructions.append(Instruction("jump", extra=exit_label))

        replacements = {phi.dst: current[phi.dst] for phi in phis}
        surviving = []
        for block in function.blocks:
            if block.label in loop.blocks:
                continue
            items = []
            for item in block.instructions:
                args = tuple(
                    replacements.get(value, final_map.get(value, value))
                    for value in item.args
                )
                extra = item.extra
                if item.op == "phi":
                    extra = tuple(
                        (source, value)
                        for source, value in item.extra
                        if source not in loop.blocks
                    ) + tuple(
                        (
                            preheader,
                            replacements.get(value, final_map.get(value, value)),
                        )
                        for source, value in item.extra
                        if source == exit_source
                    )
                items.append(Instruction(item.op, item.dst, args, item.type, extra))
            surviving.append(BasicBlock(block.label, items))
        function.blocks = surviving
        return True
    return False


def _fixed_trip_plan(cfg, loop, phis, preheader, definitions, def_block, limit):
    """Prove a header-controlled trip count without choosing body predicates."""
    latches = {source for source, _ in loop.back_edges}
    incoming = {}
    for phi in phis:
        sources = dict(phi.extra)
        updates = {sources.get(latch) for latch in latches}
        if None in updates or len(updates) != 1:
            return None
        incoming[phi.dst] = (sources[preheader], updates.pop())

    cache = {}

    def static(value, visiting=None):
        if value in cache:
            return cache[value]
        visiting = set() if visiting is None else visiting
        if value in visiting or def_block.get(value) in loop.blocks:
            return UNKNOWN
        visiting.add(value)
        item = definitions.get(value)
        result = _evaluate_item(item, lambda arg: static(arg, visiting))
        cache[value] = result
        return result

    values = {}
    for phi in phis:
        initial, _ = incoming[phi.dst]
        value = static(initial)
        values[phi.dst] = value

    def evaluate(value, visiting=None):
        if value in values:
            return values[value]
        visiting = set() if visiting is None else visiting
        if value in visiting:
            return UNKNOWN
        visiting.add(value)
        item = definitions.get(value)
        return _evaluate_item(item, lambda arg: evaluate(arg, visiting))

    header = cfg.by_label[loop.header]
    condition = header.terminator()
    if condition is None or condition.op not in ("branch_if", "cbranch_if"):
        return None
    explicit = cfg.label_blocks.get(condition.extra[1], condition.extra[1])
    alternatives = [value for value in header.successors if value != explicit]
    if len(alternatives) != 1:
        return None

    body_entry = None
    for count in range(limit + 1):
        for item in header.instructions:
            if item.op in ("label", "phi") or item is condition:
                continue
            result = _evaluate_item(item, evaluate)
            if item.dst is None:
                return None
            values[item.dst] = result
        if condition.op == "branch_if":
            value = evaluate(condition.args[0])
            if value is UNKNOWN:
                return None
            taken = bool(value) == condition.extra[0]
        else:
            left, right = (evaluate(value) for value in condition.args)
            if UNKNOWN in (left, right):
                return None
            result = _safe_binary(condition.extra[0], left, right, condition.type)
            if result is None:
                return None
            taken = bool(result)
        chosen = explicit if taken else alternatives[0]
        if chosen not in loop.blocks:
            return count, body_entry, chosen
        if body_entry is None:
            body_entry = chosen
        elif body_entry != chosen:
            return None
        if count == limit:
            return None
        updates = {}
        for phi in phis:
            result = evaluate(incoming[phi.dst][1])
            updates[phi.dst] = result
        values.update(updates)
    return None


def _evaluate_item(item, operand):
    if item is None:
        return UNKNOWN
    if item.op == "const":
        return _normalize(item.extra, item.type)
    if item.op in ("copy", "cast"):
        value = operand(item.args[0])
        return UNKNOWN if value is UNKNOWN else _normalize(value, item.type)
    if item.op == "unary":
        value = operand(item.args[0])
        folded = None if value is UNKNOWN else _fold_unary(item.extra, value, item.type)
        return UNKNOWN if folded is None else folded
    if item.op == "binary":
        left, right = (operand(value) for value in item.args)
        if UNKNOWN in (left, right):
            return UNKNOWN
        folded = _safe_binary(item.extra, left, right, item.type)
        return UNKNOWN if folded is None else folded
    return UNKNOWN


def _clone_loop_cfg(function, cfg, loop, phis, preheader, plan, definitions):
    trip_count, body_entry, normal_exit = plan
    if trip_count == 0:
        return False
    if any(
        successor != normal_exit
        for label in loop.blocks
        for successor in cfg.by_label[label].successors
        if successor not in loop.blocks
    ):
        return False
    ordered = [block for block in cfg.blocks if block.label in loop.blocks]
    label_map = {
        (iteration, block.label): f"{block.label}.unroll{iteration}"
        for iteration in range(trip_count)
        for block in ordered
    }
    value_maps = []
    current = {phi.dst: dict(phi.extra)[preheader] for phi in phis}
    latch_inputs = {
        phi.dst: next(iter({dict(phi.extra)[source] for source, _ in loop.back_edges}))
        for phi in phis
    }
    for _ in range(trip_count):
        mapping = dict(current)
        for block in ordered:
            for item in block.instructions:
                if item.dst is None or (block.label == loop.header and item.op == "phi"):
                    continue
                mapping[item.dst] = function.values
                function.values += 1
        value_maps.append(mapping)
        current = {
            phi.dst: mapping.get(latch_inputs[phi.dst], latch_inputs[phi.dst])
            for phi in phis
        }

    edge_source = {}
    clones = []

    for iteration in range(trip_count):
        for block in ordered:
            terminator = block.terminator()
            explicit = None
            if terminator is not None and terminator.op in ("branch_if", "cbranch_if"):
                explicit = cfg.label_blocks.get(terminator.extra[1], terminator.extra[1])
            for successor in block.successors:
                source = label_map[(iteration, block.label)]
                if explicit is not None and successor != explicit:
                    source = f"{source}.fallthrough"
                edge_source[(iteration, block.label, successor)] = source

    def target(iteration, successor):
        if successor == loop.header:
            return (
                label_map[(iteration + 1, loop.header)]
                if iteration + 1 < trip_count else normal_exit
            )
        if successor in loop.blocks:
            return label_map[(iteration, successor)]
        return successor

    for iteration in range(trip_count):
        mapping = value_maps[iteration]
        for block in ordered:
            clone_label = label_map[(iteration, block.label)]
            items = [Instruction("label", extra=clone_label)]
            for item in block.instructions:
                if item.op == "label" or item is block.terminator():
                    continue
                if block.label == loop.header and item.op == "phi":
                    continue
                if item.op == "phi":
                    extra = tuple(
                        (
                            edge_source.get((iteration, source, block.label),
                                            label_map[(iteration, source)]),
                            mapping.get(value, value),
                        )
                        for source, value in item.extra
                    )
                    items.append(Instruction("phi", mapping[item.dst], (), item.type, extra))
                    continue
                items.append(Instruction(
                    item.op, mapping.get(item.dst, item.dst),
                    tuple(mapping.get(value, value) for value in item.args),
                    item.type, item.extra,
                ))
            terminator = block.terminator()
            if block.label == loop.header:
                successor = body_entry
                items.append(Instruction("jump", extra=target(iteration, successor)))
                edge_source[(iteration, block.label, successor)] = clone_label
            elif terminator is not None and terminator.op == "jump":
                successor = cfg.label_blocks.get(terminator.extra, terminator.extra)
                items.append(Instruction("jump", extra=target(iteration, successor)))
                edge_source[(iteration, block.label, successor)] = clone_label
            elif terminator is not None and terminator.op in ("branch_if", "cbranch_if"):
                explicit = cfg.label_blocks.get(terminator.extra[1], terminator.extra[1])
                fallthrough = next(value for value in block.successors if value != explicit)
                extra = (terminator.extra[0], target(iteration, explicit))
                items.append(Instruction(
                    terminator.op, None,
                    tuple(mapping.get(value, value) for value in terminator.args),
                    terminator.type, extra,
                ))
                trampoline = f"{clone_label}.fallthrough"
                edge_source[(iteration, block.label, explicit)] = clone_label
                edge_source[(iteration, block.label, fallthrough)] = trampoline
                clones.append(BasicBlock(clone_label, items))
                clones.append(BasicBlock(
                    trampoline, [
                        Instruction("label", extra=trampoline),
                        Instruction("jump", extra=target(iteration, fallthrough)),
                    ]
                ))
                continue
            else:
                if len(block.successors) != 1:
                    return False
                successor = block.successors[0]
                items.append(Instruction("jump", extra=target(iteration, successor)))
                edge_source[(iteration, block.label, successor)] = clone_label
            clones.append(BasicBlock(clone_label, items))

    preheader_block = cfg.by_label[preheader]
    terminator = preheader_block.terminator()
    if terminator is not None:
        if terminator.op != "jump":
            return False
        terminator.extra = label_map[(0, loop.header)]
    elif cfg.index_of(loop.header) != cfg.index_of(preheader) + 1:
        return False

    surviving = []
    insertion = cfg.index_of(loop.header)
    for index, block in enumerate(function.blocks):
        if index == insertion:
            surviving.extend(clones)
        if block.label in loop.blocks:
            continue
        items = []
        for item in block.instructions:
            if item.op != "phi":
                items.append(Instruction(
                    item.op, item.dst,
                    tuple(current.get(value, value) for value in item.args),
                    item.type, item.extra,
                ))
                continue
            extra = [(source, value) for source, value in item.extra if source not in loop.blocks]
            for source, value in item.extra:
                if source not in loop.blocks:
                    continue
                for iteration in range(trip_count):
                    actual = edge_source.get((iteration, source, block.label))
                    if actual is not None:
                        extra.append((actual, value_maps[iteration].get(value, value)))
                if source == loop.header and block.label == normal_exit:
                    for latch, _ in loop.back_edges:
                        actual = edge_source.get((trip_count - 1, latch, loop.header))
                        if actual is not None:
                            extra.append((actual, current.get(value, value)))
            items.append(Instruction("phi", item.dst, (), item.type, tuple(extra)))
        surviving.append(BasicBlock(block.label, items))
    function.blocks = surviving
    return True


def _iteration_traces(cfg, loop, phis, preheader, definitions, def_block, limit):
    cache = {}

    def constant(value, visiting=None):
        if value in cache:
            return cache[value]
        visiting = set() if visiting is None else visiting
        if value in visiting or def_block.get(value) in loop.blocks:
            return UNKNOWN
        visiting.add(value)
        item = definitions.get(value)
        result = UNKNOWN
        if item is not None and item.op == "const":
            result = _normalize(item.extra, item.type)
        elif item is not None and item.op in ("copy", "cast"):
            result = constant(item.args[0], visiting)
            if result is not UNKNOWN:
                result = _normalize(result, item.type)
        elif item is not None and item.op == "unary":
            operand = constant(item.args[0], visiting)
            if operand is not UNKNOWN:
                folded = _fold_unary(item.extra, operand, item.type)
                result = UNKNOWN if folded is None else folded
        elif item is not None and item.op == "binary":
            left = constant(item.args[0], set(visiting))
            right = constant(item.args[1], set(visiting))
            if left is not UNKNOWN and right is not UNKNOWN:
                folded = _safe_binary(item.extra, left, right, item.type)
                result = UNKNOWN if folded is None else folded
        cache[value] = result
        return result

    values = {
        value: result
        for value in definitions
        if def_block.get(value) not in loop.blocks
        if (result := constant(value)) is not UNKNOWN
    }
    for phi in phis:
        incoming = dict(phi.extra)[preheader]
        values[phi.dst] = values.get(incoming, constant(incoming))
    traces = []
    predecessor, label = preheader, loop.header
    trace = []
    steps = 0
    while steps < 1024:
        block = cfg.by_label[label]
        if label == loop.header:
            if predecessor in loop.blocks:
                traces.append(trace)
                if len(traces) > limit:
                    return None
                trace = []
        else:
            trace.append(label)

        incoming_values = {}
        for item in block.instructions:
            if item.op != "phi":
                continue
            incoming = dict(item.extra).get(predecessor)
            if incoming is None:
                return None
            incoming_values[item.dst] = values.get(incoming, UNKNOWN)
        values.update(incoming_values)

        transferred = False
        for item in block.instructions:
            if item.op in ("label", "phi"):
                continue
            steps += 1
            if item.op == "const":
                values[item.dst] = _normalize(item.extra, item.type)
            elif item.op in ("copy", "cast"):
                result = values.get(item.args[0], UNKNOWN)
                values[item.dst] = (
                    _normalize(result, item.type) if result is not UNKNOWN else UNKNOWN
                )
            elif item.op == "unary":
                operand = values.get(item.args[0], UNKNOWN)
                folded = None if operand is UNKNOWN else _fold_unary(
                    item.extra, operand, item.type
                )
                values[item.dst] = UNKNOWN if folded is None else folded
            elif item.op == "binary":
                left, right = (values.get(value, UNKNOWN) for value in item.args)
                folded = None if UNKNOWN in (left, right) else _safe_binary(
                    item.extra, left, right, item.type
                )
                values[item.dst] = UNKNOWN if folded is None else folded
            elif item.op == "jump":
                next_label = cfg.label_blocks.get(item.extra, item.extra)
                predecessor, label = label, next_label
                transferred = True
                break
            elif item.op in ("branch_if", "cbranch_if"):
                if item.op == "branch_if":
                    value = values.get(item.args[0], UNKNOWN)
                    if value is UNKNOWN:
                        return None
                    take_target = bool(value) == item.extra[0]
                else:
                    left, right = (values.get(value, UNKNOWN) for value in item.args)
                    if UNKNOWN in (left, right):
                        return None
                    folded = _safe_binary(item.extra[0], left, right, item.type)
                    if folded is None:
                        return None
                    take_target = bool(folded)
                target = cfg.label_blocks.get(item.extra[1], item.extra[1])
                alternatives = [value for value in block.successors if value != target]
                if not take_target and len(alternatives) != 1:
                    return None
                next_label = target if take_target else alternatives[0]
                if next_label not in loop.blocks:
                    return traces, trace, label, next_label
                predecessor, label = label, next_label
                transferred = True
                break
            elif item.dst is not None:
                values[item.dst] = UNKNOWN
        if transferred:
            continue
        if len(block.successors) != 1:
            return None
        next_label = block.successors[0]
        if next_label not in loop.blocks:
            return traces, trace, label, next_label
        predecessor, label = label, next_label
    return None


__all__ = ["unroll_known_trip_loops"]
