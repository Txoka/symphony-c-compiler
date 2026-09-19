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


def lower_self_tail_calls_to_loops(function):
    """Turn direct self-tail calls into a loop with SSA header phis.

    Parameters have already been promoted into entry ``param`` values.  A
    recursive call supplies the next iteration's values, so each parameter
    becomes a phi at the original entry block: its preheader operand is the
    incoming ABI value and each tail-call block supplies one backedge operand.
    This is the SSA equivalent of the old pass's parallel reassignment.
    """
    tails = [
        (block, instruction)
        for block in function.blocks
        for instruction in block.instructions
        if instruction.op == "direct_tailcall" and instruction.extra == function.name
    ]
    if not tails:
        return False
    parameters = sorted(
        (
            instruction
            for block in function.blocks
            for instruction in block.instructions
            if instruction.op == "param"
        ),
        key=lambda instruction: instruction.extra[0],
    )
    if (
        len(parameters) != len(function.params)
        or any(len(instruction.args) != len(parameters) for _, instruction in tails)
    ):
        return False
    entry = function.blocks[0]
    if any(instruction not in entry.instructions for instruction in parameters):
        return False

    labels = {block.label for block in function.blocks}
    preheader_label = f"{function.name}.tail_entry"
    serial = 0
    while preheader_label in labels:
        serial += 1
        preheader_label = f"{function.name}.tail_entry{serial}"

    phi_values = {}
    phis = []
    for parameter in parameters:
        value = function.values
        function.values += 1
        phi_values[parameter.dst] = value
        operands = [(preheader_label, parameter.dst)]
        position = parameter.extra[0] - 1
        operands.extend((block.label, tail.args[position]) for block, tail in tails)
        phis.append(Instruction("phi", value, (), parameter.type, tuple(operands)))

    def rewrite_args(instruction):
        args = tuple(phi_values.get(value, value) for value in instruction.args)
        if args == instruction.args:
            return instruction
        return Instruction(instruction.op, instruction.dst, args, instruction.type, instruction.extra)

    preheader = BasicBlock(
        preheader_label,
        [*parameters, Instruction("jump", extra=entry.label)],
    )
    blocks = [preheader]
    for block in function.blocks:
        items = []
        for instruction in block.instructions:
            if instruction in parameters:
                continue
            if instruction.op == "direct_tailcall" and instruction.extra == function.name:
                items.append(Instruction("jump", extra=entry.label))
            else:
                items.append(rewrite_args(instruction))
        if block is entry:
            if not any(item.op == "label" and item.extra == entry.label for item in items):
                items.insert(0, Instruction("label", extra=entry.label))
            first_non_label = next(
                (index for index, item in enumerate(items) if item.op != "label"), len(items)
            )
            items[first_non_label:first_non_label] = phis
        blocks.append(BasicBlock(block.label, items))
    function.blocks = blocks
    return True
