"""Internal invariants of the persistent SSA layer (construct/destruct/verify),
independent of end-to-end compilation output. These exercise machinery that
no single C-source test would otherwise reach, such as repeated
destruct/construct round trips (what inlining will need to splice a callee's
body into a caller and rebuild SSA once).
"""

import unittest

from symphony.frontends.c import CFrontend
from symphony.middle.passes.pipeline import lower_intrinsics
from symphony.middle.analysis.cfg import prune_unreachable_blocks
from symphony.middle.ir import BasicBlock, FunctionIR, Instruction, ModuleIR
from symphony.middle.model import INT
from symphony.middle.ssa import (
    construct,
    verify,
    destruct,
    remove_dead_values,
    inline_single_call_functions,
    simplify_control_flow,
    reduce_induction_strength,
    sparse_conditional_constant_propagation,
    hoist_loop_invariants,
    eliminate_redundant_loop_memory,
    propagate_global_copies,
    simplify_algebra,
    fuse_comparison_branches,
    reduce_strength,
    promote_readonly_parameters,
    eliminate_tail_calls,
    lower_self_tail_calls_to_loops,
)
from symphony.middle.ssa.destruct import _sequentialize
from symphony.middle.ssa.verify import SSAVerificationError
from symphony.emulator import Machine, native_available, native_run
from symphony.targets.symphony import generate, Target
from symphony.targets.symphony.legalize import legalize_runtime_arithmetic


def _build(source):
    frontend = CFrontend()
    lowered = frontend.lower(source, "<input>")
    ir = lowered.ir
    for function in ir.functions:
        lower_intrinsics(function)
        construct(function)
        verify(function)
    return ir


