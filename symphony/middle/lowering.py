"""Mandatory target-independent lowering before SSA construction."""

from .ir import Instruction
from ..runtime.intrinsics import NAMES as INTRINSIC_NAMES


def lower_intrinsics(function):
    """Replace direct calls to built-in device functions with target IR ops."""
    rewritten = []
    for instruction in function.instructions:
        if instruction.op == "direct_call" and instruction.extra in INTRINSIC_NAMES:
            instruction = Instruction(
                "intrinsic",
                instruction.dst,
                instruction.args,
                instruction.type,
                instruction.extra,
            )
        rewritten.append(instruction)
    function.instructions = rewritten
