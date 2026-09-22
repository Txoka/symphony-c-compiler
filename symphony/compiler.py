"""Language/frontend-independent pipeline orchestration and public C shortcut."""

from copy import deepcopy
from dataclasses import dataclass
from .frontends.c import CFrontend
from .frontends.protocol import SourceFrontend
from .middle.lowering import lower_intrinsics
from .middle.ssa import (
    construct,
    destruct,
    verify,
    sparse_conditional_constant_propagation,
    hoist_loop_invariants,
    remove_dead_values,
    inline_single_call_functions,
    simplify_control_flow,
    reduce_induction_strength,
    reduce_scaled_induction_strength,
    convert_pointer_limit_loops,
    eliminate_redundant_loop_memory,
    eliminate_redundant_straight_line_memory,
    propagate_global_copies,
    simplify_algebra,
    fuse_comparison_branches,
    fuse_comparison_zero_tests,
    pair_unsigned_divmod,
    eliminate_common_expressions,
    evaluate_constant_calls,
    evaluate_constant_loops,
    unroll_known_trip_loops,
    reduce_strength,
    identify_direct_calls,
    promote_readonly_parameters,
    eliminate_tail_calls,
    lower_self_reductions_to_loops,
    lower_self_tail_calls_to_loops,
    remove_unreachable_symbols,
    fold_immutable_global_loads,
    remove_unused_stack_initialization,
)
from .targets.symphony import generate, Target
from .targets.symphony.legalize import legalize_runtime_arithmetic
from .middle.ssa.optimizations import enabled as optimization_enabled


def _optional_optimization(name, function):
    """Gate one optional pass through the centralized default-on registry."""
    def run(*args, **kwargs):
        return function(*args, **kwargs) if optimization_enabled(name) else False
    return run


sparse_conditional_constant_propagation = _optional_optimization(
    "sccp", sparse_conditional_constant_propagation
)
propagate_global_copies = _optional_optimization(
    "global_copy_propagation", propagate_global_copies
)
identify_direct_calls = _optional_optimization(
    "direct_call_identification", identify_direct_calls
)
simplify_algebra = _optional_optimization(
    "algebraic_simplification", simplify_algebra
)
reduce_strength = _optional_optimization("strength_reduction", reduce_strength)
hoist_loop_invariants = _optional_optimization(
    "loop_invariant_hoisting", hoist_loop_invariants
)
reduce_induction_strength = _optional_optimization(
    "induction_strength_reduction", reduce_induction_strength
)
reduce_scaled_induction_strength = _optional_optimization(
    "scaled_induction_strength_reduction", reduce_scaled_induction_strength
)
convert_pointer_limit_loops = _optional_optimization(
    "pointer_limit_loops", convert_pointer_limit_loops
)
eliminate_redundant_loop_memory = _optional_optimization(
    "redundant_loop_memory_elimination", eliminate_redundant_loop_memory
)
eliminate_redundant_straight_line_memory = _optional_optimization(
    "straight_line_memory_forwarding", eliminate_redundant_straight_line_memory
)
fuse_comparison_branches = _optional_optimization(
    "comparison_branch_fusion", fuse_comparison_branches
)
fuse_comparison_zero_tests = _optional_optimization(
    "comparison_zero_test_fusion", fuse_comparison_zero_tests
)
pair_unsigned_divmod = _optional_optimization("paired_divmod", pair_unsigned_divmod)
eliminate_common_expressions = _optional_optimization(
    "expensive_expression_cse", eliminate_common_expressions
)
remove_dead_values = _optional_optimization("dead_value_elimination", remove_dead_values)
simplify_control_flow = _optional_optimization("cfg_simplification", simplify_control_flow)
promote_readonly_parameters = _optional_optimization(
    "readonly_parameter_promotion", promote_readonly_parameters
)
eliminate_tail_calls = _optional_optimization(
    "tail_call_elimination", eliminate_tail_calls
)
lower_self_reductions_to_loops = _optional_optimization(
    "self_reduction_loop_lowering", lower_self_reductions_to_loops
)
evaluate_constant_calls = _optional_optimization(
    "bounded_constant_call_evaluation", evaluate_constant_calls
)
evaluate_constant_loops = _optional_optimization(
    "constant_loop_evaluation", evaluate_constant_loops
)
unroll_known_trip_loops = _optional_optimization(
    "known_trip_full_unrolling", unroll_known_trip_loops
)
lower_self_tail_calls_to_loops = _optional_optimization(
    "self_tail_loop_lowering", lower_self_tail_calls_to_loops
)
remove_unreachable_symbols = _optional_optimization(
    "unreachable_symbol_elimination", remove_unreachable_symbols
)
fold_immutable_global_loads = _optional_optimization(
    "immutable_global_folding", fold_immutable_global_loads
)
remove_unused_stack_initialization = _optional_optimization(
    "unused_stack_initialization_removal", remove_unused_stack_initialization
)
inline_single_call_functions = _optional_optimization(
    "single_call_inlining", inline_single_call_functions
)


