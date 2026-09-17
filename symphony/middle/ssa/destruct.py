"""Destroy SSA form: replace phi nodes with parallel copies on predecessor edges.

This runs once, after every SSA-based pass has finished, and produces the same
flat, phi-free ``Instruction`` stream the backend already consumes. A
predecessor that reaches a phi-bearing block along a critical edge (the
predecessor has more than one successor, and the target has more than one
predecessor) is split with a fresh trampoline block so the inserted copies run
only on that edge. The lowerer always gives every block that can be a phi
join point its own label, so a trampoline can always jump straight to that
label without inventing one.

The backend trusts the IR's own choice of branch direction and fallthrough
instead of rediscovering it (see docs/design.md), so a trampoline for the
*fallthrough* edge out of a block must be spliced in immediately after that
block -- it becomes the new fallthrough target. A trampoline for the
*explicit* (labeled jump/branch) edge has no such positional requirement, so
every one of those is collected and appended once at the end of the
function, after every ordinary block, to avoid ever displacing a real
fallthrough successor out of position.

Multiple phis in the same target block reading values from the same
predecessor must be treated as one parallel (simultaneous) assignment, not a
naive sequence of copies: sequential ``copy`` instructions can clobber a value
another phi still needs to read from that predecessor. This uses standard
parallel-copy sequentialization -- emit copies whose destination is not read
by another pending copy first, and break any remaining cycle with one
temporary.
"""

from ..ir import BasicBlock, Instruction
from ..analysis.cfg import TERMINATORS, build_cfg


def _sequentialize(pairs, fresh_value):
    """Order ``[(dst, src), ...]`` into copies safe to run one at a time."""
    pending = dict(pairs)
    result = []
    while pending:
        ready = [dst for dst, src in pending.items() if src not in pending or src == dst]
        if ready:
            for dst in ready:
                src = pending.pop(dst)
                if src != dst:
                    result.append((dst, src))
            continue
        # Every remaining pair is part of a cycle; break one edge with a
        # temporary so the rest can proceed as ordinary copies.
        dst = next(iter(pending))
        temp = fresh_value()
        result.append((temp, pending[dst]))
        pending[dst] = temp
    return result


def _terminator_index(items):
    return next(
        (i for i in range(len(items) - 1, -1, -1) if items[i].op != "label"), None
    )


def destruct(function):
    """Rewrite ``function.blocks`` in place, removing every phi."""
    cfg = build_cfg(function)
    phi_blocks = [
        block.label
        for block in cfg.blocks
        if any(item.op == "phi" for item in block.instructions)
    ]
    if not phi_blocks:
        return

    types = {}
    for block in cfg.blocks:
        for item in block.instructions:
            if item.dst is not None:
                types[item.dst] = item.type

    def fresh_value():
        v = function.values
        function.values += 1
        return v

    edge_copies = {}
    for successor in phi_blocks:
        phis = [item for item in cfg.by_label[successor].instructions if item.op == "phi"]
        by_predecessor = {}
        for phi in phis:
            for predecessor, value in phi.extra:
                if value is not None:
                    by_predecessor.setdefault(predecessor, []).append((phi.dst, value))
        for predecessor, pairs in by_predecessor.items():
            edge_copies[(predecessor, successor)] = _sequentialize(pairs, fresh_value)

    phi_block_set = set(phi_blocks)
    for block in cfg.blocks:
        if block.label in phi_block_set:
            block.instructions = [item for item in block.instructions if item.op != "phi"]

    label_id = 0

    def new_label():
        nonlocal label_id
        label_id += 1
        return f"{function.name}.ssa_edge{label_id}"

    def copy_instruction(dst, src):
        return Instruction("copy", dst, (src,), types[dst])

    output_blocks = []
    tail_trampolines = []
    for index, block in enumerate(cfg.blocks):
        items = list(block.instructions)
        last_index = _terminator_index(items)
        has_terminator = last_index is not None and items[last_index].op in TERMINATORS
        successors = sorted(block.successors)
        explicit_successor = None
        if has_terminator and items[last_index].op == "jump":
            explicit_successor = cfg.label_blocks[items[last_index].extra]
        elif has_terminator and items[last_index].op in ("branch_if", "cbranch_if"):
            explicit_successor = cfg.label_blocks[items[last_index].extra[1]]

        inline_copies = []
        fallthrough_trampoline = None
        for successor in successors:
            copies = edge_copies.get((block.label, successor))
            if not copies:
                continue
            critical = len(successors) > 1 and len(cfg.by_label[successor].predecessors) > 1
            if not critical:
                inline_copies.extend(copies)
                continue
            label = new_label()
            body = [Instruction("label", None, (), extra=label)]
            body.extend(copy_instruction(dst, src) for dst, src in copies)
            body.append(Instruction("jump", None, (), extra=successor))
            if successor == explicit_successor:
                items[last_index] = (
                    Instruction("jump", None, (), extra=label)
                    if items[last_index].op == "jump"
                    else Instruction(
                        items[last_index].op,
                        None,
                        items[last_index].args,
                        extra=(items[last_index].extra[0], label),
                    )
                )
                tail_trampolines.append(BasicBlock(label, body[1:]))
            else:
                # The fallthrough edge: the trampoline becomes the new
                # fallthrough target, so it must sit immediately after this
                # block and before whatever block used to follow it.
                fallthrough_trampoline = BasicBlock(label, body[1:])

        if inline_copies:
            insert_at = last_index if has_terminator else len(items)
            for offset, (dst, src) in enumerate(inline_copies):
                items.insert(insert_at + offset, copy_instruction(dst, src))

        output_blocks.append(BasicBlock(block.label, items))
        if fallthrough_trampoline is not None:
            output_blocks.append(fallthrough_trampoline)

    output_blocks.extend(tail_trampolines)
    function.blocks = output_blocks