class RoundTripTests(unittest.TestCase):
    def test_global_copy_propagation_resolves_cross_block_phi_operand(self):
        function = FunctionIR(
            "copies",
            [],
            [],
            [
                BasicBlock(
                    "entry",
                    [
                        Instruction("const", 0, (), INT, 0),
                        Instruction("copy", 1, (0,), INT),
                        Instruction("jump", extra="header"),
                    ],
                ),
                BasicBlock(
                    "header",
                    [
                        Instruction("phi", 2, (), INT, (("entry", 1), ("latch", 4))),
                        Instruction("copy", 3, (2,), INT),
                        Instruction("branch_if", None, (2,), INT, (False, "exit")),
                    ],
                ),
                BasicBlock(
                    "latch",
                    [
                        Instruction("const", 5, (), INT, 1),
                        Instruction("binary", 4, (3, 5), INT, "+"),
                        Instruction("jump", extra="header"),
                    ],
                ),
                BasicBlock("exit", [Instruction("return", None, (2,), INT)]),
            ],
            6,
        )
        verify(function)
        self.assertTrue(propagate_global_copies(function))
        verify(function)
        phi = function.blocks[1].instructions[0]
        self.assertEqual(dict(phi.extra)["entry"], 0)
        update = function.blocks[2].instructions[1]
        self.assertEqual(update.args[0], 2)

    def test_loop_memory_forwards_exact_loads_and_removes_overwritten_store(self):
        function = FunctionIR(
            "memory",
            [],
            [],
            [
                BasicBlock(
                    "entry",
                    [
                        Instruction("const", 0, (), INT, 1),
                        Instruction("jump", extra="header"),
                    ],
                ),
                BasicBlock(
                    "header",
                    [
                        Instruction("branch_if", None, (0,), INT, (False, "exit")),
                    ],
                ),
                BasicBlock(
                    "body",
                    [
                        Instruction("local_addr", 1, (), INT, "slot"),
                        Instruction("const", 2, (), INT, 7),
                        Instruction("store", None, (1, 2), INT),
                        Instruction("load", 3, (1,), INT),
                        Instruction("load", 4, (1,), INT),
                        Instruction("const", 5, (), INT, 9),
                        Instruction("store", None, (1, 5), INT),
                        Instruction("jump", extra="header"),
                    ],
                ),
                BasicBlock("exit", [Instruction("return", None, (0,), INT)]),
            ],
            6,
        )
        verify(function)
        self.assertTrue(eliminate_redundant_loop_memory(function))
        verify(function)
        body = function.blocks[2].instructions
        self.assertEqual(sum(item.op == "store" for item in body), 1)
        self.assertEqual(
            [(item.op, item.args) for item in body if item.dst in (3, 4)],
            [("copy", (2,)), ("copy", (2,))],
        )

    def test_induction_strength_reduction_builds_derived_phi(self):
        function = FunctionIR(
            "induction",
            [],
            [],
            [
                BasicBlock(
                    "entry",
                    [
                        Instruction("const", 0, (), INT, 0),
                        Instruction("const", 1, (), INT, 1),
                        Instruction("const", 2, (), INT, 100),
                        Instruction("jump", extra="header"),
                    ],
                ),
                BasicBlock(
                    "header",
                    [
                        Instruction("phi", 3, (), INT, (("entry", 0), ("latch", 5))),
                        Instruction("binary", 4, (2, 3), INT, "+"),
                        Instruction("branch_if", None, (3,), INT, (False, "exit")),
                    ],
                ),
                BasicBlock(
                    "latch",
                    [
                        Instruction("binary", 5, (3, 1), INT, "+"),
                        Instruction("jump", extra="header"),
                    ],
                ),
                BasicBlock("exit", [Instruction("return", None, (4,), INT)]),
            ],
            6,
        )
        verify(function)
        self.assertTrue(reduce_induction_strength(function))
        verify(function)
        header = function.blocks[1]
        phis = [item for item in header.instructions if item.op == "phi"]
        self.assertEqual(len(phis), 2)
        self.assertEqual(next(item for item in header.instructions if item.dst == 4).op, "copy")
        derived_phi = next(item for item in phis if item.dst != 3)
        self.assertEqual({label for label, _ in derived_phi.extra}, {"entry", "latch"})

    def test_induction_recurrence_preserves_addition_of_negative_step(self):
        function = FunctionIR(
            "descending",
            [],
            [],
            [
                BasicBlock(
                    "entry",
                    [
                        Instruction("const", 0, (), INT, 4),
                        Instruction("const", 1, (), INT, -1),
                        Instruction("const", 2, (), INT, 100),
                        Instruction("jump", extra="header"),
                    ],
                ),
                BasicBlock(
                    "header",
                    [
                        Instruction("phi", 3, (), INT, (("entry", 0), ("latch", 5))),
                        Instruction("binary", 4, (2, 3), INT, "+"),
                        Instruction("branch_if", None, (3,), INT, (False, "exit")),
                    ],
                ),
                BasicBlock(
                    "latch",
                    [
                        Instruction("binary", 5, (3, 1), INT, "+"),
                        Instruction("jump", extra="header"),
                    ],
                ),
                BasicBlock("exit", [Instruction("return", None, (4,), INT)]),
            ],
            6,
        )
        self.assertTrue(reduce_induction_strength(function))
        verify(function)
        latch_updates = [
            item
            for item in function.blocks[2].instructions
            if item.op == "binary" and item.dst != 5
        ]
        self.assertEqual(len(latch_updates), 1)
        self.assertEqual(latch_updates[0].extra, "+")
        self.assertEqual(latch_updates[0].args[1], 1)

    def test_backend_emits_reachable_edge_block_after_halt_layout(self):
        start = FunctionIR(
            "_start",
            [],
            [],
            [
                BasicBlock(
                    "_start.entry",
                    [
                        Instruction("const", 0, (), INT, 0),
                        Instruction(
                            "branch_if", None, (0,), INT, (True, "_start.ssa_edge1")
                        ),
                    ],
                ),
                BasicBlock(
                    "_start.halt",
                    [
                        Instruction("label", extra="_start.halt"),
                        Instruction("const", 1, (), INT, 0),
                        Instruction("halt", None, (1,), INT),
                    ],
                ),
                BasicBlock(
                    "_start.ssa_edge1",
                    [
                        Instruction("label", extra="_start.ssa_edge1"),
                        Instruction("jump", extra="_start.halt"),
                    ],
                ),
            ],
            2,
        )
        image = generate(ModuleIR([], [start]), Target())
        self.assertIn("_start.ssa_edge1", image.symbols)

    def test_jump_threading_removes_stale_phi_predecessor(self):
        function = FunctionIR(
            "thread",
            [],
            [],
            [
                BasicBlock(
                    "entry",
                    [
                        Instruction("const", 0, (), INT, 0),
                        Instruction("const", 1, (), INT, 10),
                        Instruction("branch_if", None, (0,), INT, (True, "trampoline")),
                    ],
                ),
                BasicBlock(
                    "other",
                    [
                        Instruction("const", 2, (), INT, 20),
                        Instruction("jump", extra="join"),
                    ],
                ),
                BasicBlock("trampoline", [Instruction("jump", extra="join")]),
                BasicBlock(
                    "join",
                    [
                        Instruction(
                            "phi", 3, (), INT, (("other", 2), ("trampoline", 1))
                        ),
                        Instruction("return", None, (3,), INT),
                    ],
                ),
            ],
            4,
        )
        verify(function)
        self.assertTrue(simplify_control_flow(function))
        verify(function)
        self.assertEqual([block.label for block in function.blocks], ["entry", "other", "join"])
        phi = function.blocks[-1].instructions[0]
        self.assertEqual(phi.extra, (("entry", 1), ("other", 2)))

    def test_parallel_copy_sequentialization_preserves_old_values(self):
        def execute(pairs, sequence):
            values = {1: "old1", 2: "old2", 3: "old3"}
            expected = dict(values)
            for destination, source in pairs:
                expected[destination] = values[source]
            for destination, source in sequence:
                values[destination] = values[source]
            return values, expected

        fresh = iter(range(10, 20))
        for pairs in (((1, 2), (2, 3)), ((1, 2), (2, 1))):
            with self.subTest(pairs=pairs):
                sequence = _sequentialize(pairs, lambda _source: next(fresh))
                actual, expected = execute(pairs, sequence)
                self.assertEqual(actual[1], expected[1])
                self.assertEqual(actual[2], expected[2])

    def test_verifier_rejects_same_block_use_before_definition(self):
        function = FunctionIR(
            "invalid",
            [],
            [],
            [
                BasicBlock(
                    "invalid.entry",
                    [
                        Instruction("copy", 1, (2,), INT),
                        Instruction("const", 2, (), INT, 7),
                        Instruction("return", None, (1,), INT),
                    ],
                )
            ],
            3,
        )
        with self.assertRaisesRegex(SSAVerificationError, "used before"):
            verify(function)

    def test_destruct_construct_round_trip_on_nested_loop_with_dead_join(self):
        """A dead &&/||/?: join-value phi with a None operand on one edge
        (nothing ever reaches it with a defined value on that path, since
        nothing reads the phi's result there either) must not survive
        destruct() and confuse a second construct() call -- regression test
        for the bug that triggered the stable-block-identity refactor: see
        SSA_MIGRATION_PLAN.md."""
        ir = _build(
            """
            int f(int n) {
                int i = n;
                int j = 0;
                while (j < n) {
                    while (i > 0 && j > 0) {
                        i = i - 1;
                    }
                    j = j + 1;
                }
                return i + j;
            }
            int main(void) { return f(3); }
            """
        )
        f = next(fn for fn in ir.functions if fn.name == "f")
        destruct(f)
        prune_unreachable_blocks(f)
        remove_dead_values(f)
        construct(f)
        verify(f)  # must not raise

    def test_repeated_round_trip_is_stable(self):
        """Two consecutive destruct/construct cycles on the same function
        (closer to what a fixed-point optimizer loop would do) stay valid."""
        ir = _build(
            """
            int f(int n) {
                int total = 0;
                for (int i = 0; i < n; i++) {
                    if (i % 2 == 0 && i > 0) {
                        total += i;
                    }
                }
                return total;
            }
            int main(void) { return f(10); }
            """
        )
        f = next(fn for fn in ir.functions if fn.name == "f")
        for _ in range(2):
            destruct(f)
            prune_unreachable_blocks(f)
            remove_dead_values(f)
            construct(f)
            verify(f)  # must not raise on either pass


