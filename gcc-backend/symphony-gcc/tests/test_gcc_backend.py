"""Regression test suite for the real Symphony/Dynphony GCC backend
(gcc-backend/symphony-gcc/). Opt-in and skipped by default: unlike the
repo's fast default suite (tests/test_compiler.py, tests/test_integration.py,
which need nothing but the Python package), these tests need a fully built
GCC cross-compiler (xgcc) + real libgcc.a + this project's custom
assembler/linker, which takes a real GCC bootstrap (~30-60 minutes from
scratch) to produce. See "Running the tests" in
gcc-backend/symphony-gcc/README.md for the exact build recipe.

Set SYMPHONY_GCC_PREFIX to a built GCC stage1 directory (containing
`gcc/xgcc`, `gcc/as` -- the assembler wrapper script -- and
`symphony-elf/libgcc/libgcc.a`) to enable this module; otherwise every
test here is skipped automatically (pytest collects the module either
way, so a typo in the env var shows up as "all skipped", not "not found").

Every test in this module actually compiles real C source through xgcc,
assembles it with the real assembler, links it with the real linker
(against real libgcc and, where relevant, the real runtime library), and
runs the resulting flat binary on symphony.emulator's Machine -- asserting
on the actual numeric/output result, not just "it built". This is what
distinguishes this suite from all of this project's prior verification,
which was ad hoc (write a .c file, compile/run by hand, check output,
throw it away) and left nothing behind for future regressions to trip.

The four dedicated bug-regression tests below (frame pointer placement,
missing callee-saved registers, assembler pass1/pass2 desync, and the
__dyn_heap_anchor BSS-ordering bug) were each independently verified
during this suite's development to actually FAIL when run against a
scratch build with the corresponding fix reverted, and PASS again once
the fix was restored -- see gcc-backend/symphony-gcc/README.md's
"Running the tests" section and the project memory file for that
verification trail. That is the standard a regression test needs to
meet: it must be shown to have teeth, not just to pass against
already-fixed code.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from toolchain import CompileError, Toolchain, ToolchainNotBuilt, screen_text, toolchain_prefix  # noqa: E402
from symphony.emulator import Machine  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
SELFHOST_SOURCES = tuple(sorted(
    path.relative_to(REPO_ROOT / "selfhost").as_posix()
    for path in (REPO_ROOT / "selfhost" / "src").rglob("*.c")
))


pytestmark = pytest.mark.skipif(
    toolchain_prefix() is None,
    reason=(
        "SYMPHONY_GCC_PREFIX is not set -- these tests need a fully built "
        "GCC cross-compiler + real libgcc.a for this target (a real GCC "
        "bootstrap, ~30-60 minutes from scratch). See gcc-backend/"
        "symphony-gcc/README.md's 'Running the tests' section."
    ),
)


@pytest.fixture(scope="session")
def toolchain():
    prefix = toolchain_prefix()
    try:
        return Toolchain(prefix)
    except ToolchainNotBuilt as exc:
        pytest.skip(str(exc))


# ---------------------------------------------------------------------
# Milestone-3-style basic sanity: trivial functions, loops, direct calls,
# mixed leaf/non-leaf, at -O0 and -O2.
# ---------------------------------------------------------------------

class TestBasicSanity:
    @pytest.mark.parametrize("optimize", ["-O0", "-O1", "-O2"])
    def test_trivial_add_function(self, toolchain, tmp_path, optimize):
        src = """
        int add(int a, int b) { return a + b; }
        int main(void) { return add(6, 7); }
        """
        result, machine, symtab = toolchain.build_and_run(
            src, tmp_path, name=f"add_{optimize.strip('-')}", optimize=optimize
        )
        assert result == 13

    @pytest.mark.parametrize("optimize", ["-O0", "-O2"])
    def test_loop_and_direct_calls(self, toolchain, tmp_path, optimize):
        src = """
        int sum_to(int n) {
            int total = 0;
            for (int i = 1; i <= n; i++) total += i;
            return total;
        }
        int doubled_sum(int n) { return sum_to(n) * 2; }
        int main(void) { return doubled_sum(10); }
        """
        result, machine, symtab = toolchain.build_and_run(
            src, tmp_path, name=f"loop_{optimize.strip('-')}", optimize=optimize
        )
        assert result == 110

    @pytest.mark.parametrize("optimize", ["-O0", "-O2"])
    def test_mixed_leaf_and_nonleaf_functions(self, toolchain, tmp_path, optimize):
        src = """
        int leaf_double(int x) { return x * 2; }
        int nonleaf_wrapper(int x) { return leaf_double(x) + leaf_double(x + 1); }
        int main(void) { return nonleaf_wrapper(5); }
        """
        result, machine, symtab = toolchain.build_and_run(
            src, tmp_path, name=f"mixed_{optimize.strip('-')}", optimize=optimize
        )
        assert result == 5 * 2 + 6 * 2

    @pytest.mark.parametrize("optimize", ["-O0", "-Os", "-O2"])
    def test_eighth_argument_is_loaded_from_stack(self, toolchain, tmp_path, optimize):
        src = """
        void set_eighth(unsigned int a, unsigned int b, unsigned int c,
                        unsigned int d, unsigned int e, unsigned int f,
                        unsigned int g, unsigned int *out) {
            *out = a + b + c + d + e + f + g;
        }
        int main(void) {
            unsigned int value = 0;
            set_eighth(1, 2, 3, 4, 5, 6, 7, &value);
            return (int)value;
        }
        """
        result, _machine, _symtab = toolchain.build_and_run(
            src,
            tmp_path,
            name=f"stack_arg_{optimize.strip('-')}",
            optimize=optimize,
        )
        assert result == 28

    def test_eighth_argument_with_fixed_callee_saved_register(self, toolchain, tmp_path):
        """The fixed incoming save area must remain 24 bytes even when a
        caller reserves one of its normally callee-saved registers."""
        src = """
        int eighth(int a, int b, int c, int d, int e, int f, int g, int h) {
            return h;
        }
        int main(void) { return eighth(1, 2, 3, 4, 5, 6, 7, 42); }
        """
        result, _machine, _symtab = toolchain.build_and_run(
            src,
            tmp_path,
            name="stack_arg_fixed_r8",
            optimize="-O2",
            extra_flags=("-ffixed-r8",),
        )
        assert result == 42


def test_linker_relaxes_far_direct_call():
    """A linked image may put a direct-call target above U16 without
    changing the compact form of ordinary calls."""
    from symphony_as import Assembler
    from symphony_ld import Linker
    from symphony_obj import ObjectFile

    caller_asm = Assembler("caller")
    caller_asm.assemble("""
        .text
        .global main
    main:
        link_call target
        link_return
    """)
    target_asm = Assembler("target")
    target_asm.assemble("""
        .text
        .global target
    target:
        mov r1, 42
        link_return
    """)
    linker = Linker()
    linker.add_object(caller_asm.to_object())
    # This object is never executed; it gives target a >64KiB text address.
    linker.add_object(ObjectFile("padding", text=bytes(0x10000)))
    linker.add_object(target_asm.to_object())
    image, symbols, entry = linker.link(entry_symbol="main")
    assert symbols["target"] > 0xFFFF
    machine = Machine(image, symphony=True)
    machine.pc = entry
    machine.regs[13] = 0xFFFFF0
    assert machine.run(0xFFFFF0, max_steps=50) == 42


def test_global_relocation_ignores_another_units_static_label():
    """A duplicate unit name must not turn an external relocation into a
    reference to another input object's local label."""
    from symphony_as import Assembler
    from symphony_ld import Linker

    caller = Assembler("shared")
    caller.assemble("""
        .text
        .global main
    main:
        link_call target
        link_return
    """)
    shadow = Assembler("shared")
    shadow.assemble("""
        .text
    target:
        mov r1, 111
        link_return
    """)
    provider = Assembler("provider")
    provider.assemble("""
        .text
        .global target
    target:
        mov r1, 222
        link_return
    """)
    linker = Linker()
    linker.add_object(caller.to_object())
    linker.add_object(shadow.to_object())
    linker.add_object(provider.to_object())
    image, _symbols, entry = linker.link(entry_symbol="main")
    machine = Machine(image, symphony=True)
    machine.pc = entry
    machine.regs[13] = 0xFFFFF0
    assert machine.run(0xFFFFF0, max_steps=50) == 222


