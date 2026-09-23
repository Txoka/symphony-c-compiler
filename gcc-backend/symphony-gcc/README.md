# Real Symphony/Dynphony GCC target

This is a **real** GCC backend for the Symphony/Dynphony ISA (`docs/isa.txt`) —
not the moxie-hijack comparison backend at `gcc-backend/dynphony/`
(branch `gcc-comparison-backend`). GCC emits genuine target assembly for
`symphony-elf` from its own `.md`/`.cc`/`.h` port; there is no ELF, no
real `as`, no `ld` — the resulting `.s` is consumed by this project's own
from-scratch assembler+linker (`tools/symphony_as.py`/`symphony_ld.py`,
DONE — see "Status" below) that produces a flat binary runnable on
`symphony/emulator`, the same shape dyncc's own backend already produces.

Triple: `symphony-elf`. Dynphony (the variable-length encoding of the
identical instruction set/RTL/ABI) is exposed via the `-mdynphony` target
option (`symphony.opt`'s `mdynphony`) rather than a second triple or
multilib — GCC's own codegen decisions never change between the two, only
the final byte encoding the assembler+linker produce (see "Dynphony
support" below for exactly what does and doesn't work in this mode
today).

## Why no ELF/gas/ld

Porting binutils (a full BFD backend) to a from-scratch ISA is a separate,
large project not worth taking on. GCC itself never needs `as`/`ld` to run
`-S` compiles; this target's `config.gcc` entry deliberately omits
`elfos.h` and any `gas=yes`/`gnu_ld=yes` setup (see the comment in
`gcc-src.patch`). `symphony.h` defines its own minimal `ASM_OUTPUT_*`
macros directly (the same approach `mmix-knuth-mmixware` uses upstream for
its own non-ELF object format).

## Directory layout

- `config/symphony/` — the real target port: `symphony.md` (machine
  description — every mnemonic/operand form here comes from
  `docs/isa.txt`/`symphony/targets/symphony/isa.py`, NOT moxie),
  `symphony.cc` (target hooks), `symphony.h` (target macros),
  `symphony.opt`/`symphony.opt.urls`, `symphony-protos.h`, `t-symphony`.
  These files are symlinked into a GCC source checkout's
  `gcc/config/symphony/` during the build (see below) — they are never
  copied, so edits here are picked up directly by a rebuild.
- `gcc-src.patch` — the two small changes needed *outside*
  `gcc/config/symphony/` in an upstream GCC checkout: a `symphony*)` case
  in `gcc/config.gcc` (cpu_type + `target_has_targetm_common=no`, mirroring
  moxie) and a `symphony-*-elf)` case (sets `tmake_file`, skips ELF setup),
  plus a `| symphony \` addition to top-level `config.sub`'s CPU
  recognition list. GCC's own source tree is never committed to this repo
  (per repo policy) — apply this patch to a fresh clone instead.

## Build recipe: source to a running binary on the emulator

This is the full, verified pipeline: GCC source tree → stage1 `xgcc`/`cc1`
→ real `libgcc.a` → assemble+link a C program (including the runtime
library below) → run it on `symphony/emulator`. Every step here has
actually been executed and its result actually run, not just inspected.

### 1. Get a GCC 14 source tree and apply the patch

The upstream `gcc.gnu.org` git server can be extremely slow to clone from
directly; the GitHub mirror is generally much faster for the same content.

```sh
git clone --depth 1 -b releases/gcc-14 https://github.com/gcc-mirror/gcc.git gcc-src
cd gcc-src
git apply /path/to/gcc-backend/symphony-gcc/gcc-src.patch
```

The patch touches three places: `gcc/config.gcc` (the `symphony*)` and
`symphony-*-elf)` cases), top-level `config.sub` (CPU recognition), and
`libgcc/config.host` (the `symphony*-*-elf*)` stanza that points libgcc's
own build at `libgcc/config/symphony/t-symphony` and clears `extra_parts`
— this target has no `crt*.o`/ELF-section convention of its own). All
three are required; the `libgcc/config.host` stanza in particular is easy
to miss since it lives outside `gcc/config/symphony/` and libgcc's build
fails with a generic "cannot compile" configure error without it, not an
obviously-related message.

### 2. Symlink the real target files into the GCC tree

```sh
mkdir -p gcc/config/symphony
for f in symphony.h symphony.cc symphony.md symphony.opt \
         symphony.opt.urls symphony-protos.h t-symphony; do
  ln -sf /path/to/gcc-backend/symphony-gcc/config/symphony/$f \
         gcc/config/symphony/$f
done

mkdir -p libgcc/config/symphony
for f in t-symphony lib2funcs.c; do
  ln -sf /path/to/gcc-backend/symphony-gcc/libgcc-config/symphony/$f \
         libgcc/config/symphony/$f
done
```

### 3. Configure + build stage1, then libgcc

```sh
mkdir -p ../build-stage1 && cd ../build-stage1
../gcc-src/configure \
  --target=symphony-elf \
  --prefix=$PWD/../install \
  --enable-languages=c \
  --without-headers --with-newlib \
  --disable-shared --disable-threads --disable-libssp \
  --disable-libquadmath --disable-libgomp --disable-libatomic \
  --disable-libstdcxx --disable-bootstrap --disable-nls \
  --disable-gcov \
  --with-insnemit-partitions=1
make -j$(nproc) all-gcc
```

**`--with-insnemit-partitions=1` is required, not optional.** GCC's
build system hardcodes `genemit`'s output into exactly
`NUM_INSNEMIT_SPLITS` (default 10) files via a Makefile rule
(`s-tmp-emit`) that fails outright (`tmp-emit-10.cc: No such file or
directory`) if `genemit` doesn't end up needing all 10 buckets — which it
never will for a target this small (~35 insn patterns total). Any small
value (1 is simplest) fixes it; this is a real, previously-undocumented
gotcha for small out-of-tree targets, not a target-file bug.

**`--disable-gcov` is required.** Without it, `all-target-libgcc` builds
`libgcc.a` successfully but then fails separately compiling `libgcov.a`'s
`__gcov_info_to_gcda` with the same "maximum number of generated reload
insns" ICE class described below (a 64-bit-arithmetic-adjacent codegen
gap, not specific to gcov). `bpf-*-*` already defaults `enable_gcov=no`
in libgcc's own `configure.ac`; this target needs the flag explicit.

**Important:** do not run `gcc/configure` directly inside the `gcc/`
build subdirectory to iterate faster — it works but skips the top-level
configure's host-library detection (`GMPLIBS`/`GMPINC` etc.), producing a
`cc1` that fails to *link* with "undefined reference to `mpfr_*`" even
though compilation succeeded. Always reconfigure and build from the
top-level `build-stage1` directory.

**`gcc/as` gotcha:** this target has no real assembler wired into the
GCC build (there is no binutils port — see "Why no ELF/gas/ld" above), so
`gcc/as` inside the build tree is GCC's own in-tree stub script, which
fails outright when actually invoked (it expects a bundled binutils
build). libgcc's own `configure` step needs a *working* `as` to pass its
compile-sanity check, so before building libgcc, replace `gcc/as` with a
tiny wrapper that shells out to the real assembler:

```sh
cat > gcc/as <<'EOF'
#!/bin/sh
exec python3 /path/to/gcc-backend/symphony-gcc/tools/symphony_as.py "$@"
EOF
chmod +x gcc/as
```

This file gets **overwritten every time `all-gcc` is rebuilt or the tree
is reconfigured** — reapply it after any such rebuild, right before
building libgcc or assembling anything.

```sh
make -j$(nproc) all-target-libgcc
```

(`symphony-elf-ranlib`, from `$PWD/../install/.../bin` or wherever the
build placed it, needs to be on `PATH` for the final `ar`/`ranlib`
archive step inside `all-target-libgcc` to succeed — add
`../install/symphony-elf/bin` or the equivalent `toolchain-bin` staging
directory to `PATH` before running this step.)

### 4. Validate codegen (milestone 3)

```sh
cd build-stage1
echo 'int add(int a,int b){return a+b;}' > /tmp/add.c
gcc/xgcc -Bgcc/ -S -O2 /tmp/add.c -o /tmp/add.s
cat /tmp/add.s
```

Expected output (matches `docs/isa.txt` / `symphony/targets/symphony/abi.py`
exactly — args in r1/r2, `add` is a real 3-operand ALU form, `link_return`
is the cheap leaf-function return convention using r13 as a hardware link
register):

```asm
	.text
	.global	add
