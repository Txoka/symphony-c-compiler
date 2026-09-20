# Optimization roadmap

This tracks the standard optimization work proposed for Symphony C. Checked
items are implemented. Partially checked sections describe the
working subset and the remaining work explicitly.

## Current priorities

Completed foundations:

- [x] Fuse comparisons directly into conditional branches.
- [x] Inline small leaf functions, including intrinsic-only helpers such as `move_one`.
- [x] Use immediate static calls for already-laid-out low fixed addresses.
- [x] Keep straight-line intrinsic functions in incoming/caller-saved registers.
- [x] Allocate values that die at calls in `r3`-`r6` without callee-save traffic.
- [x] Perform ABI argument placement as a parallel assignment, breaking register cycles safely.
- [x] Eliminate safe tail calls after restoring the current frame.
- [x] Emit known-symbol calls directly from lowering and retain a separate indirect-call form.
- [x] Emit conditional branches with one explicit target and one CFG fallthrough edge.
- [x] Model promoted locals with transient SSA versions and phi joins during sparse analysis.
- [x] Run sparse conditional constant propagation across blocks and remove infeasible CFG edges.
- [x] Propagate immutable copy/cast chains across blocks without breaking mutable snapshots.
- [x] Legalize surviving software arithmetic into explicit target runtime calls, then rerun
  the global fixed point so ordinary reachability and inlining remove one-use wrappers.
- [x] Inline callees containing tail calls by preserving their outer continuation.
- [x] Convert safe direct self-tail recursion into parallel parameter updates and a loop backedge.
- [x] Apply the expanded no-growth identity set, including self-comparisons and division or
  remainder by one.
- [x] Remove unreachable globals together with dead initializer-relocation chains and
  functions referenced only by dead function-pointer data.
- [x] Fold typed big-endian loads from proven immutable scalar and array globals, including
  zero-addend symbolic pointer initializers, and rerun the global fixed point.

## Benchmark-derived performance priorities

The maintained native-emulator comparison in `docs/example-benchmarks.md`
shows that the completed SSA pipeline is consistently smaller than GCC and
already wins several large workloads, but GCC still wins cycles in particular
on `pi`, `primes`, `insertion_sort`, `dynamic_sensor_report`, and small
runtime-heavy programs. The remaining gap is primarily code generation and
loop quality, not another local constant-folding rule.

Implement the following in this order, measuring native termination steps and
preserving the full suite after each atomic change:

1. [ ] **SSA liveness and allocation.** Retain SSA value identity through
   allocation; compute block liveness/live intervals or interference; make
   loop-depth/use-frequency spill decisions; keep loop-carried pointers,
   bounds, and invariant results in callee-saved registers across calls.
2. [ ] **Conservative value numbering/CSE.** Reuse repeated arithmetic,
   address formation, and loads whose memory version is proven unchanged.
3. [ ] **Stronger loop optimization.** Extend existing LICM/basic induction
   work with pointer induction, pointer-limit exits, trip-count facts, and
   proven bulk-fill/copy idioms. Keep growth-oriented unrolling opt-in.
4. [ ] **Paired division/remainder.** Recognize same-operand `/` and `%` and
   lower them to one two-result divmod operation when purity and ordering make
   that valid.
5. [ ] **Post-allocation target peepholes.** Remove physical-register moves,
   redundant spills/reloads, and needless address materializations; iterate
   with branch/call relaxation.
6. [ ] **Costed interprocedural specialization.** Consider constant-argument
   specialization and multi-site inlining only under an explicit size/cycle
   profitability policy, after the preceding foundations expose their gains.

Do not treat a terminating program alone as a valid benchmark result: compare
its return value and externally visible output to the reference run. The GCC
`-O2` runtime `memset` self-recursion incident demonstrated why this check is
required.

The next milestone is one global fixed point containing only Tier 1 transformations:
surviving function bodies are never duplicated, and a transformation is kept
only when it preserves behavior without increasing final code size or runtime.
Every iteration includes both local/CFG and call-graph work; these are not two
one-shot phases.

Remaining work, in dependency and payoff order:

1. [ ] Add a bounded IR evaluator for side-effect-free calls and loops with known
   inputs. Give it explicit instruction, recursion-depth, and memory limits; reject
   device operations, volatile access, unknown calls, undefined operations, and
   writes outside private evaluator state. Model immutable global bytes and private
   local storage in the evaluator, then feed successful results back into the
   ordinary fixed point. Together with the completed global passes, this should collapse the
   constant demo to `mov r1, 146` plus halt.
2. [ ] Add general dead-storage and dead-allocation elimination. Remove unused
   fixed local storage, VLA `stack_alloc` operations, and other allocation-like
   setup when the object cannot be accessed or escape and the operation itself is
   not observable. Preserve required initialization, bound evaluation, cleanup,
   allocation-failure, overflow, and stack/heap-collision behavior; separate
   side-effectful evaluation from removable storage setup where necessary. Add
   regression tests for unused locals, arrays, VLAs, temporaries, side-effectful
   bounds/initializers, and binary-layout changes.
3. [ ] Identify natural loops, induction variables, and proven constant trip counts.
   Use these facts for loop-invariant code motion and bounded evaluation first;
   retain code-growing unrolling behind its explicit option.
4. [ ] Recognize matching quotient/remainder expressions with identical proven-pure
   operands and lower them to a two-result `divmod` IR operation. Preserve signed
   C semantics and reject intervening mutation, volatile access, and calls.
5. [ ] Add local value numbering, then global value numbering, using conservative
   alias invalidation for loads. This removes repeated arithmetic and address work.
6. [ ] Add block liveness and interference-based register/stack-slot reuse, followed
   by loop-depth spill costs and better caller-saved allocation.
7. [ ] Add the small post-allocation peephole pass and iterate it with branch/call
   relaxation. It should only remove artifacts requiring physical-register knowledge.
8. [ ] Add non-tail recursive fallthrough-call layout for a recursive function
   with exactly one external direct caller. Split the caller around that site,
   pre-push its known continuation, place the recursive function next, and let
   the first entry fall through while recursive entries keep calling the stable
   function label. Compare the ordinary-call and fallthrough layouts after all
   alignment and branch/call relaxation; apply it only when binary size does not
   increase and first-entry runtime cost decreases or remains equal. Reject it
   when the function address is observable, the call is indirect, code movement
   lengthens another executed path, PIC continuation materialization erases the
   saving, or stack-argument/frame cleanup cannot be preserved exactly. Add
   regression tests for fixed and PIC images, low and high addresses, stack-passed
   arguments, nested non-tail recursion, caller continuation execution, `sp`
   restoration, stable recursive entry labels, branch-distance thresholds, and
   a deliberately unprofitable layout that must remain unchanged.
9. [ ] Add flag liveness and profile/cost-guided block ordering after the preceding
    CFG and register foundations are stable.
10. [ ] Extend singleton function-pointer recognition through immutable global loads.
    Local single-target pointers already reduce to direct calls after copy propagation.
    Keep guarded multi-target devirtualization deferred because it can grow code.
11. [x] Implement the memory runtime, heap allocation, overflow-safe VLA sizing,
    and bidirectional heap/stack collision checks independently of optimizer correctness.

## Deferred and optional work

- [x] Guard dynamic stack allocation against zero/overflowed sizes, address
  wraparound, static data, and the live heap boundary. Invalid VLA allocation
  enters the named `_stack_overflow` loop; heap growth returns `NULL` when it
  reaches the live stack.
- [ ] Defer guarded multi-target function-pointer devirtualization. Continue using
  conservative address-taken reachability when a singleton target cannot be proven.
- [ ] Defer PIC startup self-relocation. If implemented later, place it behind an
  explicit non-default option and benchmark it against the current `r12`
  base-relative sequences. Symphony-family branches are absolute, not PC-relative, so
  code branch sites are candidates too. Patch sites must retain fixed width;
  account for the relocation table, patching code, startup time, program reentry,
  and writable-code assumptions.
- [ ] Consider specialization, multi-site inlining, and loop unrolling only behind
  an explicit speed/size policy because they can grow the image.