@pytest.mark.parametrize("optimize", ["-Os", "-O2"])
@pytest.mark.parametrize("relative", SELFHOST_SOURCES)
def test_selfhost_translation_unit_compiles_optimized(toolchain, tmp_path, optimize, relative):
    """Keep the two former reload-ICE sites covered as part of the actual
    complete self-host compiler source set, not as synthetic lookalikes."""
    source = REPO_ROOT / "selfhost" / relative
    toolchain.compile_to_asm(
        source,
        tmp_path / f"{source.stem}_{optimize[1:]}.s",
        optimize=optimize,
        extra_flags=(f"-I{REPO_ROOT / 'selfhost' / 'include'}",),
    )


# ---------------------------------------------------------------------
# Real libgcc soft arithmetic: __mulsi3/__divsi3/__udivsi3/__modsi3/
# __umodsi3, signed and unsigned, at -O0 and -O2.
# ---------------------------------------------------------------------

class TestLibgccArithmetic:
    def test_signed_multiply_divide_modulo(self, toolchain, tmp_path):
        src = """
        int compute(int a, int b) { return a * b + a / b + a % b; }
        int main(void) { return compute(-17, 5); }
        """
        a, b = -17, 5
        q = abs(a) // abs(b)
        if (a < 0) != (b < 0):
            q = -q
        expected = (a * b + q + (a - q * b)) & 0xFFFFFFFF
        for optimize in ("-O0", "-O2"):
            result, machine, symtab = toolchain.build_and_run(
                src, tmp_path, name=f"signed_arith_{optimize.strip('-')}", optimize=optimize
            )
            assert result == expected

    def test_unsigned_multiply_divide_modulo(self, toolchain, tmp_path):
        src = """
        unsigned int computeu(unsigned int a, unsigned int b) {
            return a * b + a / b + a % b;
        }
        int main(void) { return (int)computeu(17u, 5u); }
        """
        a, b = 17, 5
        expected = (a * b + a // b + a % b) & 0xFFFFFFFF
        for optimize in ("-O0", "-O2"):
            result, machine, symtab = toolchain.build_and_run(
                src, tmp_path, name=f"unsigned_arith_{optimize.strip('-')}", optimize=optimize
            )
            assert result == expected


# ---------------------------------------------------------------------
# Runtime/libc: malloc/free/calloc/realloc, printf family arities.
# ---------------------------------------------------------------------

class TestRuntimeLibc:
    def test_persistent_memory_intrinsics(self, toolchain, tmp_path):
        src = """
        #include <symphony.h>
        int main(void) {
            persistent_store(12, 0x12345678u);
            return persistent_load(12);
        }
        """
        objects = [toolchain.compile_and_assemble(src, tmp_path, name="persistent")]
        objects += toolchain.full_runtime_objects(tmp_path)
        image, _symbols, entry = toolchain.link(objects, entry_symbol="main")
        machine = Machine(image, persistent_size=256, symphony=True)
        machine.pc = entry
        machine.regs[14] = 0x800000
        machine.regs[13] = 0xfffff0
        result = machine.run(0xfffff0)
        assert result == 0x12345678
        assert machine.persistent_read(12) == 0x12345678

    def test_malloc_free_calloc_realloc_correctness(self, toolchain, tmp_path):
        src = """
        void *malloc(unsigned int);
        void *calloc(unsigned int, unsigned int);
        void *realloc(void*, unsigned int);
        void free(void*);
        int main(void) {
            int *a = malloc(4 * sizeof(int));
            for (int i = 0; i < 4; i++) a[i] = i * 10;
            int *z = calloc(4, sizeof(int));
            int zsum = z[0] + z[1] + z[2] + z[3];
            free(z);
            int *b = realloc(a, 8 * sizeof(int));
            for (int i = 4; i < 8; i++) b[i] = i * 10;
            int sum = 0;
            for (int i = 0; i < 8; i++) sum += b[i];
            free(b);
            return sum + zsum;
        }
        """
        result, machine, symtab = toolchain.build_and_run(src, tmp_path, name="heap_correctness")
        assert result == sum(i * 10 for i in range(8))

    def test_printf_family_all_arities(self, toolchain, tmp_path):
        src = """
        int printf(const char*);
        int printf1(const char*, unsigned int);
        int printf2(const char*, unsigned int, unsigned int);
        int printf3(const char*, unsigned int, unsigned int, unsigned int);
        int main(void) {
            printf("no-args");
            printf1(" one=%d", 42u);
            printf2(" two=%u,%x", 7u, 255u);
            printf3(" three=%d,%d,%d", (unsigned int)-5, 1u, 2u);
            return 0;
        }
        """
        result, machine, symtab = toolchain.build_and_run(src, tmp_path, name="printf_arities")
        text = screen_text(machine, symtab)
        assert text.startswith("no-args one=42 two=7,ff three=-5,1,2")


# ---------------------------------------------------------------------
# Known, documented-but-not-fixed ICE gap: 64-bit multiply/divide
# (_muldi3 etc.) hit the reload ICE class inside the libgcc build and are
# excluded from libgcc entirely (see libgcc-config/symphony/t-symphony).
# These are xfail, not skip, so a future fix flips them green automatically
# instead of the gap silently going untracked.
# ---------------------------------------------------------------------

class TestKnownIceGaps:
    def test_calloc_compiles_at_o1(self, toolchain, tmp_path):
        """The narrow-move memory alternatives also close calloc's former
        -O1 reload ICE; this strict xfail became an XPASS with that fix."""
        toolchain.compile_to_asm(
            Path(__file__).resolve().parents[1] / "runtime" / "heap.c",
            tmp_path / "heap_o1.s",
            optimize="-O1",
        )

    def test_calloc_compiles_at_o2(self, toolchain, tmp_path):
        """Was xfail (strict) before the *movsi_reg memory-alternative fix
        (README.md's Known gaps 'Bug 4') -- calloc() now compiles cleanly
        at -O2, confirmed as a direct side effect of that fix, not assumed."""
        toolchain.compile_to_asm(
            Path(__file__).resolve().parents[1] / "runtime" / "heap.c",
            tmp_path / "heap_o2.s",
            optimize="-O2",
        )

    @pytest.mark.parametrize("optimize", ["-O1", "-O2"])
    def test_printf_unsigned_compiles_at_o1_o2(self, toolchain, tmp_path, optimize):
        """Was xfail (strict) at both -O1 and -O2 before the *movsi_reg
        memory-alternative fix (README.md's Known gaps 'Bug 4') --
        __dyn_printf_unsigned() now compiles cleanly at both levels,
        confirmed as a direct side effect of that fix, not assumed."""
        toolchain.compile_to_asm(
            Path(__file__).resolve().parents[1] / "runtime" / "printf.c",
            tmp_path / f"printf_{optimize.strip('-')}.s",
            optimize=optimize,
        )

    @pytest.mark.xfail(
        reason="64-bit (long long) multiply (__muldi3) hits the same "
        "'maximum number of generated reload insns' ICE class inside "
        "libgcc's own build at -O2 (libgcc's required optimization level) "
        "and is therefore excluded from libgcc entirely (LIB2FUNCS_EXCLUDE "
        "in libgcc-config/symphony/t-symphony) -- not root-caused, not "
        "fixed. User code that compiles a 64-bit multiply builds fine "
        "(GCC just emits a libcall to __muldi3), so the gap only surfaces "
        "at LINK time: __muldi3 is undefined in libgcc.a. This test "
        "documents that by compiling+assembling a real 64-bit multiply and "
        "expecting the link step to fail with an undefined-symbol error, "
        "so a future fix (adding __muldi3 back to libgcc) flips it green "
        "automatically.",
        strict=True,
    )
    def test_64bit_multiply_links(self, toolchain, tmp_path):
        src = "long long mul64(long long a, long long b) { return a * b; }\n" \
              "int main(void) { return (int)mul64(3, 4); }\n"
        obj = toolchain.compile_and_assemble(src, tmp_path, name="mul64", optimize="-O2")
        # Expected to raise ValueError("undefined symbol '__muldi3'") --
        # if this stops raising, __muldi3 has been added back to libgcc
        # and the xfail should be removed.
        toolchain.link([obj], entry_symbol="main")


# ---------------------------------------------------------------------
# Bug-specific regression tests. Each of these reproduces the exact shape
# of program that triggered a real, silently-wrong-output bug found and
# fixed during this project (see the fix commits named in each docstring
# and gcc-backend/symphony-gcc/README.md's "Fixed bugs" section).
# ---------------------------------------------------------------------

class TestBugRegressions:
    def test_frame_pointer_top_of_frame(self, toolchain, tmp_path):
        """Regression test for commit 4ed9415 ("Fix hard frame pointer to
        point at top of frame, not bottom").

        symphony_expand_prologue used to materialize r11 (the hard frame
        pointer) AFTER `sub sp,sp,size` -- i.e. at the BOTTOM of the
        frame. With FRAME_GROWS_DOWNWARD and STARTING_FRAME_OFFSET=0, GCC
        assigns every local a negative offset from r11, so locals landed
        below the allocated frame entirely -- directly in the red zone a
        callee's own prologue pushes (push r13/push r11) also write into,
        silently corrupting the caller's locals the moment any non-leaf
        call happened.

        This reproduces that exact shape: compute() has three locals and
        calls a non-leaf helper() (which itself calls into libgcc's
        __divsi3, forcing helper to establish its own real frame) --
        exactly the "compute() calling a non-leaf routine" stress case
        from milestone 3/4 verification. If locals were misplaced, the
        post-call sums would silently read back wrong values with no
        build-time diagnostic.

        Independently verified (during this suite's development, not
        re-verified automatically here): reverting just this fix and
        rebuilding stage1 makes this exact test fail (wrong numeric
        result, no crash) at -O0; restoring the fix makes it pass again.
        """
        src = """
        int helper(int x) { return (x * 3) / 2; }
        int compute(int a, int b, int c) {
            int local1 = a + 1;
            int local2 = b + 2;
            int local3 = c + 3;
            int r = helper(a + b + c);
            return local1 + local2 + local3 + r;
        }
        int main(void) { return compute(10, 20, 30); }
        """
        expected = ((10 + 1) + (20 + 2) + (30 + 3) + ((10 + 20 + 30) * 3) // 2) & 0xFFFFFFFF
        for optimize in ("-O0", "-O2"):
            result, machine, symtab = toolchain.build_and_run(
                src, tmp_path, name=f"framebug_{optimize.strip('-')}", optimize=optimize
            )
            assert result == expected

    def test_callee_saved_registers_survive_nested_calls(self, toolchain, tmp_path):
        """Regression test for commit 1744570 ("save/restore callee-saved
        r8-r10/r12 in prologue/epilogue").

        CALL_USED_REGISTERS in symphony.h declares r8, r9, r10 and r12
        callee-saved (the ABI promises callers these survive an ordinary
        call), but symphony_expand_prologue/_epilogue used to only ever
        push/pop r13 and r11 -- nothing saved the others. A non-leaf
        function holding a live value in one of r8-r10/r12 across its OWN
        call to another non-leaf function had that value silently
        clobbered by the callee (which, following the same buggy
        prologue, never saved/restored it either).

        This reproduces that shape at -O2, confirmed by inspecting the
        generated assembly during development: `helper` materializes two
        live values into r8/r10 and calls `side_effect` through r12,
        keeping the first call's result live in r9 across the second
        call -- and `side_effect` itself, given the same multi-call
        shape one level down (via `inner`), also uses r8/r9/r10/r12
        internally. Without the fix, side_effect's own use of those
        registers clobbers helper's still-live copies with no
        diagnostic, just a wrong final sum.

        Independently verified (during this suite's development, not
        re-verified automatically here): reverting just this fix and
        rebuilding stage1 makes this exact test return 3745 instead of
        the correct 5131 -- a silent wrong-output bug, not a crash;
        restoring the fix makes it pass again.
        """
        src = """
        __attribute__((noinline)) int inner(int x) {
            return x * 97 + 3;
        }
        __attribute__((noinline)) int side_effect(int x) {
            int p = x + 1, q = x + 2;
            return inner(p) + inner(q) + p + q;
        }
        __attribute__((noinline)) int helper(int x) {
            int a = x + 1, b = x + 2;
            return side_effect(a) + side_effect(b) + a + b;
        }
        int main(void) {
            volatile int n = 10;
            return helper(n);
        }
        """
        result, machine, symtab = toolchain.build_and_run(
            src, tmp_path, name="calleesaved", optimize="-O2"
        )
        assert result == 5131

    def test_assembler_data_before_code_layout(self, toolchain, tmp_path):
        """Regression test for commit edf4f89 ("fix assembler pass1/pass2
        layout desync").

        symphony_as.py's pass 1 used to write data-directive bytes
        (`.ascii`, `.zero`, `.p2align` padding, etc.) directly into the
        output section bytearrays while pass 2 independently re-tracked
        byte offsets from zero for instruction encoding -- the moment any
        data directive preceded code in the same section (i.e. almost any
        translation unit with a string literal, or, as reproduced here in
        hand-written .s, a `.zero` reservation), the two coordinate
        systems desynced: pass 2 re-emitted the SAME bytes pass 1 already
        wrote, corrupting every later relocation site's actual offset by
        the directive's length with no build-time diagnostic.

        This assembles hand-written .s directly (bypassing xgcc, to
        control the exact layout deterministically): an 8-byte `.zero`
        reservation and a string literal both precede `_start`, which
        `link_call`s a labeled function (`compute`) further down in the
        same .text section -- if the desync bug were present, that call
        would jump to a garbage address instead of `compute`.

        Independently verified (during this suite's development, not
        re-verified automatically here): assembling this exact .s with
        the pre-fix assembler (edf4f89~1) produces a binary that hangs
        (PC never reaches the halt address, execution-limit exceeded)
        instead of returning 99; the fixed assembler returns 99.
        """
        asm_source = """\
	.text
	.global	_start
msg:
	.ascii	"hi!\\0"
	.zero	4
_start:
	link_call	compute
	link_return
compute:
	mov	r1, 99
	link_return
"""
        obj = toolchain.assemble_asm_source(asm_source, tmp_path, name="desync")
        result, machine, symtab = toolchain.link_and_run([obj], entry_symbol="_start")
        assert result == 99

    def test_heap_anchor_bss_ordering(self, toolchain, tmp_path):
        """Regression test for commit 4196956 ("fix printf3+malloc hang --
        __dyn_heap_anchor BSS-ordering bug").

        runtime/heap.c's malloc() used to compute "start of heap" as the
        address right after its own __dyn_heap_anchor[7] BSS array,
        relying on that array being the LAST symbol in the whole linked
        image -- true only by accident of link/object order, since
        symphony_ld.py's Linker.link() lays out each object's BSS in
        whatever order objects were added. As soon as another object's
        BSS landed after heap.o's (normal in any real multi-file link),
        the first malloc()'s returned block silently overlapped that
        neighbor's storage instead of free memory; a second malloc() grew
        the heap pointer far enough for the corruption to reach a symbol
        (__dyn_heap_end itself, in the original trace) whose corrupted
        value later got used as a jump target, manifesting as printf3()
        hanging several calls downstream.

        This is the exact repro used to root-cause and verify the fix:
        two malloc() calls (to grow the heap pointer far enough to
        collide with a neighboring BSS symbol under the old buggy
        layout), then a printf3() call (which needs several live
        argument registers), linked with heap.o deliberately NOT last
        (intrinsics.c, heap.c, printf.c order -- heap.o links before
        printf.o, matching the original trigger condition) -- verifies
        both the return value and the actual printed screen output.

        Independently verified (during this suite's development, not
        re-verified automatically here): linking the equivalent program
        against the pre-fix runtime/heap.c + the pre-fix symphony_ld.py
        (4196956~1) hangs (execution-limit exceeded, PC wanders into
        corrupted memory); the fixed linker halts cleanly with the
        correct result.
        """
        src = """
        void *malloc(unsigned int);
        int printf3(const char*, unsigned int, unsigned int, unsigned int);
        int main(void) {
            int *a = malloc(16);
            int *b = malloc(16);
            *a = 111;
            *b = 222;
            printf3("a=%d b=%d sum=%d", *a, *b, *a + *b);
            return *a + *b;
        }
        """
        result, machine, symtab = toolchain.build_and_run(
            src, tmp_path, name="heapanchor",
            runtime_order=["intrinsics.c", "heap.c", "printf.c"],  # heap.o NOT last
        )
        assert result == 333
        text = screen_text(machine, symtab)
        assert text.startswith("a=111 b=222 sum=333")

    def test_heap_anchor_stale_object_rejected(self, toolchain, tmp_path):
        """Negative-test companion to test_heap_anchor_bss_ordering: the
        linker's new synthesize-__dyn_heap_anchor-itself logic (see
        symphony_ld.py's Linker.link()) raises a clear ValueError if any
        input object still DEFINES __dyn_heap_anchor as a real symbol --
        i.e. a stale .o built against the pre-fix runtime/heap.c (which
        reserved real 7-byte BSS storage for it). Without this check, a
        stale object would silently reintroduce the exact BSS-ordering
        bug this fix removes, with no diagnostic.
        """
        old_heap_c = """
        unsigned char __dyn_heap_anchor[7];
        int use_anchor(void) { return __dyn_heap_anchor[0]; }
        """
        obj = toolchain.compile_and_assemble(old_heap_c, tmp_path, name="stale_heap")
        with pytest.raises(ValueError, match="__dyn_heap_anchor"):
            toolchain.link([obj], entry_symbol=None)

    def test_la_symbol_addend_relocation(self, toolchain, tmp_path):
        """`la symbol+N` must retain N in its abs32 relocation.

        GCC's heap runtime initializes its bump pointer from
        ``__dyn_heap_anchor+3``.  The assembler originally parsed addends
        for data and call operands but recorded the whole ``la`` operand as
        a literal symbol name, making that runtime impossible to link.
        """
        asm_source = """\
\t.text
\t.global\t_start
_start:
\tla\tr1, __dyn_heap_anchor+3
\tlink_return
"""
        obj = toolchain.assemble_asm_source(asm_source, tmp_path, name="la_addend")
        result, _machine, symtab = toolchain.link_and_run([obj], entry_symbol="_start")
        assert result == symtab["__dyn_heap_anchor"] + 3

    def test_optimized_runtime_does_not_recurse_through_memset(self, toolchain, tmp_path):
        """The target runtime must not compile its own memset as a builtin call.

        At ``-O2``, without ``-fno-builtin``, GCC rewrote memset's byte loop
        into ``link_call memset`` with the original arguments.  Any runtime
        use therefore self-recursed until stack corruption.  A volatile
        function pointer prevents the test program itself from folding the
        call away and exercises the separately optimized runtime object.
        """
        src = """
        void *memset(void *, int, unsigned int);
        int main(void) {
            char bytes[4] = {0, 0, 0, 0};
            void *(*volatile fill)(void *, int, unsigned int) = memset;
            fill(bytes, 23, 4);
            return bytes[3];
        }
        """
        result, _machine, _symtab = toolchain.build_and_run(
            src, tmp_path, name="runtime_memset", optimize="-O2",
            runtime_optimize="-O2", max_steps=10_000,
        )
        assert result == 23

    def test_frame_pointer_not_reused_as_general_register(self, toolchain, tmp_path):
        """Regression test for the r11-not-FIXED_REGISTERS bug fixed
        alongside this test (see symphony.h's long comment above
        FIXED_REGISTERS).

        r11 (HARD_FRAME_POINTER_REGNUM) was not marked FIXED_REGISTERS,
        so IRA/LRA could treat it as an ordinary GENERAL_REGS value
        eligible for copy-propagation into pseudos -- e.g. ivopts
        hoisting a loop-invariant copy of r11 into a pseudo compared
        every loop iteration. Since r11 is permanently pinned to the
        frame-pointer role (symphony_frame_pointer_required() always
        returns true) and this target's flat register-class structure
        gave LRA no fallback, the resulting equivalence-substitution
        loop for that pseudo could never converge, hitting reload's
        "maximum number of generated reload insns per insn achieved
        (90)" internal compiler error.

        This is the minimal reproducer that was gdb-traced to root-cause
        the bug: a fill loop + an insertion-sort-shaped loop + a sum
        loop over a small char array, at -O2 -- structurally identical
        to examples/insertion_sort.c's main, which this fix closes at
        -O2 (previously ICE'd there per this README's Known gaps
        section; -Os and -O0 were unaffected). Asserts BOTH that it
        compiles (the ICE this test targets) and that -O2 and -O0
        produce the byte-identical correct result (the values are
        sorted in place, so their sum is order-independent and easy to
        check), since a compile-only assertion wouldn't catch a
        register-allocation fix that silently produces wrong code.
        """
        src = """
        int foo(void) {
            char values[16];
            int i;
            for (i = 0; i < 16; i++) values[i] = (char)(i * 3);

            int index;
            for (index = 1; index < 16; index++) {
                char value = values[index];
                int position = index;
                while (position > 0 && values[position - 1] > value) {
                    values[position] = values[position - 1];
                    position -= 1;
                }
                values[position] = value;
            }
            int sum = 0;
            for (index = 0; index < 16; index++) sum += values[index];
            return sum;
        }
        int main(void) { return foo(); }
        """
        result_o2, _, _ = toolchain.build_and_run(src, tmp_path, name="frameptr_o2", optimize="-O2")
        result_o0, _, _ = toolchain.build_and_run(src, tmp_path, name="frameptr_o0", optimize="-O0")
        assert result_o0 == result_o2
        assert result_o2 == 360  # sum of (i*3)&0xff, signed char, for i in 0..15

    def test_link_register_preserved_in_leaf_function(self, toolchain, tmp_path):
        """Regression test for the r13-save/restore-only-for-non-leaf bug
        fixed alongside this test (see symphony_expand_prologue/epilogue's
        long comment in symphony.cc).

        symphony_expand_prologue/epilogue only saved/restored r13 (the
        ABI link register) when the function itself made calls
        (!symphony_call_is_leaf()), on the assumption a leaf function
        "never touches r13". That's false: CALL_USED_REGISTERS marks
        r13 an ordinary allocatable register, so GCC's register
        allocator can (and, under enough register pressure, does) pick
        it to hold an arbitrary local in a LEAF function, silently
        destroying the caller's return address -- the leaf function's
        own `link_return` (`jmp r13`) then jumps into garbage instead of
        back to the caller.

        This is the minimal reproducer that was gdb/emulator-traced to
        root-cause the bug: a leaf function (no calls of its own) whose
        body is register-pressure-heavy enough at -O2 that the allocator
        picks r13 for the insertion-sort loop's `index` counter,
        structurally identical to examples/insertion_sort.c's main
        called from a real main() (needed here because the bug is about
        RETURNING from the leaf function correctly, which only shows up
        when something calls it and needs control back). Before the fix,
        this hung (execution-limit exceeded) at -O2; -O0 was unaffected
        since -O0 never puts enough pressure on r13 to get it allocated
        this way.
        """
        src = """
        int foo(void) {
            char values[16];
            int i;
            for (i = 0; i < 16; i++) values[i] = (char)(i * 3);

            int index;
            for (index = 1; index < 16; index++) {
                char value = values[index];
                int position = index;
                while (position > 0 && values[position - 1] > value) {
                    values[position] = values[position - 1];
                    position -= 1;
                }
                values[position] = value;
            }
            int sum = 0;
            for (index = 0; index < 16; index++) sum += values[index];
            return sum;
        }
        int main(void) { return foo(); }
        """
        result, _, _ = toolchain.build_and_run(
            src, tmp_path, name="linkreg", optimize="-O2", entry_symbol="main",
        )
        assert result == 360

    def test_movsi_memory_alternative_under_register_pressure(self, toolchain, tmp_path):
        """Regression test for the *movsi_reg-had-no-memory-alternative
        bug (see the long comment above *movsi_reg in symphony.md and
        this project's README's "Bug 4" writeup in Known gaps).

        The very first port of this target split SImode moves into an
        always-register-to-register *movsi_reg plus entirely separate
        *load_si/*store_si patterns for memory access, with no overlap.
        Under real register pressure (a recursive function with more
        live cross-call values than callee-saved hard registers, forcing
        IRA to spill a pseudo to a stack slot), LRA had no insn
        alternative that could accept the spilled pseudo's memory
        location directly -- *movsi_reg's only alternatives were
        register-to-register. Instead of reloading the operand in place,
        LRA fell back to its generic equivalence/inheritance
        substitution path, repeatedly minting fresh temporary pseudos
        that could ALSO fail to get a hard register under the same
        pressure, never converging -- hitting reload's "maximum number
        of generated reload insns per insn achieved (90)" internal
        compiler error.

        This reproduces that exact shape: a recursive function with 4
        parameters that must all survive its own recursive call (only 4
        callee-saved hard registers are available), structurally
        identical to examples/towers_of_hanoi.c's move_pile, which this
        fix closes at -O2 (previously ICE'd there at every optimization
        level above -O0). Asserts both that it compiles at -O2 (the ICE
        this test targets) and that -O0 and -O2 produce the
        byte-identical correct result via their side-effecting output()
        call sequence, since a compile-only assertion wouldn't catch a
        reload fix that silently produced wrong code.
        """
        src = """
        void output(unsigned int x);
        void move_one(unsigned int source, unsigned int destination) {
            output(source);
            output(5);
            output(destination);
            output(5);
        }
        void move_pile(
            unsigned int highest_disk,
            unsigned int source,
            unsigned int destination,
            unsigned int spare
        ) {
            if (highest_disk != 0) {
                move_pile(highest_disk - 1, source, spare, destination);
            }
            move_one(source, destination);
            if (highest_disk != 0) {
                move_pile(highest_disk - 1, spare, destination, source);
            }
        }
        int main(void) {
            move_pile(2, 0, 2, 1);
            return 0;
        }
        """
        result_o0, machine_o0, _ = toolchain.build_and_run(
            src, tmp_path, name="movsi_mem_o0", optimize="-O0"
        )
        result_o2, machine_o2, _ = toolchain.build_and_run(
            src, tmp_path, name="movsi_mem_o2", optimize="-O2"
        )
        assert result_o0 == 0
        assert result_o2 == 0
        assert list(machine_o0.outputs) == list(machine_o2.outputs)
        # 3-disk Towers of Hanoi: 2*2^3-1 = 7 moves, 4 output() calls each.
        assert len(machine_o2.outputs) == 28


# ---------------------------------------------------------------------
# Dynphony (variable-length encoding) support in the custom assembler+
# linker. Symphony (the default, exercised by every test above) pads
# every sub-instruction to a fixed 4-byte slot; Dynphony is the identical
# instruction set/RTL/ABI emitted back-to-back with no padding at all --
# GCC's codegen is unchanged either way (-mdynphony only flips
# symphony.h's ASM_SPEC, which forwards -mdynphony to `as`/symphony_as.py
# so it knows which encoding to produce; see symphony.opt's comment).
# libgcc.a and runtime/*.c are only ever built in Symphony mode today (a
# real, current limitation -- see README.md), so these tests use
# self-contained programs with no libgcc/runtime dependency.
# ---------------------------------------------------------------------

class TestDynphonyEncoding:
    def test_dynphony_smaller_than_symphony(self, toolchain, tmp_path):
        """Sanity check that -mdynphony actually produces the
        variable-length encoding, not a no-op: the identical -O0
        instruction sequence must assemble to fewer bytes than the
        Symphony (fixed 4-byte-padded) encoding of the same source,
        since Dynphony packs sub-instructions back-to-back with no
        padding at all."""
        src = "int add(int a, int b) { return a + b; }"
        sym_obj = toolchain.compile_and_assemble(
            src, tmp_path, name="dynphony_size_sym", optimize="-O0", dynphony=False
        )
        dyn_obj = toolchain.compile_and_assemble(
            src, tmp_path, name="dynphony_size_dyn", optimize="-O0", dynphony=True
        )
        assert sym_obj.fixed_width is True
        assert dyn_obj.fixed_width is False
        assert len(dyn_obj.text) < len(sym_obj.text)

    def test_dynphony_arithmetic_runs_correctly(self, toolchain, tmp_path):
        """Assembles, links (no libgcc -- pure register/ALU code, no
        multiply/divide), and RUNS a real GCC-compiled function in
        Dynphony mode on symphony.emulator.Machine(symphony=False) --
        confirms actual correct execution, not just that it assembles."""
        src = "int add(int a, int b) { return a + b; }"
        result, machine, symtab = toolchain.build_and_run(
            src, tmp_path, name="dynphony_add", optimize="-O0", dynphony=True,
            entry_symbol="add", with_runtime=False,
        )
        # add() reads its arguments from r1/r2 per the ABI -- set them
        # directly and re-run rather than relying on build_and_run's
        # zero-initialized registers, since add(0, 0) would trivially
        # "pass" even with a broken calling convention.
        machine.pc = symtab["add"]
        machine.regs[14] = 0x800000
        machine.regs[13] = 0xFFFFF0
        machine.regs[1] = 17
        machine.regs[2] = 25
        result = machine.run(halt_address=0xFFFFF0, max_steps=2000)
        assert machine.regs[1] == 42

    def test_dynphony_link_call_and_global_and_loop(self, toolchain, tmp_path):
        """Regression test for a real bug found and fixed while adding
        Dynphony support to symphony_as.py: link_call's symbol-target
        form hardcoded return_offset=12 (correct ONLY for Symphony, where
        every link_call sub-instruction is padded to a 4-byte slot, so
        the whole sequence is exactly 12 bytes). In Dynphony mode the
        real unpadded sequence is only 10 bytes (counter=2 +
        add-immediate=4 + jmp-immediate=4), so the old hardcoded 12
        computed a return address 2 bytes past the real next
        instruction -- link_return's `jmp r13` then jumped into the
        middle of an unrelated instruction instead of back to the
        caller, corrupting control flow on the very first non-leaf call.
        Caught by actually running this exact program on the emulator
        (traced live: execution jumped straight from the callee's
        link_return into the CALLEE's own entry point again instead of
        back to the caller, an infinite-recursion-shaped hang) --
        not by inspection.

        This program exercises link_call to a symbol (helper), `la`
        (materialized address of a global), and a backward jmp loop
        together -- the combination that most directly depends on
        Dynphony's variable-length instruction sizes being computed
        correctly and consistently between symphony_as.py's pass-1
        sizing and its pass-2 encoding.
        """
        src = """
        int g = 10;
        int helper(int x) { return x + g; }
        int compute(int a, int b) {
            int s = 0;
            for (int i = 0; i < b; i++) {
                s = helper(s + a);
            }
            return s;
        }
        """
        result, machine, symtab = toolchain.build_and_run(
            src, tmp_path, name="dynphony_multi", optimize="-O0", dynphony=True,
            entry_symbol="compute", with_runtime=False,
        )
        machine.pc = symtab["compute"]
        machine.regs[14] = 0x800000
        machine.regs[13] = 0xFFFFF0
        machine.regs[1] = 3   # a
        machine.regs[2] = 4   # b (loop count)
        result = machine.run(halt_address=0xFFFFF0, max_steps=5000)
        # s=0; 4 iterations of s = helper(s+a) = (s+a)+g(10):
        # 3+10=13, 13+3+10=26, 26+3+10=39, 39+3+10=52
        assert machine.regs[1] == 52

    def test_dynphony_symphony_objects_cannot_be_mixed(self, toolchain, tmp_path):
        """The linker must refuse to link a Symphony (fixed-width) object
        together with a Dynphony (variable-length) one -- mixing them
        would silently misinterpret byte boundaries (Symphony expects
        every sub-instruction in its own padded 4-byte slot; Dynphony
        packs them back-to-back), which would manifest as a confusing
        emulator crash deep into execution rather than a clear build-time
        error without this check."""
        src = "int add(int a, int b) { return a + b; }"
        sym_obj = toolchain.compile_and_assemble(
            src, tmp_path, name="mix_sym", optimize="-O0", dynphony=False
        )
        dyn_obj = toolchain.compile_and_assemble(
            src, tmp_path, name="mix_dyn", optimize="-O0", dynphony=True
        )
        from symphony_ld import Linker
        linker = Linker()
        linker.add_object(sym_obj)
        linker.add_object(dyn_obj)
        with pytest.raises(ValueError, match="Symphony.*Dynphony|Dynphony.*Symphony"):
            linker.link()