def _run(source, ram=1 << 16):
    """Compile via the real pipeline (construct/opts/inline/destruct) and run
    on the native-preferring emulator, returning the halt return value."""
    frontend = CFrontend()
    lowered = frontend.lower(source, "<input>")
    ir = lowered.ir
    for function in ir.functions:
        lower_intrinsics(function)
        construct(function)
        verify(function)
        sparse_conditional_constant_propagation(function)
        verify(function)
        propagate_global_copies(function)
        verify(function)
        simplify_algebra(function)
        verify(function)
        propagate_global_copies(function)
        verify(function)
        hoist_loop_invariants(function)
        verify(function)
        reduce_induction_strength(function)
        verify(function)
        fuse_comparison_branches(function)
        verify(function)
        reduce_strength(function)
        verify(function)
    inline_single_call_functions(ir)
    for function in ir.functions:
        verify(function)
        promote_readonly_parameters(function)
        verify(function)
        eliminate_tail_calls(function)
        verify(function)
        lower_self_tail_calls_to_loops(function)
        verify(function)
        remove_dead_values(function)
        verify(function)
        destruct(function)
    legalize_runtime_arithmetic(ir)
    image = generate(ir, Target())
    machine = Machine(image.binary, ram)
    halt = image.symbols["_halt"]
    if native_available():
        return native_run(machine, halt)
    return machine.run(halt)


