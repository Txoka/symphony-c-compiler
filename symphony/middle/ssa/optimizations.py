"""Default-on switches for independently bisectable SSA optimizations.

Tests and experiments may temporarily set an entry in ``OPTIMIZATIONS`` to
``False``.  Required SSA construction, verification, destruction, intrinsic
lowering, and target legalization are deliberately not represented here.
"""

OPTIMIZATIONS = {
    "sccp": True,
    "global_copy_propagation": True,
    "direct_call_identification": True,
    "algebraic_simplification": True,
    "strength_reduction": True,
    "loop_invariant_hoisting": True,
    "induction_strength_reduction": True,
    "scaled_induction_strength_reduction": True,
    "pointer_limit_loops": True,
    "redundant_loop_memory_elimination": True,
    "loop_optimization_fixed_point": True,
    "straight_line_memory_forwarding": True,
    "comparison_branch_fusion": True,
    "comparison_zero_test_fusion": True,
    "paired_divmod": True,
    "alias_aware_divmod_pairing": True,
    "expensive_expression_cse": True,
    "dead_value_elimination": True,
    "cfg_simplification": True,
    "readonly_parameter_promotion": True,
    "tail_call_elimination": True,
    "self_reduction_loop_lowering": True,
    "bounded_constant_call_evaluation": True,
    "constant_loop_evaluation": True,
    "known_trip_full_unrolling": True,
    "unroll_no_code_growth": True,
    "self_tail_loop_lowering": True,
    "unreachable_symbol_elimination": True,
    "immutable_global_folding": True,
    "unused_stack_initialization_removal": True,
    "single_call_inlining": True,
    # Experimental until the estimate uses true live-at-call pressure.
    "loop_pressure_aware_inlining": False,
}


def enabled(name):
    """Return whether the named optional optimization is enabled."""
    return OPTIMIZATIONS[name]
