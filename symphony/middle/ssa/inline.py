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

from collections import defaultdict, deque

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
                if (
                    instruction.op in ("direct_call", "direct_tailcall")
                    and instruction.extra in names
                ):
                    edges[caller.name].add(instruction.extra)
                    if instruction.op == "direct_call":
                        sites[instruction.extra].append((caller, block.label, instruction))
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
    incoming_edges = {function.name: 0 for function in module.functions}
    for caller in module.functions:
        for block in caller.blocks:
            for instruction in block.instructions:
                if (
                    instruction.op in ("direct_call", "direct_tailcall")
                    and instruction.extra in incoming_edges
                ):
                    incoming_edges[instruction.extra] += 1
    observable = _observable_addresses(module)
    candidates = {}
    for function in module.functions:
        call_list = sites[function.name]
        # Runtime forwarding wrappers (for example ``__dyn_udiv`` forwarding
        # to ``__dyn_udivmod`` with a fixed remainder flag) are deliberately
        # tiny and side-effect-free.  Unlike ordinary functions, cloning them
        # at each caller removes an otherwise exported helper and exposes the
        # real implementation to subsequent module cleanup.
        body = [
            item
            for block in function.blocks
            for item in block.instructions
            if item.op != "label"
        ]
        calls = [item for item in body if item.op in ("direct_call", "direct_tailcall")]
        trivial_runtime_wrapper = (
            function.name.startswith("__dyn_")
            and len(calls) == 1
            and all(item.op in ("param", "const", "copy", "cast", "direct_call", "direct_tailcall", "return") for item in body)
        )
        caller = call_list[0][0] if call_list else None
        promoted = {
            item.extra[1]
            for block in function.blocks
            for item in block.instructions
            if item.op == "param"
        }
        returns = sum(
            item.op == "return"
            for block in function.blocks
            for item in block.instructions
        )
        # A direct call saves one call sequence and one standalone return.
        # Reject clones whose parameter binding and continuation machinery
        # exceed that conservative budget.
        clone_overhead = 2 * sum(p.key not in promoted for p in function.params)
        clone_overhead += returns + (returns > 1)
        if (
            function.name != "_start"
            and function.name not in observable
            and caller is not None
            and (len(call_list) == 1 or trivial_runtime_wrapper)
            and incoming_edges[function.name] == 1
            and clone_overhead <= 3
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
    promoted_parameters = {
        instruction.extra[1]: instruction.dst
        for block in callee.blocks
        for instruction in block.instructions
        if instruction.op == "param"
    }
    for index, parameter in enumerate(callee.params):
        value = promoted_parameters.get(parameter.key)
        if value is not None:
            value_map[value] = call_instruction.args[index]

    label_map = {
        block.label: f"{caller.name}.inline{inline_id}.{block.label}"
        for block in callee.blocks
    }
    continuation = f"{caller.name}.inline{inline_id}.return"

    caller.locals.extend(
        parameter for parameter in callee.params
        if parameter.key not in promoted_parameters
    )
    caller.locals.extend(callee.locals)

    param_addresses = {}
    binding = []
    for index, parameter in enumerate(callee.params):
        if parameter.key in promoted_parameters:
            continue
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
            if instruction.op == "param":
                continue
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

    # The cloned entry may itself be a loop header. Parameter stores must run
    # once on entry to the clone, not every time a back edge reaches that
    # header, so give them a distinct one-shot block. Putting them after the
    # entry label reinitializes modified parameters on every iteration;
    # putting them before an existing copy of that label lets the assembler's
    # label target skip them (and can create a duplicate symbol).
    entry_body = cloned_blocks[0].instructions
    split = next((i for i, item in enumerate(entry_body) if item.op != "label"), len(entry_body))
    leading_labels = entry_body[:split]
    if not any(item.extra == cloned_blocks[0].label for item in leading_labels):
        cloned_blocks[0].instructions.insert(
            0, Instruction("label", extra=cloned_blocks[0].label)
        )
    if binding:
        binding_label = f"{caller.name}.inline{inline_id}.entry"
        cloned_blocks.insert(
            0,
            BasicBlock(
                binding_label,
                [
                    Instruction("label", extra=binding_label),
                    *binding,
                    Instruction("jump", extra=label_map[callee.blocks[0].label]),
                ],
            ),
        )

    continuation_items = [Instruction("label", extra=continuation)]
    if call_instruction.dst is not None and any(v is not None for _, v in return_values):
        phi = Instruction("phi", call_instruction.dst, (), call_instruction.type, tuple(return_values))
        continuation_items.append(phi)
    continuation_block = BasicBlock(continuation, continuation_items)

    return cloned_blocks + [continuation_block], continuation


def _retarget_predecessor(function, old_label, new_label, phi_users=None):
    """Every phi anywhere in ``function`` that names ``old_label`` as a
    predecessor now reaches its block via ``new_label`` instead: splitting
    the call site's block moved its terminator (and thus its identity as a
    CFG predecessor of whatever it originally jumped/fell through to) onto
    the new continuation block, while ``old_label`` keeps only the leading
    half, which now always jumps straight into the clone."""
    if phi_users is None:
        instructions = (
            instruction
            for block in function.blocks
            for instruction in block.instructions
            if instruction.op == "phi"
        )
    else:
        instructions = phi_users.pop(old_label, ())
    for instruction in instructions:
        instruction.extra = tuple(
            (new_label if label == old_label else label, value)
            for label, value in instruction.extra
        )
        if phi_users is not None:
            phi_users[new_label].append(instruction)


class _BlockNode:
    """A temporary O(1)-splice view of a function's serialized blocks."""

    __slots__ = ("block", "previous", "next")

    def __init__(self, block):
        self.block = block
        self.previous = None
        self.next = None


def _inline_one_call(
    caller, call_node, call_instruction, callee, inline_id, phi_users=None,
):
    cloned_blocks, continuation = _clone_callee(callee, caller, call_instruction, inline_id)
    continuation_block = cloned_blocks[-1]
    assert continuation_block.label == continuation

    call_block = call_node.block
    call_block_label = call_block.label
    before = []
    after = []
    seen_call = False
    for instruction in call_block.instructions:
        if instruction is call_instruction:
            seen_call = True
            continue
        (after if seen_call else before).append(instruction)
    before.append(Instruction("jump", extra=cloned_blocks[0].label))
    # Splice into the temporary linked sequence.  Materializing
    # ``caller.blocks`` after every clone made selfhost quadratic in its
    # growing block count.
    call_node.block = BasicBlock(call_block.label, before)
    inserted = [_BlockNode(block) for block in cloned_blocks[:-1]]
    continuation_node = _BlockNode(
        BasicBlock(continuation, continuation_block.instructions + after)
    )
    inserted.append(continuation_node)
    previous, following = call_node, call_node.next
    for node in inserted:
        previous.next = node
        node.previous = previous
        previous = node
    previous.next = following
    if following is not None:
        following.previous = previous
    if phi_users is not None:
        for block in cloned_blocks:
            for instruction in block.instructions:
                if instruction.op == "phi":
                    for label, _ in instruction.extra:
                        phi_users[label].append(instruction)
    _retarget_predecessor(caller, call_block_label, continuation, phi_users)
    return cloned_blocks, continuation_node


def _settle(function, candidates, edges, inline_id):
    """Inline every candidate call inside ``function`` until none remain,
    returning the next unused ``inline_id``. A lexical worklist and temporary
    linked block sequence make call selection and splicing independent of the
    caller's growing size while preserving the prior rescan order."""
    phi_users = defaultdict(list)
    for block in function.blocks:
        for instruction in block.instructions:
            if instruction.op == "phi":
                for label, _ in instruction.extra:
                    phi_users[label].append(instruction)
    nodes = []
    previous = None
    for block in function.blocks:
        node = _BlockNode(block)
        node.previous = previous
        if previous is not None:
            previous.next = node
        nodes.append(node)
        previous = node
    head = nodes[0] if nodes else None
    active_candidates = dict(candidates)
    worklist = deque()
    for node in nodes:
        reference = [node]
        for instruction in node.block.instructions:
            if (
                instruction.op == "direct_call"
                and instruction.extra in active_candidates
                and active_candidates[instruction.extra] is not function
            ):
                worklist.append((instruction, reference))
    while worklist:
        call_instruction, reference = worklist.popleft()
        callee = active_candidates.get(call_instruction.extra)
        if callee is None or callee is function:
            continue
        # A candidate can be reached from more than one syntactic form: a
        # tail edge is not itself inlined here, but it still closes a recursive
        # SCC. Never clone such a callee into a member of that SCC.
        if _reaches(edges, callee.name, function.name):
            active_candidates.pop(callee.name)
            continue
        inline_id += 1
        cloned_blocks, continuation_node = _inline_one_call(
            function, reference[0], call_instruction, callee, inline_id, phi_users
        )
        # Queued calls from the split source block follow the call and now
        # reside in the continuation.  A cloned callee precedes them.
        reference[0] = continuation_node
        cloned_calls = []
        for index, block in enumerate(cloned_blocks):
            block_reference = reference if index == len(cloned_blocks) - 1 else [_BlockNode(block)]
            # The node must be the one spliced above, not a detached wrapper.
            if index < len(cloned_blocks) - 1:
                block_reference[0] = reference[0].previous
                for _ in range(len(cloned_blocks) - 2 - index):
                    block_reference[0] = block_reference[0].previous
            for instruction in block.instructions:
                if (
                    instruction.op == "direct_call"
                    and instruction.extra in active_candidates
                    and active_candidates[instruction.extra] is not function
                ):
                    cloned_calls.append((instruction, block_reference))
        worklist.extendleft(reversed(cloned_calls))
    function.blocks = []
    while head is not None:
        function.blocks.append(head.block)
        head = head.next
    return inline_id


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
        inline_id = _settle(candidates[name], candidates, edges, inline_id)

    inline_id = getattr(module, "_inline_serial", 0)
    for function in module.functions:
        settle_transitively(function.name)
    for function in module.functions:
        inline_id = _settle(function, candidates, edges, inline_id)
    module._inline_serial = inline_id

    return True
