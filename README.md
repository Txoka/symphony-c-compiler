# Symphony C compiler

A runnable Python compiler for a useful C subset, with first-class Symphony and
Dynphony targets. It emits flat, big-endian binaries directly; no separate
assembler or linker is needed.

The implementation separates syntax parsing, semantic analysis, typed syntax, IR lowering, optimization, instruction selection, and binary layout. It includes a reference emulator and automated execution/encoding tests.

A second compiler, written in the C subset it compiles, lives in
[`selfhost/`](selfhost/README.md). It reads projects from persistent storage,
emits Symphony or Dynphony images, and rebuilds itself byte-for-byte. Use
`make selfhost-test` for its complete
compile-the-compiler/compile-a-program/run-the-program test.

## Run it

Requires Python 3.10+ and `pycparser`. From this project's directory:

```sh
python -m pip install -e .
scc examples/demo.c -o demo.bin --run
```

Compile and link a project from multiple translation units, with project headers
and command-line macros:

```sh
scc src/main.c src/parser.c src/backend.c \
  -I include -D DEBUG=1 -o compiler.bin
```

Each source file is preprocessed and type-checked in its own translation-unit
scope. External functions and objects are resolved across the project, while
file-scope `static` definitions remain private. The linked program is optimized
as one unit before the final flat image is laid out.

Expected output includes `main returned 146`. Installing the package provides
two first-class commands:

- `scc` targets Symphony by default.
- `dcc` targets Dynphony by default.

Both accept `--target symphony` or `--target dynphony` explicitly. If
`pycparser` is already installed, `python -m symphony` behaves like `scc`.

For long emulator runs, display live host-side instruction throughput and raise
the safety limit as needed:

```sh
scc examples/pi.c -o pi.bin --run \
  --hz-meter --max-steps 100000000
```

The meter's Hz value is decoded instructions executed per real second,
not a simulated hardware clock frequency.

Build and select the optional native C emulator with:

```sh
make native
scc examples/pi.c -o pi.bin --run --hz-meter --engine native
```

`--engine auto` is the default and prefers the native extension when installed;
`--engine python` always selects the portable reference emulator. Build local
wheels with `make wheel`, an sdist with `make sdist`, or both with `make dist`.
`make ci-wheels` invokes cibuildwheel for the current platform.

Symphony's fixed four-byte instruction encoding is the default. The native
build contains separately compiled Dynphony and Symphony
cores; target selection happens before execution and adds no ISA-mode branch to
either instruction loop.

```sh
scc examples/demo.c -o demo.symphony.bin --run --engine auto

dcc examples/demo.c -o demo.dynphony.bin --run --engine auto
```

Tags matching `v*` trigger `.github/workflows/release.yml`. The workflow checks
that the tag matches `pyproject.toml`, runs the test suite, builds and smoke-tests
CPython 3.10–3.15 wheels for mainstream Linux x86_64/arm64, macOS Intel/Apple
Silicon, and Windows AMD64/ARM64 targets, builds an sdist, and attaches every
distribution to a GitHub Release.
Create a release with, for example, `git tag v0.14.0 && git push origin v0.14.0`.

Generate a position-independent image and execute it at another address:

```sh
scc examples/demo.c -o demo.pic.bin \
  --pic --run --run-address 0x12345 \
  --emit-ir demo.ir --map demo.map.json
```

Compile for a fixed nonzero address, with configurable memory sizes:

```sh
scc examples/demo.c -o demo.bin \
  --load-address 0x10000 --ram-size 0x100000 \
  --persistent-size 0x10000
```

The raw file begins with the first instruction; it is **not padded to the load address**. Load the file's first byte at the selected address and begin execution there. PIC images can instead run at any address where the image fits contiguously and leaves enough room for the stack. `--run-address` only controls emulator placement. JSON maps contain absolute symbols for fixed-address images and image-relative offsets for PIC images.

Run the compiler and self-host test suites against both ISAs:

```sh
make test
```

To run one suite for one ISA, set `SYMPHONY_TEST_ISA` (default `symphony`):

```sh
SYMPHONY_TEST_ISA=dynphony python -m unittest discover -s tests -v
```

## Supported language

The complete support matrix, known limitations, and Symphony-family built-ins
are documented in [docs/c-language-support.md](docs/c-language-support.md).

