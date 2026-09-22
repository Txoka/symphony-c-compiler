# GCC hot-loop differential and next-branch plan

## Decision

The next optimization branch should be named:

`opt/gcc-hot-loop-codegen`

Its goal is to reduce executed instructions in `bigprime.c`, `pi.c`,
`insertion_sort.c`, and `dynamic_sensor_report.c` by improving hot-loop SSA
lowering, register allocation, and target cleanup. Static BSS startup is
explicitly out of scope: all measurements must compile dyncc with
`--bss=assume-zeroed`, and GCC size must count serialized text plus initialized
data only. Adding explicit zero bytes or relying on a zeroing loader is an
acceptable deployment trade-off, so neither compiler receives credit or blame
for static zero-fill startup.

This exception applies only to static BSS. A source-level `memset`, `calloc`, or
scratch-array initialization performed during normal execution is observable
program work and remains in the runtime measurement.

## Fresh baseline

These figures were regenerated after the div/mod, loop, fixed-point, and call
tower changes. Steps are native-emulator target instructions; sizes are
serialized load-image bytes with static BSS excluded.

| Program | dyncc bytes | dyncc steps | GCC `-Os` bytes | GCC `-Os` steps | GCC `-O2` bytes | GCC `-O2` steps | dyncc / GCC `-O2` |
|---|---:|---:|---:|---:|---:|---:|---:|
| `bigprime.c` | 7,060 | 512,181,658 | 8,926 | 259,276,837 | 13,354 | 185,733,013 | 2.76x |
| `pi.c` | 3,126 | 1,805,306,952 | 7,168 | 1,054,528,544 | 10,936 | 1,050,620,051 | 1.72x |
| `insertion_sort.c` | 516 | 3,552 | 5,028 | 1,500 | 8,232 | 1,462 | 2.43x |
| `dynamic_sensor_report.c` | 8,812 | 14,533 | 7,808 | 5,436 | 11,236 | 4,330 | 3.36x |

Dyncc remains substantially smaller than GCC on three programs, so this branch
may accept small, measured code growth when it buys a meaningful hot-path win.
Each such trade must remain behind its own toggle or policy slider and be
reported explicitly.

## Machine-code findings

The review compared current dyncc binaries with fresh same-ISA GCC `-Os` and
`-O2` assembly and decoded instruction streams. Full traces were collected for
insertion sort and the sensor report; the first two million executed
instructions were sampled for the two billion-scale workloads.

### Cross-program finding: moves and register placement dominate

Dyncc uses `r1`/`r2` mainly as backend operand staging registers and `r7` as an
address scratch, leaving fewer volatile registers available to the allocator.
Hot code consequently contains many physical `or dst,zr,src` moves around
otherwise simple ALU operations. In the first two million `pi` instructions,
789,302 (39.5%) are `or`; GCC `-O2` executes 275,962 (13.8%). Bigprime shows
864,627 dyncc `or` instructions versus 430,073 for GCC `-O2` in the same-size
sample. Loop-depth-unaware coloring also gives cold and hot values comparable
priority, and every unallocated SSA value receives a distinct stack slot.

CFG liveness, interference, and conservative copy coalescing already exist.
The next work is therefore not to build liveness again, but to make allocation
frequency-aware, expose more legal registers, reduce operand staging, reuse
noninterfering spill slots, and clean up remaining physical moves.

### `pi.c`

The sampled run spends 1,613,219 of its first two million instructions in
`div_small`'s inner 32-bit restoring-division loop. Dyncc uses roughly 18--22
instructions per bit: several copies, three shifts, comparison/branch,
optional subtract/set, and loop control. GCC expresses the same algorithm in
roughly 11--13 instructions by keeping `rem`, `x`, `q`, `d`, and the bit counter
in stable registers and using direct three-operand operations.

The outer 402-word loop also recomputes `base + i*4` for both load and store.
Scaled induction currently rejects every constant-bounded loop as if it were a
small evaluation/unrolling candidate. It also fails to see this particular
address because the unsigned index passes through a same-width signed cast
before the shift. Both restrictions need costed, semantics-preserving fixes.

### `insertion_sort.c`

The hot inner condition `position > 0 && values[position-1] > value` becomes a
loop-carried Boolean flowing through copies/joins and then a zero test. Between
the first guard and the actual element move, dyncc repeatedly materializes
0/1 values, including three-instruction full-width constructions of constants
0 and 1. GCC branches directly on the two predicates and maintains advancing
pointers. The dynamic opcode totals are 1,273 `or`, 322 shifts, and 476
branches for dyncc, versus 153 `or`, no materialization shifts in the hot
condition, and 254 branches for GCC `-O2`.

This confirms that the already-documented short-circuit predicate/phi fusion
is a top priority. Independent target cleanup must also replace comparison
materialization's generic `constant()` sequences with `cheap_constant()` and
remove physical self/copy moves.

