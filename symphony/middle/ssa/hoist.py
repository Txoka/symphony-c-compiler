"""Loop-invariant code motion directly on persistent SSA.

A value is invariant when its defining instruction is a pure op sitting
inside the loop and every one of its operands is either defined outside the
loop (dominates the loop preheader) or already proven invariant -- a direct
dominance query. On the old mutable IR this required first checking that a
value had exactly one definition at all (ruling out mem2reg-promoted slots
reassigned on multiple paths); on real SSA every value already has exactly
one definition, so that check disappears entirely. A ``phi`` is never
treated as invariant: it represents a merge of values from different paths,
not a computation, so it always stays put (correctly -- one of its operands
is, by construction, reached via a back edge from inside the loop).

Loads are only hoisted out of loops with no stores/calls, since the IR
carries no alias information to prove a load and a store elsewhere never
conflict -- unchanged from the old pass's conservative rule.
"""

from ..analysis.cfg import build_cfg, TERMINATORS
from ..analysis.dominance import build_dominator_tree, find_natural_loops

PURE = {
    "const",
    "global_addr",
    "local_addr",
    "cast",
    "copy",
    "load",
    "unary",
    "binary",
}

MEMORY_EFFECTS = {
    "store",
    "call",
    "direct_call",
    "tailcall",
    "direct_tailcall",
    "intrinsic",
    "stack_alloc",
}


def hoist_loop_invariants(function):
    """Move loop-invariant pure computations to a preheader before the header."""
    changed = False
    # Loop headers already tried and found unhoistable this call, identified by
    # the header block's first instruction (stable across block-shape-shifting
    # rewrites elsewhere in the function).
    skip_headers = set()
    # Process one loop per CFG snapshot: rewriting instructions can shift block
    # shape, so the CFG, dominators and loop set are rebuilt after each move.
    # Innermost-first (smallest block set) lets a nested loop's invariants
    # become hoistable to its parent once freed of the inner loop's values.
    while True:
        cfg = build_cfg(function)
        if not cfg.blocks:
            break
        dominators = build_dominator_tree(cfg)
        loops = [
            loop
            for loop in find_natural_loops(dominators)
            if id(cfg.by_label[loop.header].instructions[0]) not in skip_headers
        ]
        if not loops:
            break

        definitions = {}
        block_of = {}
        for block in cfg.blocks:
            for instruction in block.instructions:
                if instruction.dst is not None:
                    definitions[instruction.dst] = instruction
                block_of[id(instruction)] = block.label

        loop = min(loops, key=lambda l: len(l.blocks))
        header_key = id(cfg.by_label[loop.header].instructions[0])
        has_memory_effects = any(
            instruction.op in MEMORY_EFFECTS
            for label in loop.blocks
            for instruction in cfg.by_label[label].instructions
        )
        preheader_predecessors = [
            p for p in cfg.by_label[loop.header].predecessors if p not in loop.blocks
        ]
        if len(preheader_predecessors) != 1:
            skip_headers.add(header_key)
            continue  # no single edge to insert a preheader jump/label pair on
        invariant = set()

        def is_invariant(value):
            if value in invariant:
                return True
            instruction = definitions.get(value)
            if instruction is None:
                return False
            if block_of.get(id(instruction)) not in loop.blocks:
                return True
            if instruction.op not in PURE:
                return False
            if instruction.op == "load" and has_memory_effects:
                return False
            if not all(is_invariant(arg) for arg in instruction.args):
                return False
            invariant.add(value)
            return True

        hoisted_ids = []
        for label in sorted(loop.blocks):
            for instruction in cfg.by_label[label].instructions:
                if instruction.dst is not None and is_invariant(instruction.dst):
                    hoisted_ids.append(id(instruction))

        if not hoisted_ids:
            skip_headers.add(header_key)
            continue

        hoisted_ids = set(hoisted_ids)
        preheader = preheader_predecessors[0]
        # Keep the block graph intact.  Flattening through
        # FunctionIR.instructions re-splits every block and turns a local
        # LICM rewrite into an unnecessary representation round trip.
        preamble = [
            instruction
            for block in function.blocks
            for instruction in block.instructions
            if id(instruction) in hoisted_ids
        ]
        for label in loop.blocks:
            block = cfg.by_label[label]
            block.instructions = [
                instruction
                for instruction in block.instructions
                if id(instruction) not in hoisted_ids
            ]
        # Insert before the preheader's terminator (if any) so the preamble
        # stays reachable, rather than after it where it would be dead code.
        preheader_block = cfg.by_label[preheader]
        last = preheader_block.terminator()
        insert_at = len(preheader_block.instructions)
        if last is not None and last.op in TERMINATORS:
            insert_at -= 1
        preheader_block.instructions[insert_at:insert_at] = preamble
        changed = True
        # Loop back to the top of the while: cfg/dominators/loops are rebuilt
        # fresh against the rewritten instructions before the next pick.

    return changed
