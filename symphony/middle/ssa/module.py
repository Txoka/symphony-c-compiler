"""Whole-program reachability cleanup without leaving persistent SSA."""

from ..ir import BasicBlock


def remove_unreachable_symbols(module):
    """Retain only functions and globals reachable from ``_start``."""
    before_functions = tuple(function.name for function in module.functions)
    before_globals = tuple(global_.symbol.key for global_ in module.globals)
    functions = {function.name: function for function in module.functions}
    globals_ = {global_.symbol.key: global_ for global_ in module.globals}
    pending_functions, pending_globals = ["_start"], []
    reachable_functions, reachable_globals = set(), set()
    while pending_functions or pending_globals:
        while pending_functions:
            name = pending_functions.pop()
            if name in reachable_functions or name not in functions:
                continue
            reachable_functions.add(name)
            for block in functions[name].blocks:
                for instruction in block.instructions:
                    if instruction.op == "stack_alloc":
                        pending_globals.extend(g.symbol.key for g in module.globals if g.symbol.name == "__dyn_heap_anchor")
                    if instruction.op in ("direct_call", "direct_tailcall"):
                        pending_functions.append(instruction.extra)
                    elif instruction.op == "global_addr":
                        (pending_functions if instruction.extra in functions else pending_globals).append(instruction.extra)
                    if instruction.op == "binary" and instruction.extra in ("*", "/", "%"):
                        pending_functions.append("__dyn_mul" if instruction.extra == "*" else "__dyn_" + ("s" if instruction.type.signed else "u") + ("div" if instruction.extra == "/" else "mod"))
        while pending_globals:
            name = pending_globals.pop()
            if name in reachable_globals or name not in globals_:
                continue
            reachable_globals.add(name)
            for _, target, _ in globals_[name].relocations:
                (pending_functions if target in functions else pending_globals).append(target)
    module.functions = [function for function in module.functions if function.name in reachable_functions]
    module.globals = [global_ for global_ in module.globals if global_.symbol.key in reachable_globals]
    if not any(global_.relocations for global_ in module.globals):
        entry = next((function for function in module.functions if function.name == "_start"), None)
        if entry is not None:
            entry.blocks = [BasicBlock(block.label, [item for item in block.instructions if item.op != "relocate_globals"]) for block in entry.blocks]
    return tuple(function.name for function in module.functions) != before_functions or tuple(global_.symbol.key for global_ in module.globals) != before_globals


remove_unreachable_functions = remove_unreachable_symbols
