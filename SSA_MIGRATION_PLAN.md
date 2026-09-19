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
shaky foundation — and go further than the minimal fix. Rather than a
surgical patch (e.g. keying phi operands off predecessor label instead of
block index, which alone would have fixed the triggering bug), the decision
made after several rounds of "how big should this be" was to make
**block-based representation the compiler's primary IR everywhere**, not just
a derived view used temporarily inside the SSA passes:

- `BasicBlock` gets a persistent identity (every block, including the entry
  block and anonymous fallthrough blocks, gets a real synthetic label backing
  its identity — identity and the block's own jump-target name coincide, so
  no separate remapping table is needed).
- Phi operands key off predecessor identity instead of a freshly rebuilt CFG's
  positional index.
- `FunctionIR` stores a block list as its primary representation instead of a
  flat `Instruction` list.
- This threads through the **lowerer** (`middle/ir.py`'s `Lowerer`,
  `frontends/c/frontend.py`), every SSA pass (`construct`, `destruct`,
  `verify`, `sccp`, `hoist_loop_invariants`), and the **backend**
  (`targets/symphony/backend.py`, `targets/symphony/legalize.py`) — all of
  which currently assume a flat instruction list.
- New CFG mutation helpers (redirect edge, split edge/insert trampoline,
  remove block) centralize the exact bookkeeping that's caused every bug this
  session, so future structural passes (`simplify_control_flow`,
  `remove_dead_values`, inlining) use shared, correct primitives instead of
  hand-rolling instruction-list surgery each time.

This is a large, invasive rewrite touching parts of the compiler (the lowerer,
the backend) that were not broken and had nothing to do with the triggering
bug — a deliberate, explicit choice to fix the representation once rather than
patch around it repeatedly. Delegated to a background agent working in its own
worktree, staged with the test suite checked after each stage.

The in-progress `symphony/middle/ssa/inline.py` (destruct/construct approach)
was discarded; inlining is rebuilt as a true SSA-preserving clone once this
refactor lands (clone blocks with an identity map alongside the value map,
clone phis directly, no destruct/reconstruct round trip).

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

**Important caveat found after this refactor landed**: stable identity fixed
the *symptom* the triggering bug was diagnosed by (a phi reading a stale
predecessor after a CFG rebuild), but re-testing the original repro (nested
`while` with a dead `&&` join-value phi, one operand `None`) against a direct
`destruct()` → `construct()` round trip on the same function still fails
`verify()` — now with a clear label-based error instead of an index one, but
the same underlying cause. This was always two separate bugs layered
together: block-identity instability (now fixed) and `construct()`
re-promoting `destruct()`'s leftover copies for a *dead* value where one
predecessor path has no defining copy at all (not yet fixed). The suite is
still fully green because nothing in the real pipeline performs a repeated
destruct→construct round trip today — only inlining will. Fixing this is
`remove_dead_values`'s job, moved up to unblock inlining; see that item below.

