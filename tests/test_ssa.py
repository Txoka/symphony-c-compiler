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
from symphony.middle.ssa import construct, verify, destruct, remove_dead_values


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


if __name__ == "__main__":
    unittest.main()
