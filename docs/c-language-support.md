# C language and Symphony extension reference

Symphony C implements a practical freestanding subset of C. It preprocesses and
links one or more C translation units into a flat Symphony or Dynphony memory image without
invoking a host assembler, system linker, or operating-system runtime.

## Implemented C features

### Scalar types and conversions

| Feature | Status and target representation |
|---|---|
| `char` | 8-bit unsigned by default |
| `signed char`, `unsigned char` | 8 bit |
| `_Bool`, `bool` | 8-bit values canonically stored as `0` or `1`; include `<stdbool.h>` for the standard `bool`, `true`, and `false` macros |
| `short`, `unsigned short` | 16 bit |
| `int`, `unsigned int` | 32 bit |
| `long`, `unsigned long` | 32 bit, with the same current representation as `int` |
| `void` | Functions, return types, casts, and `void *` |
| Pointers | 32-bit byte addresses; object, pointer-to-pointer, and function pointers |
| Enums | Named or anonymous; implicit and integer-constant values; represented as signed 32-bit `int` |
| `const` | Const objects and pointers, modification diagnostics, and qualifier-preserving pointer conversions |
| Casts | Explicit integer and pointer casts, integer promotions, and usual arithmetic conversions for supported widths |

The standard spellings `signed` and `unsigned` mean `signed int` and `unsigned
int`. Names such as `uint` are not built-in C type names; a program may define
one with `typedef unsigned int uint;`.

Integer loads are big-endian. Eight- and sixteen-bit loads zero-extend; signed
values are explicitly sign-extended when required. Integer arithmetic wraps in
the generated machine operations. The compiler does not currently exploit
signed-overflow undefined behavior.

### Expressions and operators

- Decimal, octal, and hexadecimal integer constants, including supported `U`
  and `L` suffixes.
- Single-byte character and string literals, adjacent string concatenation, and
  ordinary C escapes.
- `//` line comments and `/* ... */` block comments.
- `+`, `-`, `*`, `/`, `%`, unary `+`/`-`, and bitwise complement.
- `&`, `|`, `^`, `<<`, and `>>`.
- `==`, `!=`, `<`, `<=`, `>`, and `>=`.
- Short-circuit `&&` and `||`, plus logical `!`.
- Assignment and arithmetic/bitwise compound assignment.
- Prefix and postfix increment/decrement.
- Address-of, dereference, array indexing, pointer arithmetic, and pointer
  difference.
- Conditional `?:` and comma expressions.
- `sizeof expression` and `sizeof(type)`, including declaration-time runtime
  sizes for variably modified array types.

Multiplication, division, and remainder use software helpers only when surviving
optimized code needs them. Constant expressions and suitable power-of-two
operations do not pull those helpers into the image.

### Declarations, storage, and scopes

- Local variables with nested lexical scopes.
- File-scope globals with zero or constant initialization.
- `static` file-scope objects and functions.
- Static local objects with program lifetime and block scope. Their initializers
  must currently be compile-time constants.
- File-scope `extern` declarations resolved across all linked translation units.
- File-scope and block-scope `typedef` declarations, including normal shadowing.
- Fixed-size arrays, multidimensional arrays, inferred outer bounds, partial
  initialization, brace elision, member designators such as `.field = value`,
  array-index designators such as `[3] = value`, and character-array
  initialization from strings.
- Runtime-sized local arrays (VLAs), including `char bytes[count]`,
  `int matrix[rows][columns]`, pointer-to-VLA parameters, and VLA typedefs. Bounds
  are evaluated once at declaration or function entry. Storage is reserved when the
  declaration executes and released when its block, loop, or function scope
  exits, including `break`, `continue`, and `return` paths.
- Symbolic static pointer initializers such as `int *p = &values[2]`.

### Structures

- Named and anonymous structures.
- Forward declarations and self-referential structure pointers.
- Natural member alignment capped at four bytes, including tail padding.
- Nested structures and arrays as members.
- Member access with `.` and `->`.
- Nested, designated, and brace-elided initialization for global and local
  structure objects.
- Anonymous structure and union members, with recursively promoted member
  access and member designators (for example, `value.x` and `.x = 1`).
- `sizeof` for complete structure types and structure expressions.
- Structure assignment, including assignments that overlap in memory.
- Structure-returning functions, including direct and function-pointer calls.
  The ABI passes a hidden pointer to caller-owned result storage as the first
  argument; source code still uses ordinary C `return object;` and
  `struct T value = make();` forms.

### Unions

- Named and anonymous unions, including nested arrays and structures as members.
- Member access with `.` and `->`, initialization of a selected member, and
  union assignment.
