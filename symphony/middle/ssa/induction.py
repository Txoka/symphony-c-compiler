"""SSA-native induction-variable strength reduction.

For a header phi ``i = phi(entry, i.next...)`` whose back-edge values are
``i +/- constant``, replace a repeated loop computation ``base + i`` with a
derived recurrence:

    address.entry = base + i.entry
    address = phi(address.entry, address.next...)
    address.next = address +/- constant

The transformation is driven entirely by phi edges. Unlike the former
mutable-value pass, it does not require one update site or require that the
update dominate every use in the loop; each back edge gets its own derived
update and every use of the header phi observes the right iteration value.
"""

from ..analysis.cfg import TERMINATORS, build_cfg
from ..analysis.dominance import build_dominator_tree, find_natural_loops
from ..ir import Instruction


def _insert_before_terminator(block, instructions):
    position = len(block.instructions)
    if block.terminator() is not None:
        position -= 1
    block.instructions[position:position] = instructions


def _step_from(definition, induction_value, constants, resolve):
    """Return ``(operator, constant_value)`` for one recurrence update."""
    if definition is None or definition.op != "binary":
        return None
    left, right = (resolve(value) for value in definition.args)
    if definition.extra == "+":
        if left == induction_value and right in constants:
            return "+", right
        if right == induction_value and left in constants:
            return "+", left
    elif definition.extra == "-" and left == induction_value and right in constants:
        return "-", right
    return None


def reduce_induction_strength(function):
    """Turn loop-local ``base + induction_phi`` expressions into recurrences."""
    changed = False
    while True:
        cfg = build_cfg(function)
        if not cfg.blocks:
            return changed
        dominators = build_dominator_tree(cfg)
        loops = sorted(find_natural_loops(dominators), key=lambda loop: len(loop.blocks))

        definitions = {}
        def_block = {}
        constants = {}
        for block in cfg.blocks:
            for instruction in block.instructions:
                if instruction.dst is not None:
                    definitions[instruction.dst] = instruction
                    def_block[instruction.dst] = block.label
                if instruction.op == "const":
                    constants[instruction.dst] = instruction.extra

        def resolve(value):
            """Look through SSA copies and representation-preserving casts."""
            seen = set()
            while value not in seen:
                seen.add(value)
                instruction = definitions.get(value)
                if instruction is None or instruction.op not in ("copy", "cast"):
                    break
                source = instruction.args[0]
                source_definition = definitions.get(source)
                if (
                    instruction.op == "cast"
                    and source_definition is not None
                    and source_definition.type != instruction.type
                ):
                    break
                value = source
            return value

        rewrite = None
        for loop in loops:
            header = cfg.by_label[loop.header]
            outside = [p for p in header.predecessors if p not in loop.blocks]
            if len(outside) != 1:
                continue
            preheader = outside[0]
            for phi in (item for item in header.instructions if item.op == "phi"):
                operands = dict(phi.extra)
                entry_value = operands.get(preheader)
                if entry_value is None:
                    continue
                updates = {}
                for predecessor in header.predecessors:
                    if predecessor not in loop.blocks:
                        continue
                    update_value = operands.get(predecessor)
                    resolved_update = resolve(update_value)
                    step = _step_from(
                        definitions.get(resolved_update), phi.dst, constants, resolve
                    )
                    if step is None:
                        updates = {}
                        break
                    updates[predecessor] = step
                if not updates:
                    continue
                update_values = {
                    resolve(operands[predecessor]) for predecessor in updates
                }

                for label in sorted(loop.blocks):
                    for instruction in cfg.by_label[label].instructions:
                        if instruction.op != "binary" or instruction.extra != "+":
                            continue
                        if instruction.dst in update_values:
                            continue
                        left, right = (resolve(value) for value in instruction.args)
                        if right == phi.dst:
                            base = left
                        elif left == phi.dst:
                            base = right
                        else:
                            continue
                        base_block = def_block.get(base)
                        if base_block is None or base_block in loop.blocks:
                            continue
                        if not dominators.dominates(base_block, preheader):
                            continue
                        rewrite = (loop, header, preheader, phi, entry_value, updates,
                                   instruction, base)
                        break
                    if rewrite is not None:
                        break
                if rewrite is not None:
                    break
            if rewrite is not None:
                break

        if rewrite is None:
            return changed

        loop, header, preheader, phi, entry_value, updates, derived, base = rewrite
        entry_address = function.values
        function.values += 1
        address_phi = function.values
        function.values += 1
        _insert_before_terminator(
            cfg.by_label[preheader],
            [Instruction("binary", entry_address, (base, entry_value), derived.type, "+")],
        )

        phi_operands = [(preheader, entry_address)]
        for predecessor, (operator, constant_value) in sorted(updates.items()):
            next_address = function.values
            function.values += 1
            _insert_before_terminator(
                cfg.by_label[predecessor],
                [
                    Instruction(
                        "binary",
                        next_address,
                        (address_phi, constant_value),
                        derived.type,
                        operator,
                    )
                ],
            )
            phi_operands.append((predecessor, next_address))

        insert_at = next(
            (i for i, item in enumerate(header.instructions) if item.op not in ("label", "phi")),
            len(header.instructions),
        )
        header.instructions.insert(
            insert_at,
            Instruction(
                "phi",
                address_phi,
                (),
                derived.type,
                tuple(sorted(phi_operands, key=lambda pair: pair[0])),
            ),
        )
        for block in function.blocks:
            for index, instruction in enumerate(block.instructions):
                if instruction is derived:
                    block.instructions[index] = Instruction(
                        "copy", derived.dst, (address_phi,), derived.type
                    )
                    break
        changed = True
