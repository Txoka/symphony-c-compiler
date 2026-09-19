"""Tail-call formation over persistent SSA blocks."""

from ..ir import BasicBlock, Instruction


def eliminate_tail_calls(function):
    """Replace an in-block call/return pair with a tail-call terminator.

    This deliberately handles only the canonical adjacent form.  SSA block
    structure makes that form unambiguous, avoids control-flow surgery, and
    covers all calls emitted by the current lowerer after CFG simplification.
    """
    local_keys = {symbol.key for symbol in function.locals}
    if any(
        instruction.op == "local_addr" and instruction.extra in local_keys
        for block in function.blocks
        for instruction in block.instructions
    ):
        return False
    changed = False
    blocks = []
    for block in function.blocks:
        items = block.instructions
        if len(items) >= 2:
            call, result = items[-2:]
            arguments = len(call.args) - (call.op == "call")
            if (
                call.op in ("call", "direct_call")
                and arguments <= 6
                and result.op == "return"
                and ((not result.args and call.type.kind == "void") or result.args == (call.dst,))
            ):
                tail = Instruction(
                    "tailcall" if call.op == "call" else "direct_tailcall",
                    args=call.args,
                    type=call.type,
                    extra=call.extra,
                )
                items = [*items[:-2], tail]
                changed = True
        blocks.append(BasicBlock(block.label, list(items)))
    function.blocks = blocks
    return changed
