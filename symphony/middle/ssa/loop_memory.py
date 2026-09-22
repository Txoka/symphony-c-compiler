"""Conservative redundant-memory elimination within straight-line blocks.

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
    "stack_alloc", "zero_bss", "zero",
}


def eliminate_redundant_loop_memory(function):
    """Forward exact-address loads and remove overwritten stores in loops."""
    cfg = build_cfg(function)
    loops = find_natural_loops(build_dominator_tree(cfg))
    labels = {label for loop in loops for label in loop.blocks}
    return _eliminate_redundant_memory(function, cfg, labels)


def eliminate_redundant_straight_line_memory(function):
    """Forward exact-address memory facts in blocks outside natural loops.

    Loop blocks remain owned by ``eliminate_redundant_loop_memory`` so the two
    scopes can be benchmarked and disabled independently.
    """
    cfg = build_cfg(function)
    loops = find_natural_loops(build_dominator_tree(cfg))
    loop_labels = {label for loop in loops for label in loop.blocks}
    labels = {block.label for block in cfg.blocks if block.label not in loop_labels}
    return _eliminate_redundant_memory(function, cfg, labels)


def _eliminate_redundant_memory(function, cfg, labels):
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

    def type_key(type_):
        return (
            type_.kind,
            type_.size,
            type_.signed,
            id(type_.base),
            id(type_.record),
            type_.qualifiers,
        )

    changed = False
    for label in sorted(labels):
        block = cfg.by_label[label]
        values = {}       # exact address -> most recently known value
        stores = {}       # exact address -> removable store instruction
        observed = set()  # base address read from memory since that store
        replacement = {}
        dead_stores = set()
        for instruction in block.instructions:
            if instruction.op in MEMORY_BARRIERS:
                values.clear()
                stores.clear()
                observed.clear()
                continue
            if instruction.op == "store":
                address = address_key(instruction.args[0])
                if address is None:
                    values.clear()
                    stores.clear()
                    observed.clear()
                    continue
                key = address, type_key(instruction.type)
                # Equal base addresses accessed through a different type may
                # overlap only partially (notably unions).  Forget those
                # facts rather than manufacturing an invalid typed copy or
                # treating a narrow store as a complete overwrite.
                for known in tuple(values):
                    if known[0] == address and known != key:
                        values.pop(known)
                for known in tuple(stores):
                    if known[0] == address and known != key:
                        stores.pop(known)
                previous = stores.get(key)
                if previous is not None and address not in observed:
                    dead_stores.add(id(previous))
                values[key] = instruction.args[1]
                stores[key] = instruction
                observed.discard(address)
                continue
            if instruction.op != "load":
                continue
            address = address_key(instruction.args[0])
            if address is None:
                continue
            key = address, type_key(instruction.type)
            if key in values:
                replacement[id(instruction)] = values[key]
            else:
                values[key] = instruction.dst
                observed.add(address)

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
        changed = True
    return changed


__all__ = [
    "eliminate_redundant_loop_memory",
    "eliminate_redundant_straight_line_memory",
]
