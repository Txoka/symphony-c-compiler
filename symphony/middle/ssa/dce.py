"""Dead-value elimination directly on persistent SSA.

A value is required if it feeds a side-effecting instruction or a control-flow
terminator, directly or transitively through other required values -- the
standard mark phase of mark-and-sweep DCE. A ``phi``'s operands only count as
uses when the phi's own destination is itself required: an unused phi (dead
in its own right, or part of a cycle where every member is only read by other
dead phis) is prunable exactly like any other pure, unused instruction, and
its operands stop propagating "required" to whatever fed them.

Deletion is gated by an *allowlist* of provably pure ops (``PURE``, ported
unchanged from the old block-lattice pass, plus ``phi``), not a blocklist of
known side-effecting ones: an instruction whose op isn't recognized as pure
is always kept regardless of whether its ``dst`` looks unused. An
enumerate-every-side-effect blocklist is one missed opcode away from
silently deleting something load-bearing (e.g. a backend-only op like
``init_text_screen``, which has no ``dst`` at all and exists purely for its
side effect); the allowlist can only ever be too conservative, never wrong.

This matters beyond ordinary cleanup: a dead phi can carry a ``None`` operand
on some predecessor edge (nothing reaches it with a defined value on that
path, since nothing ever reads the phi's result there either). ``destruct``
correctly emits no copy for a ``None`` operand, so if such a dead phi's
leftover copies survive to a later ``construct`` call (e.g. inlining's
clone-then-splice), that second construction would try to re-promote them as
a genuine variable and place a phi with no reaching definition on that edge,
breaking dominance. Running this pass before any repeated construct/destruct
cycle removes the dead phi (and its copies) first, so that never happens.
"""

from ..ir import BasicBlock

PURE = {
    "const",
    "global_addr",
    "local_addr",
    "cast",
    "copy",
    "load",
    "unary",
    "binary",
    "phi",
}


def remove_dead_values(function):
    """Delete pure, unused instructions (including dead phis) from every block."""
    instructions = [
        instruction for block in function.blocks for instruction in block.instructions
    ]

    required = set()
    for instruction in instructions:
        if instruction.dst is None or instruction.op not in PURE:
            required.update(v for v in instruction.args if isinstance(v, int))
            if instruction.op == "phi":
                required.update(v for _, v in instruction.extra if isinstance(v, int))

    changed = True
    while changed:
        changed = False
        for instruction in instructions:
            if instruction.dst not in required:
                continue
            if instruction.op == "phi":
                operands = (v for _, v in instruction.extra)
            else:
                operands = instruction.args
            for value in operands:
                if isinstance(value, int) and value not in required:
                    required.add(value)
                    changed = True

    def keep(instruction):
        return instruction.op not in PURE or instruction.dst in required

    new_blocks = []
    removed = False
    for block in function.blocks:
        kept = [instruction for instruction in block.instructions if keep(instruction)]
        if len(kept) != len(block.instructions):
            removed = True
        new_blocks.append(BasicBlock(block.label, kept))
    function.blocks = new_blocks
    return removed
