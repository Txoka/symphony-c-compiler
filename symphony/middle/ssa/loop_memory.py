"""Conservative redundant-memory elimination inside natural-loop blocks.

SSA makes values immutable, but ordinary loads and stores still describe
mutable memory.  Until the compiler grows MemorySSA or alias analysis, this
pass only forwards facts through one straight-line block and only for addresses
whose identity is proven from their SSA definition (a local/global address or
a copy thereof).  An unproven store or call is a full memory barrier.
"""

from ..analysis.cfg import build_cfg
from ..analysis.dominance import build_dominator_tree, find_natural_loops
from ..ir import Instruction


MEMORY_BARRIERS = {
    "call", "direct_call", "tailcall", "direct_tailcall", "intrinsic",
    "stack_alloc", "init_text_screen", "clear_text_framebuffer", "zero",
}


def eliminate_redundant_loop_memory(function):
    """Forward exact-address loads and remove overwritten stores in loops."""
    cfg = build_cfg(function)
    definitions = {
        instruction.dst: instruction
        for block in cfg.blocks
        for instruction in block.instructions
        if instruction.dst is not None
    }

    def address_key(value, seen=None):
        seen = set() if seen is None else seen
        if value in seen:
            return None
        seen.add(value)
        instruction = definitions.get(value)
        if instruction is None:
            return None
        if instruction.op in ("local_addr", "global_addr"):
            return instruction.op, instruction.extra
        if instruction.op == "copy":
            return address_key(instruction.args[0], seen)
        return None

    loops = find_natural_loops(build_dominator_tree(cfg))
    changed = False
    rewritten = set()
    for loop in sorted(loops, key=lambda item: len(item.blocks)):
        for label in sorted(loop.blocks):
            block = cfg.by_label[label]
            if id(block) in rewritten:
                continue
            values = {}       # exact address -> most recently known value
            stores = {}       # exact address -> removable store instruction
            observed = set()  # address read from memory since that store
            replacement = {}
            dead_stores = set()
            for instruction in block.instructions:
                if instruction.op in MEMORY_BARRIERS:
                    values.clear()
                    stores.clear()
                    observed.clear()
                    continue
                if instruction.op == "store":
                    key = address_key(instruction.args[0])
                    if key is None:
                        values.clear()
                        stores.clear()
                        observed.clear()
                        continue
                    previous = stores.get(key)
                    if previous is not None and key not in observed:
                        dead_stores.add(id(previous))
                    values[key] = instruction.args[1]
                    stores[key] = instruction
                    observed.discard(key)
                    continue
                if instruction.op != "load":
                    continue
                key = address_key(instruction.args[0])
                if key is None:
                    continue
                if key in values:
                    replacement[id(instruction)] = values[key]
                else:
                    values[key] = instruction.dst
                    observed.add(key)

            if not replacement and not dead_stores:
                continue
            block.instructions = [
                (
                    Instruction("copy", instruction.dst, (replacement[id(instruction)],), instruction.type)
                    if id(instruction) in replacement
                    else instruction
                )
                for instruction in block.instructions
                if id(instruction) not in dead_stores
            ]
            rewritten.add(id(block))
            changed = True
    return changed
