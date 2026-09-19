"""Promote load-only parameter stack slots to SSA entry values."""

from ..ir import BasicBlock, Instruction


def promote_readonly_parameters(function):
    """Replace loads of unmodified parameter slots with entry ``param`` values."""
    definitions = {
        instruction.dst: instruction
        for block in function.blocks
        for instruction in block.instructions
        if instruction.dst is not None
    }
    uses = {}
    for block in function.blocks:
        for instruction in block.instructions:
            for position, value in enumerate(instruction.args):
                uses.setdefault(value, []).append((instruction, position))

    replacements = {}
    params = []
    for index, parameter in enumerate(function.params, 1):
        addresses = [
            value for value, instruction in definitions.items()
            if instruction.op == "local_addr" and instruction.extra == parameter.key
        ]
        if not addresses or not all(
            all(user.op == "load" and position == 0 for user, position in uses.get(address, ()))
            for address in addresses
        ):
            continue
        value = function.values
        function.values += 1
        params.append(Instruction("param", value, (), parameter.type, (index, parameter.key)))
        for address in addresses:
            for load, _ in uses.get(address, ()):
                replacements[id(load)] = Instruction("copy", load.dst, (value,), load.type)

    if not params:
        return False
    entry = function.blocks[0]
    function.blocks = [
        BasicBlock(entry.label, params + entry.instructions),
        *[
            BasicBlock(block.label, [replacements.get(id(item), item) for item in block.instructions])
            for block in function.blocks[1:]
        ],
    ]
    # Replacements in a non-entry block were handled above; apply the same map
    # to the entry without moving its new dominating parameter definitions.
    function.blocks[0].instructions = params + [replacements.get(id(item), item) for item in entry.instructions]
    return True
