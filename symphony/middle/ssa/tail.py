"""Tail-call formation over persistent SSA blocks."""

from ..ir import BasicBlock, Instruction


def lower_self_reductions_to_loops(function):
    """Turn a simple associative self reduction into an accumulator loop.

    The accepted form has one read-only parameter, one recursive call, a base
    return equal to the operator's identity, and
    ``pure_parameter_expression OP recurse(next)`` returned directly.  The
    supported integer operators are associative for every defined C execution;
    signed-overflowing executions are already undefined.
    """
    identities = {"+": 0, "*": 1, "|": 0, "^": 0, "&": -1}
    self_calls = [
        (block, index, item)
        for block in function.blocks
        for index, item in enumerate(block.instructions)
        if item.op == "direct_call" and item.extra == function.name
    ]
    if len(function.params) != 1 or len(self_calls) != 1:
        return False
    recursive_block, call_index, call = self_calls[0]
    items = recursive_block.instructions
    if (
        len(call.args) != 1
        or call_index + 2 >= len(items)
        or items[call_index + 1].op != "binary"
        or items[call_index + 1].extra not in identities
        or items[call_index + 2].op != "return"
        or items[call_index + 2].args != (items[call_index + 1].dst,)
        or call.dst not in items[call_index + 1].args
    ):
        return False
    combine = items[call_index + 1]
    recursive_return = items[call_index + 2]
    if not combine.type.integer:
        return False
    definitions = {
        item.dst: item
        for block in function.blocks
        for item in block.instructions
        if item.dst is not None
    }
    original_other = (
        combine.args[1] if combine.args[0] == call.dst else combine.args[0]
    )

    def parameter_expression(value, seen=None):
        seen = set() if seen is None else seen
        if value in seen:
            return False, False
        seen.add(value)
        definition = definitions.get(value)
        if definition is None:
            return False, False
        if definition.op == "param":
            return True, definition.extra[1] == function.params[0].key
        if definition.op == "load":
            address = definitions.get(definition.args[0])
            valid = (
                address is not None
                and address.op == "local_addr"
                and address.extra == function.params[0].key
            )
            return valid, valid
        if definition.op == "const":
            return True, False
        if definition.op not in ("copy", "cast", "unary", "binary"):
            return False, False
        operands = [parameter_expression(arg, set(seen)) for arg in definition.args]
        return all(valid for valid, _ in operands), any(
            dependent for _, dependent in operands
        )

    returns = [
        item
        for block in function.blocks
        for item in block.instructions
        if item.op == "return"
    ]
    original_base_returns = [item for item in returns if item is not recursive_return]
    if (
        not all(parameter_expression(original_other))
        or len(original_base_returns) != 1
        or len(original_base_returns[0].args) != 1
    ):
        return False
    original_base = definitions.get(original_base_returns[0].args[0])
    mask = (1 << (combine.type.size * 8)) - 1
    identity_value = identities[combine.extra] & mask
    if (
        original_base is None
        or original_base.op != "const"
        or original_base.extra & mask != identity_value
    ):
        return False

    # Manufacture the ordinary ABI-entry SSA value only after the structural
    # shape has matched, so rejected functions are left untouched.
    from .copies import propagate_global_copies
    from .parameters import promote_readonly_parameters

    parameters = [
        item
        for block in function.blocks
        for item in block.instructions
        if item.op == "param"
    ]
    if not parameters:
        if not promote_readonly_parameters(function):
            return False
        propagate_global_copies(function)
        parameters = [
            item
            for block in function.blocks
            for item in block.instructions
            if item.op == "param"
        ]
    if len(parameters) != 1:
        return False
    parameter = parameters[0]

    # Copy propagation may have rebuilt the blocks, so find the matched
    # recursive tail again before checking its exact operands.
    recursive_block = next(
        block
        for block in function.blocks
        if any(item.op == "direct_call" and item.extra == function.name
               for item in block.instructions)
    )
    items = recursive_block.instructions
    call_index = next(
        index
        for index, item in enumerate(items)
        if item.op == "direct_call" and item.extra == function.name
    )
    call, combine, recursive_return = items[call_index:call_index + 3]
    other = combine.args[1] if combine.args[0] == call.dst else combine.args[0]

    definitions = {
        item.dst: item
        for block in function.blocks
        for item in block.instructions
        if item.dst is not None
    }
    returns = [
        item
        for block in function.blocks
        for item in block.instructions
        if item.op == "return"
    ]
    base_returns = [item for item in returns if item is not recursive_return]
    if len(base_returns) != 1 or len(base_returns[0].args) != 1:
        return False
    base_value = definitions.get(base_returns[0].args[0])
    if (
        base_value is None
        or base_value.op != "const"
        or base_value.extra & mask != identity_value
    ):
        return False

    entry = function.blocks[0]
    labels = {block.label for block in function.blocks}
    preheader_label = f"{function.name}.reduction_entry"
    serial = 0
    while preheader_label in labels:
        serial += 1
        preheader_label = f"{function.name}.reduction_entry{serial}"

    identity = function.values
    current = function.values + 1
    accumulator = function.values + 2
    accumulated = function.values + 3
    function.values += 4
    next_value = current if call.args[0] == parameter.dst else call.args[0]
    reduction_value = current if other == parameter.dst else other

    def rewrite(item):
        args = tuple(current if value == parameter.dst else value for value in item.args)
        extra = item.extra
        if item.op == "phi":
            extra = tuple(
                (predecessor, current if value == parameter.dst else value)
                for predecessor, value in item.extra
            )
        return Instruction(item.op, item.dst, args, item.type, extra)

    preheader = BasicBlock(
        preheader_label,
        [
            parameter,
            Instruction("const", identity, (), combine.type, base_value.extra),
            Instruction("jump", extra=entry.label),
        ],
    )
    rewritten_blocks = []
    for block in function.blocks:
        rewritten = []
        index = 0
        while index < len(block.instructions):
            item = block.instructions[index]
            if item is parameter:
                index += 1
                continue
            if block.label == recursive_block.label and index == call_index:
                rewritten.extend((
                    Instruction(
                        "binary",
                        accumulated,
                        (accumulator, reduction_value),
                        combine.type,
                        combine.extra,
                    ),
                    Instruction("jump", extra=entry.label),
                ))
                index += 3
                continue
            if item is base_returns[0]:
                rewritten.append(
                    Instruction("return", args=(accumulator,), type=item.type)
                )
            else:
                rewritten.append(rewrite(item))
            index += 1
        if block is entry:
            if not any(
                item.op == "label" and item.extra == entry.label
                for item in rewritten
            ):
                rewritten.insert(0, Instruction("label", extra=entry.label))
            first = next(
                (i for i, item in enumerate(rewritten) if item.op != "label"),
                len(rewritten),
            )
            rewritten[first:first] = [
                Instruction(
                    "phi", current, (), parameter.type,
                    ((preheader_label, parameter.dst), (recursive_block.label, next_value)),
                ),
                Instruction(
                    "phi", accumulator, (), combine.type,
                    ((preheader_label, identity), (recursive_block.label, accumulated)),
                ),
            ]
        rewritten_blocks.append(BasicBlock(block.label, rewritten))
    function.blocks = [preheader, *rewritten_blocks]
    return True


