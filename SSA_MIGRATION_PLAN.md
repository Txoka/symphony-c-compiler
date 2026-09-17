# SSA optimization migration plan (temporary — delete once migration is complete)

Tracks the order we're porting `symphony/middle/passes/pipeline.py`'s optimizations
onto persistent SSA (branch `ssa-no-opt`), one at a time, full suite green after each.
Hardest/riskiest first, so the highest-risk bugs surface while the harness is
still simple. Check items off with commit hashes as they land.

Status legend: `[ ]` not started, `[~]` in progress, `[x]` done (commit hash).

## Done

- [x] Stage 1: construct/destruct/verify, zero optimizations (`eb6f36c`)
- [x] SCCP (`1d13c89`) — real edge-feasibility + phi-to-copy collapse "for free"

## Tier 1 — hardest, migrate first

- [ ] `hoist_loop_invariants` (currently on `gcc-backend-real`/loop-passes branches,
      not yet on `ssa-no-opt` — needs porting over first). On real SSA, "is this
      value loop-invariant" reduces to "every operand is either a constant or
      defined outside the loop (dominates the loop preheader) or is itself
      already proven invariant" — a direct dominance query, no need to
      rediscover loop structure by re-walking back-edges per value. Natural loop
      detection still needed (back-edge -> dominator), but hoisting itself
      should get much simpler and more powerful (handles invariant chains
      through phis cleanly). Do this before `simplify_control_flow` touches
      loop-shaped CFGs, so if hoisting has bugs they aren't tangled with
      block-pruning bugs too.
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

- `hoist_loop_invariants`, `reduce_induction_strength`, and
  `eliminate_redundant_loop_memory` currently live only on the loop-passes
  work (see `project_dyncc_optimizer_roadmap` memory), not on `ssa-no-opt`.
  Porting them here means re-implementing against real SSA, not copying the
  existing mutable-IR versions verbatim.
- Delete this file once every item above is checked off and merged.
