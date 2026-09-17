"""Test harness for the real Symphony/Dynphony GCC toolchain.

This wraps the exact source-to-running-binary pipeline documented in
gcc-backend/symphony-gcc/README.md's "Build recipe" section: xgcc (-S) ->
tools/symphony_as.py -> tools/symphony_ld.py's Linker -> symphony.emulator's
Machine. It exists so test_gcc_backend.py (and any future test module) can
compile+link+run a real C program in a few lines instead of re-deriving the
pipeline by hand each time, which is exactly what earlier verification of
this project did NOT do -- every prior check was ad hoc and discarded (see
the project memory file and this task's brief).

Everything here is gated by the caller checking `SYMPHONY_GCC_PREFIX` first
(see test_gcc_backend.py's skip condition) -- this module does not itself
skip anything, so importing it is always safe, but constructing a
`Toolchain` without a real prefix will fail fast with a clear error.
"""
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
GCC_BACKEND_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = GCC_BACKEND_ROOT / "runtime"
TOOLS_DIR = GCC_BACKEND_ROOT / "tools"

sys.path.insert(0, str(TOOLS_DIR))
sys.path.insert(0, str(REPO_ROOT))

from symphony_obj import ObjectFile  # noqa: E402
from symphony_ld import Linker  # noqa: E402

from symphony.emulator.machine import Machine  # noqa: E402

# The full runtime library, in an order that deliberately does NOT put
# heap.o last -- see test_heap_anchor_bss_ordering_bug in
# test_gcc_backend.py. Any ordering here should keep working: the linker's
# fix for the __dyn_heap_anchor bug (see symphony_ld.py's Linker.link())
# makes the anchor's address independent of link order by construction.
RUNTIME_SOURCES = ["intrinsics.c", "heap.c", "printf.c"]

DEFAULT_STACK_POINTER = 0x800000
DEFAULT_HALT_ADDRESS = 0xFFFFF0
DEFAULT_MAX_STEPS = 2_000_000


def toolchain_prefix():
    """Returns the configured toolchain prefix, or None if not configured.

    The prefix is a GCC build-stage1 directory (see README.md's build
    recipe): `<prefix>/gcc/xgcc` and `<prefix>/gcc/as` (the assembler
    wrapper script) must exist, and
    `<prefix>/symphony-elf/libgcc/libgcc.a` must exist.
    """
    value = os.environ.get("SYMPHONY_GCC_PREFIX")
    return Path(value) if value else None


class ToolchainNotBuilt(RuntimeError):
    pass


class CompileError(RuntimeError):
    pass


