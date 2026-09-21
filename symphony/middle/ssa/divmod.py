"""Combine a matching unsigned remainder and quotient calculation.

The Symphony ISA has no divide instruction.  Both ``/`` and ``%`` therefore
call the same software routine, but calling it twice repeats the entire
shift/subtract algorithm.  When a remainder is immediately stored and the
same operands are subsequently divided, pass that store as an out-parameter
and perform the operation once.
"""

from ..ir import Instruction
from ..model import Symbol, UINT, pointer
from .optimizations import enabled


def pair_unsigned_divmod(function):
    """Replace ``r = a % b; q = a / b`` with one runtime call when safe.

    This intentionally only crosses side-effect-free instructions.  It uses a
    compiler-created stack word for the remainder, so the paired operation can
    preserve arbitrary pure uses of the modulo result without alias analysis.
    """
    changed = False
    all_definitions = {
        instruction.dst: instruction
        for block in function.blocks
        for instruction in block.instructions
        if instruction.dst is not None
    }
    all_constants = {
        instruction.dst: instruction.extra
        for block in function.blocks
        for instruction in block.instructions
        if instruction.op == "const"
    }
    all_uses = {}
    for candidate_block in function.blocks:
        for instruction in candidate_block.instructions:
            for position, value in enumerate(instruction.args):
                if isinstance(value, int):
                    all_uses.setdefault(value, []).append((instruction, position))
            if instruction.op == "phi":
                for predecessor, value in instruction.extra:
                    if isinstance(value, int):
                        all_uses.setdefault(value, []).append((instruction, predecessor))
    for block in function.blocks:
        constants = all_constants
        definitions = all_definitions
        instructions = block.instructions
        removed = set()
        inserted = {}
        for index, modulo in enumerate(instructions):
            if (
                modulo.op != "binary"
                or modulo.extra != "%"
                or modulo.type.signed
            ):
                continue
            modulo_uses = all_uses.get(modulo.dst, ())
            following = {id(item) for item in instructions[index + 1:]}
            if not modulo_uses or any(id(user) not in following for user, _ in modulo_uses):
                continue
            operand_locals = {
                local
                for value in modulo.args
                if (local := _loaded_local(value, definitions)) is not None
            }
            for later in instructions[index + 1:]:
                if later.op in ("call", "direct_call", "intrinsic"):
                    break
                if later.op == "store" and (
                    not enabled("alias_aware_divmod_pairing")
                    or _addressed_local(later.args[0], definitions) is None
                    or _addressed_local(later.args[0], definitions) in operand_locals
                ):
                    break
                if (
                    later.op == "binary"
                    and later.extra == "/"
                    and not later.type.signed
                    and _same_operands(later.args, modulo.args, constants, definitions)
                ):
                    slot_key = f"{function.name}.__divmod_remainder.{function.values}"
                    function.locals.append(Symbol("__divmod_remainder", UINT, "local", slot_key))
                    address = function.values
                    function.values += 1
                    inserted[id(modulo)] = [
                        Instruction("local_addr", address, (), pointer(UINT), slot_key),
                        Instruction(
                        "direct_call",
                        later.dst,
                        (*modulo.args, address),
                        later.type,
                        "__dyn_udivmod_pair",
                        ),
                        Instruction("load", modulo.dst, (address,), modulo.type),
                    ]
                    removed.add(id(later))
                    changed = True
                    break
        if changed:
            rewritten = []
            for instruction in instructions:
                if id(instruction) in removed:
                    continue
                if id(instruction) in inserted:
                    rewritten.extend(inserted[id(instruction)])
                else:
                    rewritten.append(instruction)
            block.instructions = rewritten
    return changed


__all__ = ["pair_unsigned_divmod"]


def _same_operands(left, right, constants, definitions):
    return all(
        a == b
        or (a in constants and b in constants and constants[a] == constants[b])
        or (
            _loaded_local(a, definitions) is not None
            and _loaded_local(a, definitions) == _loaded_local(b, definitions)
        )
        for a, b in zip(left, right)
    )


def _loaded_local(value, definitions):
    load = definitions.get(value)
    if load is None or load.op != "load":
        return None
    return _addressed_local(load.args[0], definitions)


def _addressed_local(value, definitions):
    instruction = definitions.get(value)
    if instruction is None:
        return None
    if instruction.op == "local_addr":
        return instruction.extra
    if instruction.op == "cast":
        return _addressed_local(instruction.args[0], definitions)
    if instruction.op == "binary" and instruction.extra == "+":
        left = _addressed_local(instruction.args[0], definitions)
        right = _addressed_local(instruction.args[1], definitions)
        if left is not None and right is None:
            return left
        if right is not None and left is None:
            return right
    return None