## Tier 1 — hardest, migrate first
- [x] `remove_dead_values` (`symphony/middle/ssa/dce.py`, wired into
      `compiler.py` after `hoist_loop_invariants`) — moved up ahead of
      inlining to fix the destruct→construct round-trip bug above. Phi-aware:
      a phi's operands only count as uses when its own `dst` is required, so
      a dead phi (or a whole dead phi cycle) is prunable exactly like any
      other pure, unused instruction. Gated by an *allowlist* of provably
      pure ops (ported from the old pass's `PURE` set, plus `phi`), not a
      blocklist of known side-effecting ones — first attempt used a blocklist
      and silently deleted `init_text_screen` (a real side-effecting,
      `dst=None` backend-only op not in the enumerated list), regressing
      `test_printf_and_framebuffer_modes`; switching to the allowlist (an
      instruction whose op isn't recognized as pure is always kept,
      regardless of `dst`) fixed it and is categorically safer, since it can
      only ever be too conservative, never wrong. Added `tests/test_ssa.py`
      with direct destruct→construct round-trip regression coverage
      (including a repeated-round-trip test) so this bug class is caught by
      the suite going forward, not just by inlining's future use of it.
      Verified: full suite green on both ISAs, byte-identical failure set to
      the pre-existing baseline (diffed test-by-test, not just counted),
      zero regressions; all example programs compile and run identically on
      the native emulator (insertion_sort's instruction count even dropped
      slightly, confirming real dead code is being removed).
- [x] `inline_single_call_functions` (`symphony/middle/ssa/inline.py`, run
      once module-wide in `compiler.py` between the per-function
      construct/SCCP/hoist/DCE loop and the per-function verify/DCE/destruct
      loop) — a true SSA-preserving clone, no destruct/construct round trip.
      Candidate selection (non-recursive, exactly one direct call site,
      address never observable, not `_start`/a `startup`/`halt`-containing
      function) is ported from the old pipeline's heuristic unchanged. Each
      callee clone gets fresh value ids and block labels through two maps;
      phis are cloned directly (their `extra` re-keyed through the label
      map, same as any `jump`/`branch_if`/`label` target); a callee
      parameter still binds via a fresh caller-side `local_addr`+`store`
      into a memory-form local (matching `construct.py`'s own choice to
      leave parameters unpromoted, so nothing needs re-promoting after the
      splice); every cloned `return` becomes a `jump` to a fresh
      continuation block, merged by one phi keyed on each cloned return
      block's own (already-stable) label. Three real bugs found and fixed
      while building this, all now covered by `tests/test_ssa.py`'s
      `InlineTests`:
      1. Dead code after a callee's real `return` (the lowerer's own
         trailing `const 0; return 0;` idiom) still counted as a phantom
         predecessor of the continuation phi despite never running — fixed
         by pruning the callee to its reachable blocks (`prune_unreachable_blocks`)
         before cloning it.
      2. Cloning a function that had *itself* already had a callee inlined
         into it (e.g. `add` inlined into `main`, then `main` inlined into
         `_start`) produced an unresolvable jump target, because the
         cloned copies of the `label` instructions synthesized by the
         *first* inlining were never re-keyed through the second clone's
         label map — `remap_extra` only handled `phi`/`jump`/`branch_if`,
         not `label`. This is also why candidates are settled bottom-up
         (innermost candidate inlined first, so a function is only ever
         cloned once it contains no further pending candidate calls) rather
         than in whatever order `module.functions` happens to list them.
      3. Splitting the call site's own block into a `before`/`continuation`
         pair changes which block is the real CFG predecessor for anything
         *downstream* of the original call site (e.g. an existing phi
         elsewhere in the same function that already listed the original,
         now-split block as one of its predecessors) — the terminator (and
         therefore predecessor identity) moved onto the new continuation
         block. Fixed by rewriting every such phi's predecessor label from
         the original block to the continuation after every splice.
      Verified: full suite failure count went from the 31-failure baseline
      to 32 (one new failure, a heap-allocation RAM-fit test whose binary
      grew past its fixed test RAM size) — traced directly to inlining
      duplicating callee bodies (e.g. `malloc`/`__dyn_heap_take_free`) with
      no dead-code/unreachable-function cleanup pass to claw the size back
      down yet; this is expected and exactly why `remove_unreachable_functions`
      and friends are scheduled after inlining in the old pipeline's own
      fixed-point loop (see Tier 3). Confirmed via a stash-based before/after
      comparison that this size growth is caused by inlining specifically
      (e.g. `examples/bigprime.c` grew from 35088 to 63304 bytes with
      inlining wired in, unchanged otherwise), not a pre-existing issue.
      No other diffs against the baseline failure set. Added 4 new tests to
      `tests/test_ssa.py::InlineTests`, each compiling through the real
      pipeline (construct → per-function opts → inline → destruct →
      backend) and running on the native-preferring emulator: a basic
      single-call-site inline, a chain of two candidates (regression
      coverage for bug 2/bottom-up ordering), a recursive function
      (confirms it's correctly never selected), and a multi-call-site
      function (confirms it's correctly never selected either).
- [x] `simplify_control_flow` + `thread_jumps` (`symphony/middle/ssa/simplify_cfg.py`)
      — prunes unreachable blocks and redirects explicit edges through pure
      jump trampolines directly on SSA. Each redirect copies the trampoline's
      existing phi operand onto the new predecessor; unreachable-block pruning
      then removes operands belonging to deleted predecessors. A trampoline is
      deliberately retained when it distinguishes two edges from the same
      source carrying different phi values, since collapsing those edges would
      make the distinction unrepresentable by an ordinary block phi. Added a
      direct regression for redirect -> prune -> phi cleanup. Stabilization of
      this stage also added regressions and fixes for three pre-existing bugs
      exposed by structural CFG work: parallel-copy destruction used the
      dependency direction backwards, the verifier accepted a same-block use
      before its definition, and inlining a parameterized callee whose entry
      was a loop header either duplicated/skipped parameter binding or repeated
      it on every back edge. Parameter binding now uses a distinct one-shot
      entry block. Finally, the backend continues serializing reachable labeled
      blocks laid out after `_start`'s `halt`, fixing an undefined appended
      critical-edge trampoline in the self-host build. Focused SSA suite: 11/11
      green on both ISAs; the ordinary Dynphony suite retains the same 32 known
      missing-optimization failures as the pre-stabilization baseline, with no
      new failing test names.
- [x] `reduce_induction_strength` (`symphony/middle/ssa/induction.py`) —
      recognizes a basic induction variable directly from a loop-header phi,
      its one preheader seed, and constant-step values on every back edge.
      A loop expression `base + i` becomes its own derived phi: compute
      `base + i.entry` once in the preheader and update the derived value on
      each back edge using that edge's proven `+`/`-` constant operation.
      This is more general than the mutable-IR version: it needs neither one
      update site nor an update that dominates every use, and naturally
      supports multiple back edges. Copy and representation-preserving cast
      chains are resolved while matching, since those remain until the later
      global-copy pass. Regressions cover the exact derived-phi shape, a real
      C loop running end to end, and the easily-miscompiled `i + (-1)` case.
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