- [ ] Treat self-hosting as a separate language/runtime roadmap. Structs, an arena,
  and bootstrap stages are useful project goals, but they are not optimizer passes.

## 1. Control-flow graph

- [x] Construct explicit basic blocks from each function's linear serialized IR.
- [x] Record predecessor and successor edges.
- [x] Remove instructions after unconditional transfers through the next block boundary.
- [x] Remove unreachable blocks using graph reachability.
- [x] When a condition folds, remove its untaken CFG edge and delete the adjacent
  fallthrough block if it has no other reachable predecessor.
- [x] Thread jumps through blocks containing only labels and an unconditional jump.
- [x] Merge adjacent blocks when removing a jump to any immediately following label.
- [x] Choose the lexical fallthrough edge in IR and invert the condition when the
  true block follows, before rebuilding the CFG in the global fixed point.
- [ ] Reorder non-adjacent blocks using execution frequency and size costs.

## 2. Constant propagation and folding

- [x] Fold integer arithmetic and bitwise operations with constant operands.
- [x] Fold constant comparisons, shifts, unary operations, and casts.
- [x] Fold constant branches.
- [x] Remove arithmetic helpers when folding eliminates their last operation.
- [x] Propagate constants conservatively within basic blocks.
- [x] Propagate constants across blocks using data-flow information.

## 3. Copy propagation and algebraic simplification

- [x] Propagate safe copies within basic blocks.
- [x] Preserve snapshot semantics when a copied mutable value is later reassigned.
- [x] Remove redundant same-width casts.
- [x] Simplify multiplication by zero, one, and powers of two.
- [x] Simplify unsigned division and remainder by powers of two.
- [x] Add the core identity set, including `x + 0`, `x - 0`, `x & 0`,
  `x | 0`, `x ^ x`, and shifts by zero.
- [x] Combine immutable longer cast and copy chains across blocks while preserving
  snapshots of mutable promoted locals.

## 4. Promote locals to IR values

- [x] Promote scalar locals whose addresses do not escape.
- [x] Keep arrays, aliased objects, and address-escaping locals in memory.
- [x] Preserve assignments and loop-carried values with mutable virtual values.
- [x] Construct transient SSA versions for promoted locals during sparse analysis.
- [x] Model phi values at control-flow joins and lower the results back to ordinary
  IR before code generation.

## 5. Sparse conditional constant propagation

- [x] Implement SSA-based sparse conditional constant propagation.
- [x] Propagate constants through transient phi joins.
- [x] Discover executable edges while propagating values.
- [x] Remove blocks and edges proven unreachable.

## Whole-program globals and compile-time evaluation

- [x] Remove unreferenced globals from the module before binary layout.
- [x] Follow global-initializer relocations transitively, so a live pointer keeps
  its target alive while an unreachable pointer and target can both disappear.
- [x] Recompute PIC startup relocation work after global deletion.
- [x] Prove immutable globals conservatively using direct stores and address escapes.
- [x] Fold typed big-endian scalar and array loads from proven-immutable initialized data.
- [ ] Fold symbolic pointer initializers with nonzero addends; zero-addend pointers
  already become ordinary `global_addr` values and compound through the fixed point.
- [ ] Refine interprocedural alias summaries so passing an address to a known,
  non-writing callee does not unnecessarily disqualify immutable data.
- [ ] Evaluate bounded side-effect-free loops and calls with known arguments.
- [ ] Re-run SCCP, call-graph reachability, and dead-global elimination after each
  successful compile-time evaluation.

## 6. Register allocation across control flow

- [x] Keep up to four frequently used values in `r8`-`r11` across branches and loops.
- [x] Preserve allocated values across nested calls.
- [x] Save and restore only the callee-saved registers selected by each function.
- [ ] Retain SSA value identity through register allocation instead of lowering
  back to a move-heavy physical-register convention first.
- [ ] Compute block-level liveness.
- [ ] Allocate SSA values with a real linear-scan or interference-based allocator,
  selecting physical registers from operation and ABI constraints rather than
  funneling ordinary arithmetic through `r1` and `r2`.