class Toolchain:
    """A configured, ready-to-use xgcc + assembler + linker pipeline."""

    def __init__(self, prefix):
        self.prefix = Path(prefix)
        self.xgcc = self.prefix / "gcc" / "xgcc"
        self.gcc_dir = self.prefix / "gcc"
        self.libgcc = self.prefix / "symphony-elf" / "libgcc" / "libgcc.a"
        self.as_py = TOOLS_DIR / "symphony_as.py"
        missing = [p for p in (self.xgcc, self.libgcc, self.as_py) if not p.exists()]
        if missing:
            raise ToolchainNotBuilt(
                "SYMPHONY_GCC_PREFIX is set to "
                f"{self.prefix}, but required toolchain artifacts are "
                f"missing: {', '.join(str(p) for p in missing)}. Build the "
                "toolchain following gcc-backend/symphony-gcc/README.md's "
                "build recipe first."
            )
        self._runtime_object_cache = {}

    # ---- compile ------------------------------------------------------

    def compile_to_asm(self, c_source_path, asm_out_path, optimize="-O0",
                        extra_flags=()):
        cmd = [str(self.xgcc), f"-B{self.gcc_dir}/", "-S", optimize,
               str(c_source_path), "-o", str(asm_out_path), *extra_flags]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise CompileError(
                f"xgcc failed ({' '.join(cmd)}):\n{result.stdout}\n{result.stderr}"
            )
        return result.stderr  # warnings, if any

    def assemble(self, asm_path, obj_path, dynphony=False):
        cmd = [sys.executable, str(self.as_py), str(asm_path), "-o", str(obj_path)]
        if dynphony:
            cmd.append("-mdynphony")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise CompileError(
                f"symphony_as.py failed ({' '.join(cmd)}):\n"
                f"{result.stdout}\n{result.stderr}"
            )

    def compile_and_assemble(self, c_source, tmp_path, name="unit",
                              optimize="-O0", extra_flags=(), dynphony=False):
        """Compiles a C source string to an ObjectFile, via real xgcc + the
        real assembler (not a shortcut) -- tmp_path is a pytest `tmp_path`
        fixture or any writable directory. `dynphony=True` passes -mdynphony
        to both xgcc (so symphony.h's ASM_SPEC forwards it to `as`) and
        directly to the assembler step, selecting the variable-length
        Dynphony encoding instead of the default Symphony fixed-width one."""
        c_path = tmp_path / f"{name}.c"
        c_path.write_text(c_source)
        asm_path = tmp_path / f"{name}.s"
        obj_path = tmp_path / f"{name}.o"
        flags = (*extra_flags, "-mdynphony") if dynphony else extra_flags
        self.compile_to_asm(c_path, asm_path, optimize=optimize, extra_flags=flags)
        self.assemble(asm_path, obj_path, dynphony=dynphony)
        return ObjectFile.load(obj_path)

    def assemble_asm_source(self, asm_source, tmp_path, name="unit", dynphony=False):
        """Assembles hand-written .s source directly (no xgcc step) -- for
        tests that need to construct a specific instruction/data layout the
        C frontend wouldn't reliably produce (e.g. the assembler
        pass1/pass2 desync regression test)."""
        asm_path = tmp_path / f"{name}.s"
        asm_path.write_text(asm_source)
        obj_path = tmp_path / f"{name}.o"
        self.assemble(asm_path, obj_path, dynphony=dynphony)
        return ObjectFile.load(obj_path)

    # ---- runtime library objects (cached across tests in a session) ---

    def runtime_object(self, tmp_path, source_name, optimize="-O0"):
        """Compiles one runtime/*.c file to an ObjectFile, cached by
        (source_name, optimize) for the lifetime of this Toolchain
        instance (tests share one session-scoped Toolchain fixture, so
        this avoids recompiling heap.c/printf.c/intrinsics.c for every
        single test)."""
        key = (source_name, optimize)
        if key in self._runtime_object_cache:
            return self._runtime_object_cache[key]
        c_path = RUNTIME_DIR / source_name
        stem = c_path.stem
        asm_path = tmp_path / f"runtime_{stem}.s"
        obj_path = tmp_path / f"runtime_{stem}.o"
        self.compile_to_asm(c_path, asm_path, optimize=optimize)
        self.assemble(asm_path, obj_path)
        obj = ObjectFile.load(obj_path)
        self._runtime_object_cache[key] = obj
        return obj

    def full_runtime_objects(self, tmp_path, optimize="-O0", order=None):
        """Returns ObjectFile instances for intrinsics.c/heap.c/printf.c,
        in the given order (default RUNTIME_SOURCES order). Pass a custom
        `order` to control link order deliberately -- e.g. to reproduce
        the historical __dyn_heap_anchor BSS-ordering bug's exact
        trigger condition (heap.o NOT last)."""
        names = order if order is not None else RUNTIME_SOURCES
        return [self.runtime_object(tmp_path, name, optimize=optimize) for name in names]

    # ---- link + run -----------------------------------------------------

    def link(self, objects, entry_symbol="main", load_address=0, with_libgcc=True):
        linker = Linker(load_address=load_address)
        for obj in objects:
            linker.add_object(obj)
        if with_libgcc:
            linker.add_archive(str(self.libgcc))
        return linker.link(entry_symbol=entry_symbol)

    def run_image(self, image, entry, *, stack_pointer=DEFAULT_STACK_POINTER,
                   halt_address=DEFAULT_HALT_ADDRESS, max_steps=DEFAULT_MAX_STEPS,
                   fixed_width=True):
        # `symphony=` selects the emulator's byte-decoding mode: True reads
        # every sub-instruction from a padded 4-byte slot (Symphony), False
        # reads variable-length instructions back-to-back (Dynphony) -- must
        # match whichever mode the image was actually assembled+linked in,
        # or decoding desyncs immediately.
        machine = Machine(image, load_address=0, symphony=fixed_width)
        machine.pc = entry
        machine.regs[14] = stack_pointer
        machine.regs[13] = halt_address
        result = machine.run(halt_address=halt_address, max_steps=max_steps)
        return result, machine

    def link_and_run(self, objects, *, entry_symbol="main", load_address=0,
                      stack_pointer=DEFAULT_STACK_POINTER,
                      halt_address=DEFAULT_HALT_ADDRESS,
                      max_steps=DEFAULT_MAX_STEPS, with_libgcc=True,
                      fixed_width=True):
        image, symtab, entry = self.link(
            objects, entry_symbol=entry_symbol, load_address=load_address,
            with_libgcc=with_libgcc,
        )
        result, machine = self.run_image(
            image, entry, stack_pointer=stack_pointer, halt_address=halt_address,
            max_steps=max_steps, fixed_width=fixed_width,
        )
        return result, machine, symtab

    # ---- convenience: compile a whole C program + full runtime + run --

    def build_and_run(self, c_source, tmp_path, *, name="prog", optimize="-O0",
                       with_runtime=True, runtime_order=None,
                       runtime_optimize="-O0", dynphony=False, **run_kwargs):
        """`with_runtime=False` only works for programs that do NOT define
        `main` (use `entry_symbol` for a differently-named entry point
        instead, e.g. via `run_kwargs`) -- GCC's expand_main_function
        always inserts an implicit `link_call __main` at the start of any
        real `main`, and libgcc's __main -> __do_global_ctors always calls
        `atexit(...)` even when there are no real global constructors, so
        linking a `main()`-defined program needs at least intrinsics.c's
        `atexit` (see its definition and comment) even if the test itself
        never touches I/O, heap, or printf.

        `dynphony=True` builds the Dynphony (variable-length) encoding
        instead of the default Symphony fixed-width one. libgcc.a and
        runtime/*.c are only ever built in Symphony mode today (a real,
        documented limitation -- see README.md), so `dynphony=True` forces
        `with_runtime=False` and `with_libgcc=False`: a Dynphony test
        program must be self-contained (no libgcc calls, no printf/malloc),
        matching this codebase's real current Dynphony support surface.
        """
        objects = [self.compile_and_assemble(c_source, tmp_path, name=name,
                                              optimize=optimize, dynphony=dynphony)]
        if with_runtime and not dynphony:
            objects += self.full_runtime_objects(
                tmp_path, optimize=runtime_optimize, order=runtime_order
            )
        run_kwargs.setdefault("with_libgcc", not dynphony)
        run_kwargs.setdefault("fixed_width", not dynphony)
        return self.link_and_run(objects, **run_kwargs)


def screen_text(machine, symtab, length=96):
    """Reads back `length` bytes of the printf family's text-screen
    framebuffer as a decoded string (NUL bytes trimmed from the right) --
    the same mechanism test_integration.py's test_printf_and_framebuffer_modes
    uses to assert on printed output (reading machine.memory at the
    __dyn_printf_framebuffer symbol directly), applied to the GCC-compiled
    runtime/printf.c instead of dyncc's own intrinsics.py."""
    base = symtab["__dyn_printf_framebuffer"]
    raw = bytes(machine.memory[base: base + length])
    return raw.rstrip(b"\x00").decode("ascii", errors="replace")
