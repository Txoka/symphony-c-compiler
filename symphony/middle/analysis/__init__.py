"""Reusable analyses over the common IR."""

from .cfg import (
    BasicBlock,
    ControlFlowGraph,
    build_cfg,
    prune_unreachable_blocks,
    redirect_edge,
    remove_block,
    split_edge,
)

__all__ = [
    "BasicBlock",
    "ControlFlowGraph",
    "build_cfg",
    "prune_unreachable_blocks",
    "redirect_edge",
    "remove_block",
    "split_edge",
]