- Anonymous members promote their named submembers through surrounding
  structures and unions. An ambiguous promoted name is diagnosed.

### Statements and functions

- Expression, compound, and return statements.
- `if`/`else`.
- `while`, `do`/`while`, and `for`, including declaration initializers.
- `break` and `continue` inside loops.
- `switch` with integer constant-expression `case` labels, `default`,
  fallthrough, and `break`. The selector is evaluated once and currently
  lowers to a linear comparison-and-branch chain; no jump-table or hash
  dispatch is emitted.
- Function-scope labels and forward/backward `goto`. Jumps leaving VLA scopes
  release their dynamic stack storage; jumps entering the scope of a variably
  modified object are diagnosed, as required by C.
- Function declarations and definitions, direct calls, indirect function-pointer
  calls, ordinary recursion, and optimized tail calls.
- Scalar parameters and scalar return values. Structures may be returned by
  value through a hidden caller-provided result pointer; aggregate parameters
  remain unsupported. Arguments one through seven use `r1` through `r7`; later
  scalar arguments are passed on the stack. `r1` holds a scalar return value.

The program entry point must be `int main(void)` or `int main()`. In this subset,
an empty parameter list means no parameters. Falling out of `main` returns zero.

[`examples/c_aggregate_compat.c`](../examples/c_aggregate_compat.c) is an
executable combined example of forward `goto`, `switch`, designated and
brace-elided initialization, unions, aggregate assignment, and structure
returns.

[`examples/anonymous_aggregate_members.c`](../examples/anonymous_aggregate_members.c)
shows a tagged point-or-color value using anonymous struct/union member
promotion, a structure-returning constructor, and pointer-based mutation.

## Unsupported or incomplete C features

| Area | Missing support |
|---|---|
| Source processing | Macro stringification/pasting, variadic macros, full hosted headers, and some implementation-specific directives |
| Separate compilation | Serializable object files, archives, incremental linking, external binary libraries, and dynamic linking |
| Types | `long long`, floating point, complex types, and bit-fields |
| Qualifiers/specifiers | `volatile`, `restrict`, `_Atomic`, thread-local storage, and local `extern` |
| Aggregate operations | Aggregates passed to functions by value, unions returned by value, and aggregate assignment whose right-hand side is not an lvalue |
| Initializers | Some complex continuation cases after nested designated initializers |
| Arrays | Flexible array members |
| Functions | Variadic functions, old-style definitions, aggregate parameter conventions, and union-return conventions |
| Hosted runtime | File I/O, locale, and the rest of a hosted C library beyond the small freestanding headers listed below |
| Character support | Wide and Unicode character/string literal types |
| Low-level extensions | Inline assembly and compiler-specific attribute syntax |

Multiple tentative definitions of one global are rejected rather than merged.
Non-VLA array bounds must be compile-time constants. Designators may select a
structure member or a constant fixed-array index; nested/brace-elided forms work
for ordinary aggregate initialization, while some C continuation edge cases after
a nested designator remain unsupported. Decimal constants above `2147483647`
need an explicit `U` suffix when their value fits `unsigned int`; values that
require a 64-bit C type are unsupported.

### Dynamic storage and runtime-sized arrays

Fixed arrays are complete: this includes `char text[64]`, inferred bounds from
string literals, multidimensional arrays, globals, locals, indexing, and
pointer decay. The compiler also supports using a pointer as dynamic storage
once application code obtains that pointer; `examples/arena_allocator.c` shows
an aligned bump allocator built entirely in the supported subset.

VLA bounds and strides are saved when their declarations execute. Runtime size
multiplication is overflow-checked; invalid zero-sized, overflowing, or
heap-colliding dynamic stack allocations enter the `_stack_overflow` loop.

Include `<stdlib.h>` for `malloc`, `free`, `calloc`, and `realloc`; include
`<string.h>` for `memcpy`, `memmove`, `memset`, and `memcmp`. Their size/count
parameters use `size_t`, the target's 32-bit unsigned size type. The heap begins after all static
storage (including BSS), grows upward, and is checked against the live descending
stack whenever it grows. Allocations are 4-byte aligned. The allocator uses a
first-fit free list with block splitting and adjacent-block coalescing. Zero-size
allocation returns null; allocation failure and `calloc` multiplication overflow
also return null. As in C, invalid frees, double frees, and overlapping `memcpy`
arguments have undefined behavior; use `memmove` for overlap.