class InlineTests(unittest.TestCase):
    def test_single_call_site_function_is_inlined_and_runs_correctly(self):
        self.assertEqual(
            _run(
                """
                int add(int a, int b) { return a + b; }
                int main(void) { return add(3, 4); }
                """
            ),
            7,
        )


class OptimizationTests(unittest.TestCase):
    def test_self_tail_recursion_becomes_ssa_loop(self):
        source = """
            int sum(int n, int total) {
                if (n == 0) return total;
                return sum(n - 1, total + n);
            }
            int main(void) { return sum(4, 0); }
        """
        self.assertEqual(_run(source), 10)
        ir = _build(source)
        function = next(item for item in ir.functions if item.name == "sum")
        promote_readonly_parameters(function)
        eliminate_tail_calls(function)
        self.assertTrue(lower_self_tail_calls_to_loops(function))
        verify(function)
        self.assertFalse(any(
            item.op == "direct_tailcall"
            for block in function.blocks for item in block.instructions
        ))
        self.assertTrue(any(item.op == "phi" for item in function.blocks[1].instructions))

    def test_adjacent_direct_call_and_return_becomes_tailcall(self):
        function = FunctionIR("tail", [], [], [BasicBlock("entry", [
            Instruction("const", 0, (), INT, 4),
            Instruction("direct_call", 1, (0,), INT, "next"),
            Instruction("return", None, (1,), INT),
        ])], 2)
        verify(function)
        self.assertTrue(eliminate_tail_calls(function))
        verify(function)
        self.assertEqual(function.blocks[0].instructions[-1].op, "direct_tailcall")

    def test_readonly_parameter_is_promoted_to_dominating_ssa_value(self):
        ir = _build("int f(int x) { return x + 1; } int main(void) { return f(4); }")
        function = next(item for item in ir.functions if item.name == "f")
        self.assertTrue(promote_readonly_parameters(function))
        verify(function)
        instructions = [item for block in function.blocks for item in block.instructions]
        self.assertTrue(any(item.op == "param" for item in instructions))
        self.assertFalse(any(item.op == "load" for item in instructions))

    def test_strength_reduction_rewrites_multiply_and_unsigned_divide(self):
        from symphony.middle.model import UINT
        function = FunctionIR(
            "strength",
            [], [],
            [BasicBlock("entry", [
                Instruction("param", 0, (), UINT, "x"),
                Instruction("const", 1, (), UINT, 8),
                Instruction("const", 2, (), UINT, 4),
                Instruction("binary", 3, (0, 1), UINT, "*"),
                Instruction("binary", 4, (3, 2), UINT, "/"),
                Instruction("return", None, (4,), UINT),
            ])], 5,
        )
        verify(function)
        self.assertTrue(reduce_strength(function))
        verify(function)
        operations = [item.extra for item in function.blocks[0].instructions if item.op == "binary"]
        self.assertEqual(operations, ["<<", ">>"])

    def test_comparison_branch_fusion_preserves_ssa_and_removes_temporary(self):
        function = FunctionIR(
            "fuse",
            [],
            [],
            [
                BasicBlock("entry", [
                    Instruction("param", 0, (), INT, "left"),
                    Instruction("param", 1, (), INT, "right"),
                    Instruction("binary", 2, (0, 1), INT, "<"),
                    Instruction("branch_if", None, (2,), INT, (True, "yes")),
                ]),
                BasicBlock("no", [Instruction("return", None, (0,), INT)]),
                BasicBlock("yes", [Instruction("return", None, (1,), INT)]),
            ],
            3,
        )
        verify(function)
        self.assertTrue(fuse_comparison_branches(function))
        verify(function)
        entry = function.blocks[0].instructions
        self.assertEqual(entry[-1], Instruction("cbranch_if", args=(0, 1), type=INT, extra=("<", "yes")))
        self.assertFalse(any(item.op == "binary" for item in entry))

    def test_algebra_simplifies_unknown_ssa_values_without_losing_validity(self):
        function = FunctionIR(
            "algebra",
            [],
            [],
            [BasicBlock("entry", [
                Instruction("param", 0, (), INT, "x"),
                Instruction("const", 1, (), INT, 0),
                Instruction("const", 2, (), INT, -1),
                Instruction("binary", 3, (0, 1), INT, "+"),
                Instruction("binary", 4, (3, 2), INT, "&"),
                Instruction("binary", 5, (4, 4), INT, "=="),
                Instruction("return", None, (5,), INT),
            ])],
            6,
        )
        verify(function)
        self.assertTrue(simplify_algebra(function))
        verify(function)
        instructions = function.blocks[0].instructions
        self.assertEqual(instructions[3].op, "copy")
        self.assertEqual(instructions[4].op, "copy")
        self.assertEqual((instructions[5].op, instructions[5].extra), ("const", 1))

    def test_source_loop_uses_derived_induction_recurrence_and_runs(self):
        source = """
            int sum_offsets(int count) {
                int total = 0;
                for (int i = 0; i < count; i++) total += 100 + i;
                return total;
            }
            int main(void) { return sum_offsets(4); }
        """
        ir = _build(source)
        function = next(item for item in ir.functions if item.name == "sum_offsets")
        sparse_conditional_constant_propagation(function)
        hoist_loop_invariants(function)
        before = sum(
            instruction.op == "phi"
            for block in function.blocks
            for instruction in block.instructions
        )
        self.assertTrue(reduce_induction_strength(function))
        verify(function)
        after = sum(
            instruction.op == "phi"
            for block in function.blocks
            for instruction in block.instructions
        )
        self.assertEqual(after, before + 1)
        self.assertEqual(_run(source), 406)


