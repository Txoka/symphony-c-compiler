"""Dominator tree and dominance frontiers over a ControlFlowGraph.

Cooper/Harvey/Kennedy's iterative engineering-friendly algorithm, computed over
reverse postorder so it reaches a fixed point in a small number of passes on
the small functions this compiler produces. Blocks are identified by their
stable ``label`` throughout, not by any position in a particular CFG build.
"""

from dataclasses import dataclass

from .cfg import ControlFlowGraph


@dataclass
class Loop:
    """A natural loop: a header dominating all blocks reachable via a back edge."""

    header: str
    blocks: set[str]
    back_edges: set[tuple[str, str]]


@dataclass
class DominatorTree:
    cfg: ControlFlowGraph
    order: list[str]
    idom: dict[str, str]
    frontier: dict[str, set[str]]
    children: dict[str, list[str]]

    def dominates(self, a: str, b: str) -> bool:
        while b != a:
            if b not in self.idom or self.idom[b] == b:
                return False
            b = self.idom[b]
        return True


def _reverse_postorder(cfg: ControlFlowGraph) -> list[str]:
    if not cfg.blocks:
        return []
    visited = set()
    order = []

    def visit(label):
        visited.add(label)
        for successor in sorted(cfg.by_label[label].successors):
            if successor not in visited:
                visit(successor)
        order.append(label)

    visit(cfg.blocks[0].label)
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
                p for p in cfg.by_label[block].predecessors if p in idom
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
        predecessors = cfg.by_label[block].predecessors
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


def find_natural_loops(dominators: DominatorTree) -> list[Loop]:
    """Discover natural loops from back edges (edges into a dominating header).

    Loops sharing a header are merged, matching how a single ``for``/``while``
    with multiple continue-like back edges is one loop with one header.
    """
    cfg = dominators.cfg
    by_header: dict[str, Loop] = {}
    for block in cfg.blocks:
        for successor in block.successors:
            if not dominators.dominates(successor, block.label):
                continue
            back_edge = (block.label, successor)
            body = {successor}
            stack = [block.label]
            while stack:
                node = stack.pop()
                if node in body:
                    continue
                body.add(node)
                stack.extend(cfg.by_label[node].predecessors)
            if successor in by_header:
                loop = by_header[successor]
                loop.blocks |= body
                loop.back_edges.add(back_edge)
            else:
                by_header[successor] = Loop(successor, body, {back_edge})
    return list(by_header.values())
