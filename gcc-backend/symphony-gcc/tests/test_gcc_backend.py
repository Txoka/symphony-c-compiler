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
# Known, documented-but-not-fixed ICE gaps: calloc()/__dyn_printf_unsigned()
# hit "maximum number of generated reload insns" at -O1/-O2 (works at -O0);
# 64-bit multiply/divide (_muldi3 etc.) hit the same ICE class and are
# excluded from libgcc entirely (see libgcc-config/symphony/t-symphony).
# These are xfail, not skip, so a future fix flips them green automatically
# instead of the gap silently going untracked.
# ---------------------------------------------------------------------

class TestKnownIceGaps:
    @pytest.mark.parametrize("optimize", ["-O1", "-O2"])
    @pytest.mark.xfail(
        reason="runtime/heap.c's calloc() hits GCC's 'maximum number of "
        "generated reload insns' ICE at -O1/-O2 (works at -O0); real, "
        "unresolved backend limitation, not a scope decision -- see "
        "README.md's Known gaps section.",
        strict=True,
    )
    def test_calloc_compiles_at_o1_o2(self, toolchain, tmp_path, optimize):
        toolchain.compile_to_asm(
            Path(__file__).resolve().parents[1] / "runtime" / "heap.c",
            tmp_path / f"heap_{optimize.strip('-')}.s",
            optimize=optimize,
        )

    @pytest.mark.parametrize("optimize", ["-O1", "-O2"])
    @pytest.mark.xfail(
        reason="runtime/printf.c's __dyn_printf_unsigned() (reached via "
        "printf.c as a whole) hits the same 'maximum number of generated "
        "reload insns' ICE at -O1/-O2 (works at -O0); real, unresolved "
        "backend limitation -- see README.md's Known gaps section.",
        strict=True,
    )
    def test_printf_unsigned_compiles_at_o1_o2(self, toolchain, tmp_path, optimize):
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