- Plain `char` is unsigned; explicit signed/unsigned `char`, `short`, `int`, and `long` are supported.
- Pointers, pointers to pointers, function pointers, explicit integer/pointer casts, and `void` functions/pointers.
- Local variables and lexical scopes; file-scope globals, `static` globals/functions, static locals, external declarations resolved across linked translation units, and file/block-scope typedefs.
- Named and anonymous structures, self-referential structure pointers, natural member layout, `.`/`->`, nested structure/array members, anonymous aggregate-member promotion, and brace initialization for structure objects.
- Enumerations with implicit or integer-constant enumerator values.
- `const` objects and pointers with qualifier-preserving conversions and modification diagnostics.
- Decimal/octal/hex integer literals, character literals, ordinary single-byte strings, and comments.
- Arithmetic `+ - * / %`, bitwise operations, shifts, comparisons, logical operators, prefix/postfix increment/decrement, assignment and compound assignment.
- Short-circuit `&&`/`||`, conditional `?:`, comma expressions, and unevaluated `sizeof`.
- Conditional 96×40 ASCII text-screen support through literal-format `printf`,
  `screen_framebuffer`, and `screen_cursor`.
- `if`/`else`, `while`, `for`, `do`/`while`, `break`, `continue`.
- Functions, direct/indirect calls, recursion, and returns. The first seven scalar arguments use registers; later scalar arguments are passed on the stack.
- Fixed-size and multidimensional arrays, inferred outer array bounds, brace/string initializers, array indexing/decay, pointer scaling/difference, dereference, and address-of.
- Zero-filled globals, partially initialized arrays, integer constant initializers, and symbolic pointer initializers such as `int *p = &a[2]`.
- Software multiplication and signed/unsigned division/remainder. Division is bounded to 32 iterations, including for large unsigned divisors.
- Freestanding library declarations through `stdio.h`, `stdlib.h`, and
  `string.h`, plus Symphony device extensions through `symphony.h`.

Entry must be `int main(void)` or `int main()`. An empty parameter list is treated as exactly zero parameters. Falling off `main` returns zero. Other non-void functions also get a deterministic zero fallthrough, although callers must not rely on this for portable C.

## Machine and ABI

| Property | Value |
|---|---|
| Registers / byte addresses | 32 bits |
| Instruction encoding | Symphony: fixed 4 bytes; Dynphony: variable width |
| Instruction immediates | 16 bits, unsigned |
| Byte order | Big-endian |
| Loads | 8/16/32 bits; narrow loads zero-extend |
| Unaligned memory access | Allowed |
| `char` / `short` / `int` / `long` | 1 / 2 / 4 / 4 bytes |
| Pointers | 4 bytes |
| Object alignment | Natural, capped at 4 bytes |
| Arguments | first seven in `r1`–`r7`, later arguments on stack |
| Results | `flags`, `r1`–`r7`; scalar C results use `r1` |
| Caller-saved | `r1`–`r7`, `flags` |
| Callee-saved | `r8`–`r12` |
| Link register | `r13` |
| Stack | `sp = 0` at startup, downward, 4-byte aligned |
| RAM | Unified; addresses wrap modulo configured power-of-two size |
| Default RAM / load address | 16 MiB / 0 |
| Termination | Infinite jump loop; main's result remains in `r1` |

Calls place the continuation address in `r13`; leaf functions return with a
single `jmp r13`. A function that still contains a non-tail call after the
whole-program optimization fixed point saves its incoming `r13` once and
restores it before returning. `r11` is the frame pointer when a function needs
a stack frame. In PIC mode startup obtains the image base in `r12` using
`counter` at image offset zero and generated functions preserve it; fixed-address
builds make `r12` available to the allocator. The `_start` IR root initializes
`sp` when reachable code can use the stack; the optimizer removes that operation
from fully stack-free images.

Fallible ABI functions return zero in `flags` on success and an odd status code
on error, allowing `je` to branch directly to an error path. Other functions may
clobber `flags`. The ABI permits multiple word results in `r1`–`r7`; the current
C language subset produces one scalar result in `r1`.

Large constants and all label addresses use fixed-width materialization, avoiding a 64 KiB code/address limit. PIC addresses add the runtime base. Startup initializes pointer-valued globals from symbol offsets on every entry; it never repeatedly adds a base to previously rebased values. Other mutable globals are not reset on reentry unless the image is reloaded.

RAM size participates in layout diagnostics and emulator configuration. Persistent size is validated and recorded in the map. It does not partition main RAM into persistent and volatile regions.

## Symphony device functions

Include `<symphony.h>` to declare the target-specific API. Each call emits the
matching target instruction without ordinary function-call overhead:

```c
#include <symphony.h>

unsigned int input(void);                         /* in */
void output(unsigned int value);                  /* out */
unsigned int keyboard(void);                      /* keyboard */
void screen(unsigned int setting, unsigned int value);
unsigned int time(void);                          /* low 32 bits */
unsigned int time_low(void);                      /* low 32 bits */
unsigned int time_high(void);                     /* high 32 bits */
unsigned int persistent_load(unsigned int address);
void persistent_store(unsigned int address, unsigned int value);
void jump(unsigned int address);                    /* does not return */
char *screen_framebuffer(void);                   /* 96x40 ASCII cells */
void screen_cursor(unsigned int x, unsigned int y);
```

For example:

```c
int main(void) {
    unsigned int value = input();
    output(value + keyboard());
    screen(2, value);
    persistent_store(0, value);
    return time();
}
```

`input()` and `keyboard()` return zero in the reference emulator when their
queues are empty. `output()` appends to `machine.outputs`, and `screen()` appends
`(setting, value)` to `machine.screen_updates`. Set `persistent_size` on the
compiler target to record and validate the hardware size; pass the same size to
`Machine` for emulation. The CLI does this automatically when `--run` is used.
`Machine` makes `time()`/`time_low()`/`time_high()` read Unix-epoch nanoseconds
from the host clock. Passing an explicit `time_value` selects the frozen clock
used by deterministic tests; `time_per_step_ns` may additionally advance that
clock by a fixed amount per completed instruction for deterministic virtual-time
simulation. `time_frequency_hz` instead derives time from an exact target clock
frequency, including frequencies that take a fractional number of nanoseconds
per instruction. A `screen_update_callback` may inspect
`(setting, value, completed_steps)` and return true to stop immediately after
that screen instruction, which is useful for exact frame-boundary sampling. A
native callback may inspect or modify existing RAM and persistent-memory bytes,
but cannot resize either backing bytearray while execution is active.
Device addresses retain the hardware's wrapping behavior. These names are
reserved and cannot be used for user-defined functions.

## Limitations

This is a C subset compiler, not a conforming full C implementation. Unsupported constructs produce diagnostics where encountered:

- The built-in preprocessor supports includes, object/function macros,
  conditional compilation, `#undef`, `#pragma once`, and `#error`. Macro
  stringification, token pasting, variadic macros, and a hosted standard library
  remain unsupported. Minimal freestanding `stdbool.h`, `stddef.h`, `stdint.h`,
  `stdio.h`, `stdlib.h`, and `string.h` headers are provided, along with the
  target-specific `symphony.h`.
- Multiple source translation units link directly into one optimized flat image.
  Serializable object files, archives, dynamic linking, and incremental linking
  are not yet implemented.
- No 64-bit `long long`, floating point, bit-fields, or variadic functions.
- No `volatile` or `restrict`, local `extern`, or inline assembly.
- No aggregate arguments, union returns, or old-style function definitions.
- Structures return by value through caller-owned result storage. Aggregate initializers support ordinary and designated forms, including brace elision; advanced designated-initializer continuation cases remain incomplete. Non-VLA array bounds must be compile-time constants. Multiple tentative global definitions are rejected rather than merged.
- Decimal literals above `2147483647` need an explicit `U` suffix because unsuffixed decimal values would require an unsupported 64-bit C type. Write the minimum signed integer as `(-2147483647 - 1)` or cast `0x80000000u`.
- Strings use ordinary single-byte characters and escapes; no wide/Unicode literal types. String literals reside in writable unified memory, but modifying one is still C undefined behavior.
- The optimizer promotes non-escaping scalar locals, propagates copies and constants, folds scalar expressions, simplifies control flow, rematerializes constants and addresses, selects immediate ALU forms, removes unused pure values and functions, strength-reduces power-of-two arithmetic, and includes only reachable arithmetic helpers.
- The compiler checks static image fit and a single largest frame, but cannot guarantee stack capacity across recursion or nested calls. Stack collision and out-of-bounds accesses are not trapped by generated code.

Signed overflow, invalid shifts, invalid pointer operations, and division by zero remain C undefined behavior. The compiler does not exploit signed-overflow UB for optimization. Software division by zero deterministically returns zero; signed minimum divided by minus one wraps in the helper. Those are implementation behaviors, not portable guarantees.

## Current optimization scope

The optimizer currently applies safe local and whole-program reductions:

- Small integer constants use one immediate instruction; small negative constants use immediate subtraction from `zr`.
- Constants, global addresses, and local addresses are regenerated at their uses instead of occupying stack slots.
- ALU operations and comparisons use immediate forms when the operand fits 16 bits.
- Multiplication by a positive power of two becomes a left shift. Unsigned division and remainder by powers of two become a right shift and mask.
- Unused pure IR values are removed. Calls and memory/control-flow operations are retained.
- Non-escaping scalar locals become IR values. Copies and constants propagate within basic blocks; constant arithmetic, comparisons, casts, and branches are folded.
- Comparisons used only by a branch remain in flags and branch directly, without constructing, spilling, and retesting a Boolean value.
- Instructions after an unconditional transfer are removed through the next block boundary. Jumps are threaded through forwarding blocks, jumps to any immediately following label are removed, and conditional branches use the following block as fallthrough.
- Startup is represented by the `_start` IR root. Functions and software arithmetic helpers unreachable from it, calls, function pointers, or static relocations are omitted.
- Functions without locals, parameters, or live computed stack values omit the `r11` frame-pointer save/restore.
- Read-only parameters whose addresses are never taken become ordinary IR values. A local register allocator keeps straight-line leaf expressions in `r1`–`r7`, preferring their incoming argument registers. For example, `int add(int a, int b) { return a + b; }` begins with `add r1, r1, r2` and needs no frame.
- Functions with control flow assign frequently used values to `r8`–`r10` and,
  for fixed-address images, `r12`. Each function saves and restores only the
  callee-saved registers it actually uses, so those values survive calls and
  loop backedges. PIC images reserve `r12` instead.
- Values that die at a call can use `r3`–`r6` without save/restore traffic. Call arguments are placed as a parallel assignment, including register-cycle breaking and a safe mixed-source fallback.
- A global Tier 1 fixed point alternates local/CFG simplification with call-graph reachability. Non-recursive functions with exactly one surviving direct call site are relocated into that site and their standalone body is deleted. This naturally absorbs `main` into `_start` when possible.
- Known-symbol calls use explicit direct-call IR, while function-pointer calls remain indirect. This gives reachability, inlining, and tail-call analysis the callee symbol directly.
- Explicit CFG construction records predecessors and successors, selects one-target conditional branches with an implicit fallthrough edge inside the fixed point, removes unreachable blocks after branch folding, and deletes unused labels. Straight-line intrinsic functions use the leaf allocator, so input parameters can remain in their incoming registers.
- Safe tail calls restore the current frame and jump directly to the callee. Functions with addressable local objects stay on the ordinary call path because a callee may receive a pointer into that frame.
- After final layout, all symbolic fixed-address branches and direct calls relax to their shortest legal immediate or register-target encoding. Shrinking is repeated until instruction sizes and label addresses are stable, with no unreachable padding retained.
- Termination repeats one jump instruction. Fixed low-address images use `jmp immediate`; PIC and high-address images materialize the target once outside the loop and repeat `jmp r7`.

The next substantial opportunities are dead-global elimination, immutable-global load folding, bounded compile-time evaluation, loop analysis, paired division/remainder, common-subexpression elimination, and control-flow-aware stack-slot reuse. Stack-slot reuse must use control-flow liveness rather than textual instruction intervals because loop backedges make the latter incorrect. The dependency-ordered checklist is in [docs/optimization-todos.md](docs/optimization-todos.md).

## Comparing against GCC

`gcc-backend/` holds two independent GCC targets for this ISA, neither of
which is part of dyncc's own build:

- [`gcc-backend/symphony-gcc/`](gcc-backend/symphony-gcc/README.md) is a
  **real** GCC backend — a genuine `.md`/`.cc`/`.h` machine-description
  port (triple `symphony-elf`) that emits real target assembly, consumed
  by this project's own from-scratch assembler+linker (no ELF, no `as`,
  no `ld`) to produce a flat binary runnable on `symphony/emulator`, and
  linked against a real `libgcc.a` plus a runtime library (printf family,
  heap, I/O intrinsics) ported from dyncc's own C sources. It supports
  both Symphony and Dynphony encodings. See that README for the build
  recipe, known gaps, and a full dcc-vs-GCC size/step comparison table
  across `examples/*.c`.
- `gcc-backend/dynphony/` is an older, narrower bootstrap target used
  purely to benchmark dyncc's codegen/optimizer output against real GCC
  (`-Os`/`-O2`), by hijacking GCC's `moxie` target and hand-assembling its
  output into Symphony machine code — no real machine description, no
  libgcc, no linker of its own. See
  [gcc-backend/README.md](gcc-backend/README.md) for how to build it.

To build the real same-ISA GCC toolchain and generate the maintained example
comparison table in one command:

```sh
python tools/benchmark_examples.py --build-gcc
```

The first run installs the host build dependencies on Debian/Ubuntu systems,
clones GCC 14 into `.cache/symphony-gcc/`, applies the local Symphony target,
and builds `xgcc` plus `libgcc.a`. It can take some time. To do that step
explicitly (or choose a different cache directory), use:

```sh
tools/build_symphony_gcc.sh --cache /path/to/gcc-cache
SYMPHONY_GCC_PREFIX=/path/to/gcc-cache/build-stage1 \
  python tools/benchmark_examples.py
```

The generated [example benchmark table](docs/example-benchmarks.md) compares
the current checkout only with GCC `-Os` and `-O2`; it uses final target binary
size and native-emulator instruction steps with documented deterministic inputs.
Both size columns exclude zero-fill storage: dyncc defaults to
`--bss=assume-zeroed` in this harness, and the GCC measurement counts only its
loadable text and data rather than the linker's emulator-only trailing BSS
reservation. This also keeps BSS startup clearing out of the step counts.

The report separates terminating examples from `examples/unbounded/` frame
benchmarks. To make a new unbounded example measurable, publish exactly one
framebuffer-selection update after each completed frame. Double-buffered code
normally does this with `screen(1, newly_completed_back_buffer)`, changing the
address on every frame. A single-buffer or other frame system must explicitly
re-submit or change its framebuffer selection once per completed frame. The
first `screen(1, ...)` selection establishes the baseline; subsequent setting-1
updates are frame boundaries regardless of screen-mode configuration order. All
other screen settings are ignored. The benchmark warms up through frame 3 and
records the total instructions through frame 63.

## Project structure

```text
symphony/
  frontends/
    protocol.py      source-language frontend contract
    c/               C parsing, semantics, and common-IR lowering
  middle/
    model.py         shared types, symbols, typed nodes, and static objects
    ir.py            canonical language-neutral IR
    analysis/        CFG and reusable middle-end analyses
    passes/          fixed-point manager and optimization passes
  targets/symphony/
    registers.py     architectural register names
    abi.py           C calling-convention roles
    isa.py           instruction names and byte encoders
    config.py        target options and image metadata
    assembler.py     symbols, relocations, and final relaxation
    backend.py       instruction selection, registers, and stack frames
  runtime/           device declarations and selectively linked helpers
  emulator/          reference machine, device model, and native cores
  compiler.py        frontend-independent pipeline orchestrator
  project.py         DCP1/DCC1 persistent project and control records
  cli.py             scc/dcc: raw binary, IR dump, JSON map, optional execution
selfhost/       self-hosting compiler written in C
examples/       example C programs
tests/          compiler, integration, and encoding tests
docs/           language reference, design notes, and Dynphony ISA text
```

Thin root-level compatibility modules preserve imports such as `symphony.isa`
and `symphony.frontend`.

`examples/towers_of_hanoi.c` is a recursive controller for the Turing Complete
magnet puzzle. It reads the highest disk number, source, destination, and spare
locations from the first four inputs and emits the magnet-control sequence.

`examples/constant_folding.c` demonstrates whole-entry constant collapse. Its
three locals and `a + b * c` expression compile to `mov r1, 1466` followed by
the halt jump; no multiplication helper or standalone `main` remains.

`examples/interprocedural_constant_folding.c` computes the same value through a
sole-called `foo(int)`. Function relocation exposes its argument and locals to
the global fixed point, producing the identical 8-byte result.

`examples/arena_allocator.c` remains an example of a specialized bump allocator.
Ordinary programs can instead call the bundled `malloc`/`free` and `mem*`
functions directly without headers.

`examples/dynamic_sensor_report.c` is a complete input-driven example using the
new runtime. It grows a heap-backed sample vector, sorts with VLA workspace,
deduplicates overlapping storage, builds a heap-backed histogram and statistical
report, emits the results, and releases every allocation.

The API exposes each pipeline stage:

```python
from symphony import Target, compile_source

result = compile_source("int main(void) { return 6 * 7; }", target=Target(pic=True))
raw_bytes = result.image.binary
print(result.ir.dump())
print(result.image.metadata())
# result.parsed: pycparser AST; result.typed: independent typed AST
```

The emulator verifies generated binaries, and encoding tests compare with the supplied ISA text. Its flags model implements the specified signed/unsigned branch relations without assuming a hardware flag bit layout. **Execution on the actual Turing Complete circuit has not been verified.** See [docs/design.md](docs/design.md) for extension points and validation details.
