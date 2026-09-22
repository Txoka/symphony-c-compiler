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
        if len(outside) != 1 or len(exits) != 1:
            continue
        preheader, exit_label = outside[0], next(iter(exits))
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
        loop_definitions = {
            item.dst
            for label in loop.blocks
            for item in cfg.by_label[label].instructions
            if item.dst is not None
        }
        non_phi = loop_definitions - {phi.dst for phi in phis}
        if any(
            value in non_phi
            for block in cfg.blocks
            if block.label not in loop.blocks
            for item in block.instructions
            for value in item.args
        ):
            continue
        traces = _iteration_traces(
            cfg, loop, phis, preheader, definitions, def_block, iteration_limit
        )
        if traces is None:
            continue

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
                args = tuple(replacements.get(value, value) for value in item.args)
                extra = item.extra
                if item.op == "phi":
                    extra = tuple(
                        (
                            preheader if source in loop.blocks else source,
                            replacements.get(value, final_map.get(value, value)),
                        )
                        for source, value in item.extra
                    )
                items.append(Instruction(item.op, item.dst, args, item.type, extra))
            surviving.append(BasicBlock(block.label, items))
        function.blocks = surviving
        return True
    return False


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
                    return traces if not trace else None
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
            return traces if not trace else None
        predecessor, label = label, next_label
    return None


__all__ = ["unroll_known_trip_loops"]
