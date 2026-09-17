"""Basic-block construction and reachability for Symphony C IR.

``BasicBlock`` identity is its ``label``: a name that is assigned once and
never reused, so it survives being rebuilt from a differently-shaped CFG.
Every block has a label backing its identity -- including the function entry
and anonymous fallthrough blocks the lowerer used to leave unlabeled -- so
identity and a block's own jump-target name always coincide, and nothing
needs a separate identity-remapping table.

A block's ``successors``/``predecessors`` are label tuples. ``successors`` is
ordered with the fallthrough edge (if any) first: this is also how
``FunctionIR.blocks`` chooses fallthrough when flattened back to a linear
instruction stream for the backend -- whichever block is serialized
immediately after another is its fallthrough target, and nothing rediscovers
branch direction from the instructions themselves.
"""

from dataclasses import dataclass, field

from ..ir import Instruction


TERMINATORS = {
    "jump",
    "branch_if",
    "cbranch_if",
    "return",
    "tailcall",
    "direct_tailcall",
    "halt",
}


@dataclass
class BasicBlock:
    """A maximal straight-line instruction sequence, identified by ``label``."""

    label: str
    instructions: list[Instruction] = field(default_factory=list)
    successors: list[str] = field(default_factory=list)
    predecessors: list[str] = field(default_factory=list)

    def terminator(self):
        """The block's trailing control-flow instruction, if it has one."""
        return self.instructions[-1] if self.instructions and self.instructions[-1].op in TERMINATORS else None

    def add_successor(self, label, fallthrough=False):
        if label in self.successors:
            return
        if fallthrough:
            self.successors.insert(0, label)
        else:
            self.successors.append(label)

    def remove_successor(self, label):
        if label in self.successors:
            self.successors.remove(label)


@dataclass
class ControlFlowGraph:
    """A thin, index-free view over ``FunctionIR.blocks`` for analyses that
    want positional order (reverse postorder, dominance) without caring
    about identity. ``blocks`` is in the function's own serialization order;
    ``by_label`` gives O(1) identity lookup."""

    blocks: list[BasicBlock]
    by_label: dict[str, BasicBlock]
    label_blocks: dict[str, str] = field(default_factory=dict)

    def index_of(self, label):
        return self._index[label]

    def __post_init__(self):
        self._index = {block.label: i for i, block in enumerate(self.blocks)}

    def reachable(self) -> set[str]:
        if not self.blocks:
            return set()
        result = set()
        pending = [self.blocks[0].label]
        while pending:
            label = pending.pop()
            if label in result:
                continue
            result.add(label)
            for successor in self.by_label[label].successors:
                if successor not in result:
                    pending.append(successor)
        return result


def _targets(instruction: Instruction) -> tuple[str, ...]:
    if instruction.op == "jump":
        return (instruction.extra,)
    if instruction.op == "branch_if":
        return (instruction.extra[1],)
    if instruction.op == "cbranch_if":
        return (instruction.extra[1],)
    return ()


def build_cfg(function) -> ControlFlowGraph:
    """A ``ControlFlowGraph`` view over ``function.blocks``, wiring
    successors/predecessors from each block's own terminator. Block identity
    (``label``) is exactly ``function.blocks[i].label`` -- nothing here
    invents or renumbers it, so results stay valid across rebuilds as long as
    the function's blocks/labels themselves haven't changed shape. A jump can
    name any label a block carries (its canonical one or an extra alias from
    an empty lowered arm); both resolve to that block here."""
    blocks = function.blocks
    by_label = {block.label: block for block in blocks}
    alias = {
        item.extra: block.label
        for block in blocks
        for item in block.instructions
        if item.op == "label"
    }
    for block in blocks:
        block.successors = []
        block.predecessors = []

    for index, block in enumerate(blocks):
        last = block.terminator()
        explicit = _targets(last) if last is not None else ()
        for label in explicit:
            resolved = alias.get(label, label)
            if resolved in by_label:
                block.add_successor(resolved)
        falls_through = index + 1 < len(blocks) and (
            last is None or last.op not in TERMINATORS or last.op in ("branch_if", "cbranch_if")
        )
        if falls_through:
            block.add_successor(blocks[index + 1].label, fallthrough=True)

    for block in blocks:
        for successor in block.successors:
            target = by_label[successor]
            if block.label not in target.predecessors:
                target.predecessors.append(block.label)

    return ControlFlowGraph(blocks, by_label, alias)


def prune_unreachable_blocks(function) -> bool:
    """Delete blocks not reachable from the function entry."""
    cfg = build_cfg(function)
    reachable = cfg.reachable()
    if len(reachable) == len(cfg.blocks):
        return False
    function.blocks = [block for block in cfg.blocks if block.label in reachable]
    return True


def split_edge(function, source_label, target_label, new_label):
    """Insert a fresh, empty trampoline block on the ``source -> target``
    edge, jumping straight to ``target``. Returns the new block. Caller is
    responsible for filling its body (e.g. parallel copies for phi
    destruction) before the jump, and for redirecting ``source``'s
    terminator/fallthrough to the trampoline via ``redirect_edge``.

    The trampoline is appended at the very end of ``function.blocks``: since
    it targets ``target`` via an explicit jump (never a fallthrough), its own
    position in the serialization order is not load-bearing, but every
    *other* block's fallthrough position is -- so callers must never insert
    a fallthrough-edge trampoline here; that one has to be spliced positionally
    right after ``source`` instead (see ``destruct.py``).
    """
    cfg = build_cfg(function)
    trampoline = BasicBlock(
        new_label,
        [Instruction("jump", None, (), extra=target_label)],
    )
    function.blocks.append(trampoline)
    return trampoline


def redirect_edge(function, source_label, old_target, new_target):
    """Repoint one of ``source``'s CFG edges from ``old_target`` to
    ``new_target``, rewriting its terminator (or fallthrough) as needed."""
    source = next(block for block in function.blocks if block.label == source_label)
    last = source.terminator()
    if last is not None and last.op == "jump" and last.extra == old_target:
        last.extra = new_target
        return
    if last is not None and last.op in ("branch_if", "cbranch_if") and last.extra[1] == old_target:
        last.extra = (last.extra[0], new_target)
        return
    # Otherwise this must be the fallthrough edge: the caller is expected to
    # reposition blocks in function.blocks so new_target's block is the one
    # that immediately follows source; nothing to rewrite in-place here since
    # fallthrough isn't spelled out as an instruction.
    raise AssertionError(
        f"redirect_edge: {source_label} has no explicit edge to {old_target} "
        "(fallthrough edges are redirected by reordering function.blocks)"
    )


def remove_block(function, label):
    """Delete a block by identity. Does not fix up any remaining edges into
    it; callers must redirect those first."""
    function.blocks = [block for block in function.blocks if block.label != label]
