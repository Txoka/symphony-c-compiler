"""Direct-call recovery from immutable SSA function-address values."""

from ..ir import BasicBlock, Instruction


def identify_direct_calls(function):
    """Turn a call through a known ``global_addr`` into ``direct_call``.

    Immutable SSA definitions make this valid across blocks, unlike the old
    mutable-IR version which could only trust a local scan.
    """
    definitions = {
        item.dst: item
        for block in function.blocks
        for item in block.instructions
        if item.dst is not None
    }
    changed = False
    blocks = []
    for block in function.blocks:
        items = []
        for item in block.instructions:
            if item.op == "call":
                target = definitions.get(item.args[0])
                if target is not None and target.op == "global_addr":
                    item = Instruction(
                        "direct_call", item.dst, item.args[1:], item.type, target.extra
                    )
                    changed = True
            items.append(item)
        blocks.append(BasicBlock(block.label, items))
    function.blocks = blocks
    return changed