add:
	add	r1, r1, r2
	link_return
```

### 5. Compile, link, and run a real program — the easy way

`tools/symphony_gcc_run.py` is a one-command build-and-run wrapper: given
a `.c` file, it drives xgcc, the assembler, and the linker (against
`libgcc.a` and, by default, the full runtime library), then runs the
result on the emulator and prints the outcome. This is the normal way to
use this toolchain day to day — no need to touch the `Linker`/`ObjectFile`
API directly unless you're extending the port itself.

```sh
export SYMPHONY_GCC_PREFIX=$PWD/build-stage1   # the build dir from step 3

python3 tools/symphony_gcc_run.py my_program.c              # -O0, runs main()
python3 tools/symphony_gcc_run.py my_program.c -O2           # optimized
python3 tools/symphony_gcc_run.py my_program.c -o out.bin    # also save the binary
python3 tools/symphony_gcc_run.py my_program.c --screen      # print the printf-family text screen after halt
```

It prints the step count and the return value (`r1` per the ABI) on
halt, e.g.:

```
halted after 116 steps, r1 (return value) = 0x2a (42)
```

Run `python3 tools/symphony_gcc_run.py --help` for the full option list
(entry symbol, `--no-runtime` for programs that don't define `main` or
use the runtime library, `--max-steps` for long-running programs — see
"Running the tests" above for why a large step count is normal, not a
hang).

### 6. Doing it by hand (the low-level API)

For anything `symphony_gcc_run.py` doesn't cover — linking multiple
program source files, controlling link order, or extending the port
itself — the underlying pipeline is `tools/symphony_as.py` (assembler)
and `tools/symphony_ld.py`'s `Linker`/`ObjectFile` classes (linker), used
directly:

```sh
gcc/xgcc -Bgcc/ -S -O0 my_program.c -o my_program.s
python3 tools/symphony_as.py my_program.s -o my_program.o
```

`symphony_ld.py` also has its own command-line entry point for linking
already-assembled objects and archives without writing any Python:

```sh
python3 tools/symphony_ld.py -o out.bin my_program.o \
    build-stage1/symphony-elf/libgcc/libgcc.a --entry main
