"""Inline non-recursive, single-call-site functions directly on persistent SSA.

A true SSA-preserving clone: the callee's blocks (each already valid SSA) are
copied into the caller with every value id and block label remapped through
fresh maps, so the clone is itself valid SSA without any destruct/construct
round trip. A callee parameter never gets an SSA value of its own here (see
``construct.py``: parameters are deliberately excluded from mem2reg
promotion, so a callee's body already reads them via ``local_addr``+``load``
against its own memory-form parameter locals) -- inlining preserves that
shape exactly: the caller gets a fresh copy of the callee's parameter locals,
each initialized with a ``store`` of the matching call argument, and the rest
of the clone is spliced in unchanged. This sidesteps ever needing to
re-promote anything after the splice.

Each cloned ``return`` becomes a ``jump`` to a fresh continuation block, and
a ``copy`` of the returned value into the original call's ``dst`` (when
used). Since a function can return from several blocks, the continuation
needs a phi merging each return's value -- keyed by the real, already-stable
label of whichever cloned return block jumps to it, exactly like any other
phi (see ``construct.py``'s docstring on why phi operands are label-keyed).
A void callee, or a call whose result is unused, needs no such phi.
"""

from ..ir import BasicBlock, Instruction
from ..analysis.cfg import prune_unreachable_blocks
from ..model import pointer


def _observable_addresses(module):
    """Function names whose address is taken anywhere (as a value, not just
    the callee of a direct call) -- these can be called indirectly, so their
    body must stay a real, standalone function."""
    names = {function.name for function in module.functions}
    observable = {
        symbol
        for global_ in module.globals
        for _, symbol, _ in global_.relocations
        if symbol in names
    }
    for function in module.functions:
        for block in function.blocks:
            for instruction in block.instructions:
                if instruction.op == "global_addr" and instruction.extra in names:
                    observable.add(instruction.extra)
    return observable


def _call_sites(module):
    """name -> [(caller, block_label, call_instruction), ...] for every
    direct_call in the module."""
    names = {function.name for function in module.functions}
    sites = {name: [] for name in names}
    edges = {name: set() for name in names}
    for caller in module.functions:
        for block in caller.blocks:
            for instruction in block.instructions:
                if instruction.op == "direct_call" and instruction.extra in names:
                    sites[instruction.extra].append((caller, block.label, instruction))
                    edges[caller.name].add(instruction.extra)
    return sites, edges


def _reaches(edges, start, target):
    pending = list(edges.get(start, ()))
    seen = set()
    while pending:
        name = pending.pop()
        if name == target:
            return True
        if name in seen:
            continue
        seen.add(name)
        pending.extend(edges.get(name, ()))
    return False


def _select_candidates(module):
    sites, edges = _call_sites(module)
    observable = _observable_addresses(module)
    candidates = {}
    for function in module.functions:
        call_list = sites[function.name]
        caller = call_list[0][0] if len(call_list) == 1 else None
        if (
            function.name != "_start"
            and function.name not in observable
            and caller is not None
            and caller is not function
            and not _reaches(edges, function.name, caller.name)
            and not any(
                instruction.op in ("startup", "halt")
                for block in function.blocks
                for instruction in block.instructions
            )
        ):
            candidates[function.name] = function
    return candidates


def _clone_callee(callee, caller, call_instruction, inline_id):
    """Clone every block of ``callee`` into fresh blocks with remapped value
    ids and labels, binding parameters via fresh caller locals. Returns
    ``(cloned_blocks, continuation_label)``; ``caller`` is mutated in place
    (``values``, ``locals`` extended) but its own ``blocks`` are untouched --
    splicing them in is the caller's job. ``callee`` is pruned to only its
    reachable blocks first (in place -- a candidate has exactly one call
    site, so it is only ever cloned once): dead code after a ``return`` (the
    lowerer's own trailing ``const 0; return 0;`` idiom for a function whose
    real return already covers every path) would otherwise still count as a
    predecessor of the continuation block's phi despite never running,
    corrupting SSA."""
    prune_unreachable_blocks(callee)
    base = caller.values
    value_map = {value: base + value for value in range(callee.values)}
    caller.values += callee.values

    label_map = {
        block.label: f"{caller.name}.inline{inline_id}.{block.label}"
        for block in callee.blocks
    }
    continuation = f"{caller.name}.inline{inline_id}.return"

    caller.locals.extend(callee.params)
    caller.locals.extend(callee.locals)

    param_addresses = {}
    binding = []
    for index, parameter in enumerate(callee.params):
        address = caller.values
        caller.values += 1
        param_addresses[parameter.key] = address
        binding.append(
            Instruction("local_addr", address, (), pointer(parameter.type), parameter.key)
        )
        binding.append(
            Instruction("store", args=(address, call_instruction.args[index]), type=parameter.type)
        )

    def remap_value(value):
        return value_map[value] if isinstance(value, int) else value

    def remap_extra(instruction):
        if instruction.op == "phi":
            return tuple((label_map[label], remap_value(value)) for label, value in instruction.extra)
        if instruction.op in ("label", "jump"):
            return label_map[instruction.extra]
        if instruction.op in ("branch_if", "cbranch_if"):
            return (instruction.extra[0], label_map[instruction.extra[1]])
        return instruction.extra

    return_values = []  # (predecessor_label, value_or_None)
    cloned_blocks = []
    for block in callee.blocks:
        items = []
        for instruction in block.instructions:
            if instruction.op in ("return", "tailcall", "direct_tailcall"):
                break
            items.append(
                Instruction(
                    instruction.op,
                    remap_value(instruction.dst),
                    tuple(remap_value(v) for v in instruction.args),
                    instruction.type,
                    remap_extra(instruction),
                )
            )
        terminator = block.terminator()
        new_label = label_map[block.label]
        if terminator is not None and terminator.op == "return":
            value = remap_value(terminator.args[0]) if terminator.args else None
            return_values.append((new_label, value))
            items.append(Instruction("jump", extra=continuation))
        elif terminator is not None and terminator.op in ("tailcall", "direct_tailcall"):
            result = caller.values
            caller.values += 1
            items.append(
                Instruction(
                    "call" if terminator.op == "tailcall" else "direct_call",
                    result if terminator.type.kind != "void" else None,
                    tuple(remap_value(v) for v in terminator.args),
                    terminator.type,
                    terminator.extra,
                )
            )
            return_values.append((new_label, result if terminator.type.kind != "void" else None))
            items.append(Instruction("jump", extra=continuation))
        cloned_blocks.append(BasicBlock(new_label, items))

    # The entry clone and the continuation are both explicit jump targets
    # (from the call site, and from every cloned return, respectively), so
    # each needs its own `label` instruction naming its canonical label --
    # exactly what the lowerer already does for any block reachable other
    # than by fallthrough (see `build_cfg`'s alias table, keyed off `label`
    # instructions, not off `BasicBlock.label` directly).
    entry_label = Instruction("label", extra=cloned_blocks[0].label)
    entry_body = binding + cloned_blocks[0].instructions if binding else cloned_blocks[0].instructions
    cloned_blocks[0] = BasicBlock(cloned_blocks[0].label, [entry_label, *entry_body])

    continuation_items = [Instruction("label", extra=continuation)]
    if call_instruction.dst is not None and any(v is not None for _, v in return_values):
        phi = Instruction("phi", call_instruction.dst, (), call_instruction.type, tuple(return_values))
        continuation_items.append(phi)
    continuation_block = BasicBlock(continuation, continuation_items)

    return cloned_blocks + [continuation_block], continuation


