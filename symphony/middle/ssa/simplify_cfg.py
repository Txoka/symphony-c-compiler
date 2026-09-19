"""Structural CFG cleanup directly on persistent SSA: drop unreachable blocks
and thread jumps through trampoline blocks that do nothing but jump
somewhere else.

Constant-branch folding and infeasible-edge pruning are already SCCP's job
(see ``sccp.py``'s docstring: a phi with a single surviving executable
predecessor collapses to a ``copy`` there, "for free"). What SCCP never does
is restructure the block list itself -- it always emits exactly one output
block per input block, by design, so it composes cleanly with every other
per-function pass. This pass is where the resulting dead/trampoline blocks
actually get removed.

A block is threadable when its only real instruction is an unconditional
``jump`` (no phi -- a phi makes it a genuine merge point, not a trampoline).
Redirecting a predecessor's *explicit* edge through it is a plain rewrite of
that predecessor's own terminator (``redirect_edge``); redirecting a
*fallthrough* edge would require physically relocating blocks to keep the
fallthrough contract intact (see ``analysis/cfg.py``), which is more churn
than this pass buys back, so fallthrough edges into a threadable block are
simply left alone -- still correct, just not the one further hop shorter.

One edge is threaded at a time, immediately followed by its own phi fixup,
rather than batching every redirect and reconciling phis afterward: once a
predecessor's edge is redirected straight to the trampoline's target, that
predecessor becomes a new, direct predecessor of a block that may have its
own phi -- one that never had an operand for this predecessor before,
because it used to reach it only indirectly through the trampoline. The
trampoline itself carries no phi (nothing to merge; every path through it
already carries one unambiguous value), so the value to give the new
operand is exactly whatever operand the target's phi already had recorded
for the trampoline's own label -- no need to search or resolve chains, the
answer is sitting right there before the redirect touches anything else.
"""

from ..analysis.cfg import build_cfg, prune_unreachable_blocks, redirect_edge


def _threadable(cfg):
    """label -> the single label a threadable block jumps to, for every
    block whose only real instruction is an unconditional jump."""
    targets = {}
    for block in cfg.blocks:
        body = [item for item in block.instructions if item.op != "label"]
        if len(body) == 1 and body[0].op == "jump":
            targets[block.label] = cfg.label_blocks.get(body[0].extra, body[0].extra)
    return targets


def _retarget_phis(function, source_label, old_target, new_target):
    """After redirecting ``source``'s edge from ``old_target`` straight to
    ``new_target``, give every phi in ``new_target`` a ``source``-keyed
    operand carrying whatever value it already recorded for ``old_target``
    (the trampoline's own predecessor entry) -- ``source`` is now a direct
    predecessor and needs its own operand, but the value flowing along this
    path never changes, since a trampoline is pure control flow."""
    for block in function.blocks:
        if block.label != new_target:
            continue
        for instruction in block.instructions:
            if instruction.op != "phi":
                continue
            by_label = dict(instruction.extra)
            if old_target not in by_label or source_label in by_label:
                continue
            by_label[source_label] = by_label[old_target]
            instruction.extra = tuple(sorted(by_label.items(), key=lambda pair: pair[0]))


def _thread_jumps(function):
    changed = False
    while True:
        cfg = build_cfg(function)
        targets = _threadable(cfg)
        if not targets:
            return changed

        redirected = False
        for block in cfg.blocks:
            last = block.terminator()
            if last is None:
                continue
            if last.op == "jump":
                spelled_target = last.extra
            elif last.op in ("branch_if", "cbranch_if"):
                spelled_target = last.extra[1]
            else:
                continue
            old_target = cfg.label_blocks.get(spelled_target, spelled_target)
            new_target = targets.get(old_target)
            if new_target is None or new_target == block.label or new_target == old_target:
                continue
            # Keep a trampoline when it distinguishes two edges from the same
            # source that carry different phi values. Collapsing both to one
            # source->target edge would make that distinction unrepresentable.
            if new_target in block.successors:
                continue
            redirect_edge(function, block.label, spelled_target, new_target)
            _retarget_phis(function, block.label, old_target, new_target)
            redirected = True
            changed = True

        if not redirected:
            return changed


def simplify_control_flow(function):
    """Fixed point of unreachable-block pruning and jump threading."""
    changed = False
    while True:
        step = False
        if prune_unreachable_blocks(function):
            step = True
        if _thread_jumps(function):
            step = True
        cfg = build_cfg(function)
        for index, block in enumerate(cfg.blocks[:-1]):
            last = block.terminator()
            if last is None or last.op != "jump":
                continue
            target = cfg.label_blocks.get(last.extra, last.extra)
            if target != cfg.blocks[index + 1].label:
                continue
            block.instructions.pop()
            step = True
        referenced = {
            target
            for block in function.blocks
            for instruction in block.instructions
            for target in (
                (instruction.extra,) if instruction.op == "jump" else
                (instruction.extra[1],) if instruction.op in ("branch_if", "cbranch_if") else
                ()
            )
        }
        for block in function.blocks:
            kept = [
                item for item in block.instructions
                if item.op != "label" or item.extra in referenced
            ]
            if len(kept) != len(block.instructions):
                block.instructions = kept
                step = True
        if not step:
            break
        changed = True
    return changed
