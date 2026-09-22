"""User-adjustable SSA optimization settings and their complete reference.

Boolean entries are independent optimization toggles unless their comment
states a dependency. Numeric entries are sliders consumed by the named pass.
Tests and experiments may temporarily change entries in ``OPTIMIZATIONS``;
the values below are the compiler defaults.

Required correctness work is intentionally absent: SSA construction,
verification and destruction, intrinsic lowering, and target legalization
cannot be disabled through this registry.
"""

OPTIMIZATIONS = {
    # Fold constants through reachable CFG paths, simplify constant branches,
    # and discard infeasible edges using sparse conditional constant propagation.
    "sccp": True,

    # Replace SSA copies/casts with their dominating source across blocks.
    "global_copy_propagation": True,

    # Turn indirect calls through a known function address into direct calls.
    "direct_call_identification": True,

    # Apply no-growth integer identities such as x+0, x*1, and self-comparisons.
    "algebraic_simplification": True,

    # Replace multiply/divide/remainder by powers of two with shifts or masks.
    "strength_reduction": True,

    # Move computations proven invariant and safe from loop bodies to preheaders.
    "loop_invariant_hoisting": True,

    # Apply a per-loop register-pressure guard to LICM. A proposed hoist is
    # rolled back when it raises peak liveness beyond loop_register_budget.
    "pressure_aware_licm": True,

    # Replace affine expressions of a basic induction variable with derived phis.
    "induction_strength_reduction": True,

    # Extend induction reduction to scaled/indexed address expressions.
    "scaled_induction_strength_reduction": True,

    # Let scaled-induction matching look through integer casts that preserve
    # all bits (same width, signedness-only change).
    "representation_preserving_induction_casts": True,

    # Apply the shared setup/steady-state/pressure profitability model to
    # constant-bounded scaled induction instead of rejecting every such loop.
    "loop_profitability": True,

    # Numeric slider: assumed trip count when canonical analysis cannot derive
    # an exact count. Must be a non-negative integer.
    "loop_unknown_trip_count": 8,

    # Numeric slider: multiplicative execution-frequency weight per nesting
    # level beyond the first. Must be at least one.
    "loop_depth_weight": 4,

    # Numeric slider: maximum iterations used to prove an exact trip count by
    # target-width recurrence simulation. Larger/uncertain loops use the
    # unknown-trip estimate. Must be a non-negative integer.
    "loop_trip_count_analysis_limit": 65536,

    # Numeric slider: volatile/callee-saved register homes available to a leaf
    # loop in the current backend. A proposed recurrence exceeding this budget
    # receives a per-iteration spill penalty. Must be a positive integer.
    "loop_register_budget": 8,

    # Change eligible scaled index exit tests into advancing-pointer limit tests.
    "pointer_limit_loops": True,

    # Forward exact-address loads and remove overwritten stores inside loops.
    "redundant_loop_memory_elimination": True,

    # Repeat scalar cleanup and the preceding loop transformations until none
    # changes the function. Individual pass toggles above still apply inside it.
    "loop_optimization_fixed_point": True,

    # Numeric safety bound for the loop optimization fixed point. Exhausting
    # it is a compiler error rather than silently emitting partially optimized
    # code. Must be a positive integer.
    "loop_fixed_point_iteration_limit": 64,

    # Perform the same conservative exact-address memory forwarding outside loops.
    "straight_line_memory_forwarding": True,

    # Feed a comparison directly to its sole conditional branch, removing its
    # materialized 0/1 result when safe.
    "comparison_branch_fusion": True,

    # Collapse (comparison == 0) / (comparison != 0) branches back to the
    # original comparison predicate.
    "comparison_zero_test_fusion": True,

    # Combine matching unsigned remainder and quotient operations into one
    # __dyn_udivmod_pair call. The alias-aware policy below only affects how
    # far this enabled matcher may search.
    "paired_divmod": True,

    # Allow paired_divmod to cross stores proven to target distinct local
    # objects. False makes every intervening store stop the matcher.
    "alias_aware_divmod_pairing": True,

    # Reuse dominated repeated multiply/divide/remainder expressions.
    "expensive_expression_cse": True,

    # Delete unused pure SSA definitions.
    "dead_value_elimination": True,

    # Prune unreachable blocks, fold redundant branches/jumps, merge eligible
    # blocks, repair phis, and remove unused labels.
    "cfg_simplification": True,

    # Represent read-only scalar parameters as incoming SSA values rather than
    # repeated stack/local loads.
    "readonly_parameter_promotion": True,

    # Replace safe call-immediately-followed-by-return forms with tail calls.
    "tail_call_elimination": True,

    # Convert supported single-parameter associative self-recursive reductions
    # (for example factorial) into accumulator loops.
    "self_reduction_loop_lowering": True,

    # Interpret pure direct calls whose arguments are compile-time constants.
    # Nested call towers are supported; recursion and observable effects reject
    # evaluation. The numeric setting immediately below bounds each attempt.
    "bounded_constant_call_evaluation": True,

    # Numeric slider: maximum total SSA instructions interpreted by one constant
    # call attempt, shared by the entire nested call tower. Zero disables every
    # attempt without disabling the pass itself. Must be a non-negative integer.
    "bounded_constant_call_instruction_limit": 1024,

    # Interpret side-effect-free natural loops whose inputs and executed control
    # path are compile-time known. Current fixed limits: 8 iterations and 1,024
    # interpreted SSA instructions; these are not yet configurable sliders.
    "constant_loop_evaluation": True,

    # Fully clone loops with a statically known trip count of at most 4. That
    # current fixed trip limit is not yet a configurable slider.
    "known_trip_full_unrolling": True,

    # Profitability policy for known-trip unrolling: True retains the unrolled
    # module only when its final image is no larger; False permits code growth.
    # This has no effect when known_trip_full_unrolling is False.
    "unroll_no_code_growth": True,

    # Convert direct self-tail calls into parameter phis and a loop backedge.
    "self_tail_loop_lowering": True,

    # Remove functions/globals not transitively reachable from _start.
    "unreachable_symbol_elimination": True,

    # Fold typed loads from closed-world globals proven never to be written.
    "immutable_global_folding": True,

    # Remove startup stack initialization when the final entry path is stack-free.
    "unused_stack_initialization_removal": True,

    # Inline non-recursive functions with one surviving direct call site, plus
    # eligible trivial runtime forwarding wrappers.
    "single_call_inlining": True,

    # Guard single-call inlining of a loop callee inside a caller loop using
    # caller-live-at-call plus callee peak-live pressure. False disables only
    # this guard, not inlining.
    "loop_pressure_aware_inlining": False,

    # Numeric slider: maximum combined caller-live-at-call and callee peak-live
    # values accepted by loop_pressure_aware_inlining. Must be positive.
    "inlining_register_budget": 7,
}


def enabled(name):
    """Return a boolean toggle or numeric slider value from the registry."""
    return OPTIMIZATIONS[name]
