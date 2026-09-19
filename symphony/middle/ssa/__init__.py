"""Persistent SSA construction, destruction, and verification."""

from .construct import construct
from .destruct import destruct
from .verify import verify, SSAVerificationError
from .sccp import sparse_conditional_constant_propagation
from .hoist import hoist_loop_invariants
from .dce import remove_dead_values
from .inline import inline_single_call_functions
from .simplify_cfg import simplify_control_flow
from .induction import reduce_induction_strength
from .loop_memory import eliminate_redundant_loop_memory
from .copies import propagate_global_copies
from .algebra import simplify_algebra
from .fuse_branches import fuse_comparison_branches
from .strength import reduce_strength
from .parameters import promote_readonly_parameters

__all__ = [
    "construct",
    "destruct",
    "verify",
    "SSAVerificationError",
    "sparse_conditional_constant_propagation",
    "hoist_loop_invariants",
    "remove_dead_values",
    "inline_single_call_functions",
    "simplify_control_flow",
    "reduce_induction_strength",
    "eliminate_redundant_loop_memory",
    "propagate_global_copies",
    "simplify_algebra",
    "fuse_comparison_branches",
    "reduce_strength",
    "promote_readonly_parameters",
]
