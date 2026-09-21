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


def remove_unused_stack_initialization(module):
    """Drop startup stack setup when the final entry IR is stack-free."""
    entry = next((function for function in module.functions if function.name == "_start"), None)
    if entry is None:
        return False
    instructions = [item for block in entry.blocks for item in block.instructions]
    if not any(item.op == "init_stack" for item in instructions):
        return False
    stack_free = {"init_pic", "init_stack", "zero_bss", "relocate_globals", "const", "global_addr", "copy", "cast", "unary", "binary", "intrinsic", "halt", "label"}
    if any(item.op not in stack_free for item in instructions):
        return False
    if any(item.op == "binary" and item.extra in ("*", "/", "%") for item in instructions):
        return False
    entry.blocks = [BasicBlock(block.label, [item for item in block.instructions if item.op != "init_stack"]) for block in entry.blocks]
    return True


def fold_immutable_global_loads(module):
    """Fold integer loads from closed-world, provably unmodified globals."""
    globals_ = {global_.symbol.key: global_ for global_ in module.globals}
    unsafe = set()
    facts = []
    for function in module.functions:
        constants, addresses, origins = {}, {}, {}
        for block in function.blocks:
            for instruction in block.instructions:
                if instruction.op == "const":
                    constants[instruction.dst] = instruction.extra
                elif instruction.op == "global_addr" and instruction.extra in globals_:
                    addresses[instruction.dst] = (instruction.extra, 0)
                    origins[instruction.dst] = {instruction.extra}
                elif instruction.op in ("copy", "cast") and instruction.args:
                    source = instruction.args[0]
                    if source in constants:
                        constants[instruction.dst] = constants[source]
                    if source in addresses:
                        addresses[instruction.dst] = addresses[source]
                    if source in origins:
                        origins[instruction.dst] = origins[source]
                elif instruction.op == "binary":
                    left, right = instruction.args
                    inherited = origins.get(left, set()) | origins.get(right, set())
                    if inherited:
                        origins[instruction.dst] = inherited
                    if instruction.extra in ("+", "-") and left in addresses and right in constants:
                        symbol, offset = addresses[left]
                        addresses[instruction.dst] = (symbol, offset + (constants[right] if instruction.extra == "+" else -constants[right]))
                    elif instruction.extra == "+" and right in addresses and left in constants:
                        symbol, offset = addresses[right]
                        addresses[instruction.dst] = (symbol, offset + constants[left])
                argument_origins = set().union(*(origins.get(value, set()) for value in instruction.args))
                if argument_origins and instruction.op not in ("load", "copy", "cast", "binary"):
                    unsafe.update(argument_origins)
                if instruction.op == "store":
                    unsafe.update(argument_origins)
        facts.append((function, addresses))

    changed = False
    for function, addresses in facts:
        blocks = []
        for block in function.blocks:
            items = []
            for instruction in block.instructions:
                replacement = None
                if instruction.op == "load" and instruction.type.integer and instruction.args[0] in addresses:
                    symbol, offset = addresses[instruction.args[0]]
                    global_ = globals_[symbol]
                    if symbol not in unsafe and 0 <= offset and offset + instruction.type.size <= len(global_.data):
                        raw = int.from_bytes(global_.data[offset:offset + instruction.type.size], "big", signed=False)
                        if instruction.type.signed:
                            sign = 1 << (instruction.type.size * 8 - 1)
                            raw = (raw ^ sign) - sign
                        replacement = type(instruction)("const", instruction.dst, (), instruction.type, raw)
                elif instruction.op == "load" and instruction.type.kind == "pointer" and instruction.args[0] in addresses:
                    symbol, offset = addresses[instruction.args[0]]
                    global_ = globals_[symbol]
                    relocation = next(((target, addend) for at, target, addend in global_.relocations if at == offset), None)
                    if symbol not in unsafe and relocation is not None and relocation[1] == 0:
                        replacement = type(instruction)("global_addr", instruction.dst, (), instruction.type, relocation[0])
                items.append(replacement or instruction)
                changed |= replacement is not None
            blocks.append(BasicBlock(block.label, items))
        function.blocks = blocks
    return changed
