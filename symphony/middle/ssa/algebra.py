"""Side-effect-free algebraic simplification over persistent SSA."""

from ..ir import BasicBlock, Instruction


def simplify_algebra(function):
    """Replace integer identities with copies or constants.

    SSA values denote already-evaluated expressions, so each rewrite is safe:
    unlike an AST-level simplifier, replacing ``x * 0`` (were it supported)
    could not accidentally remove evaluation of ``x``.  We deliberately keep
    this pass to identities which preserve the target's integer semantics.
    Constant-only expressions are SCCP's responsibility.
    """
    constants = {
        instruction.dst: instruction.extra
        for block in function.blocks
        for instruction in block.instructions
        if instruction.op == "const" and instruction.dst is not None
    }
    changed = False
    blocks = []
    for block in function.blocks:
        rewritten = []
        for instruction in block.instructions:
            replacement = None
            if instruction.op == "binary":
                left, right = instruction.args
                left_constant = constants.get(left)
                right_constant = constants.get(right)
                operator = instruction.extra

                if right_constant == 0 and operator in ("+", "-", "|", "^", "<<", ">>"):
                    replacement = Instruction("copy", instruction.dst, (left,), instruction.type)
                elif left_constant == 0 and operator in ("+", "|", "^"):
                    replacement = Instruction("copy", instruction.dst, (right,), instruction.type)
                elif (left_constant == 0 or right_constant == 0) and operator == "&":
                    replacement = Instruction("const", instruction.dst, (), instruction.type, 0)
                elif left == right and operator in ("-", "^"):
                    replacement = Instruction("const", instruction.dst, (), instruction.type, 0)
                elif left == right and operator in ("&", "|"):
                    replacement = Instruction("copy", instruction.dst, (left,), instruction.type)
                elif left == right and operator in ("==", "<=", ">="):
                    replacement = Instruction("const", instruction.dst, (), instruction.type, 1)
                elif left == right and operator in ("!=", "<", ">"):
                    replacement = Instruction("const", instruction.dst, (), instruction.type, 0)
                elif right_constant == 1 and operator == "/":
                    replacement = Instruction("copy", instruction.dst, (left,), instruction.type)
                elif right_constant == 1 and operator == "%":
                    replacement = Instruction("const", instruction.dst, (), instruction.type, 0)
                elif operator == "|" and right_constant is not None:
                    mask = (1 << (instruction.type.size * 8)) - 1
                    if right_constant & mask == mask:
                        replacement = Instruction(
                            "const", instruction.dst, (), instruction.type, right_constant & mask
                        )
                elif operator == "&" and right_constant is not None:
                    mask = (1 << (instruction.type.size * 8)) - 1
                    if right_constant & mask == mask:
                        replacement = Instruction("copy", instruction.dst, (left,), instruction.type)
            rewritten.append(replacement or instruction)
            changed |= replacement is not None
        blocks.append(BasicBlock(block.label, rewritten))
    function.blocks = blocks
    return changed