Generated code does not trap null dereferences, ordinary out-of-bounds accesses,
invalid shifts, division by zero, or fixed-frame/recursive stack exhaustion.
Dynamic VLA allocation does trap size overflow and collision with static or heap
storage. Heap growth detects the current stack boundary, but a later unusually
deep ordinary call can still collide with an existing allocation. The compiler
checks that the static image and largest individual frame fit configured RAM,
but recursion depth remains a program responsibility.

## Library and Symphony headers

The compiler does not inject library declarations into every translation unit.
The available freestanding headers are `<stdbool.h>`, `<stddef.h>`,
`<stdint.h>`, `<stdio.h>`, `<stdlib.h>`, and `<string.h>`. Include `<stdio.h>`
for literal-format `printf`.

Target-specific APIs are kept separate in `<symphony.h>`. Include it to declare
the following functions; device calls still lower directly to instructions
without normal function-call overhead.

```c
unsigned int input(void);
void output(unsigned int value);

unsigned int keyboard(void);
void screen(unsigned int setting, unsigned int value);

unsigned int time(void);
unsigned int time_low(void);
unsigned int time_high(void);

unsigned int persistent_load(unsigned int address);
void persistent_store(unsigned int address, unsigned int value);
void jump(unsigned int address);

char *screen_framebuffer(void);
void screen_cursor(unsigned int x, unsigned int y);
```

| Built-in | Meaning |
|---|---|
| `input()` | Read the next value from the ordinary input device |
| `output(value)` | Send one value to the ordinary output device |
| `keyboard()` | Read the keyboard input value |
| `screen(setting, value)` | Send a setting/value update to the screen device |
| `time()` | Alias for the low 32 bits of the time device |
| `time_low()` | Read the low 32 bits of time |
| `time_high()` | Read the high 32 bits of time |
| `jump(address)` | Transfer execution to an address without returning |
| `persistent_load(address)` | Load a value from persistent storage |
| `persistent_store(address, value)` | Store a value in persistent storage |
| `screen_framebuffer()` | Return the writable ASCII framebuffer as `char *` |
| `screen_cursor(x, y)` | Set the next `printf` cell; coordinates are clamped to the framebuffer |

These names are reserved and cannot be redefined by a program. The reference
emulator records `output` values and screen updates, accepts queued input and
keyboard values, and models configurable persistent storage.

`printf(format, ...)` writes to the compiler-provided 96×40 ASCII framebuffer.
Its format must be a literal and supports `%%`, `%c`, `%d`, `%u`, `%x`, and
`%s`. Variadic function declarations are not generally supported yet; the
minimal `<stdio.h>` declaration is recognized specially for this built-in.

The text framebuffer is allocated in BSS only when a direct call to `printf`,
`screen_framebuffer`, or `screen_cursor` appears in the source. It is therefore
placed immediately after the serialized image without increasing the binary by
3,840 bytes. Startup clears the complete BSS range, selects ASCII 8 mode, and
points the screen at the framebuffer while leaving color and font settings
unchanged. `--bss=assume-zeroed` omits that clear when the loader/platform
guarantees zero-filled RAM. Newlines
advance to the next 96-character row. Output past row 40 is discarded;
scrolling can be implemented by application code that rewrites the buffer and
calls `screen_cursor`.

### Static storage layout

After whole-program optimization removes unused globals, static objects are
laid out in three logical sections. RODATA contains string literals and const
objects; DATA contains initialized mutable objects and address relocations; BSS
contains selected all-zero, non-relocatable mutable objects, including
zero-initialized globals and static locals. RODATA and DATA are serialized in
the raw image; BSS has virtual addresses after it and is zeroed at startup.
`--bss=auto` (the default) selects BSS only when its word-clear code is smaller
than serializing the zero bytes. `--bss=always` and `--bss=never` force either
layout; `--bss=assume-zeroed` uses BSS without emitting startup clears. The raw
image has no hardware read-only mapping yet, so RODATA is an organizational
distinction rather than write protection.

## Other target facilities

- Flat raw binary output with code and static data in one unified memory image.
- Configurable power-of-two RAM and persistent-memory sizes.
- Configurable fixed load address, defaulting to zero.
- Optional position-independent output. PIC startup obtains the runtime image
  base with `counter` and rebases static pointer initializers.
- A JSON symbol/map output and an inspectable optimized IR dump.
- A reference emulator for generated instruction bytes.
- Software arithmetic and memory/allocation helpers, linked into the image only
  when reachable.
- Infinite self-jump termination with the value returned by `main` retained in
  `r1`.

The arena allocator example demonstrates a specialized application-owned bump
allocator; the bundled free-list allocator covers ordinary dynamic allocation.