- [ ] Build live intervals or an interference graph.
- [ ] Reuse registers for non-overlapping values.
- [ ] Insert spills and reloads from allocator decisions, then resolve phi values
  and edge copies with parallel-copy lowering.
- [ ] Use spill costs based on loop depth and use frequency.
- [ ] Prefer caller-saved registers for values that do not cross calls.
- [x] Prefer `r3`-`r6` for selected values whose live ranges do not cross calls.
- [x] Treat software multiply/divide/remainder operations as calls for liveness.
- [x] Stage mixed argument sources safely and resolve register-only argument permutations in parallel.

## 7. Common-subexpression elimination

- [ ] Reuse repeated arithmetic and address calculations.
- [ ] Add local value numbering within basic blocks.
- [ ] Add global value numbering after SSA construction.
- [ ] Reuse loads only when conservative alias analysis proves it safe.
- [ ] Invalidate memory expressions across stores, calls, and device intrinsics.

## 8. Loop optimization

- [ ] Identify natural loops and their nesting depth.
- [ ] Move loop-invariant calculations out of loops.
- [ ] Simplify induction variables.
- [ ] Detect constant trip counts.
- [ ] Remove redundant loop loads and stores.
- [ ] Recognize count-down loops when they are cheaper for the target ISA.

### GCC optimization techniques dyncc's own optimizer should adopt

Verified by compiling `examples/primes.c` (sieve core, printf stripped),
`examples/insertion_sort.c`, and `examples/pi.c`'s `div_small` (the
bignum digit-extraction loop) with the real GCC backend
(`gcc-backend/symphony-gcc/`) at `-O2` and reading the actual generated
`.s`, then comparing against what `symphony/middle/passes/pipeline.py`
currently does (read directly, not assumed). `pipeline.py` already has
real optimization work -- SSA-based SCCP, cross-block copy/constant
propagation, inlining, tail-call-to-loop conversion, dead-code/CFG
cleanup, and (`strength_reduce`, below section 3) a LIMITED strength
reduction that only replaces power-of-two multiply/divide/modulo by a
*constant* operand with shifts/masks. It has **no loop pass of any
kind** today (confirmed: no `loop`, `induction`, or `invariant` hits
anywhere in `middle/passes/`) and **no pattern-matched runtime-call
substitution** (no `memset` reference anywhere in the pipeline). The
gap below is specifically what a real loop pass needs to close, not a
restatement of "GCC is more mature":

