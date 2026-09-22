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
from ..analysis.profitability import (
    LoopTransformationCost,
    exact_trip_count,
    peak_live_values,
    profitable,
)
from ..ir import Instruction
from .optimizations import enabled


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


def reduce_induction_strength(function, allow_scaled=False, scaled_only=False):
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

        def resolve(value, representation=False):
            """Look through SSA copies and representation-preserving casts."""
            seen = set()
            while value not in seen:
                seen.add(value)
                instruction = definitions.get(value)
                if instruction is None or instruction.op not in ("copy", "cast"):
                    break
                source = instruction.args[0]
                source_definition = definitions.get(source)
                if instruction.op == "cast" and source_definition is not None:
                    same_type = source_definition.type == instruction.type
                    same_representation = (
                        representation
                        and enabled("representation_preserving_induction_casts")
                        and source_definition.type.integer
                        and instruction.type.integer
                        and source_definition.type.size == instruction.type.size
                    )
                    if not (same_type or same_representation):
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
                terminator = header.terminator()
                predicate = (
                    definitions.get(terminator.args[0])
                    if terminator is not None and terminator.op == "branch_if"
                    else terminator
                )
                constant_bounded = (
                    entry_value in constants
                    and predicate is not None
                    and predicate.op in ("binary", "cbranch_if")
                    and phi.dst in predicate.args
                    and any(
                        value != phi.dst and resolve(value) in constants
                        for value in predicate.args
                    )
                )
                evaluable_region = all(
                        item.op in (
                            "label", "phi", "const", "global_addr", "copy", "cast",
                            "unary", "binary", "load", "jump", "branch_if", "cbranch_if",
                        )
                        for label in loop.blocks
                        for item in cfg.by_label[label].instructions
                    )
                if constant_bounded and evaluable_region:
                    # Leave fully bounded constant loops in their scalar form;
                    # the bounded evaluator can remove the whole region.
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
                        scale = None
                        representation_cast = False
                        if right == phi.dst:
                            base = left
                        elif left == phi.dst:
                            base = right
                        else:
                            offset, base = (right, left)
                            offset_definition = definitions.get(offset)
                            if (
                                offset_definition is None
                                or offset_definition.op != "binary"
                                or offset_definition.extra not in ("*", "<<")
                            ):
                                continue
                            offset_left, offset_right = (
                                resolve(value) for value in offset_definition.args
                            )
                            if offset_left == phi.dst and offset_right in constants:
                                scale_operand = offset_right
                            elif (
                                offset_definition.extra == "*"
                                and offset_right == phi.dst
                                and offset_left in constants
                            ):
                                scale_operand = offset_left
                            else:
                                offset_left, offset_right = (
                                    resolve(value, representation=True)
                                    for value in offset_definition.args
                                )
                                if offset_left == phi.dst and offset_right in constants:
                                    scale_operand = offset_right
                                elif (
                                    offset_definition.extra == "*"
                                    and offset_right == phi.dst
                                    and offset_left in constants
                                ):
                                    scale_operand = offset_left
                                else:
                                    continue
                                representation_cast = True
                            scale = (offset_definition.extra, scale_operand,
                                     offset_definition.type)
                        if scale is not None and not allow_scaled:
                            continue
                        if scale is None and scaled_only:
                            continue
                        if scale is not None and (constant_bounded or representation_cast):
                            if constant_bounded and not enabled("loop_profitability"):
                                continue
                            if enabled("loop_profitability"):
                                trip_count = None
                                if constant_bounded:
                                    steps = {
                                        constants[constant]
                                        if operator == "+" else -constants[constant]
                                        for operator, constant in updates.values()
                                    }
                                    if len(steps) != 1:
                                        continue
                                    predicate_operator = (
                                        predicate.extra[0]
                                        if predicate.op == "cbranch_if"
                                        else predicate.extra
                                    )
                                    if predicate.args[1] == phi.dst:
                                        predicate_operator = {
                                            "<": ">", "<=": ">=", ">": "<",
                                            ">=": "<=", "==": "==", "!=": "!=",
                                        }.get(predicate_operator)
                                    target = cfg.label_blocks.get(
                                        terminator.extra[1], terminator.extra[1]
                                    )
                                    continue_operator = (
                                        predicate_operator if target in loop.blocks
                                        else {
                                            "==": "!=", "!=": "==", "<": ">=",
                                            "<=": ">", ">": "<=", ">=": "<",
                                        }.get(predicate_operator)
                                    )
                                    other = next(
                                        value for value in predicate.args
                                        if value != phi.dst
                                    )
                                    trip_count = exact_trip_count(
                                        constants[entry_value], next(iter(steps)),
                                        continue_operator, constants[resolve(other)],
                                        phi.type,
                                        simulation_limit=enabled(
                                            "loop_trip_count_analysis_limit"
                                        ),
                                    )
                                depth = sum(
                                    loop.blocks <= enclosing.blocks for enclosing in loops
                                )
                                cost = LoopTransformationCost(
                                    trip_count=trip_count,
                                    loop_depth=depth,
                                    setup=2,
                                    before_each=2,
                                    after_each=1,
                                    pressure=1,
                                    pressure_each=max(
                                        0,
                                        peak_live_values(function) + 1
                                        - enabled("loop_register_budget"),
                                    ),
                                )
                                if not profitable(
                                    cost,
                                    unknown_trip_count=enabled("loop_unknown_trip_count"),
                                    depth_weight=enabled("loop_depth_weight"),
                                ):
                                    continue
                        base_block = def_block.get(base)
                        if base_block is None or base_block in loop.blocks:
                            continue
                        if not dominators.dominates(base_block, preheader):
                            continue
                        rewrite = (loop, header, preheader, phi, entry_value, updates,
                                   instruction, base, scale)
                        break
                    if rewrite is not None:
                        break
                if rewrite is not None:
                    break
            if rewrite is not None:
                break

        if rewrite is None:
            return changed

        loop, header, preheader, phi, entry_value, updates, derived, base, scale = rewrite
        entry_address = function.values
        function.values += 1
        address_phi = function.values
        function.values += 1
        entry_offset = entry_value
        setup = []
        if scale is not None:
            scale_operator, scale_operand, scale_type = scale
            entry_offset = function.values
            function.values += 1
            setup.append(Instruction(
                "binary", entry_offset, (entry_value, scale_operand),
                scale_type, scale_operator,
            ))
        setup.append(
            Instruction("binary", entry_address, (base, entry_offset), derived.type, "+")
        )
        _insert_before_terminator(
            cfg.by_label[preheader],
            setup,
        )

        phi_operands = [(preheader, entry_address)]
        for predecessor, (operator, constant_value) in sorted(updates.items()):
            next_address = function.values
            function.values += 1
            step_value = constant_value
            step_setup = []
            if scale is not None:
                scale_operator, scale_operand, scale_type = scale
                step_value = function.values
                function.values += 1
                step_setup.append(Instruction(
                    "binary", step_value, (constant_value, scale_operand),
                    scale_type, scale_operator,
                ))
            _insert_before_terminator(
                cfg.by_label[predecessor],
                step_setup + [
                    Instruction(
                        "binary",
                        next_address,
                        (address_phi, step_value),
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


def reduce_scaled_induction_strength(function):
    """Add affine scaled derived recurrences without changing the narrow pass."""
    return reduce_induction_strength(function, allow_scaled=True, scaled_only=True)


def convert_pointer_limit_loops(function):
    """Compare a derived pointer recurrence with ``base + bound`` at loop exit."""
    cfg = build_cfg(function)
    if not cfg.blocks:
        return False
    dominators = build_dominator_tree(cfg)
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

    for loop in sorted(find_natural_loops(dominators), key=lambda item: len(item.blocks)):
        header = cfg.by_label[loop.header]
        outside = [value for value in header.predecessors if value not in loop.blocks]
        condition = header.terminator()
        if len(outside) != 1 or condition is None:
            continue
        predicate = condition
        if condition.op == "branch_if":
            predicate = definitions.get(condition.args[0])
        predicate_operator = (
            predicate.extra[0] if predicate is not None and predicate.op == "cbranch_if"
            else predicate.extra if predicate is not None else None
        )
        if (
            predicate is None
            or predicate.op not in ("binary", "cbranch_if")
            or predicate_operator not in ("<", "<=", ">", ">=", "==", "!=")
        ):
            continue
        preheader = outside[0]
        phis = [item for item in header.instructions if item.op == "phi"]
        for index_phi in phis:
            if index_phi.dst not in predicate.args:
                continue
            bound = predicate.args[1] if predicate.args[0] == index_phi.dst else predicate.args[0]
            if def_block.get(bound) in loop.blocks:
                continue
            index_operands = dict(index_phi.extra)
            entry_index = index_operands.get(preheader)
            if entry_index is None:
                continue
            for pointer_phi in phis:
                if pointer_phi is index_phi:
                    continue
                pointer_operands = dict(pointer_phi.extra)
                entry_pointer = definitions.get(pointer_operands.get(preheader))
                if entry_pointer is None or entry_pointer.op != "binary" or entry_pointer.extra != "+":
                    continue
                base, entry_offset = entry_pointer.args
                if definitions.get(base) is None or definitions[base].type.kind != "pointer":
                    base, entry_offset = entry_offset, base
                if definitions.get(base) is None or definitions[base].type.kind != "pointer":
                    continue
                scale = None
                if entry_offset != entry_index:
                    offset = definitions.get(entry_offset)
                    if offset is None or offset.op != "binary" or offset.extra not in ("*", "<<"):
                        continue
                    if offset.args[0] == entry_index:
                        scale_operand = offset.args[1]
                    elif offset.extra == "*" and offset.args[1] == entry_index:
                        scale_operand = offset.args[0]
                    else:
                        continue
                    scale = (offset.extra, scale_operand, offset.type)
                if scale is None:
                    # A byte-stride cursor already has the cheapest possible
                    # update; replacing its integer exit can add setup and
                    # register pressure without removing per-iteration work.
                    continue
                if def_block.get(base) in loop.blocks:
                    continue
                matched = True
                for latch, _ in loop.back_edges:
                    index_update = definitions.get(index_operands.get(latch))
                    pointer_update = definitions.get(pointer_operands.get(latch))
                    same_step = False
                    if index_update is not None and pointer_update is not None:
                        pointer_step = pointer_update.args[1]
                        if scale is None:
                            same_step = pointer_step == index_update.args[1]
                        else:
                            scaled = definitions.get(pointer_step)
                            same_step = bool(
                                scaled is not None
                                and scaled.op == "binary"
                                and scaled.extra == scale[0]
                                and scaled.args == (index_update.args[1], scale[1])
                            )
                    if not (
                        index_update is not None
                        and pointer_update is not None
                        and index_update.op == pointer_update.op == "binary"
                        and index_update.extra == pointer_update.extra
                        and index_update.extra in ("+", "-")
                        and same_step
                        and index_update.args[0] == index_phi.dst
                        and pointer_update.args[0] == pointer_phi.dst
                    ):
                        matched = False
                        break
                if not matched:
                    continue
                end_pointer = function.values
                function.values += 1
                bound_offset = bound
                setup = []
                if scale is not None:
                    bound_offset = function.values
                    function.values += 1
                    setup.append(Instruction(
                        "binary", bound_offset, (bound, scale[1]), scale[2], scale[0]
                    ))
                setup.append(Instruction(
                    "binary", end_pointer, (base, bound_offset), pointer_phi.type, "+"
                ))
                _insert_before_terminator(
                    cfg.by_label[preheader],
                    setup,
                )
                args = list(predicate.args)
                args[args.index(index_phi.dst)] = pointer_phi.dst
                args[args.index(bound)] = end_pointer
                predicate.args = tuple(args)
                predicate.type = pointer_phi.type
                return True
    return False
