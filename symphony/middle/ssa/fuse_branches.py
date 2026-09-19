"""Fuse single-use comparison values into conditional branches."""

from ..ir import BasicBlock, Instruction


_COMPARISONS = {"==", "!=", "<", "<=", ">", ">="}
_INVERT = {"==": "!=", "!=": "==", "<": ">=", "<=": ">", ">": "<=", ">=": "<"}


def fuse_comparison_branches(function):
    """Turn a sole-use compare plus ``branch_if`` into ``cbranch_if``.

    The comparison result is an SSA value, so its use count is exact even
    across blocks.  We only remove it when its *only* use is the branch; a
    phi-edge use counts as a genuine use and prevents fusion.
    """
    definitions = {}
    uses = {}
    for block in function.blocks:
        for instruction in block.instructions:
            if instruction.dst is not None:
                definitions[instruction.dst] = instruction
            operands = (
                (value for _, value in instruction.extra if value is not None)
                if instruction.op == "phi"
                else instruction.args
            )
            for value in operands:
                uses.setdefault(value, []).append(instruction)

    fused_values = set()
    blocks = []
    for block in function.blocks:
        rewritten = []
        for instruction in block.instructions:
            if instruction.op == "branch_if":
                value = instruction.args[0]
                comparison = definitions.get(value)
                if (
                    comparison is not None
                    and comparison.op == "binary"
                    and comparison.extra in _COMPARISONS
                    and uses.get(value) == [instruction]
                ):
                    operator = comparison.extra
                    if not instruction.extra[0]:
                        operator = _INVERT[operator]
                    fused_values.add(value)
                    instruction = Instruction(
                        "cbranch_if",
                        args=comparison.args,
                        type=comparison.type,
                        extra=(operator, instruction.extra[1]),
                    )
            rewritten.append(instruction)
        blocks.append(BasicBlock(block.label, rewritten))

    if not fused_values:
        return False
    function.blocks = [
        BasicBlock(
            block.label,
            [
                instruction
                for instruction in block.instructions
                if instruction.dst not in fused_values
            ],
        )
        for block in blocks
    ]
    return True