### `dynamic_sensor_report.c`

The same Boolean problem appears in the inlined `merge_range` condition. Dyncc
materializes both sides of `a < middle && b < right`; GCC emits direct compares
and branches. Dyncc also repeatedly reconstructs `base + index*4` and spills
temporary next-index/address values in the merge loops, while GCC holds
advancing pointers and end pointers.

The full dyncc trace attributes 1,408 steps to runtime `memset`, 1,196 to the
inlined duplicate-removal `memmove`, 644 to `memcpy`, and roughly 4,200 across
the unsigned division helper's hot blocks. These are runtime operations, not
static BSS clearing. GCC calls the same kinds of helpers but generates tighter
loops. Dyncc's division helper intentionally uses the same variable-shift
algorithm family as libgcc, so allocator, predicate, and loop-code improvements
must be measured before considering another algorithm change.

### `bigprime.c`

The sampled hot regions are four-limb `sub`, `shr1`, `copy`, `cmp`, and
`addmod`. Dyncc repeatedly computes `base + i*4`, copies operands through
staging registers, and materializes carry/borrow comparisons as integer 0/1
values. GCC maintains pointer cursors and uses shorter conditional-value paths.

A direct experiment relaxing scaled induction's blanket constant-bound veto
reduced the complete run from 512,181,658 to 457,901,426 steps (54,280,232,
or 10.6%) while growing the image from 7,060 to 7,104 bytes. The other three
programs were unchanged in that experiment. This proves the transformation is
valuable, but the retained rule must use a real trip-count/cost decision rather
than simply enabling every fixed-bound loop; genuinely tiny loops can still
lose from cursor setup and register pressure.

## Prioritized implementation order

Every item must be independently toggleable. After each item, run both ISA test
suites, both selfhost suites, and the four-program before/after/fresh-GCC table
with loader-zeroed BSS. Also run the complete maintained example benchmark to
catch unrelated regressions.

1. **Hot-loop-aware allocation and direct operand use.** Weight definitions and
   uses by loop depth, keep loop-carried values/cursors/bounds in registers,
   permit `r1`, `r2`, and where safe `r7` as ordinary volatile homes, and teach
   emission to consume allocated operands without staging copies. Reuse stack
   slots for noninterfering spills. This attacks the largest measured common
   symptom in all four programs.
2. **Predicate paths without Boolean materialization.** Extend comparison and
   zero-test fusion through Boolean-preserving copies, phis, and short-circuit
   joins. Lower a branch-only comparison directly to target flags. Separately
   use cheap 0/1 constants when a Boolean value really escapes. This is expected
   to pay immediately in insertion sort, sensor merge loops, bigint carry/borrow,
   and the division helpers.
3. **Costed address recurrences and pointer exits.** Replace the blanket
   constant-bound veto with an actual trip-count/setup/pressure rule; retain the
   small-loop exclusion only when justified. See through representation-safe
   same-width signedness casts so `pi`'s 402-word loop becomes eligible. Reuse
   one cursor for repeated load/store addresses and form end pointers once.
   Preserve the measured `bigprime` win as the first acceptance case.
4. **Post-allocation machine cleanup.** Iterate a physical-register peephole
   with branch/call relaxation: remove self moves, redundant move chains,
   store/reload pairs to the same spill, repeated address materialization, and
   jumps to the next instruction. This must follow the allocator work so it
   removes residual artifacts rather than compensating for avoidable allocation.
5. **Runtime memory-loop lowering and helper code quality.** After items 1--4,
   remeasure `memset`, `memcpy`, `memmove`, and `__dyn_udivmod`. Add aligned
   word-at-a-time runtime loops, safe overlap direction, cursor/end-pointer
   lowering, and small constant-size inline forms when profitable. Do not count
   static BSS clearing, and do not replace the division algorithm unless a
   like-for-like machine-code comparison still shows an algorithmic gap.
6. **Costed inlining under measured loop pressure.** The current pressure guard
   is not a suitable global default. Enabling it changes the four targets as
   follows: `pi` improves by 7,724,819 steps and sensor by 430, but `bigprime`
   grows 624 bytes and slows by 22,192 steps; insertion sort is unchanged.
   Revisit candidate-specific profitability only after allocation changes alter
   the pressure landscape. Compare complete final images and runtime, rather
   than using the current seven-value proxy alone.
7. **Block layout and flag liveness.** Once predicate lowering is stable, choose
   hot fallthrough edges, remove avoidable unconditional backedge/exit jumps,
   and retain comparison flags across intervening instructions proven not to
   overwrite them.

## Acceptance target

The branch is successful only if it produces a material aggregate reduction in
steps across all four priority programs without a large unexplained regression
in any one of them. Optimize against GCC `-O2` runtime but continue reporting
`-Os`, because dyncc's size advantage is valuable. Every report must include
return values and externally visible output, not merely termination and step
counts.
