# SSA optimization migration plan (temporary — delete once migration is complete)

Tracks the order we're porting `symphony/middle/passes/pipeline.py`'s optimizations
onto persistent SSA (branch `ssa-no-opt`), one at a time, full suite green after each.
Hardest/riskiest first, so the highest-risk bugs surface while the harness is
still simple. Check items off with commit hashes as they land.

Status legend: `[ ]` not started, `[~]` in progress, `[x]` done (commit hash).

## Done

- [x] Stage 1: construct/destruct/verify, zero optimizations (`eb6f36c`)
- [x] SCCP (`1d13c89`) — real edge-feasibility + phi-to-copy collapse "for free"
- [x] `hoist_loop_invariants` (`symphony/middle/ssa/hoist.py`, wired into
      `compiler.py`) — ported from the loop-passes branch onto real SSA. The
      "exactly one definition" check the old pass needed (to rule out
      mem2reg-promoted slots reassigned on multiple paths) disappears entirely,
      since every SSA value already has one definition by construction; the
      invariance test is now a direct dominance query via the new
      `find_natural_loops` in `analysis/dominance.py`. Found and fixed a real
      latent bug in `DominatorTree.dominates()` along the way: it infinite-looped
      whenever asked whether a non-ancestor dominates a block on the path back to
      the entry, because the entry's self-referential `idom[0] = 0` sentinel
      never triggered the "not found" exit — fixed by detecting the idom chain
      reaching a fixed point (`self.idom[b] == b`) without hitting `a`. This bug
      was latent since stage 1 (`verify.py` only ever calls `dominates` in the
      direction that's always true or skipped for unreachable blocks) and would
      have hung any future pass that queries dominance the other way. Verified:
      full suite green on both ISAs (same 31 pre-existing shape/tiny-RAM
      failures, zero regressions), confirmed hoisting actually moves computation
      out of a loop body via IR dump inspection, all demo programs still compile
      and run correctly on the native emulator.

## Tier 1 — hardest, migrate first
- [ ] `inline_single_call_functions` — splices an entire callee CFG into the
      caller mid-stream, remaps values/labels, must synthesize a new phi at the
      continuation point merging the callee's multiple returns. Structurally
      "run construction again, by hand, on spliced code" — hardest single pass.
- [ ] `simplify_control_flow` + `thread_jumps` (same family) — deletes/merges/
      redirects blocks and edges. Every deletion must rewrite `phi.extra`'s
      `(predecessor_index, value)` pairs; block-index renumbering is the exact
      bug class that already bit `construct.py`, `destruct.py`, and `sccp.py`
      once each (see commit `1d13c89` fix and prior stage-1 fixes).
- [ ] `remove_dead_values` — needs phi-awareness: phi operands are uses, and a
      wholly-dead phi (including dead phi cycles) should be prunable.
- [ ] `reduce_induction_strength` — induction-variable recognition is a phi
      pattern (self-referential phi with a constant per-iteration step); real
      SSA should make identifying induction variables far more direct than
      re-deriving them from a mutable loop counter each iteration.
- [ ] `eliminate_redundant_loop_memory` — loop-invariant load/store elimination;
      depends on the same natural-loop/dominance infrastructure as hoisting,
      do it right after `hoist_loop_invariants` while that machinery is fresh.

## Tier 2 — moderate, mostly free/simpler on SSA

- [ ] `propagate_global_copies` — nearly degenerates into "resolve copy/cast
      chains once" on SSA (every value has one definition already).
- [ ] `propagate_and_fold` — most of its barrier/alias-invalidation machinery
      exists only to cope with non-SSA mutable locals; should shrink a lot.
- [ ] `fuse_comparison_branches` — local peephole, doesn't touch CFG shape,
      should port nearly unchanged.

## Tier 3 — easy, mechanical

- [ ] `strength_reduce`, `simplify_algebra` — pure per-instruction rewrites, no
      CFG/phi interaction.
- [ ] `eliminate_tail_calls`, `lower_self_tail_calls_to_loops` — self-contained,
      single-function.
- [ ] `promote_readonly_parameters` — check whether `construct.py`'s mem2reg
      already subsumes this before porting.
- [ ] Module-level cleanup: `remove_unreachable_symbols`,
      `remove_unreachable_functions`, `fold_immutable_global_loads`,
      `remove_unused_stack_initialization` — orthogonal to SSA form, port last.

## Notes

- `reduce_induction_strength` and `eliminate_redundant_loop_memory` currently
  live only on the loop-passes work (see `project_dyncc_optimizer_roadmap`
  memory), not on `ssa-no-opt`. Porting them here means re-implementing
  against real SSA, not copying the existing mutable-IR versions verbatim.
- Delete this file once every item above is checked off and merged.