def eliminate_tail_calls(function, self_only=False):
    """Replace an in-block call/return pair with a tail-call terminator.

    This deliberately handles only the canonical adjacent form.  SSA block
    structure makes that form unambiguous, avoids control-flow surgery, and
    covers all calls emitted by the current lowerer after CFG simplification.
    """
    # A tail call may not retain an address into the dismantled frame.  The
    # old mutable pass rejected the entire function when it contained a local
    # address, which also rejected perfectly safe calls in functions that use
    # a temporary earlier in another branch (notably Hanoi).  Trace just the
    # prospective call arguments through transparent copies instead.
    definitions = {
        item.dst: item
        for block in function.blocks
        for item in block.instructions
        if item.dst is not None
    }

    def is_local_address(value):
        seen = set()
        while value not in seen:
            seen.add(value)
            definition = definitions.get(value)
            if definition is None:
                return False
            if definition.op == "local_addr":
                return True
            if definition.op not in ("copy", "cast") or not definition.args:
                return False
            value = definition.args[0]
        return False
    changed = False
    by_label = {block.label: block for block in function.blocks}

    def trailing_return(items):
        if len(items) >= 2 and items[-1].op == "return":
            return items[-2], items[-1], 2
        if len(items) >= 2 and items[-1].op == "jump":
            target = by_label.get(items[-1].extra)
            body = [] if target is None else [item for item in target.instructions if item.op != "label"]
            if len(body) == 1 and body[0].op == "return":
                return items[-2], body[0], 2
        return None

    blocks = []
    for block in function.blocks:
        items = block.instructions
        tail_pair = trailing_return(items)
        if tail_pair is not None:
            call, result, width = tail_pair
            arguments = len(call.args) - (call.op == "call")
            if (
                call.op in ("call", "direct_call")
                and arguments <= 6
                and (not self_only or call.op == "direct_call" and call.extra == function.name)
                and not any(is_local_address(value) for value in call.args[-arguments:])
                and result.op == "return"
                and ((not result.args and call.type.kind == "void") or result.args == (call.dst,))
            ):
                tail = Instruction(
                    "tailcall" if call.op == "call" else "direct_tailcall",
                    args=call.args,
                    type=call.type,
                    extra=call.extra,
                )
                items = [*items[:-width], tail]
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
