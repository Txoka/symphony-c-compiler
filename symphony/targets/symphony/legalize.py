"""Symphony-family legalization from semantic IR to runtime calls."""

from ...middle.ir import Instruction


def legalize_runtime_arithmetic(module):
    """Turn surviving software arithmetic into explicit runtime call edges."""
    for function in module.functions:
        for block in function.blocks:
            rewritten = []
            for instruction in block.instructions:
                if instruction.op == "binary" and instruction.extra in ("*", "/", "%"):
                    if instruction.extra == "*":
                        helper = "__dyn_mul"
                    else:
                        helper = (
                            "__dyn_"
                            + ("s" if instruction.type.signed else "u")
                            + ("div" if instruction.extra == "/" else "mod")
                        )
                    instruction = Instruction(
                        "direct_call",
                        instruction.dst,
                        instruction.args,
                        instruction.type,
                        helper,
                    )
                rewritten.append(instruction)
            block.instructions = rewritten
    return module


__all__ = ["legalize_runtime_arithmetic"]
