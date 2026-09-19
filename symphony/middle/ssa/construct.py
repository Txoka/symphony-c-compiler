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

A phi's ``extra`` holds a tuple of ``(predecessor_label, value)`` pairs,
sorted by predecessor label, keyed by the predecessor block's stable
identity rather than a positional index into one particular
``ControlFlowGraph`` build. This is what lets a phi survive being
reconstructed from a differently-shaped CFG (e.g. after inlining splices
blocks from another function): the label a phi's operand names is exactly
the label the corresponding ``BasicBlock`` carries persistently, so it never
needs remapping when the CFG is rebuilt.
"""

from dataclasses import dataclass

from ..ir import BasicBlock, Instruction
from ..analysis.cfg import build_cfg
from ..analysis.dominance import build_dominator_tree


@dataclass
class SSAFunction:
    function: object
    cfg: object
    dominators: object
    phis: dict  # block label -> {local key -> phi Instruction}


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
    for block in cfg.blocks:
        for instruction in block.instructions:
            if instruction.op == "store" and instruction.args[0] in address_of:
                defining_blocks[address_of[instruction.args[0]]].add(block.label)
            elif instruction.op == "copy" and instruction.dst in join_vars:
                defining_blocks[instruction.dst].add(block.label)

    phis = {block.label: {} for block in cfg.blocks}
    for key in promotable:
        worklist = list(defining_blocks[key])
        has_phi = set()
        while worklist:
            block_label = worklist.pop()
            for frontier_block in dominators.frontier.get(block_label, ()):
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

    rewritten_blocks = {block.label: [] for block in cfg.blocks}

    def rename(block_label, current):
        current = dict(current)
        for key, phi in phis[block_label].items():
            current[key] = phi.dst

        out = []
        for instruction in cfg.by_label[block_label].instructions:
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
        rewritten_blocks[block_label] = out

        for successor in sorted(cfg.by_label[block_label].successors):
            for key, phi in phis[successor].items():
                phi.extra.append((block_label, current.get(key)))

        for child in dominators.children.get(block_label, ()):
            rename(child, current)

    if cfg.blocks:
        rename(cfg.blocks[0].label, {})

    # Blocks unreachable from entry have no dominator-tree path and are never
    # visited above; mem2reg is only meaningful for reachable code, so leave
    # such a block's instructions exactly as the lowerer emitted them.
    reachable = cfg.reachable()
    for block in cfg.blocks:
        if block.label not in reachable:
            rewritten_blocks[block.label] = list(block.instructions)

    new_blocks = []
    for block in cfg.blocks:
        body = rewritten_blocks[block.label]
        split = next((i for i, item in enumerate(body) if item.op != "label"), len(body))
        items = list(body[:split])
        for key in sorted(phis[block.label], key=repr):
            phi = phis[block.label][key]
            phi.extra = tuple(sorted(phi.extra, key=lambda pair: pair[0]))
            items.append(phi)
        items.extend(body[split:])
        new_blocks.append(BasicBlock(block.label, items))
    function.blocks = new_blocks
    return SSAFunction(function, cfg, dominators, phis)
