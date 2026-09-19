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
]