def _retarget_predecessor(function, old_label, new_label):
    """Every phi anywhere in ``function`` that names ``old_label`` as a
    predecessor now reaches its block via ``new_label`` instead: splitting
    the call site's block moved its terminator (and thus its identity as a
    CFG predecessor of whatever it originally jumped/fell through to) onto
    the new continuation block, while ``old_label`` keeps only the leading
    half, which now always jumps straight into the clone."""
    for block in function.blocks:
        for instruction in block.instructions:
            if instruction.op != "phi":
                continue
            instruction.extra = tuple(
                (new_label if label == old_label else label, value)
                for label, value in instruction.extra
            )


def _inline_one_call(caller, call_block_label, call_instruction, callee, inline_id):
    cloned_blocks, continuation = _clone_callee(callee, caller, call_instruction, inline_id)
    continuation_block = cloned_blocks[-1]
    assert continuation_block.label == continuation

    new_blocks = []
    for block in caller.blocks:
        if block.label != call_block_label:
            new_blocks.append(block)
            continue
        before = []
        after = []
        seen_call = False
        for instruction in block.instructions:
            if instruction is call_instruction:
                seen_call = True
                continue
            (after if seen_call else before).append(instruction)
        before.append(Instruction("jump", extra=cloned_blocks[0].label))
        new_blocks.append(BasicBlock(block.label, before))
        new_blocks.extend(cloned_blocks[:-1])
        new_blocks.append(BasicBlock(continuation, continuation_block.instructions + after))
    caller.blocks = new_blocks
    _retarget_predecessor(caller, call_block_label, continuation)


def _settle(function, candidates, inline_id):
    """Inline every candidate call inside ``function`` until none remain,
    returning the next unused ``inline_id``. Must only be called on a
    function whose own callee candidates (if any) have already been settled
    -- see ``inline_single_call_functions``'s bottom-up ordering -- so a
    just-inlined callee's blocks never themselves contain another pending
    candidate call that would need re-scanning mid-splice."""
    while True:
        call_site = next(
            (
                (block.label, instruction)
                for block in function.blocks
                for instruction in block.instructions
                if instruction.op == "direct_call"
                and instruction.extra in candidates
                and candidates[instruction.extra] is not function
            ),
            None,
        )
        if call_site is None:
            return inline_id
        call_block_label, call_instruction = call_site
        callee = candidates[call_instruction.extra]
        inline_id += 1
        _inline_one_call(function, call_block_label, call_instruction, callee, inline_id)


def inline_single_call_functions(module):
    """Inline every candidate into its sole caller, splicing a true SSA clone
    of the callee's blocks directly into the caller's block list.

    Candidates are settled bottom-up: a candidate that itself calls another
    candidate must have that inner call resolved first, so its own clone
    already reflects its final block shape by the time it gets spliced into
    its own caller. Processing top-down instead would clone a callee, then
    later inline something *into* that already-spliced clone, changing which
    of the clone's blocks actually reaches its continuation -- the phi built
    for the outer splice would then cite a predecessor label that no longer
    reaches it, exactly the bug that motivated this ordering.
    """
    candidates = _select_candidates(module)
    if not candidates:
        return False

    _, edges = _call_sites(module)
    settled = set()

    def settle_transitively(name):
        if name in settled or name not in candidates:
            return
        settled.add(name)  # mark first: candidates are non-recursive, no cycle to worry about
        for callee_name in edges.get(name, ()):
            settle_transitively(callee_name)
        nonlocal inline_id
        inline_id = _settle(candidates[name], candidates, inline_id)

    inline_id = getattr(module, "_inline_serial", 0)
    for function in module.functions:
        settle_transitively(function.name)
    for function in module.functions:
        inline_id = _settle(function, candidates, inline_id)
    module._inline_serial = inline_id

    return True
