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
- [x] Stable block identity (`b896352`, `9d4c5d5`) — see "Architecture change:
      stable block identity" below for the full writeup. `BasicBlock` now
      carries a persistent `label` assigned once at creation (every block has
      one, including the entry and previously-unlabeled anonymous fallthrough
      blocks); `FunctionIR.blocks` is the primary representation and
      `FunctionIR.instructions` is a compatibility property that flattens
      blocks to the legacy linear form (for anything, including the backend
      and `legalize.py`, that still wants a flat sequence) and re-splits on
      assignment. `construct.py`, `verify.py`, `sccp.py`, `hoist.py`, and
      `destruct.py` all key phi predecessors and block lookups off `label`
      instead of `block.index`/positional `cfg.blocks[i]`. Verified: full
      suite green on both ISAs (same 31 pre-existing failures, zero
      regressions), a targeted nested-loop/short-circuit/ternary stress
      program round-trips through construct→sccp→hoist→destruct→verify with
      identical block labels at every stage and produces the correct result
      on the native emulator, and all example programs still compile and run
      identically. This unblocks `inline_single_call_functions` as a true
      SSA-preserving clone (see the now-completed checklist immediately
      below).

## Architecture change: stable block identity

Started while implementing `inline_single_call_functions`. First attempt spliced
callee/caller by destructing both to flat IR, concatenating, and reconstructing
SSA once (since a phi's `extra` predecessor indices are only meaningful
relative to one specific `build_cfg()` call — see `verify.py` — so a cloned
callee's phis couldn't be copied over as-is without first rebuilding them
against the merged CFG). That destruct→construct round trip surfaced a real,
inlining-independent bug: a dead phi (defined, never used) whose own operand
is `None` on one predecessor edge (the lowerer's `&&`/`||`/`?:` join-value
pattern applied to a value nothing reads) round-trips incorrectly — `destruct`
correctly skips emitting a copy for the `None` edge, but the second `construct`
re-promotes the leftover copies as a "variable" and places a phi that ends up
reading a stale value id, breaking dominance. Patching this one bug (e.g. with
a dead-value sweep between destruct and construct) would just be the next in a
series of workarounds for the same underlying issue: **a phi's identity is
tied to CFG-build-relative block indices, not to a stable block/edge
identity**, so any pass that needs to reconstruct or repair SSA structurally
(not just rewrite values in place, like SCCP/hoisting do) runs into this.

Decision: stop and refactor now, before building more passes on top of the
shaky foundation. `BasicBlock` gets a persistent identity that survives
rewrites; phi operands key off block/edge identity instead of a freshly
rebuilt CFG's positional index; `construct`, `destruct`, `verify`, `sccp`,
`hoist_loop_invariants` get rewritten against it, and inlining gets rebuilt as
a true SSA-preserving transform (clone-with-remapping directly on SSA, using
real block identity to repair phis locally) instead of a destruct/construct
round trip. This is a bigger, riskier change touching everything in
`symphony/middle/ssa/` and `symphony/middle/analysis/`, done deliberately as
its own step rather than folded into inlining.

The in-progress `symphony/middle/ssa/inline.py` (destruct/construct approach)
is being discarded/rebuilt once the new representation lands.

- [x] Design + implement stable block identity for `BasicBlock`/`ControlFlowGraph`
      and label-keyed phi operands (`analysis/cfg.py`, `analysis/dominance.py`).
- [x] Rewrite `construct.py` to assign/consume identities instead of CFG-build
      positional indices for phi predecessors.
- [x] Rewrite `destruct.py` and `verify.py` against the same identities.
- [x] Re-verify `sccp.py` and `hoist.py` still work (rewritten against
      `block.label`/`cfg.by_label` in place of `block.index`/positional
      `cfg.blocks[i]`).
- [ ] Rebuild `inline_single_call_functions` as a true SSA-preserving clone:
      clone callee blocks with a block-identity map alongside the value map,
      clone phis directly (remapping through both maps), turn each callee
      `return` into a jump to the continuation, and synthesize one phi at the
      continuation keyed by the real predecessor identities — no
      destruct/construct round trip.

## Tier 1 — hardest, migrate first
- [ ] `inline_single_call_functions` — the stable-block-identity refactor
      above is done, so this is now unblocked: build it as a true
      SSA-preserving clone (see the checklist item under "Architecture
      change: stable block identity"), not the destruct/construct approach.
- [ ] `simplify_control_flow` + `thread_jumps` (same family) — deletes/merges/
      redirects blocks and edges. Every deletion must rewrite `phi.extra`'s
      `(predecessor_label, value)` pairs (`redirect_edge`/`remove_block` in
      `analysis/cfg.py` centralize this bookkeeping); labels are stable now,
      so this is no longer the block-index-renumbering bug class that used
      to bite `construct.py`, `destruct.py`, and `sccp.py` (see commit
      `1d13c89` fix and prior stage-1 fixes) — but phi operands for a
      removed predecessor still need explicit cleanup.
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
