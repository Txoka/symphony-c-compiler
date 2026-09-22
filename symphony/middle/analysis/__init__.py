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
from .profitability import (
    LoopTransformationCost,
    exact_trip_count,
    peak_live_values,
    profitable,
)

__all__ = [
    "BasicBlock",
    "ControlFlowGraph",
    "build_cfg",
    "prune_unreachable_blocks",
    "redirect_edge",
    "remove_block",
    "split_edge",
    "LoopTransformationCost",
    "exact_trip_count",
    "peak_live_values",
    "profitable",
]
