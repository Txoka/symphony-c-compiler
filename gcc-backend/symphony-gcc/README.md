# Real Symphony/Dynphony GCC target

This is a **real** GCC backend for the Symphony/Dynphony ISA (`docs/isa.txt`) —
not the moxie-hijack comparison backend at `gcc-backend/dynphony/`
(branch `gcc-comparison-backend`). GCC emits genuine target assembly for
`symphony-elf` from its own `.md`/`.cc`/`.h` port; there is no ELF, no `as`,
no `ld` — the resulting `.s` is meant to be consumed by a custom
assembler+linker (extending `gcc-backend/tools/gcc_assembler.py`, not yet
built — see status below) that produces a flat binary runnable on
`symphony/emulator`, the same shape dyncc's own backend already produces.

Triple: `symphony-elf`. Dynphony (the variable-length encoding of the same
ISA) is meant to be exposed later via a `-mdynphony` target option
(`symphony.opt`'s `mdynphony`, currently unused/unimplemented in codegen),
not a second triple or multilib.

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

### 5. Assemble, link, and run a real program on the emulator

```sh
gcc/xgcc -Bgcc/ -S -O0 my_program.c -o my_program.s
python3 tools/symphony_as.py my_program.s -o my_program.o
```

Link `my_program.o` against `libgcc.a` (at
`symphony-elf/libgcc/libgcc.a` inside the build directory) and, if the
program uses the runtime library, the compiled runtime objects
(`gcc-backend/symphony-gcc/runtime/*.c`, compiled and assembled the same
way) using `tools/symphony_ld.py`'s `Linker` class (see
`tools/symphony_ld.py` for the API — there is no command-line driver
yet, only the Python `Linker`/`ObjectFile` classes used directly). Run
the resulting flat binary via `symphony.emulator.machine.Machine`:

```python
from symphony.emulator.machine import Machine
binary = open("my_program.bin", "rb").read()
m = Machine(binary, load_address=0, symphony=True)
m.pc = entry_address       # from the linker's returned entry point
m.regs[14] = 0x800000      # stack pointer: top of a generous RAM region
m.regs[13] = 0xFFFFF0      # halt address: any address the program never jumps to
result = m.run(halt_address=0xFFFFF0, max_steps=200000)
```

`r1` (the return-value register per the ABI) holds `_start`'s return
value in `result` when the halt address is reached via `link_return`.

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
  exist in `docs/isa.txt` today — invented here as a placeholder the
  future custom assembler (milestone 5) must specifically recognize and
  expand into whatever real load-address sequence the ISA/assembler
  ultimately supports. This is a concrete open contract between this
  target's codegen and the not-yet-written assembler/linker.

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
- **Milestone 6** (runtime/libc) — DONE, with one known open bug.
  `runtime/intrinsics.c` (I/O opcodes via inline asm),
  `runtime/heap.c` (memcpy/memmove/memset/memcmp,
  malloc/free/calloc/realloc — first-fit + coalescing), `runtime/printf.c`
  (fixed-arity `printf`/`printf1`/`printf2`/`printf3`, ported from
  `symphony/runtime/intrinsics.py`'s `SCREEN_SOURCE`; real varargs are
  NOT implemented — this GCC port has no `TARGET_SETUP_INCOMING_VARARGS`,
  confirmed broken by direct testing). A second real backend bug was
  found and fixed here: `symphony_expand_prologue`/`_epilogue` only ever
  saved r13 and r11, never the other callee-saved registers
  (`CALL_USED_REGISTERS` in `symphony.h` declares r8-r10/r12 callee-saved
  too) — any non-leaf function holding a live value in one of those
  across its own call had it silently clobbered. Fixed by pushing/
  popping exactly the callee-saved registers a function's RTL actually
  uses, placed *before* the hard frame pointer is materialized (an
  earlier attempt placed them after, which aliased a spill slot with a
  real local — also fixed). Verified: `malloc(16)`+`free()`+`printf1()`
  in one function prints the correct value end to end.
  **Known open bug:** `printf3()` (3 substitution arguments) hangs when
  preceded by two or more `malloc()` calls in the same function — not
  root-caused (see the comment above `__dyn_printf_emit_one` in
  `runtime/printf.c` for what was ruled out). `printf`/`printf1`/
  `printf2` are unaffected and fully verified with malloc/free of any
  count preceding them.
- **Milestone 7** (this README) — DONE (this update).

### Known gaps / real unresolved bugs (for whoever picks this up next)

- **64-bit arithmetic** (`__muldi3`, `__divdi3`, etc.) hits a real LRA/
  reload ICE ("maximum number of generated reload insns per insn
  achieved") and is excluded from libgcc entirely. Not root-caused.
- **A whole class of "reload insns" ICEs** beyond the 64-bit case above,
  confirmed to trigger in at least: a loop containing a call combined
  with `-fmove-loop-invariants` (mitigated by compiling the runtime
  library at `-O0`, not fixed), and a libcall result (e.g. a software
  divide) feeding directly into a comparison's RTL expansion at `-O0`
  (worked around in `calloc`'s overflow check by materializing the
  division into a temporary first — see `runtime/heap.c`).
- **No real varargs.** `printf`/`printf1`/`printf2`/`printf3` are fixed-
  arity as a deliberate scope decision, not real `stdarg.h` support.
- **`printf3()` + 2+ preceding `malloc()` calls hangs** — see Milestone 6
  above. The most promising unfinished lead: the loop's read of
  `format[i]` returns garbage partway through instead of hitting the
  NUL terminator, but the exact faulting register/stack-slot was not
  identified.
