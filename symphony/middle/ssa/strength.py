"""Power-of-two arithmetic reduction over persistent SSA."""

from ..ir import BasicBlock, Instruction


def reduce_strength(function):
    """Replace exact power-of-two arithmetic with shifts and masks.

    Multiplication by a positive power of two has the same target-width
    result as a left shift.  Division and remainder reductions are limited to
    unsigned values, where truncation and bit operations agree exactly.
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
            if instruction.op != "binary":
                rewritten.append(instruction)
                continue
            left, right = instruction.args
            left_constant = constants.get(left)
            right_constant = constants.get(right)
            operator = instruction.extra
            if operator == "*" and left_constant is not None and right_constant is None:
                left, right = right, left
                right_constant = left_constant
            replacement = None
            operand = None
            if operator == "*" and isinstance(right_constant, int):
                if right_constant == 0:
                    replacement = Instruction("const", instruction.dst, (), instruction.type, 0)
                elif right_constant == 1:
                    replacement = Instruction("copy", instruction.dst, (left,), instruction.type)
                elif right_constant > 0 and right_constant & (right_constant - 1) == 0:
                    operand = right_constant.bit_length() - 1
                    operator = "<<"
            elif (
                operator in ("/", "%")
                and not instruction.type.signed
                and isinstance(right_constant, int)
                and right_constant > 0
                and right_constant & (right_constant - 1) == 0
            ):
                operand = right_constant.bit_length() - 1 if operator == "/" else right_constant - 1
                operator = ">>" if instruction.extra == "/" else "&"
            if replacement is not None:
                rewritten.append(replacement)
                constants[instruction.dst] = replacement.extra
                changed = True
            elif operand is not None:
                value = function.values
                function.values += 1
                constant = Instruction("const", value, (), instruction.type, operand)
                rewritten.extend((constant, Instruction("binary", instruction.dst, (left, value), instruction.type, operator)))
                constants[value] = operand
                changed = True
            else:
                rewritten.append(instruction)
        blocks.append(BasicBlock(block.label, rewritten))
    function.blocks = blocks
    return changed
