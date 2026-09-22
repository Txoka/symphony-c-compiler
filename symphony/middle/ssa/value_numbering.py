"""Conservative value numbering for expensive SSA expressions."""

from ..analysis.cfg import build_cfg
from ..analysis.dominance import build_dominator_tree
from ..ir import BasicBlock, Instruction


EXPENSIVE_BINARY = {"*", "/", "%"}


def eliminate_common_expressions(function):
    """Replace dominated duplicate pure expressions with SSA copies.

    Memory reads are deliberately excluded until they can be keyed by a
    conservative memory version.  Walking the dominator tree means an
    available expression is reused only where its definition is guaranteed
    to execute first.
    """
    cfg = build_cfg(function)
    dominators = build_dominator_tree(cfg)
    if not dominators.order:
        return False

    replacements = {}

    def visit(label, available):
        available = dict(available)
        block = cfg.by_label[label]
        for instruction in block.instructions:
            key = _expression_key(instruction)
            if key is None:
                continue
            previous = available.get(key)
            if previous is None:
                available[key] = instruction.dst
            else:
                replacements[id(instruction)] = previous
        for child in dominators.children[label]:
            visit(child, available)

    visit(dominators.order[0], {})
    if not replacements:
        return False

    function.blocks = [
        BasicBlock(
            block.label,
            [
                Instruction("copy", item.dst, (replacements[id(item)],), item.type)
                if id(item) in replacements
                else item
                for item in block.instructions
            ],
        )
        for block in function.blocks
    ]
    return True


def _expression_key(instruction):
    if (
        instruction.dst is None
        or instruction.op != "binary"
        or instruction.extra not in EXPENSIVE_BINARY
    ):
        return None
    args = instruction.args
    if instruction.extra == "*":
        args = tuple(sorted(args))
    return instruction.op, args, _type_key(instruction.type), instruction.extra


def _type_key(type_):
    return (
        type_.kind,
        type_.size,
        type_.signed,
        id(type_.base),
        id(type_.record),
        type_.qualifiers,
    )


__all__ = ["eliminate_common_expressions"]
