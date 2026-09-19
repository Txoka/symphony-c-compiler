"""Language/frontend-independent pipeline orchestration and public C shortcut."""

from dataclasses import dataclass
from .frontends.c import CFrontend
from .frontends.protocol import SourceFrontend
from .middle.passes.pipeline import lower_intrinsics
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
    eliminate_redundant_loop_memory,
    propagate_global_copies,
    simplify_algebra,
    fuse_comparison_branches,
    reduce_strength,
    identify_direct_calls,
    promote_readonly_parameters,
    eliminate_tail_calls,
    lower_self_tail_calls_to_loops,
    remove_unreachable_symbols,
    fold_immutable_global_loads,
    remove_unused_stack_initialization,
)
from .targets.symphony import generate, Target
from .targets.symphony.legalize import legalize_runtime_arithmetic


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
            reduce_strength(function)
            verify(function)
            hoist_loop_invariants(function)
            verify(function)
            reduce_induction_strength(function)
            verify(function)
            eliminate_redundant_loop_memory(function)
            verify(function)
            fuse_comparison_branches(function)
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
        remove_unused_stack_initialization(ir)
        for function in ir.functions:
            remove_dead_values(function)
            verify(function)
            simplify_control_flow(function)
            verify(function)
            destruct(function)
        return Compilation(
            frontend.parsed,
            frontend.typed,
            ir,
            generate(ir, self.target),
        )


def compile_source(source, filename="<input>", target=None):
    return Compiler(CFrontend(), target).compile(source, filename)


def compile_sources(sources, target=None, include_dirs=(), defines=()):
    """Compile ``[(filename, source), ...]`` as one linked C program."""
    frontend = CFrontend(include_dirs=include_dirs, defines=defines)
    return Compiler(frontend, target).compile_project(sources)