class InlineTestsContinued(unittest.TestCase):

    def test_chain_of_single_call_site_functions_inlines_bottom_up(self):
        """`inner` is only called by `middle`, which is only called by
        `main` -- both are candidates, and `inner` must be fully settled
        inside `middle`'s clone before `middle` itself gets cloned into
        `main`, or the continuation phi built for the outer splice ends up
        citing a predecessor label that no longer reaches it (the bug this
        pass's bottom-up ordering exists to avoid; see inline.py)."""
        self.assertEqual(
            _run(
                """
                int inner(int x) { if (x > 0) return x; return -x; }
                int middle(int x) { return inner(x) + 1; }
                int main(void) { return middle(-6); }
                """
            ),
            7,
        )

    def test_recursive_function_is_not_inlined(self):
        """A function that (transitively) calls back into itself must never
        be selected as a candidate, even with exactly one direct call site
        -- inlining it would recurse forever while cloning."""
        self.assertEqual(
            _run(
                """
                int fact(int n) { if (n <= 1) return 1; return n * fact(n - 1); }
                int main(void) { return fact(5); }
                """
            ),
            120,
        )

    def test_multiple_call_sites_are_not_inlined(self):
        self.assertEqual(
            _run(
                """
                int square(int x) { return x * x; }
                int main(void) { return square(3) + square(4); }
                """
            ),
            25,
        )

    def test_parameter_binding_precedes_loop_header_and_runs_once(self):
        """A callee whose entry block is also a loop header needs a distinct
        one-shot parameter-binding block. The loop back edge must neither
        skip the initial binding nor repeat it and reset the loop variable."""
        self.assertEqual(
            _run(
                """
                int count_down(int n) {
                    while (n) n = n - 1;
                    return n;
                }
                int main(void) { return count_down(3); }
                """
            ),
            0,
        )


if __name__ == "__main__":
    unittest.main()