1. **Induction-variable address/pointer hoisting** (the single biggest,
   most broadly-applicable gap). In every one of the three examples
   inspected, GCC replaces `array[i]`-style address computation
   (`base + i * element_size`, which on this ISA needs an
   `add`+multiply-or-shift per access since there is no scaled-index
   addressing mode) with a pointer variable that is **incremented by
   the element size once per loop iteration** and carried as a live
   value across the whole loop. `primes.c`'s inner marking loop walks
   `composite[x]` via `add r12, r12, r10` (no re-derivation of
   `base+x`); `insertion_sort.c`'s `sort16` walks `values[index]` via
   `add r8, r8, 1`; `pi.c`'s `div_small` walks `a[i]` via
   `add r13, r13, 4`, with the loop's own exit test also rewritten from
   an index comparison (`i < N`) to a **pointer-limit comparison**
   (`cmp r8, r13` against a precomputed `a + N*4` computed once, so
   even the trip-count multiply happens only once, not per iteration).
   dyncc's optimizer has no equivalent: `promote_scalar_locals`
   promotes whole scalar locals to registers but has no notion of an
   induction variable or of turning a repeated indexed access into a
   loop-carried cursor. This is worth building before the others below
   since it is the mechanism that actually explains most of GCC's step-
   count win on loop-heavy code in the comparison table (see "dcc vs
   GCC comparison" in `gcc-backend/symphony-gcc/README.md` --
   `arena_allocator.c` at 0.50x GCC/dcc steps, `primes.c` at 0.10x,
   `pi.c` at 0.20x, `bigprime.c` at 0.27x -- all loop-dominated
   programs where GCC's `-Os` output takes a fraction of dcc's step
   count despite dcc's much smaller binary).
2. **Loop-invariant hoisting of repeated multiplies**, closely related
   to (1) but distinct: `primes.c`'s outer sieve loop computes `p * p`
   via a real `link_call __mulsi3` (this ISA has no hardware multiply)
   exactly ONCE per outer-loop iteration, not once per inner-loop
   iteration and not re-derived from scratch each time `p` changes --
   the inner marking loop that walks `composite[p*p], composite[p*p+p],
   ...` never calls `__mulsi3` again, it only uses the hoisted value
   plus pointer increments from (1). A generic loop-invariant-code-
   motion pass (recognize that `p * p`'s operands don't change within
   the inner loop, so the multiply can't legally move there anyway --
   the real win is recognizing it does not need RE-computing on every
   OUTER iteration beyond the one dependency on `p`) would subsume
   this, but note it specifically interacts with the existing
   `strength_reduce` pass: on a target with no hardware multiply, every
   softwareized `__dyn_mul`/`link_call __mulsi3` is call-costly (dyncc
   already treats software multiply/divide/remainder as a call for
   liveness purposes per section 6 above), so hoisting one out of an
   inner loop is a strictly bigger win here than it would be on a
   target with a hardware multiply instruction.
3. **`memset` (and, by the same mechanism, `memcpy`) pattern
   recognition** for whole-array constant-fill loops. `primes.c`'s
   `for (i = 0; i < N; i++) composite[i] = false;` and `pi.c`'s
   `zero()` (`while (i < N) { a[i] = 0; i = i + 1; }`, a differently-
   shaped loop over the same pattern -- confirmed both compile to the
   identical mechanism) both become a single `link_call memset` instead
   of an N-iteration store loop. This is the dominant, sometimes only,
   difference for any example with a bulk array-clear at the top of a
   hot function -- `arena_allocator.c`'s 0.50x steps ratio is
   consistent with this being a major contributor, since it's the
   smallest and least loop-heavy of the examples that still shows a
   large GCC win. A dedicated idiom-recognition pass (constant-value
   store to every element of a provably-contiguous array/slice, no
   aliasing escape, replace with a call to dyncc's own
   `runtime/heap.c`-family `memset`) would need no new IR primitive,
   just a new fixed-point pass alongside the existing dead-store/
   dead-allocation work already planned in item 2 of "Remaining work"
   above.
4. **Whole-function register allocation keeping loop-critical values
   resident across the entire loop nest**, not just within a call-free
   span. All three examples keep 4-5 loop-critical values (array base/
   cursor pointers, loop bounds, hoisted multiply results) resident in
   `r8`-`r12` for the full duration of the function, including across
   the `link_call __mulsi3`/`memset` calls inside the loop (since
   r8-r12 are callee-saved, a call inside the loop doesn't evict them).
   dyncc's own register allocation (section 6 above) already does real
   work -- `r3`-`r6` for values not crossing calls, `r8`-`r11` for
   values that persist across branches/calls, callee-saved
   push/pop scoped to what's actually used -- but it funnels ordinary
   arithmetic through `r1`/`r2` rather than running a real interference-
   graph or linear-scan allocator over SSA values (section 6's own
   still-open items), so it can't reliably keep AS MANY concurrently-
   live loop values resident as GCC's allocator does once a loop has
   4+ genuinely live cross-iteration values (exactly the shape all
   three examples above have). This item is really section 6's
   existing "Build live intervals or an interference graph" /
   "Allocate SSA values with a real linear-scan or interference-based
   allocator" work, called out here again because loop bodies are where
   the register-pressure payoff is largest.

None of this changes dyncc's zero-runtime-overhead story (see the
"dcc vs GCC comparison" section 3-style writeup in
`gcc-backend/symphony-gcc/README.md`'s "Reading the results" paragraph)
-- these are all pure code-quality wins inside a function body, not
anything that requires linking in a runtime/libgcc-style baseline. Item
3 (`memset`) is the one exception worth flagging: it does mean calling
into dyncc's own existing `runtime/heap.c`-family `memset`
implementation (already present, used today for `memcpy`/`memmove`
support), not adding a new runtime dependency.

## 9. Inlining

- [x] Relocate arbitrary non-recursive single-call-site functions without duplicating their bodies.
- [x] Inline small single-return leaf functions when they have one surviving call site.
- [x] Legalize surviving software arithmetic to explicit runtime calls and inline
  the `__dyn_udiv` and `__dyn_umod` wrappers when they have one surviving use.
- [x] Re-run constant propagation, CFG cleanup, reachability, and dead-code elimination after inlining.
- [x] Convert tail calls inside relocated callees back into calls to the outer
  continuation, then rerun tail-call and CFG cleanup in the global fixed point.
- [ ] Limit recursive and mutually recursive inlining.

## 10. Backend relaxation and peephole optimization

Keep a small symbolic machine-instruction form through register allocation and
layout. Run peephole cleanup there, then perform final relaxation and byte
encoding. The peephole pass handles backend artifacts only; semantic rewrites
belong in IR. Every rewrite must preserve flag behavior as well as register and
memory behavior.

- [x] Use immediate backward jumps when a fixed target fits 16 bits.
- [x] Convert low forward unconditional jumps to one executed immediate jump while preserving layout.
- [x] Remove jumps to the immediately following label.
- [x] Remove an unnecessary branch to the final function epilogue.
- [x] Avoid saving unused callee-saved registers.
- [x] Relax forward branches without retaining padding.
- [x] Use immediate static calls for known backward/already-laid-out targets that fit 16 bits.
- [x] Relax forward calls to immediate form after final layout.
- [x] Lower the branch direction and fallthrough already selected by IR without
  repeating that optimization in the backend.
- [ ] Coalesce redundant register moves.
- [ ] Eliminate redundant reloads after register allocation.
- [ ] Remove identity moves such as `mov r1, r1` when flags and observable state
  are unchanged.
- [ ] Fold adjacent backend-generated moves and address materializations when the
  shorter sequence has identical flag behavior.
- [ ] Remove redundant spill/reload pairs using physical-register and memory
  alias information.
- [ ] Iterate peephole cleanup and branch/call relaxation together. Branch/call
  sizes and label addresses already relax to a fixed point; the machine peephole
  pass is still missing.

## Runtime library and dynamic memory

- [x] Ship `memcpy`, `memmove`, `memset`, and `memcmp` as ordinary runtime C
  functions and include each one only when referenced.
- [x] Define the heap between the aligned end of static data and the descending
  stack, using configured RAM size rather than a hard-coded address.
- [x] Provide `malloc`, `free`, `calloc`, and `realloc` through a compact aligned
  free-list allocator with block splitting and adjacent-block coalescing.
- [x] Detect allocation failure and heap/stack collision without requiring a
  memory-management construct in the C language.
- [x] Retain the arena allocator as an optional specialized allocator and example,
  rather than requiring programs to paste it in for ordinary allocation.

## Optional loop unrolling

- [ ] Add an optimization-level and code-size policy before enabling unrolling.
- [ ] Fully unroll very small loops with proven constant trip counts.
- [ ] Optionally partially unroll larger fixed loops by factors such as two or four.
- [ ] Add an explicit `--unroll-loops` option for growth-oriented optimization.
- [ ] Disable growth-oriented unrolling in a future size-optimization mode.
- [ ] Benchmark partial unrolling of the 32-round division loop; keep it rolled by default.

## Recursion and tail calls

- [x] Eliminate direct self-tail calls when the caller has no addressable local object that may escape.
- [x] Snapshot tail-call arguments in parallel, rewrite safe self-tail recursion
  as a CFG backedge, and let single-caller relocation absorb the resulting loop.
- [x] Eliminate safe sibling tail calls under the same frame-lifetime constraint.
- [x] Perform parallel argument moves without clobbering inputs.
- [x] Combine tail-call elimination with inlining and control-flow cleanup in
  every global fixed-point iteration.

General non-tail recursion is intentionally outside the loop-conversion task: it
requires preserving pending call state, normally through the machine stack or an
equivalent explicit stack.
