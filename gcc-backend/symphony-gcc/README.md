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

## Build recipe (stage1, no libgcc yet — milestones 1-3)

This produces a `cc1`/`xgcc` that can compile C to Symphony `.s` assembly.
It does **not** yet produce a working `libgcc.a` or a full
`symphony-elf-gcc` driver capable of linking a runnable binary —
that's milestone 4/5, not done yet (see Status below).

```sh
# 1. Get a GCC 14 source tree (shallow clone is fine) and apply the patch.
git clone --depth 1 -b releases/gcc-14 https://gcc.gnu.org/git/gcc.git gcc-src
cd gcc-src
git apply /path/to/gcc-backend/symphony-gcc/gcc-src.patch

# 2. Symlink the real target files into the GCC tree.
mkdir -p gcc/config/symphony
for f in symphony.h symphony.cc symphony.md symphony.opt \
         symphony.opt.urls symphony-protos.h t-symphony; do
  ln -sf /path/to/gcc-backend/symphony-gcc/config/symphony/$f \
         gcc/config/symphony/$f
done

# 3. Configure + build stage1 (no headers/newlib, no bootstrap -- a
#    single-stage cross-compiler is enough to validate codegen).
mkdir -p ../build-stage1 && cd ../build-stage1
../gcc-src/configure \
  --target=symphony-elf \
  --prefix=$PWD/../install \
  --enable-languages=c \
  --without-headers --with-newlib \
  --disable-shared --disable-threads --disable-libssp \
  --disable-libquadmath --disable-libgomp --disable-libatomic \
  --disable-libstdcxx --disable-bootstrap --disable-nls \
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

**Important:** do not run `gcc/configure` directly inside the `gcc/`
build subdirectory to iterate faster — it works but skips the top-level
configure's host-library detection (`GMPLIBS`/`GMPINC` etc.), producing a
`cc1` that fails to *link* with "undefined reference to `mpfr_*`" even
though compilation succeeded. Always reconfigure and build from the
top-level `build-stage1` directory.

### Validate (milestone 3)

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

Also try `-O0` (exercises stack-frame spill/reload, frame-pointer-relative
addressing via r11, and `push`/`pop r13` prologue/epilogue for
non-leaf functions) and a multi-function program with a loop and a call
(exercises `link_call`, `cmp`/conditional-branch mnemonics, and the
memory-operand store/load split described below) — both compile and
produce sane-looking assembly as of the last verified build.

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
  trivial `add(int,int)` function at `-O0`/`-O1`/`-O2`, and a
  multi-function stress program (local arrays, a `for` loop, direct
  calls, mixed leaf/non-leaf functions) at `-O0` and `-O2` — all compile
  cleanly and produce assembly that matches the ISA's real mnemonics and
  calling convention, including `-O2` successfully inlining calls and
  eliminating dead stack traffic (a good signal the target description is
  coherent enough for GCC's optimizers, not just degenerate/always-safe
  codegen).
- **Milestone 4** (real libgcc, stage1→libgcc→stage2 bootstrap) — NOT
  STARTED.
- **Milestone 5** (custom assembler+linker extension; verify a
  multiply/divide program assembles+links+runs on the emulator) — NOT
  STARTED. Must implement the `la dest, symbol` pseudo-mnemonic contract
  above.
- **Milestone 6** (runtime/libc: real libgcc for soft arithmetic +
  extracted custom I/O sources for printf-via-screen/time/keyboard) — NOT
  STARTED.
- **Milestone 7** (this README + further docs) — in progress.