@dataclass
class Compilation:
    parsed: object
    typed: object
    ir: object
    image: object


class Compiler:
    def __init__(self, frontend: SourceFrontend, target=None):
        self.frontend = frontend
        self.target = target or Target()

    def compile(self, source: str, filename: str = "<input>") -> Compilation:
        frontend = self.frontend.lower(source, filename)
        return self.finish(frontend)

    def compile_project(self, sources) -> Compilation:
        if not hasattr(self.frontend, "lower_project"):
            raise TypeError("this frontend does not support multiple translation units")
        return self.finish(self.frontend.lower_project(sources))

    def finish(self, frontend) -> Compilation:
        ir = frontend.ir
        for function in ir.functions:
            # Required legalization, not optimization: these intrinsics have
            # no C body, only a target-instruction lowering.
            lower_intrinsics(function)
            construct(function)
            verify(function)
            sparse_conditional_constant_propagation(function)
            verify(function)
            propagate_global_copies(function)
            verify(function)
            identify_direct_calls(function)
            verify(function)
            simplify_algebra(function)
            verify(function)
            propagate_global_copies(function)
            verify(function)
            eliminate_common_expressions(function)
            verify(function)
            propagate_global_copies(function)
            verify(function)
            reduce_strength(function)
            verify(function)
            hoist_loop_invariants(function)
            verify(function)
            reduce_induction_strength(function)
            verify(function)
            reduce_scaled_induction_strength(function)
            verify(function)
            convert_pointer_limit_loops(function)
            verify(function)
            eliminate_redundant_loop_memory(function)
            verify(function)
            eliminate_redundant_straight_line_memory(function)
            verify(function)
            fuse_comparison_zero_tests(function)
            verify(function)
            pair_unsigned_divmod(function)
            verify(function)
            fuse_comparison_branches(function)
            verify(function)
            remove_dead_values(function)
            verify(function)
            simplify_control_flow(function)
            verify(function)
            lower_self_reductions_to_loops(function)
            verify(function)
            propagate_global_copies(function)
            verify(function)
            remove_dead_values(function)
            verify(function)
            simplify_control_flow(function)
            verify(function)
        # Expose read-only arguments as scalar SSA inputs so the evaluator can
        # handle ordinary pure loops as well as canonicalized recurrences.
        for function in ir.functions:
            promote_readonly_parameters(function)
            verify(function)
        # Make immutable pointer initializers visible before region evaluation,
        # allowing constant loops over closed-world global arrays to collapse.
        fold_immutable_global_loads(ir)
        if evaluate_constant_loops(ir):
            for function in ir.functions:
                sparse_conditional_constant_propagation(function)
                verify(function)
                propagate_global_copies(function)
                verify(function)
                remove_dead_values(function)
                verify(function)
                simplify_control_flow(function)
                verify(function)
        # Evaluate small pure calls while arithmetic is still represented as
        # scalar SSA; legalization below would otherwise turn it into runtime
        # calls that deliberately stop the evaluator.
        if evaluate_constant_calls(ir):
            for function in ir.functions:
                sparse_conditional_constant_propagation(function)
                verify(function)
                remove_dead_values(function)
                verify(function)
                simplify_control_flow(function)
                verify(function)
        # Make surviving runtime arithmetic ordinary call edges after scalar
        # folding but before call-graph optimization, so wrapper helpers can
        # participate in inlining and reachability.
        legalize_runtime_arithmetic(ir)
        # Form and lower self tails before selecting inline candidates.  Once
        # a self call is a backedge it no longer disqualifies an otherwise
        # single-caller function from being inlined into that caller.
        for function in ir.functions:
            promote_readonly_parameters(function)
            verify(function)
            eliminate_tail_calls(function, self_only=True)
            verify(function)
            lower_self_tail_calls_to_loops(function)
            verify(function)
            propagate_global_copies(function)
            verify(function)
            remove_dead_values(function)
            verify(function)
            simplify_control_flow(function)
            verify(function)
        # Candidate selection must see the live call graph.  In particular,
        # arithmetic forwarding wrappers can have dead runtime-only callers;
        # counting those stale edges would incorrectly prevent their sole
        # reachable call site from being inlined.
        remove_unreachable_symbols(ir)
        inline_single_call_functions(ir)
        for function in ir.functions:
            verify(function)
            identify_direct_calls(function)
            verify(function)
            promote_readonly_parameters(function)
            verify(function)
            eliminate_tail_calls(function)
            verify(function)
            lower_self_tail_calls_to_loops(function)
            verify(function)
        while fold_immutable_global_loads(ir):
            for function in ir.functions:
                sparse_conditional_constant_propagation(function)
                verify(function)
                remove_dead_values(function)
                verify(function)
                simplify_control_flow(function)
                verify(function)
        for function in ir.functions:
            sparse_conditional_constant_propagation(function)
            verify(function)
            identify_direct_calls(function)
            verify(function)
            remove_dead_values(function)
            verify(function)
            simplify_control_flow(function)
            verify(function)
        remove_unreachable_symbols(ir)

        candidate = deepcopy(ir)
        unrolled = False
        for function in candidate.functions:
            unrolled |= unroll_known_trip_loops(function)
        if unrolled:
            for function in candidate.functions:
                sparse_conditional_constant_propagation(function)
                verify(function)
                propagate_global_copies(function)
                verify(function)
                remove_dead_values(function)
                verify(function)
                simplify_control_flow(function)
                verify(function)
            remove_unreachable_symbols(candidate)
            candidate_image = _finalize_module(candidate, self.target)
            if not optimization_enabled("unroll_no_code_growth"):
                ir, image = candidate, candidate_image
            else:
                baseline_image = _finalize_module(ir, self.target)
                if len(candidate_image.binary) <= len(baseline_image.binary):
                    ir, image = candidate, candidate_image
                else:
                    image = baseline_image
        else:
            image = _finalize_module(ir, self.target)
        return Compilation(
            frontend.parsed,
            frontend.typed,
            ir,
            image,
        )


def compile_source(source, filename="<input>", target=None):
    return Compiler(CFrontend(), target).compile(source, filename)


def compile_sources(sources, target=None, include_dirs=(), defines=()):
    """Compile ``[(filename, source), ...]`` as one linked C program."""
    frontend = CFrontend(include_dirs=include_dirs, defines=defines)
    return Compiler(frontend, target).compile_project(sources)


def _finalize_module(module, target):
    remove_unused_stack_initialization(module)
    for function in module.functions:
        remove_dead_values(function)
        verify(function)
        simplify_control_flow(function)
        verify(function)
        destruct(function)
    return generate(module, target)
