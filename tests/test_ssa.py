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
from symphony.middle.ssa import (
    construct,
    verify,
    destruct,
    remove_dead_values,
    inline_single_call_functions,
)
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
    inline_single_call_functions(ir)
    for function in ir.functions:
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


if __name__ == "__main__":
    unittest.main()