```

Run the resulting flat binary via `symphony.emulator.machine.Machine`:

```python
from symphony.emulator.machine import Machine
binary = open("out.bin", "rb").read()
m = Machine(binary, load_address=0, symphony=True)
m.pc = entry_address       # from the linker's returned entry point
m.regs[14] = 0x800000      # stack pointer: top of a generous RAM region
m.regs[13] = 0xFFFFF0      # halt address: any address the program never jumps to
result = m.run(halt_address=0xFFFFF0, max_steps=200000)
```

`r1` (the return-value register per the ABI) holds `_start`'s return
value in `result` when the halt address is reached via `link_return`.
`gcc-backend/symphony-gcc/tests/toolchain.py`'s `Toolchain` class (what
`symphony_gcc_run.py` itself is built on) wraps this same sequence in a
reusable, tested API if you're writing more tooling or tests against
this pipeline.

## Running the tests

`gcc-backend/symphony-gcc/tests/test_gcc_backend.py` is a real, committed
pytest regression suite for this toolchain -- compiles real C through
`xgcc`, assembles/links with the real tools, runs the binary on
`symphony/emulator`, and asserts on the actual result. It replaces this
project's earlier ad hoc verification (compile/run by hand, check
output, throw it away): every bug documented below has a dedicated
regression test that reproduces its exact trigger shape.

It is **opt-in and skipped by default**, separate from the repo's fast
default suite (`tests/test_compiler.py`, `tests/test_integration.py`,
which need nothing but the Python package) -- running it for real needs
a fully built cross-compiler + libgcc + this project's custom assembler/
linker, i.e. the entire "Build recipe" above completed once (a real GCC
bootstrap, roughly 30-60 minutes from scratch).

To run it:

```sh
export SYMPHONY_GCC_PREFIX=/path/to/build-stage1   # the stage1 build dir from step 3 above
python3 -m pytest gcc-backend/symphony-gcc/tests/test_gcc_backend.py -v
```

`SYMPHONY_GCC_PREFIX` must point at the `build-stage1` directory itself
(the one `../gcc-src/configure --target=symphony-elf ...` was run from):
the tests look for `<prefix>/gcc/xgcc`, `<prefix>/gcc/as` (the assembler
wrapper script -- reapply it per the gotcha above if you rebuilt `all-gcc`
since last using it), and `<prefix>/symphony-elf/libgcc/libgcc.a`. Without
`SYMPHONY_GCC_PREFIX` set, every test in the module is auto-skipped and
the rest of the repo's test suite is unaffected.

What it covers: milestone-3-style sanity (trivial functions, loops, mixed
leaf/non-leaf calls, at `-O0`/`-O1`/`-O2`); real libgcc signed and
unsigned multiply/divide/modulo; `malloc`/`free`/`calloc`/`realloc`
correctness; all four `printf`/`printf1`/`printf2`/`printf3` arities with
real format strings, verified by reading back the emulator's text-screen
framebuffer; xfail tests documenting the known `calloc()`-at-`-O1`
reload ICE and the `__muldi3` link-time gap (a future fix flips these
green automatically instead of the gap silently going untracked; the
`-O2`/`__dyn_printf_unsigned()`-at-`-O1`/`-O2` xfails that used to be
here were fixed and flipped to ordinary passing tests -- see the
`*movsi_reg` memory-alternative fix, "Bug 4" in Known gaps below); and
one dedicated regression test per real bug fixed during this project
(see "Fixed bugs" above) -- frame-pointer placement,
missing callee-saved registers, the assembler pass1/pass2 desync, the
`__dyn_heap_anchor` BSS-ordering bug (plus a negative test for the
linker's stale-object check), r11-not-FIXED_REGISTERS, r13-not-
liveness-gated-in-leaf-functions, and the `*movsi_reg` missing-memory-
alternative bug (Bug 4 in Known gaps below).

`gcc-backend/symphony-gcc/tests/toolchain.py` is the reusable harness
(`Toolchain.build_and_run(c_source, tmp_path, ...)` compiles+assembles+
links+runs a C program in one call) if you're adding more tests.

## Key design points baked into the `.md`/`.cc`/`.h` port

- **7 argument/return registers (r1-r7), not 6.** The old
  `gcc-backend/dynphony/dynphony.cc` stub this replaces had
  `dynphony_arg_regs[] = {1..6}` — a real bug, fixed here
  (`symphony_arg_regs[]` in `symphony.cc`).
- **Frame pointer is r11, not r12** (r12 is the PIC-base register in the
  software ABI, a different role the old stub conflated). `symphony.h`'s
  `FRAME_POINTER_REGNUM`/`ARG_POINTER_REGNUM`/`HARD_FRAME_POINTER_REGNUM`
  all correctly use `R11_REGNUM`.
- **No base+offset addressing mode.** The real hardware only addresses
  memory via a bare register or a U16 immediate (`docs/isa.txt`) — no
  `reg+displacement` form exists. `symphony_legitimate_address_p` in
  `symphony.cc` deliberately rejects anything else, and every memory
  operand in `symphony.md` (`*load_si`, `*store_si`, etc.) requires its
  address to already be a plain register — `symphony_legitimize_address`
  (and `movsi`/`movhi`/`movqi`'s `define_expand`, and `call`/`call_value`'s
  `define_expand`) forces any computed address (a frame-relative offset,
  a function-pointer expression) into a register first via `force_reg`
  before it ever reaches an insn pattern. This is the ISA's real
  addressing model, not a simplification for GCC's convenience.
- **No store-immediate or call-through-double-indirection.** The ISA's
  `store_32`/`store_16`/`store_8` only store a register value (no
  immediate operand), and `link_call` takes a register or a resolved
  label — never a bare memory cell holding an address. `movsi`/`movhi`/
  `movqi` and `call`/`call_value` are all `define_expand`s (not plain
  `define_insn`s) specifically to normalize these cases (force a constant
  into a register before a mem store; avoid GCC's generic call-expansion
  code wrapping an already-`(mem (symbol_ref))` callee in a second `mem`)
  before falling through to the real `define_insn`s
  (`*movsi_reg`/`*store_si`/`call_internal`/etc.).
- **No hardware multiply/divide.** The ALU only has
  NAND/OR/AND/NOR/ADD/SUB/XOR/LSL/LSR/ASR/CMP — `symphony.md` defines no
  `mulsi3`/`divsi3`/etc. patterns at all, so GCC falls back to libcall
  stubs (`__mulsi3`, `__divsi3`, ...) which must come from a real libgcc
  build (milestone 4, not done yet).
- **`link_call`/`link_return` (not stack-based `call`/`ret`) is the
  primary calling convention** — cheaper (no stack touch), uses r13 as a
  true hardware link register. `symphony_expand_prologue`/`_epilogue`
  only push/pop r13 around the frame-size adjustment when
  `symphony_call_is_leaf()` is false (`crtl->is_leaf`), so leaf functions
  skip the stack save entirely, matching dyncc's own backend's leaf-call
  optimization.
- **A new `la dest, symbol` pseudo-mnemonic** (`symphony_output_move` in
  `symphony.cc`, for `SYMBOL_REF`/`LABEL_REF` constants) that does not
  exist in `docs/isa.txt` today — invented here as a placeholder that
  `tools/symphony_as.py` specifically recognizes and expands into a real
  materialized-address sequence (`isa.constant()`'s 3-sub-instruction
  hi16/lsl/or-lo16 form), resolved by `tools/symphony_ld.py`'s
  `abs32_la` relocation once the symbol's address is known at link time.

## Dynphony support

`-mdynphony` selects Dynphony's variable-length encoding of the
identical instruction set/RTL/ABI Symphony uses — GCC's own codegen
(instruction selection, register allocation, everything in `symphony.md`/
`symphony.cc`) makes exactly the same decisions either way; only the
final byte encoding differs, and that is handled entirely by
`tools/symphony_as.py`/`symphony_ld.py` outside GCC itself, mirroring how
dyncc's own backend already supports both encodings
(`symphony/targets/symphony/assembler.py`'s `Assembler.emit`, gated on
`target.fixed_instruction_width`).

**How the flag flows through the toolchain**: `symphony.h`'s `ASM_SPEC`
(`"%{mdynphony:-mdynphony}"`) forwards GCC's own `-mdynphony` straight
through to `as` (`symphony_as.py`), the standard GCC mechanism for a
target flag that changes assembler behavior but no codegen decision (the
same pattern moxie uses for its own `-mel`/`-meb` endianness flag).
`symphony_as.py` records the resulting mode on every object file it
produces (`ObjectFile.fixed_width`); `symphony_ld.py` reads that flag
back off the objects being linked, refuses to link a mix of Symphony and
Dynphony objects in one image (a clear `ValueError`, not a silent
mis-decode), and uses it to patch relocations (`abs32_la`, and
`link_call`'s return-offset math — see bug 11 below) correctly for
whichever encoding was actually used.

**What works today, verified by actually running GCC-compiled Dynphony
code on the emulator** (`Machine(..., symphony=False)`, the variable-
length decode path), not just by assembling it: a self-contained
arithmetic function, and a program exercising `link_call` to a symbol,
`la` (a materialized global address), and a backward-jump loop together
— see `test_gcc_backend.py`'s `TestDynphonyEncoding` class. The
variable-length encoding is measurably smaller than Symphony's for the
same instructions, as expected (a trivial `add()` at `-O0`: 71 bytes
Dynphony vs. 84 bytes Symphony).

**What doesn't work yet**: `libgcc.a` and `runtime/*.c` are only ever
built in Symphony mode, so a Dynphony program needing multiply/divide
(libgcc) or malloc/printf/etc. (the runtime library) cannot currently be
linked — only self-contained Dynphony programs work end to end today.
See "Known gaps" below for what a Dynphony `libgcc.a`/runtime build
would need.

## Status (update as work proceeds)

- **Milestone 1** (real target files) — DONE.
- **Milestone 2** (stage1 bootstrap, no libgcc) — DONE, builds cleanly
  with `--with-insnemit-partitions=1` (see gotcha above).
- **Milestone 3** (validate codegen for real code) — DONE. Verified: a
  trivial `add(int,int)` function at `-O0`/`-O1`/`-O2`, a multi-function
  stress program at `-O0`/`-O2`, and (added later, during the frame-
  pointer bug hunt) a non-leaf caller with locals calling another
  non-leaf callee, run end to end on the emulator with a correct result.
  The hard frame pointer (r11) is materialized at the *top* of the
  frame, before allocation — `symphony_frame_pointer_required` always
  returns true, and `symphony_expand_prologue`/`_epilogue` push/pop it
  (and r13, for non-leaf functions) around the frame-size adjustment.
- **Milestone 4** (real libgcc, 32-bit multiply/divide) — DONE.
  `libgcc-config/symphony/t-symphony` pulls in real libgcc's
  `udivmod.c`/`divmod.c`/`udivmodsi4.c` plus a custom `__mulsi3`
  (`lib2funcs.c`, shift-and-add, ported from iq2000). 64-bit arithmetic
  (`_muldi3`/`_divdi3`/etc.) is excluded — a real, separate, unresolved
  LRA/reload ICE class ("maximum number of generated reload insns per
  insn achieved") on this target, not required for this milestone's bar.
  Verified: `libgcc.a` builds via `all-target-libgcc`, contains
  `__mulsi3`/`__udivsi3`/`__umodsi3`/`__divsi3`/`__modsi3`/`__udivmodsi4`,
  and a real compiled program computing `a*b`, `a/b`, `a%b` (signed and
  unsigned) through a non-leaf helper function returns the correct
  result on the emulator.
- **Milestone 5** (custom assembler+linker) — DONE.
  `tools/symphony_as.py` (two-pass assembler), `tools/symphony_ld.py`
  (linker: `jump_u16`/`abs32_la`/`data1`/`data2`/`data4` relocations),
  `tools/symphony_obj.py` (JSON object format), `tools/symphony_ar.py`
  (reads real Unix `ar` archives unmodified, for consuming `libgcc.a`).
  A real, previously-undiscovered bug was found and fixed here during
  milestone 6 work: pass 1 wrote data-directive bytes (`.ascii`, etc.)
  directly into the output sections while pass 2 independently
  re-tracked byte offsets from zero for instruction encoding, silently
  desyncing relocation targets whenever a data directive preceded code
  in the same section (i.e. almost any translation unit with a string
  literal). Fixed by making pass 2 the sole writer, consuming both
  `"data"` and `"insn"` entries from a single ordered list. `.ascii`/
  `.asciz`/`.string` directives (previously entirely unimplemented —
  string literals were silently dropped) and the raw I/O opcode
  mnemonics (`input`/`output`/`keyboard`/`time_0`/`time_1`/`screen`/
  `counter`) were also added. Verified: a multiply/divide program and,
  separately, a program mixing string literals with code assemble+link
  and run correctly on the emulator.
- **Milestone 6** (runtime/libc) — DONE. `runtime/intrinsics.c` (I/O
  opcodes via inline asm), `runtime/heap.c` (memcpy/memmove/memset/
  memcmp, malloc/free/calloc/realloc — first-fit + coalescing),
  `runtime/printf.c` (fixed-arity `printf`/`printf1`/`printf2`/`printf3`,
  ported from `symphony/runtime/intrinsics.py`'s `SCREEN_SOURCE`; real
  varargs are NOT implemented — this GCC port has no
  `TARGET_SETUP_INCOMING_VARARGS`, confirmed broken by direct testing).
  A second real backend bug was found and fixed here:
  `symphony_expand_prologue`/`_epilogue` only ever saved r13 and r11,
  never the other callee-saved registers (`CALL_USED_REGISTERS` in
  `symphony.h` declares r8-r10/r12 callee-saved too) — any non-leaf
  function holding a live value in one of those across its own call had
  it silently clobbered. Fixed initially by pushing/popping the
  callee-saved registers a function's RTL actually uses, placed
  *before* the hard frame pointer is materialized (an earlier attempt
  placed them after, which aliased a spill slot with a real local —
  also fixed). Verified: `malloc(16)`+`free()`+`printf1()` in one
  function prints the correct value end to end.
  A third real bug (`printf3()` hanging when preceded by two or more
  `malloc()` calls) was found and fixed since — see "Bugs found and
  fixed" below (bug 5); it turned out to be a heap-layout/linker
  bug, not a printf3 bug at all.
- **Milestone 7** (this README) — DONE (this update).

## Bugs found and fixed

Every real bug found across this whole project's history, in the order
found (roughly commit order), consolidated from what used to be three
separate, overlapping sections ("Fixed bugs found after milestone 6",
"Fixed bugs found while building the regression test suite", and the
FIXED-marked entries buried inside "Known gaps") plus the four codegen
bugs from the target's very first commit that were never written up
here at all. "Known gaps" below now covers only what is still genuinely
open. For each bug: what broke, root cause, how it was found, the fix,
and the commit.

1. **Four initial codegen bugs (target bring-up, commit `d16f1fb`).**
   Found and fixed while getting the very first version of the target
   description to compile *any* real code at all (a trivial `add()` and
   a small multi-function stress program), before any of the milestone
   numbering below existed:
   - An **over-permissive memory constraint** caused an `-O0` reload
     explosion (too many candidate reload alternatives for LRA to
     resolve cheaply on this small, single-register-class target).
   - A **prologue/epilogue ICE from `addsi3` with a negative immediate**
     against a constraint that only accepted unsigned values -- the
     frame-size adjustment computed a negative displacement in a case
     the insn pattern's own constraint couldn't represent.
   - **Missing mem<-constant store expansion** -- storing an immediate
     constant directly to memory had no lowering path, since this ISA's
     `store_32`/`store_16`/`store_8` only ever take a register operand
     (see "No store-immediate" in "Key design points" below); GCC's
     generic store-immediate pattern needed an explicit `force_reg`
     first.
   - **Double-wrapped call-address RTL**: GCC's generic call-expansion
     code re-legitimized an already-valid `(mem (symbol_ref ...))`
     callee address, wrapping it in a second, spurious `mem`.
   Fixed by making `movsi`/`movhi`/`movqi` and `call`/`call_value`
   `define_expand`s (not plain `define_insn`s) that normalize these
   cases -- forcing constants into registers before a memory store, and
   avoiding the double-wrap for already-legitimate call addresses --
   before falling through to the real `define_insn`s. Found via direct
   iteration against the stage1 compiler while bringing up the trivial
   `add()`/stress-program reproducers (not a dedicated investigation
   technique -- straightforward "it doesn't compile yet, why" debugging
   against GCC's own ICE/error output). Verified: both reproducers
   compile and run correctly on the emulator at `-O0` and `-O2` after
   the fix.

2. **Frame pointer materialized at the bottom of the frame instead of
   the top (commit `4ed9415`).** `symphony_expand_prologue` originally
   materialized r11 (hard frame pointer) *after* `sub sp,sp,size` -- at
   the bottom of the frame. With `FRAME_GROWS_DOWNWARD` and the default
   `STARTING_FRAME_OFFSET=0`, GCC assigns every local a *negative*
   offset from r11, so locals ended up below the allocated frame
   entirely -- inside the red zone a callee's own prologue writes into,
   silently corrupting caller locals the moment any non-leaf call
   happened. Found by actually running code on the emulator (a
   `malloc()`+`free()`+`printf1()` integration test), not by
   inspection. Fixed by materializing r11 as `sp` *before*
   `sub sp,sp,size`, so locals at `r11-4`, `r11-8`, ... land inside the
   allocated frame; `symphony_frame_pointer_required` was also made to
   always return true. Regression test:
   `test_gcc_backend.py`'s `test_frame_pointer_top_of_frame`.

3. **Missing callee-saved r8-r10/r12 saves (commit `1744570`).**
   `CALL_USED_REGISTERS` in `symphony.h` declares r8-r10 and r12
   callee-saved (a caller may assume they survive an ordinary call),
   but `symphony_expand_prologue`/`_epilogue` only ever pushed/popped
   r13 and r11 -- nothing saved the others, so a non-leaf function
   holding a live value in one of them across its own call had it
   silently clobbered by the callee. Found via the same emulator
   integration test as bug 2 above (a subtler second version of this
   bug -- placing the new saves *between* `mov r11,sp` and
   `sub sp,sp,size`, aliasing a callee-saved register's spill slot with
   a real local at the same negative r11 offset -- was itself found and
   fixed within the same investigation before landing the final,
   correct fix). Fixed by pushing/popping exactly the callee-saved
   registers a function's RTL actually uses (`df_regs_ever_live_p`),
   placed *before* r11 is materialized. Regression test:
   `test_gcc_backend.py`'s
   `test_callee_saved_registers_survive_nested_calls`.

4. **Assembler pass1/pass2 layout desync (commit `edf4f89`).**
   `symphony_as.py`'s pass 1 wrote data-directive bytes (`.ascii`, etc.)
   directly into `self.sections` while pass 2 independently re-tracked
   byte offsets from zero for instruction encoding -- any translation
   unit with a string literal preceding code in the same section (i.e.
   almost anything using `printf`) had every relocation site after that
   point silently corrupted by the directive's length, jumping to
   garbage at runtime with no build-time diagnostic. Found via direct
   emulator-level testing: a linked `link_call` jumped to the wrong
   address whenever a string literal preceded it in the same
   translation unit. Fixed by making pass 2 the sole writer to
   `self.sections`, consuming both `"data"` and `"insn"` entries from
   one ordered list built in pass 1. `.ascii`/`.asciz`/`.string`
   directives (previously entirely unimplemented -- string literals were
   silently dropped) and the raw I/O opcode mnemonics were added in the
   same commit. Regression test: `test_gcc_backend.py`'s
   `TestBugRegressions::test_assembler_pass1_pass2_desync` (assembles
   hand-written `.s` with the pre-fix assembler via `git show
   edf4f89~1:...` and confirms it hangs; the fixed assembler returns the
   correct value).

5. **`__dyn_heap_anchor` BSS-ordering bug in the linker, previously
   manifesting as "`printf3()` hangs after 2+ `malloc()` calls" (commit
   `4196956`).** `runtime/heap.c`'s `malloc()` used to compute "start of
   heap" as the address right after its own `__dyn_heap_anchor[7]` BSS
   array, relying on that array being the LAST symbol in the whole
   linked image -- true only by accident of link/object order, since
   `symphony_ld.py`'s `Linker.link()` lays out each object's BSS in
   whatever order objects were added, with no guarantee `heap.o`'s BSS
   comes last. As soon as another object's BSS symbol landed after it
   (normal in any real multi-file link), the first `malloc()`'s
   returned block silently overlapped that neighbor's storage instead
   of free memory, and the block's own field-initializing stores
   corrupted it -- traced to `__dyn_heap_end` itself in the reproducer,
   whose corrupted value was later read and used as a jump target,
   landing execution in garbage memory several calls downstream (hence
   looking printf3/5-argument-specific: reproducing needs 2+ `malloc()`
   calls to grow the pointer into a collision, and enough call depth
   afterward for the corruption to surface as a visible crash).
   Root-caused via emulator single-stepping down to the exact
   corrupting store and its target address, not by inspection. Fixed by
   having `Linker.link()` synthesize `__dyn_heap_anchor` itself, as a
   zero-size marker equal to the address right after ALL objects'
   sections are laid out (computed last, so it's correct regardless of
   link order) -- `heap.c` now just has `extern unsigned char
   __dyn_heap_anchor[];`, no real BSS storage, and the linker raises a
   clear error if any input object still defines it as a real symbol
   (stale pre-fix `.o` files). Verified: the original repro (`malloc()`
   x2 then `printf3(...)`) hangs before this fix and halts cleanly with
   the correct return value after it, both via actual emulator
   execution. Regression test: `test_gcc_backend.py`'s
   `TestBugRegressions::test_heap_anchor_bss_ordering_bug` (plus a
   negative test for the linker's stale-object error).

6. **`symphony_as.py` didn't parse `symbol+N` relocation expressions
   (commit `a1c54f4`).** GCC-emitted libgcc source (`libgcc2.c`'s
   `__main`/`__do_global_ctors`) references `__DTOR_LIST__ + 1`, which
   reaches `.s` as `__DTOR_LIST__+4` (scaled by pointer size).
   `parse_int_or_symbol` treated the whole string as one opaque symbol
   name (since `int("__DTOR_LIST__+4", 0)` raises), so the linker saw a
   relocation to a symbol that could never be defined -- an
   always-broken link for anything pulling in that libgcc object, with
   no diagnostic pointing at the real cause. Found via direct
   end-to-end testing: the very first `int main(void){...}` program
   ever linked against the real linker/libgcc (no prior milestone had
   done this) failed with "undefined symbol '__DTOR_LIST__+4'". Fixed
   by splitting a trailing `+N`/`-N` off into a proper
   `(symbol, addend)` pair before emitting the relocation.

7. **No `atexit()` (commit `a1c54f4`, same investigation as bug 6).**
   GCC's `expand_main_function` always emits an implicit
   `link_call __main` at the start of any real `main()` (this target
   defines no `HAS_INIT_SECTION`/`NAME__MAIN` override, and it survives
   `-ffreestanding` too), and libgcc's `__main` -> `__do_global_ctors`
   unconditionally calls `atexit(__do_global_dtors)` even when
   `__CTOR_LIST__`/`__DTOR_LIST__` are libgcc's own trivial empty
   two-element arrays (no real global constructors anywhere in this
   runtime). Without a real `atexit` symbol, **every** program defining
   `main()` failed to link, not just ones calling `atexit` directly.
   Fixed with a minimal `atexit()` in `runtime/intrinsics.c`: registers
   into a small fixed-size table and does nothing else -- there is no
   real `exit()` in this freestanding runtime, so nothing ever needs to
   walk or invoke the table. Both bugs 6 and 7 verified end-to-end:
   `int main(void){int a=6,b=7; return a*b+a/b;}` compiled, assembled,
   linked against real `libgcc.a` + `runtime/intrinsics.c`, and run on
   the emulator, before and after each fix.

8. **`r11` (hard frame pointer) was not marked `FIXED_REGISTERS`
   (commit `d13c15a`).** `FIXED_REGISTERS` in `symphony.h` was
   `{ 1,0,0,0,0,0,0,0, 0,0,0,0, 0,0,1,1 }` (r11 = index 11 = 0, i.e. NOT
   fixed). This let IRA/LRA treat r11 as an ordinary `GENERAL_REGS`
   value eligible for copy-propagation into pseudos -- e.g. `ivopts`
   hoisting a loop-invariant copy of r11 into a pseudo compared every
   loop iteration. Since r11 is permanently pinned to the frame-pointer
   role (`symphony_frame_pointer_required()` always returns true) and
   this target's flat register-class structure gives LRA no fallback,
   the resulting equivalence-substitution loop for that pseudo could
   never converge, hitting reload's "maximum number of generated reload
   insns per insn achieved (90)" cap on ordinary user code with no
   64-bit arithmetic, VLA, or recursion involved. Found via gdb, not
   guesswork: a minimal reproducer (fill loop + insertion-sort loop +
   sum loop over a 16-byte char array, structurally identical to
   `examples/insertion_sort.c`'s `main`) was traced into
   `lra_constraints` (`lra-constraints.cc:5392`), where the original
   insn was exactly `(set (reg N) (reg 11 r11))` and subsequent retries
   kept minting fresh pseudo copies of r11 without converging. Fixed by
   marking r11 `FIXED_REGISTERS`, matching GCC's own documented
   contract ("the frame pointer, except on machines where that can be
   used as a general register when no frame pointer is needed").
   Verified: `examples/insertion_sort.c`'s `main` now compiles at `-O2`
   (previously ICE'd there only). Regression test:
   `test_gcc_backend.py`'s
   `test_frame_pointer_not_reused_as_general_register`.

9. **`r13` (ABI link register) only saved/restored in non-leaf
   functions (commit `d13c15a`, found while investigating bug 8).** A
   real correctness bug, not an ICE.
   `symphony_expand_prologue`/`_epilogue` only saved/restored r13 for
   **non-leaf** functions, on the assumption a leaf function "never
   touches r13". False: `CALL_USED_REGISTERS` marks r13 an ordinary
   allocatable register, so GCC's register allocator can (and, under
   register pressure, does) pick it to hold an arbitrary local in a
   **leaf** function, silently destroying the caller's return address --
   the leaf function's own `link_return` (`jmp r13`) then jumps into
   garbage instead of back to the caller. Traced on the emulator: the
   bug-8 reproducer, called from `main()` at `-O2`, got r13 allocated
   for the insertion-sort loop's `index` counter; the function never
   returned, and execution looped forever with sp/r11 drifting upward
   each spurious prologue re-entry -- a silent infinite hang, no crash.
   Fixed by keying the save/restore on whether the function's own RTL
   ever writes r13 (`df_regs_ever_live_p`), exactly like the other
   callee-saved registers, instead of on leaf-vs-non-leaf. Verified:
   the same reproducer now returns the correct result at `-O0` and
   `-O2` (previously hung forever at `-O2`). Regression test:
   `test_gcc_backend.py`'s
   `test_link_register_preserved_in_leaf_function`.

10. **`*movsi_reg` had no memory ("m") alternatives at all (commit
    `a6beb95`) -- the single fix that closed most of the remaining
    "reload insns" ICE class.** The very first port of this target
    split SImode moves into an always-register-to-register
    `*movsi_reg` plus two entirely separate, non-overlapping patterns
    (`*load_si`/`*store_si`) for memory access. That split caused
    essentially all of the "reload insns" ICE class this project hit,
    including `towers_of_hanoi.c`'s `move_pile` and, it turned out
    empirically, most of the other affected examples too.
    **Investigation**: `TARGET_SECONDARY_RELOAD` was tried first (per
    an explicit task brief) and found to be structurally the wrong
    hook, confirmed by reading `check_and_process_move` in
    `lra-constraints.cc` (where `targetm.secondary_reload` is actually
    called from) -- that function bails whenever either side of a move
    is a `MEM`, so it only ever governs register-class-to-register-
    class copies, never memory-operand access; this target also has
    only one real register class (`GENERAL_REGS` == `ALL_REGS`), so
    there is no second class to move a value through even where the
    hook does apply. **Root cause**, found the same way as bugs 8/9
    (gdb, breakpoint at `lra_constraints`, `lra-constraints.cc:5392`,
    applied to `move_pile` at `-O2`): under real register pressure
    (`move_pile` has 4 live parameters that must survive its own
    recursive call, against only 4 callee-saved hard registers), IRA
    spills a pseudo to a stack slot. When LRA needs to reload that
    spilled pseudo for `(set (reg 48) (reg M))` (matched against
    `*movsi_reg`), it finds no alternative in the insn's own constraint
    set that accepts a memory operand at all -- `*movsi_reg`'s only
    alternatives were `r,r` and `r,I`. With no way to reload in place,
    LRA fell back to generic equivalence/inheritance substitution,
    minting a fresh temporary pseudo on every retry; each fresh pseudo
    could also fail to get a hard register under the same pressure, so
    the substitution never terminated. Not a deeper LRA bug -- a
    target-description gap (no memory alternative to reload against)
    masquerading as one. **Fixed** by giving `*movsi_reg` real `m`
    alternatives (`symphony.md`), the memory operand's address still
    constrained to a bare register only (matching
    `symphony_legitimate_address_p` -- this ISA genuinely has no
    base+offset addressing mode); LRA's own generic
    `process_address_1` already knows how to legitimize a spill slot's
    frame-relative address into that form via a scratch `ADD`, so no
    new hook was needed once the alternative existed for it to run
    against. `symphony_print_operand` also needed a `MEM_P` case
    (`symphony.cc`), since `%0`/`%1` can now refer to a raw `(mem ...)`
    directly instead of always routing through `*load_si`/`*store_si`'s
    own template text. Two mistakes were made and caught during
    development: widening the source predicate too far to plain
    `general_operand` first produced a NEW, narrower ICE (fixed by a
    dedicated `movsi_src_operand` predicate); and `store_32`'s operand
    order was initially backwards (`value, [addr]` instead of the
    assembler's actual `[addr], value`), caught immediately by the
    assembler's own `parse_mem_operand` raising `ValueError` during the
    regression run. **Verified**: `move_pile` compiles cleanly at `-Os`
    and `-O2` (previously ICE'd above `-O0`) with byte-identical output
    to `dcc`'s own build. **Confirmed as a side effect** (independently
    re-run and checked, not assumed): `calloc()` at `-O2` and
    `__dyn_printf_unsigned()` at `-O1`/`-O2` both now compile too (same
    root cause), and every `examples/*.c` file -- including
    `dynamic_sensor_report.c`'s VLA-using `sort_samples`, previously
    believed a separate, structurally different gap -- now compiles
    cleanly at both `-Os` and `-O2` and matches `dcc`'s output exactly.
    Regression test: `test_gcc_backend.py`'s
    `TestBugRegressions::test_movsi_memory_alternative_under_register_pressure`.

11. **`link_call`'s symbol-target return-offset hardcoded for Symphony
    only (found while adding Dynphony support to `symphony_as.py`, this
    session).** The symbol-target branch of `link_call`'s encoding in
    `symphony_as.py` hardcoded `return_offset=12` -- correct only for
    Symphony, where every `link_call` sub-instruction is padded to a
    4-byte slot, so the whole 3-sub-instruction sequence is exactly 12
    bytes. Dynphony's real unpadded sequence is only 10 bytes
    (`counter`=2 + `add`-immediate=4 + `jmp`-immediate=4), so the old
    hardcoded 12 computed a return address 2 bytes past the real next
    instruction -- `link_return`'s `jmp r13` then jumped into the middle
    of an unrelated instruction instead of back to the caller,
    corrupting control flow on the very first non-leaf call in
    Dynphony mode. Found by actually running a GCC-compiled Dynphony
    program on the emulator (not by inspection): execution decoded an
    invalid opcode a few bytes past where the call site should have
    returned, traced back to the wrong `add r13,r13,N` immediate. Fixed
    by making the symbol-target branch's `return_offset` width-aware
    (`12` for Symphony, `10` for Dynphony), matching dyncc's own
    reference (`symphony/targets/symphony/assembler.py`'s
    `Assembler._call_bytes`, `return_offset=12 if
    fixed_instruction_width else None`, where `isa.link_call`'s own
    default for the `None` case is 10). Verified before/after: the same
    regression test fails identically against the pre-fix code (PC
    decodes garbage at the wrong address) and passes with the fix.
    Regression test: `test_gcc_backend.py`'s
    `TestDynphonyEncoding::test_dynphony_link_call_and_global_and_loop`.

12. **Reload ICEs on narrow-mode spills in the complete self-host
    compiler.** `movhi` and `movqi` had the same register-only reload
    gap previously fixed for `movsi`: under the self-host compiler's
    register pressure, LRA could not reload a spilled halfword or byte
    value and repeatedly generated new reload insns. Added real memory
    alternatives for HImode and QImode moves. Every self-host
    translation unit now compiles at both `-Os` and `-O2`; the complete
    compiler runs successfully at both levels. The same fix also closes
    the previously documented `calloc()` `-O1` ICE.

13. **Large linked programs could not call targets above 64 KiB.** The
    fixed-width ISA's direct `jmp`/`link_call` relocation is U16, so the
    `-O0` self-host image failed to link. The linker now reserves one
    low-address trampoline per far target and redirects out-of-range
    calls and branches through `la flags,target; jmp flags`. Near calls
    retain their original encoding and cost. A focused regression links
    and executes a call whose real target is above `0xffff`.

14. **The runtime declared but did not implement `jump()`.** Added the
    missing non-returning intrinsic in `runtime/jump.c` using the
    register-target `jmp` instruction. It is linked only for the self-host
    workload, so ordinary examples retain their previous size and runtime.

15. **Incoming stack arguments used a variable frame offset.** The
    prologue established r11 after a variable number of register saves,
    while `FIRST_PARM_OFFSET` assumed no save area. Calls with an eighth
    argument therefore read the wrong word; in the self-host compiler,
    `dyn_preprocess_project` failed to write its `output_length` pointer.
    Functions receiving stack arguments now use a fixed six-word save area
    (r13, r11, r8-r10, r12) and a 24-byte first-parameter offset; other
    functions retain selective saves and their existing performance. A
    regression compiles and runs an eight-argument call at `-O0`, `-Os`,
    and `-O2`.

## Known gaps / real unresolved bugs (for whoever picks this up next)

- **64-bit arithmetic** (`__muldi3`, `__divdi3`, etc.) still hits the
  same ICE signature *inside libgcc's own build* at `-O2` and is still
  excluded from libgcc (`LIB2FUNCS_EXCLUDE` in
  `libgcc-config/symphony/t-symphony`) -- rebuilding libgcc with the
  exclusion removed, against the bug-10-fixed compiler, still ICEs. Not
  re-investigated in depth (out of scope for the session that closed
  bug 10, which was specifically the `move_pile`/reload-insns class) --
  worth revisiting given how much of the rest of this ICE class turned
  out to share one cause, but libgcc's own multi-word arithmetic
  (`umul_ppmm`-style multi-limb macros) may plausibly hit a
  structurally different pattern than ordinary user code did. The
  failure mode for user code is unchanged: a 64-bit multiply/divide in
  ordinary code compiles fine (GCC emits a libcall), the gap only
  surfaces at **link time** as an undefined-symbol error, since
  `__muldi3`/`__divdi3`/etc. are simply absent from `libgcc.a`.
  Exercised by `test_gcc_backend.py`'s `test_64bit_multiply_links`
  (xfail).
- **No real varargs.** `printf`/`printf1`/`printf2`/`printf3` are
  fixed-arity as a deliberate scope decision, not real `stdarg.h`
  support.
- **Dynphony support is real but narrower than Symphony's.** The
  assembler+linker now correctly assemble, link, and run real
  GCC-compiled code in Dynphony's variable-length encoding (see
  "Dynphony support" above) -- but `libgcc.a` and
  `runtime/*.c` are only ever built in Symphony mode today. A Dynphony
  program that needs libgcc (multiply/divide) or the runtime library
  (malloc/printf/etc.) cannot currently be linked; only self-contained
  Dynphony programs work end to end. Building a Dynphony `libgcc.a`
  would need a second `all-target-libgcc` pass with `-mdynphony` forced
  into its build flags (not attempted -- real but currently unexercised
  scope, not a bug).
- **An earlier attempt at a related, broader fix for bug 10's ICE class
  was tried and reverted.** Reserving r12 as a dedicated
  address-reload-scratch register class (`ADDR_REGS`, steering LRA's
  spill/stack-slot address materialization there via
  `MODE_CODE_BASE_REG_CLASS`) was built and A/B tested. It measurably
  **regressed** register pressure elsewhere: shrinking the callee-saved
  pool from 4 registers to 3 made spilling *more* likely for cases like
  `move_pile`, without closing any additional cases. Not kept.
  `TARGET_SMALL_REGISTER_CLASSES_FOR_MODE_P` (returning true
  unconditionally) was also tried and found measurably **neutral** -- no
  change to any reproducer or the xfail set -- and was likewise not
  kept, to keep the committed fix minimal and to what's demonstrably
  beneficial.

## dcc vs GCC comparison

A size (final binary bytes) and step-count (emulator instructions to
halt) comparison between `dcc` (this project's own compiler) and this
real GCC backend, for every program in `examples/*.c`. Produced with a
one-off script (not committed; see the task notes below), not a new
permanent benchmarking framework.

**Methodology:**

- **Target is Symphony, not Dynphony, for this comparison.** The custom
  assembler/linker now support both encodings correctly (see "Dynphony
  support" above) — `-mdynphony` genuinely selects Dynphony's variable-
  length encoding end to end. This table still uses Symphony throughout,
  though, because `libgcc.a` and `runtime/*.c` (both needed by nearly
  every example below) are only ever built in Symphony mode — `dcc` was
  run with `--target symphony` to match.
- **GCC optimization levels: both `-Os` and `-O2` are reported**, matching
  how this project's earlier moxie-based GCC comparison work compared
  against both levels rather than picking one. `dcc`'s own optimizer
  (`symphony/middle/passes/pipeline.py`) has no `-O`-style knob — a
  single fixed pipeline (scalar promotion, inlining, tail-call
  elimination, jump threading, dead-code/CFG simplification, constant
  folding) always runs, so there is no exact dcc-side equivalent to pick
  between; both GCC levels are shown so the reader can judge either
  comparison point rather than have one chosen for them.
- **printf equivalence.** Examples using `#include <stdio.h>` call
  `printf(fmt, ...)` with 0-3 substitution arguments. `dcc`'s frontend
  compile-time-lowers every such call into the same fixed-arity
  `__dyn_printf_put/string/unsigned/signed/hex` primitives
  `runtime/printf.c`'s `printf`/`printf1`/`printf2`/`printf3` already
  implement (ported verbatim from `symphony/runtime/intrinsics.py`) — no
  real varargs on either side. The comparison script performs a pure
  source-level call-site rewrite (`printf("%d ", i)` ->
  `printf1("%d ", i)`, chosen by argument count) before handing the file
  to `xgcc`; this changes which fixed-arity entry point is called, never
  the format-string semantics (`__dyn_printf_n` parses the identical
  `%d`/`%u`/`%x`/`%s`/`%c`/`%%` grammar `dcc`'s own frontend parses at
  compile time). `dcc`-side and GCC-side screen-framebuffer text output
  was diffed byte-for-byte for every printf-using example below — all
  matched exactly.
- **symphony.h intrinsics.** `input`/`output`/`keyboard`/`time`/
  `time_low`/`time_high` are opcode-lowered on `dcc`'s side
  (`symphony/runtime/intrinsics.py`) and already exist as equivalent
  inline-asm wrappers in `runtime/intrinsics.c` — every example using
  `<symphony.h>` compiled against the real runtime unmodified.
- **input() values for interactive examples**, reused verbatim from this
  project's own test suite so both sides execute the identical control
  path: `insertion_sort.c` — `[12, 240, 7, 7, 99, 0, 180, 42, 3, 1, 8,
  255, 25, 6, 2, 11]` (from `tests/test_compiler.py`'s
  `test_insertion_sort_demo`); `dynamic_sensor_report.c` — count `9`
  followed by `[12, -3, 7, 12, 25, 7, 0, -3, 18]` (from
  `tests/test_integration.py`'s `test_dynamic_sensor_report_demo`);
  `towers_of_hanoi.c` — `[2, 0, 2, 1]` (from `tests/test_compiler.py`'s
  `test_towers_of_hanoi_example`). `bigprime.c`/`pi.c`/`primes.c`/
  `constant_folding.c`/`interprocedural_constant_folding.c`/`demo.c`/
  `arena_allocator.c` take no runtime input (`bigprime.c`'s RNG is
  seeded with a fixed constant, `rng_state = 0x8a31f27d`, so it is
  already fully deterministic).
- **Size**: `dcc`'s own image never serializes BSS bytes (reserved
  globals get addresses past the end of the image but are not written
  into the binary, per `symphony/targets/symphony/backend.py`'s
  `build()`), while the GCC linker's `Linker.link()` always returns a
  full image including zeroed BSS bytes. To keep the byte-count
  apples-to-apples, the GCC column below is **text+data size** (excludes
  BSS), computed from the same objects the linker actually linked
  (including whatever `libgcc.a`/runtime members got pulled in and any
  far-target trampolines it generated) — not the raw returned image
  length. Older tables accidentally omitted extracted archive members;
  the benchmark now records the linker's actual text/data boundary.
- **Steps**: both sides run on the exact same `symphony.emulator.Machine`
  step-counting model (`machine.steps`, incremented once per instruction
  executed) — the GCC side via `tools/symphony_ld.py`'s linked image
  loaded at PC = the linker's resolved entry point, `dcc`'s side via its
  own `_halt` label. The native emulator extension
  (`symphony.emulator.native_run`) was used for wall-clock speed on the
  three heavy examples (`pi.c`, `bigprime.c`, `primes.c`, which run into
  the hundreds of millions to low billions of steps) — it drives the
  identical `Machine.steps` counter, just faster, so this is not a
  methodology difference between the two sides. A generous step ceiling
  (20 billion) was used throughout; none of these programs actually loop
  forever, they are just genuinely expensive (`pi.c` computes 3838
  decimal digits of pi via 402-word/12832-bit bignum arithmetic;
  `bigprime.c` searches for a 128-bit probable prime via repeated
  Miller-Rabin-style testing from a fixed seed).
- **Correctness was checked, not assumed**: for every example that built
  on both sides, the emulator's return value (`r1` at halt) and all
  observable output (`output()` call sequence and/or printf screen-
  framebuffer text, read back byte-for-byte) were compared. **No
  correctness mismatches were found** — every example that compiled on
  both sides produced identical results (return value, `output()`
  sequence, and printf text all matched exactly), including the
  multi-billion-step `pi.c` (all 3838 printed digits identical) and
  `bigprime.c` (identical 128-bit prime found, identical candidate
  count).

**Results** (rebuilt again after two `dcc`-side changes landed on `main`
since the previous revision of this table: loop optimizer passes were
added to `symphony/middle/passes/pipeline.py`, and `__dyn_udivmod` was
rewritten to match libgcc's own `__udivmodsi4` shift-align-then-subtract
algorithm instead of dcc's old fixed 32-iteration loop, both since
this GCC port's numbers were last measured, so both sides needed a
fresh run rather than reusing either column from before):

| Example | dcc size (B) | GCC `-Os` size (B) | GCC `-O2` size (B) | Size ratio (GCC `-Os` / dcc) | dcc steps | GCC `-Os` steps | GCC `-O2` steps | Steps ratio (GCC `-Os` / dcc) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `arena_allocator.c` | 1,252 | 14,668 | 14,412 | 11.72x | 553 | 277 | 116 | 0.50x |
| `bigprime.c` | 11,704 | 18,323 | 19,583 | 1.57x | 967,067,881 | 259,594,889 | 186,078,003 | 0.27x |
| `constant_folding.c` | 8 | 14,140 | 14,140 | 1767.50x | 1 | 116 | 116 | 116.00x |
| `demo.c` | 3,068 | 14,435 | 14,423 | 4.71x | 6,896 | 2,998 | 2,772 | 0.43x |
| `dynamic_sensor_report.c` | 12,960 | 17,436 | 17,696 | 1.35x | 23,713 | 13,199 | 12,367 | 0.56x |
| `insertion_sort.c` | 864 | 14,404 | 14,440 | 16.67x | 7,959 | 1,896 | 1,858 | 0.24x |
| `interprocedural_constant_folding.c` | 8 | 14,168 | 14,168 | 1771.00x | 1 | 116 | 116 | 116.00x |
| `pi.c` | 5,240 | 16,656 | 17,256 | 3.18x | 5,225,253,518 | 1,055,567,369 | 1,051,744,856 | 0.20x |
| `primes.c` | 3,428 | 14,420 | 14,480 | 4.21x | 6,962,874 | 2,196,816 | 2,196,943 | 0.32x |
| `towers_of_hanoi.c` | 312 | 14,708 | 18,176 | 47.14x | 272 | 1,197 | 888 | 4.40x |

Every row above was independently re-verified byte-for-byte correct
against `dcc`'s own output for the same program and the same
`input()`/RNG-seed values noted earlier in this section — return value
(`r1` at halt), the full `output()` call sequence, and printf
screen-framebuffer text (where applicable) all matched exactly,
including the multi-billion-step `pi.c` (5.2B dcc-side /
~1.05B GCC-side steps, all 3838 printed digits identical) and
`bigprime.c` (identical 128-bit prime found on both sides).

**What changed since the previous revision, and why**: the GCC-side
columns (`GCC -Os`/`GCC -O2` size and steps) are essentially unchanged
from before -- this port's own codegen and libgcc weren't touched.
The `dcc` column moved on several rows:

- **`primes.c`: 22,936,672 -> 6,962,874 steps (3.3x fewer)**, and
  **`demo.c`: 20,940 -> 6,896 steps (3.0x fewer)** -- both are
  loop-heavy (`primes.c`'s nested sieve loops; `demo.c`'s hot inner
  loop), and both dropped once dcc's optimizer gained real loop passes
  (`symphony/middle/passes/pipeline.py`, added on `main` after the
  previous revision of this table). This is dcc's own optimizer
  catching up on exactly the kind of win GCC's `-O2` already had over
  it in the previous table.
- Every `dcc` row's *size* grew slightly (e.g. `arena_allocator.c`
  1,252B unchanged, but rows using runtime division like
  `bigprime.c` 11,452 -> 11,704B, `demo.c` 2,816 -> 3,068B) --
  this is `__dyn_udivmod`'s algorithm swap to match libgcc's own
  `__udivmodsi4` (shift-align-then-subtract instead of dcc's old
  fixed 32-iteration loop): about +100B for the helper itself, paid
  once per program that uses runtime division, in exchange for far
  fewer steps per division at every call site (measured separately:
  ~5x fewer steps for a representative `100/7`, 3515 -> 714).
- The **GCC-side size baseline moved from ~10-10.8KB to ~14.1-14.7KB**
  across every row, and `dynamic_sensor_report.c`/`bigprime.c`/`pi.c`
  moved further still due to `printf`/`malloc` pulling in more of the
  runtime -- this is the fixed libgcc+runtime linked baseline for this
  particular stage1 toolchain build, not a regression introduced by
  this table's rebuild; it does not track anything on the `dcc` side.
- `bigprime.c`/`pi.c` GCC-side step counts (259,594,889 /
  1,055,567,369 at `-Os`; 186,078,003 / 1,051,744,856 at `-O2`) are
  bit-for-bit identical to the previous revision, confirming the
  GCC-side codegen genuinely didn't move.

**Reading the results**: dcc's own pipeline still produces
dramatically smaller binaries than this GCC port across the board --
expected, since dcc's output has no runtime/libc/libgcc baseline
overhead (a `main(){return 1466;}`-shaped program is 8 bytes / 1 step
under dcc's constant-folding vs. ~14KB / 116 steps under GCC purely
from linking in libgcc + the runtime's `atexit`/heap/printf machinery,
none of which the program actually uses), and dcc's optimizer targets
this exact ISA's addressing and calling-convention quirks directly
rather than going through a general-purpose target's RTL pipeline.
GCC's `-O2` still pulls ahead of dcc on **steps** for several examples
once the fixed ~14-19KB overhead's own startup cost is paid -- most
dramatically `arena_allocator.c` (116 vs dcc's 553, a 4.8x step
reduction) -- but dcc's new loop passes closed most of that gap for
loop-dominated programs specifically (`primes.c`'s steps ratio moved
from 0.10x to 0.32x GCC `-Os`, i.e. dcc went from 10x slower to about
3x slower than GCC `-Os` on that one program). `towers_of_hanoi.c`
(888 vs dcc's 272 is still a step INCREASE, unlike the others, since
it's dominated by call overhead rather than a hot loop `-O2` can
shrink) is unaffected by either dcc-side change, as expected --
neither loop passes nor the divide-algorithm swap touch code with no
loops or runtime division.

**Effort characterization**: this revision required no toolchain or
target-description changes -- only re-running the same measurement
against the current `main` (loop passes) and the current
`libgcc-arithmetic` branch (divide algorithm swap, cherry-picked in as
commit `9ee5a8a`). The driver script was also fixed during this run:
it originally ran `dcc`'s side through the slow pure-Python reference
interpreter instead of the native emulator extension
(`symphony.emulator.native_run`), which is why `pi.c`/`bigprime.c`
took minutes instead of under a second once fixed -- both sides of
every row above were measured with the native emulator. Every number
in the table above comes from an actual emulator run against the
current toolchain and runtime, not carried over or estimated from the
previous revision.
