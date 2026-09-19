"""Global copy propagation over immutable SSA values."""

from ..ir import BasicBlock, Instruction


def propagate_global_copies(function):
    """Resolve copy and representation-preserving cast chains everywhere.

    Every SSA definition dominates every use of its copy, so replacing a copy
    use with the copy's source remains dominance-correct even across blocks or
    on a phi edge.  Narrowing/sign-changing casts are deliberately retained;
    only a cast whose source has exactly the destination type is an alias.
    """
    definitions = {
        instruction.dst: instruction
        for block in function.blocks
        for instruction in block.instructions
        if instruction.dst is not None
    }
    aliases = {}
    for value, instruction in definitions.items():
        if instruction.op == "copy":
            aliases[value] = instruction.args[0]
        elif instruction.op == "cast":
            source = definitions.get(instruction.args[0])
            if source is not None and source.type == instruction.type:
                aliases[value] = instruction.args[0]

    def resolve(value):
        seen = set()
        while value in aliases and value not in seen:
            seen.add(value)
            value = aliases[value]
        return value

    changed = False
    new_blocks = []
    for block in function.blocks:
        rewritten = []
        for instruction in block.instructions:
            if instruction.op == "phi":
                extra = tuple(
                    (predecessor, resolve(value) if value is not None else None)
                    for predecessor, value in instruction.extra
                )
                args = instruction.args
            else:
                args = tuple(resolve(value) for value in instruction.args)
                extra = instruction.extra
            if args != instruction.args or extra != instruction.extra:
                instruction = Instruction(
                    instruction.op, instruction.dst, args, instruction.type, extra
                )
                changed = True
            rewritten.append(instruction)
        new_blocks.append(BasicBlock(block.label, rewritten))
    function.blocks = new_blocks
    return changed
