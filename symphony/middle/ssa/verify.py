"""Check that a function's IR is well-formed SSA: every value has exactly one
definition, and every use is dominated by its definition (a phi operand only
needs to be dominated along its specific predecessor edge, not at the phi
itself).

Construction only promotes and renames blocks reachable from entry (mem2reg
is meaningless for unreachable code, and the whole-program reachability pass
that would otherwise delete such code is an optimization out of scope here),
so a block unreachable from entry may keep referencing values defined in
reachable code with no dominance relationship at all. Such blocks, and any
use inside them, are excluded from the dominance check; they can never
execute, so the value they reference is never actually read.
"""

from ..analysis.cfg import build_cfg
from ..analysis.dominance import build_dominator_tree


class SSAVerificationError(AssertionError):
    pass


def verify(function):
    cfg = build_cfg(function)
    dominators = build_dominator_tree(cfg)
    reachable = cfg.reachable()

    definitions = {}
    def_block = {}
    for block in cfg.blocks:
        for instruction in block.instructions:
            if instruction.dst is None:
                continue
            if instruction.dst in definitions:
                raise SSAVerificationError(
                    f"{function.name}: value %{instruction.dst} defined more than once"
                )
            definitions[instruction.dst] = instruction
            def_block[instruction.dst] = block.label

    for block in cfg.blocks:
        if block.label not in reachable:
            continue
        for instruction in block.instructions:
            if instruction.op == "phi":
                seen = {p for p, _ in instruction.extra}
                if seen != set(block.predecessors):
                    raise SSAVerificationError(
                        f"{function.name}: phi %{instruction.dst} in block {block.label} "
                        f"covers predecessors {sorted(seen)}, block has {sorted(block.predecessors)}"
                    )
                for predecessor, value in instruction.extra:
                    if value is None:
                        continue
                    if value not in def_block:
                        raise SSAVerificationError(
                            f"{function.name}: phi %{instruction.dst} reads undefined %{value}"
                        )
                    if not _dominates_edge(dominators, def_block[value], predecessor):
                        raise SSAVerificationError(
                            f"{function.name}: %{value} does not dominate predecessor "
                            f"edge {predecessor}->{block.label} of phi %{instruction.dst}"
                        )
                continue
            for value in instruction.args:
                if not isinstance(value, int):
                    continue
                if value not in def_block:
                    raise SSAVerificationError(
                        f"{function.name}: use of undefined value %{value}"
                    )
                if not dominators.dominates(def_block[value], block.label):
                    raise SSAVerificationError(
                        f"{function.name}: %{value} defined in block {def_block[value]} "
                        f"does not dominate its use in block {block.label}"
                    )


def _dominates_edge(dominators, def_block_index, predecessor_index):
    return dominators.dominates(def_block_index, predecessor_index)
