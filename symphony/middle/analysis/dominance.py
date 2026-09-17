"""Dominator tree and dominance frontiers over a ControlFlowGraph.

Cooper/Harvey/Kennedy's iterative engineering-friendly algorithm, computed over
reverse postorder so it reaches a fixed point in a small number of passes on
the small functions this compiler produces.
"""

from dataclasses import dataclass

from .cfg import ControlFlowGraph


@dataclass
class DominatorTree:
    cfg: ControlFlowGraph
    order: list[int]
    idom: dict[int, int]
    frontier: dict[int, set[int]]
    children: dict[int, list[int]]

    def dominates(self, a: int, b: int) -> bool:
        while b != a:
            if b not in self.idom:
                return False
            b = self.idom[b]
        return True


def _reverse_postorder(cfg: ControlFlowGraph) -> list[int]:
    if not cfg.blocks:
        return []
    visited = set()
    order = []

    def visit(index):
        visited.add(index)
        for successor in sorted(cfg.blocks[index].successors):
            if successor not in visited:
                visit(successor)
        order.append(index)

    visit(0)
    order.reverse()
    return order


def _intersect(idom, position, a, b):
    while a != b:
        while position[a] > position[b]:
            a = idom[a]
        while position[b] > position[a]:
            b = idom[b]
    return a


def build_dominator_tree(cfg: ControlFlowGraph) -> DominatorTree:
    order = _reverse_postorder(cfg)
    if not order:
        return DominatorTree(cfg, [], {}, {}, {})
    position = {block: i for i, block in enumerate(order)}
    entry = order[0]
    idom = {entry: entry}
    changed = True
    while changed:
        changed = False
        for block in order[1:]:
            processed_predecessors = [
                p for p in cfg.blocks[block].predecessors if p in idom
            ]
            if not processed_predecessors:
                continue
            new_idom = processed_predecessors[0]
            for predecessor in processed_predecessors[1:]:
                new_idom = _intersect(idom, position, predecessor, new_idom)
            if idom.get(block) != new_idom:
                idom[block] = new_idom
                changed = True

    frontier = {block: set() for block in order}
    for block in order:
        predecessors = cfg.blocks[block].predecessors
        if len(predecessors) < 2:
            continue
        for predecessor in predecessors:
            if predecessor not in idom:
                continue
            runner = predecessor
            while runner != idom[block]:
                frontier[runner].add(block)
                runner = idom[runner]

    children = {block: [] for block in order}
    for block, parent in idom.items():
        if block != entry:
            children[parent].append(block)

    return DominatorTree(cfg, order, idom, frontier, children)
