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
from .loop_memory import (
    eliminate_redundant_loop_memory,
    eliminate_redundant_straight_line_memory,
)
from .copies import propagate_global_copies
from .algebra import simplify_algebra
from .fuse_branches import fuse_comparison_branches, fuse_comparison_zero_tests
from .divmod import pair_unsigned_divmod
from .value_numbering import eliminate_common_expressions
from .evaluate import evaluate_constant_calls, evaluate_constant_loops
from .strength import reduce_strength
from .calls import identify_direct_calls
from .parameters import promote_readonly_parameters
from .tail import (
    eliminate_tail_calls,
    lower_self_reductions_to_loops,
    lower_self_tail_calls_to_loops,
)
from .module import remove_unreachable_symbols, remove_unreachable_functions, fold_immutable_global_loads, remove_unused_stack_initialization

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
    "eliminate_redundant_straight_line_memory",
    "propagate_global_copies",
    "simplify_algebra",
    "fuse_comparison_branches",
    "fuse_comparison_zero_tests",
    "pair_unsigned_divmod",
    "eliminate_common_expressions",
    "evaluate_constant_calls",
    "evaluate_constant_loops",
    "reduce_strength",
    "identify_direct_calls",
    "promote_readonly_parameters",
    "eliminate_tail_calls",
    "lower_self_reductions_to_loops",
    "lower_self_tail_calls_to_loops",
    "remove_unreachable_symbols",
    "remove_unreachable_functions",
    "fold_immutable_global_loads",
    "remove_unused_stack_initialization",
]
