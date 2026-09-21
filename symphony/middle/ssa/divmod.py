"""Combine a matching unsigned remainder and quotient calculation.

The Symphony ISA has no divide instruction.  Both ``/`` and ``%`` therefore
call the same software routine, but calling it twice repeats the entire
shift/subtract algorithm.  When a remainder is immediately stored and the
same operands are subsequently divided, pass that store as an out-parameter
and perform the operation once.
"""

from ..ir import Instruction
from ..model import Symbol, UINT, pointer


def pair_unsigned_divmod(function):
    """Replace ``r = a % b; q = a / b`` with one runtime call when safe.

    This intentionally only crosses side-effect-free instructions.  It uses a
    compiler-created stack word for the remainder, so the paired operation can
    preserve arbitrary pure uses of the modulo result without alias analysis.
    """
    changed = False
    for block in function.blocks:
        uses = {}
        constants = {}
        for instruction in block.instructions:
            if instruction.op == "const":
                constants[instruction.dst] = instruction.extra
            for position, value in enumerate(instruction.args):
                if isinstance(value, int):
                    uses.setdefault(value, []).append((instruction, position))
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
            modulo_uses = uses.get(modulo.dst, ())
            if not modulo_uses or any(user not in instructions[index + 1:] for user, _ in modulo_uses):
                continue
            for later in instructions[index + 1:]:
                if later.op in ("store", "call", "direct_call"):
                    break
                if (
                    later.op == "binary"
                    and later.extra == "/"
                    and not later.type.signed
                    and all(
                        left == right
                        or (
                            left in constants
                            and right in constants
                            and constants[left] == constants[right]
                        )
                        for left, right in zip(later.args, modulo.args)
                    )
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
