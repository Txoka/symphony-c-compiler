"""Build persistent SSA form: mem2reg promotion plus phi insertion.

Construction operates on one ``FunctionIR`` at a time and promotes two kinds
of pre-SSA "variable" into real single-assignment SSA values, using
Cytron/Ferrante/Rosen/Wegman/Zadeck phi placement at dominance frontiers and
renaming uses to the reaching definition along each path:

* Non-escaping scalar locals -- address only ever used as the first operand
  of a ``load`` or ``store``, and never a function parameter, since the
  backend materializes incoming arguments directly into a parameter's stack
  slot, so a parameter's memory form stays the entry point for its value.
  Keyed by the local's symbol key.
* Bare join values the lowerer already builds by hand for ``&&``/``||``/
  ``?:``: one value id pre-allocated before either arm, assigned with a
  ``copy`` in each arm. These are exactly a hand-written phi with the
  ``phi`` node itself missing; keyed by that pre-allocated value id.

Every other instruction in the lowered IR assigns a fresh value id exactly
once (``Lowerer.emit`` never reuses a ``dst``), so the rest of the function
is already in single-assignment form and needs no renaming.

A phi's ``extra`` holds a tuple of ``(predecessor_block_index, value)``
pairs, sorted by predecessor index, rather than relying on positional
alignment with a block's unordered predecessor set. Block indices are only
meaningful for the exact ``ControlFlowGraph`` build that produced them, so
callers should treat the returned ``SSAFunction`` as the source of truth for
block/phi structure instead of rebuilding the CFG.
"""

from dataclasses import dataclass

from ..ir import Instruction
from ..analysis.cfg import build_cfg
from ..analysis.dominance import build_dominator_tree


@dataclass
class SSAFunction:
    function: object
    cfg: object
    dominators: object
    phis: dict  # block index -> {local key -> phi Instruction}


def _promotable_locals(function):
    """Local symbols whose address never escapes a load/store first operand."""
    param_keys = {p.key for p in function.params}
    addr_of = {}
    for instruction in function.instructions:
        if instruction.op == "local_addr":
            addr_of.setdefault(instruction.extra, []).append(instruction.dst)

    uses = {}
    for instruction in function.instructions:
        for position, value in enumerate(instruction.args):
            uses.setdefault(value, []).append((instruction, position))

    promotable = {}
    for symbol in function.locals:
        if symbol.key in param_keys or symbol.type.kind not in ("int", "bool", "pointer"):
            continue
        values = addr_of.get(symbol.key, ())
        if not values:
            continue
        if all(
            all(
                position == 0 and user.op in ("load", "store")
                for user, position in uses.get(value, ())
            )
            for value in values
        ):
            promotable[symbol.key] = symbol.type
    return promotable


def _promotable_join_values(function):
    """Bare values the lowerer pre-allocates and assigns via 'copy' more than
    once (its hand-written join pattern for &&/||/?:)."""
    definitions = {}
    for instruction in function.instructions:
        if instruction.dst is not None:
            definitions.setdefault(instruction.dst, []).append(instruction)
    promotable = {}
    for value, items in definitions.items():
        if len(items) > 1:
            assert all(item.op == "copy" for item in items), (
                f"{function.name}: value %{value} redefined by non-copy instructions"
            )
            promotable[value] = items[0].type
    return promotable


def construct(function):
    """Rewrite ``function.instructions`` in place into persistent SSA form.

    Returns the ``SSAFunction`` used during construction so later stages
    (verification, destruction) can reuse its CFG/dominator information
    without recomputing it.
    """
    local_vars = _promotable_locals(function)
    join_vars = _promotable_join_values(function)
    promotable = {**local_vars, **join_vars}
    cfg = build_cfg(function)
    dominators = build_dominator_tree(cfg)

    address_of = {}  # value id -> promoted local key
    for block in cfg.blocks:
        for instruction in block.instructions:
            if instruction.op == "local_addr" and instruction.extra in local_vars:
                address_of[instruction.dst] = instruction.extra

    defining_blocks = {key: set() for key in promotable}
    for index, block in enumerate(cfg.blocks):
        for instruction in block.instructions:
            if instruction.op == "store" and instruction.args[0] in address_of:
                defining_blocks[address_of[instruction.args[0]]].add(index)
            elif instruction.op == "copy" and instruction.dst in join_vars:
                defining_blocks[instruction.dst].add(index)

    phis = {index: {} for index in range(len(cfg.blocks))}
    for key in promotable:
        worklist = list(defining_blocks[key])
        has_phi = set()
        while worklist:
            block = worklist.pop()
            for frontier_block in dominators.frontier.get(block, ()):
                if frontier_block in has_phi:
                    continue
                has_phi.add(frontier_block)
                value = function.values
                function.values += 1
                phis[frontier_block][key] = Instruction(
                    "phi", value, (), promotable[key], []
                )
                if frontier_block not in defining_blocks[key]:
                    worklist.append(frontier_block)

    rewritten_blocks = {index: [] for index in range(len(cfg.blocks))}

    def rename(block_index, current):
        current = dict(current)
        for key, phi in phis[block_index].items():
            current[key] = phi.dst

        out = []
        for instruction in cfg.blocks[block_index].instructions:
            if instruction.op == "local_addr" and instruction.dst in address_of:
                continue

            args = tuple(
                current[a] if a in join_vars else a for a in instruction.args
            )
            instruction = (
                instruction
                if args == instruction.args
                else Instruction(
                    instruction.op, instruction.dst, args, instruction.type, instruction.extra
                )
            )

            if instruction.op == "load" and instruction.args[0] in address_of:
                key = address_of[instruction.args[0]]
                out.append(
                    Instruction("copy", instruction.dst, (current[key],), instruction.type)
                )
                continue
            if instruction.op == "store" and instruction.args[0] in address_of:
                key = address_of[instruction.args[0]]
                current[key] = instruction.args[1]
                continue
            if instruction.op == "copy" and instruction.dst in join_vars:
                (source,) = instruction.args
                current[instruction.dst] = source
                continue
            out.append(instruction)
        rewritten_blocks[block_index] = out

        for successor in sorted(cfg.blocks[block_index].successors):
            for key, phi in phis[successor].items():
                phi.extra.append((block_index, current.get(key)))

        for child in dominators.children.get(block_index, ()):
            rename(child, current)

    if cfg.blocks:
        rename(0, {})

    # Blocks unreachable from entry have no dominator-tree path and are never
    # visited above; mem2reg is only meaningful for reachable code, so leave
    # such a block's instructions exactly as the lowerer emitted them.
    reachable = cfg.reachable()
    for index in range(len(cfg.blocks)):
        if index not in reachable:
            rewritten_blocks[index] = list(cfg.blocks[index].instructions)

    instructions = []
    for index in range(len(cfg.blocks)):
        body = rewritten_blocks[index]
        split = next((i for i, item in enumerate(body) if item.op != "label"), len(body))
        instructions.extend(body[:split])
        for key in sorted(phis[index], key=repr):
            phi = phis[index][key]
            phi.extra = tuple(sorted(phi.extra, key=lambda pair: pair[0]))
            instructions.append(phi)
        instructions.extend(body[split:])
    function.instructions = instructions
    return SSAFunction(function, cfg, dominators, phis)
