"""Bounded full unrolling for canonical constant-trip natural loops."""

from ..analysis.cfg import build_cfg
from ..analysis.dominance import build_dominator_tree, find_natural_loops
from ..ir import BasicBlock, Instruction
from .evaluate import _safe_binary
from .sccp import _fold_unary, _normalize


UNKNOWN = object()


def unroll_known_trip_loops(function, iteration_limit=4):
    """Fully unroll linear canonical loops with at most ``iteration_limit`` trips."""
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
        if len(loop.back_edges) != 1:
            continue
        latch, header_label = next(iter(loop.back_edges))
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
        header_other = [
            item for item in header.instructions if item.op not in ("label", "phi")
        ]
        if (
            not phis
            or len(header_other) != 1
            or header_other[0].op not in ("branch_if", "cbranch_if")
            or any(set(source for source, _ in phi.extra) != {preheader, latch} for phi in phis)
        ):
            continue
        body = _linear_body(cfg, loop, header_label, exit_label)
        if body is None or not body or body[-1] != latch:
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
        trip_count = _trip_count(
            cfg, loop, phis, preheader, body, definitions, def_block, iteration_limit
        )
        if trip_count is None:
            continue

        current = {phi.dst: dict(phi.extra)[preheader] for phi in phis}
        final_map = dict(current)
        expanded = []
        for _ in range(trip_count):
            iteration = dict(current)
            for label in body:
                for item in cfg.by_label[label].instructions:
                    if item.op == "label" or item is cfg.by_label[label].terminator():
                        continue
                    if item.op == "phi" or item.op in ("branch_if", "cbranch_if", "jump", "return"):
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
            if expanded is None:
                break
            current = {
                phi.dst: iteration.get(dict(phi.extra)[latch], dict(phi.extra)[latch])
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


def _linear_body(cfg, loop, header, exit_label):
    starts = [value for value in cfg.by_label[header].successors if value != exit_label]
    if len(starts) != 1:
        return None
    result, seen = [], {header}
    label = starts[0]
    while label != header:
        if label in seen or label not in loop.blocks:
            return None
        seen.add(label)
        result.append(label)
        block = cfg.by_label[label]
        if len(block.successors) != 1:
            return None
        label = block.successors[0]
    return result if seen == loop.blocks else None


def _trip_count(cfg, loop, phis, preheader, body, definitions, def_block, limit):
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
    condition = next(
        item for item in cfg.by_label[loop.header].instructions
        if item.op in ("branch_if", "cbranch_if")
    )
    target = cfg.label_blocks.get(condition.extra[1], condition.extra[1])

    for count in range(limit + 1):
        if condition.op == "branch_if":
            value = values.get(condition.args[0], UNKNOWN)
            if value is UNKNOWN:
                return None
            take_target = bool(value) == condition.extra[0]
        else:
            left, right = (values.get(value, UNKNOWN) for value in condition.args)
            if left is UNKNOWN or right is UNKNOWN:
                return None
            folded = _safe_binary(condition.extra[0], left, right, condition.type)
            if folded is None:
                return None
            take_target = bool(folded)
        chosen = target if take_target else next(
            value for value in cfg.by_label[loop.header].successors if value != target
        )
        if chosen not in loop.blocks:
            return count
        if count == limit:
            return None
        for label in body:
            for item in cfg.by_label[label].instructions:
                if item.op in ("label", "jump"):
                    continue
                if item.dst is None:
                    continue
                result = UNKNOWN
                if item.op == "const":
                    result = _normalize(item.extra, item.type)
                elif item.op in ("copy", "cast"):
                    result = values.get(item.args[0], UNKNOWN)
                    if result is not UNKNOWN:
                        result = _normalize(result, item.type)
                elif item.op == "unary":
                    operand = values.get(item.args[0], UNKNOWN)
                    if operand is not UNKNOWN:
                        folded = _fold_unary(item.extra, operand, item.type)
                        result = UNKNOWN if folded is None else folded
                elif item.op == "binary":
                    left, right = (values.get(value, UNKNOWN) for value in item.args)
                    if left is not UNKNOWN and right is not UNKNOWN:
                        folded = _safe_binary(item.extra, left, right, item.type)
                        result = UNKNOWN if folded is None else folded
                values[item.dst] = result
        updates = {
            phi.dst: values.get(dict(phi.extra)[body[-1]], UNKNOWN) for phi in phis
        }
        values.update(updates)
    return None


__all__ = ["unroll_known_trip_loops"]
