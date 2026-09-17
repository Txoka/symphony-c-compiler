"""Sparse conditional constant propagation directly on persistent SSA.

Classic SSA-SCCP: a lattice value (unknown / constant / varying) per SSA
value, propagated by re-evaluating only the blocks that use a value whose
lattice value just changed, together with a separate discovery of which CFG
edges are executable, so a phi only merges operands along edges proven
reachable and a conditional branch with a resolved condition drops its
untaken edge instead of being folded through an approximate per-block meet.

This replaces the previous block-level abstract interpretation, which
rebuilt an entire per-block value environment from scratch on every fixed-
point iteration over the whole function. Real phi nodes let this version
track one lattice value per SSA value directly and only re-derive a block
when a value it actually reads has changed -- "sparse" in the SSA sense,
not just in name.

Two things fall out of persistent SSA for free, beyond what the old
block-lattice pass could do: a phi with a single surviving executable
predecessor collapses straight into a ``copy`` (exposing it to ordinary
copy propagation immediately, without a separate pass), and constant
propagation through a phi no longer approximates through every predecessor
unconditionally -- it merges exactly the operands of edges already proven
reachable.
"""

from ..ir import Instruction
from ..analysis.cfg import build_cfg
from ..passes.pipeline import _fold_binary, _fold_unary, _normalize

UNKNOWN = object()
VARYING = object()


def _meet(a, b):
    if a is UNKNOWN:
        return b
    if b is UNKNOWN:
        return a
    if a is VARYING or b is VARYING or a != b:
        return VARYING
    return a


def sparse_conditional_constant_propagation(function):
    """Propagate constants through phis and discover executable edges."""
    cfg = build_cfg(function)
    if not cfg.blocks:
        return False

    # Which block reads a given SSA value, directly or as a phi operand --
    # used to know which blocks to re-derive when that value's lattice
    # value changes.
    readers = {}
    for block in cfg.blocks:
        for instruction in block.instructions:
            operands = (
                (operand for _, operand in instruction.extra if operand is not None)
                if instruction.op == "phi"
                else instruction.args
            )
            for operand in operands:
                readers.setdefault(operand, set()).add(block.index)

    value_of = {}
    executable_edges = set()
    reachable_blocks = {0}
    block_worklist = [0]

    def value(v):
        return value_of.get(v, UNKNOWN)

    def set_value(dst, new):
        if value_of.get(dst, UNKNOWN) == new:
            return
        value_of[dst] = new
        for block_index in readers.get(dst, ()):
            if block_index in reachable_blocks and block_index not in block_worklist:
                block_worklist.append(block_index)

    def evaluate_phi(instruction, block_index):
        result = UNKNOWN
        for predecessor, operand in instruction.extra:
            if (predecessor, block_index) not in executable_edges:
                continue
            result = _meet(result, UNKNOWN if operand is None else value(operand))
            if result is VARYING:
                return VARYING
        return result

    def evaluate(instruction):
        op = instruction.op
        if op == "const":
            return _normalize(instruction.extra, instruction.type)
        args = [value(v) for v in instruction.args]
        if op == "copy" and args:
            return args[0]
        if op == "cast" and args and args[0] not in (UNKNOWN, VARYING):
            return _normalize(args[0], instruction.type)
        if op == "unary" and args and args[0] not in (UNKNOWN, VARYING):
            folded = _fold_unary(instruction.extra, args[0], instruction.type)
            return VARYING if folded is None else folded
        if op == "binary" and all(v not in (UNKNOWN, VARYING) for v in args):
            folded = _fold_binary(instruction.extra, args[0], args[1], instruction.type)
            return VARYING if folded is None else folded
        return VARYING if instruction.dst is not None else UNKNOWN

    def mark_edge(source, target):
        edge = (source, target)
        if edge in executable_edges:
            return
        executable_edges.add(edge)
        newly_reachable = target not in reachable_blocks
        reachable_blocks.add(target)
        if newly_reachable or any(
            item.op == "phi" for item in cfg.blocks[target].instructions
        ):
            if target not in block_worklist:
                block_worklist.append(target)

    def branch_outcome(last):
        if last.op == "branch_if":
            condition = value(last.args[0])
            return bool(condition) if condition not in (UNKNOWN, VARYING) else condition
        values = [value(v) for v in last.args]
        if all(v not in (UNKNOWN, VARYING) for v in values):
            return bool(_fold_binary(last.extra[0], values[0], values[1], last.type))
        return VARYING if VARYING in values else UNKNOWN

    while block_worklist:
        index = block_worklist.pop()
        block = cfg.blocks[index]
        for instruction in block.instructions:
            if instruction.dst is None:
                continue
            new = (
                evaluate_phi(instruction, index)
                if instruction.op == "phi"
                else evaluate(instruction)
            )
            set_value(instruction.dst, new)

        last = next((i for i in reversed(block.instructions) if i.op != "label"), None)
        if last is None or last.op not in ("branch_if", "cbranch_if"):
            for successor in block.successors:
                mark_edge(index, successor)
            continue
        outcome = branch_outcome(last)
        if outcome is UNKNOWN:
            continue
        target = cfg.label_blocks[last.extra[1]]
        taken_is_target = last.extra[0] if last.op == "branch_if" else True
        if outcome is VARYING:
            for successor in block.successors:
                mark_edge(index, successor)
        elif outcome == taken_is_target:
            mark_edge(index, target)
        else:
            for successor in block.successors - {target}:
                mark_edge(index, successor)

    original = tuple(function.instructions)
    live_successors = {}
    for source, target in executable_edges:
        live_successors.setdefault(source, set()).add(target)

    rewritten = []
    for block in cfg.blocks:
        if block.index not in reachable_blocks:
            # Leave unreachable blocks untouched, exactly like construction:
            # dropping their instructions here would shift every later
            # block's index on the next build_cfg() call, corrupting phi
            # predecessor references elsewhere in the function. Actually
            # removing dead blocks is DCE's job, not SCCP's.
            rewritten.extend(block.instructions)
            continue
        for instruction in block.instructions:
            if instruction.op == "phi":
                result = value_of.get(instruction.dst, UNKNOWN)
                if result not in (UNKNOWN, VARYING):
                    rewritten.append(
                        Instruction("const", instruction.dst, (), instruction.type, result)
                    )
                    continue
                surviving = tuple(
                    (predecessor, operand)
                    for predecessor, operand in instruction.extra
                    if (predecessor, block.index) in executable_edges
                )
                if len(surviving) == 1:
                    rewritten.append(
                        Instruction(
                            "copy", instruction.dst, (surviving[0][1],), instruction.type
                        )
                    )
                    continue
                if surviving != instruction.extra:
                    instruction = Instruction(
                        "phi", instruction.dst, (), instruction.type, surviving
                    )
                rewritten.append(instruction)
                continue

            result = (
                value_of.get(instruction.dst, UNKNOWN)
                if instruction.dst is not None
                else UNKNOWN
            )
            if result not in (UNKNOWN, VARYING) and instruction.op in (
                "copy",
                "cast",
                "unary",
                "binary",
            ):
                instruction = Instruction(
                    "const", instruction.dst, (), instruction.type, result
                )

            if instruction.op in ("branch_if", "cbranch_if"):
                live = live_successors.get(block.index, set())
                target = cfg.label_blocks[instruction.extra[1]]
                if target not in live:
                    continue
                if len(live) == 1:
                    instruction = Instruction("jump", extra=instruction.extra[1])
            rewritten.append(instruction)
    function.instructions = rewritten
    return tuple(function.instructions) != original
