#!/usr/bin/env python3
"""Regenerate docs/example-benchmarks.md for this checkout and real GCC.

Requires SYMPHONY_GCC_PREFIX to name the stage-1 GCC build described in
gcc-backend/symphony-gcc/README.md.  Results are same-ISA emulator instruction
steps, not host wall-clock time.
"""

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "gcc-backend" / "symphony-gcc" / "tests")]

from symphony import Target, compile_source
from symphony.benchmark_cases import (
    CASES,
    CONTINUOUS_CASES,
    CONTINUOUS_STEPS,
    validate_cases as validate_case_inventory,
)
from symphony.emulator import Machine, native_available, native_run
from symphony.project import decode_control, make_persistent_image, project_from_directory
from selfhost.tools.bootstrap import STAGE0_SOURCES, build_stage0
from selfhost.tools.build_stages import compile_stage
from toolchain import Toolchain, ToolchainNotBuilt


def validate_cases():
    validate_case_inventory(ROOT / "examples")


def _run(machine, halt, max_steps, continuous):
    try:
        if native_available(True):
            native_run(machine, halt, max_steps)
        else:
            machine.run(halt, max_steps=max_steps)
    except RuntimeError as exc:
        if not continuous or "execution limit exceeded" not in str(exc):
            raise


def run_current(source, inputs, max_steps, continuous=False):
    # Compare program execution, not dyncc's optional standalone BSS clearing
    # loop.  GCC's raw-image linker likewise leaves .bss zero-filled by the
    # loader/emulator, so assume-zeroed is the equivalent dyncc configuration.
    result = compile_source(
        source,
        target=Target(
            isa="symphony", ram_size=1 << 24, bss_mode="assume-zeroed"
        ),
    )
    machine = Machine(result.image.binary, ram_size=1 << 24, inputs=inputs, symphony=True)
    halt = result.image.symbols.get("_halt", 0xffffffff)
    limit = min(max_steps, CONTINUOUS_STEPS) if continuous else max_steps
    _run(machine, halt, limit, continuous)
    return len(result.image.binary), machine.steps


def run_gcc(toolchain, source, name, inputs, optimize, max_steps, tmp, continuous=False):
    objects = [toolchain.compile_and_assemble(source, tmp, name=name, optimize=optimize)]
    objects += toolchain.full_runtime_objects(tmp, optimize=optimize)
    image, _symbols, entry = toolchain.link(objects, entry_symbol="main")
    load_size = toolchain.last_load_size
    # Toolchain.run_image intentionally has no input argument because its test
    # cases mostly use pure programs; benchmarks need the emulator protocol.
    machine = Machine(image, inputs=inputs, symphony=True)
    machine.pc = entry
    # Match the GCC toolchain's ABI test harness: its heap runtime assumes
    # the normal 16 MiB target RAM layout rather than dyncc's 1 MiB default.
    machine.regs[14] = 0x800000
    machine.regs[13] = 0xfffff0
    limit = min(max_steps, CONTINUOUS_STEPS) if continuous else max_steps
    _run(machine, 0xfffff0, limit, continuous)
    # The GCC linker materializes virtual .bss as trailing zero bytes so the
    # emulator reserves its addresses.  Those bytes are not part of a load
    # image and dyncc's assume-zeroed mode does not serialize them either.
    return load_size, machine.steps


def cell(value):
    return "—" if value is None else f"{value:,}"


def concise_error(exc):
    """Keep generated benchmark reports stable and readable."""
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    for line in lines:
        for marker in ("internal compiler error:", "relocation to '"):
            if marker in line:
                return f"{type(exc).__name__}: {line[line.index(marker):]}"
    return f"{type(exc).__name__}: {lines[0] if lines else exc}"


def selfhost_project_image():
    """Pack the one compiler project consumed by every stage-0 variant."""
    project = project_from_directory(
        ROOT / "selfhost", exclude=("build", "examples", "tests")
    )
    return make_persistent_image(
        project,
        persistent_size=1 << 24,
        program_load_address=0,
        symphony=True,
    )


def run_selfhost_benchmark(persistent, max_steps):
    """Measure Python-generated stage 0 compiling the complete C compiler."""
    stage0 = build_stage0(Target(
        isa="symphony", ram_size=1 << 24, bss_mode="assume-zeroed"
    ))
    stage1, steps = compile_stage(
        stage0.image.binary, persistent, 0, max_steps, True
    )
    return len(stage0.image.binary), len(stage1), steps


def run_gcc_selfhost(toolchain, persistent, optimize, max_steps, tmp):
    """Build the complete C compiler with GCC and run the same workload."""
    include_flag = f"-I{ROOT / 'selfhost' / 'include'}"
    objects = []
    for index, relative in enumerate(STAGE0_SOURCES):
        path = ROOT / "selfhost" / relative
        objects.append(toolchain.compile_and_assemble(
            path.read_text(),
            tmp,
            name=f"selfhost_{optimize[1:]}_{index}_{path.stem}",
            optimize=optimize,
            extra_flags=(include_flag,),
        ))
    objects += toolchain.full_runtime_objects(
        tmp, optimize=optimize, include_jump=True
    )
    image, _symbols, entry = toolchain.link(objects, entry_symbol="main")
    # The linker may prepend far-call trampolines. They are real load-image
    # text and must be counted; only the trailing virtual BSS is excluded.
    load_size = toolchain.last_load_size
    machine = Machine(
        image,
        persistent_size=len(persistent),
        symphony=True,
    )
    machine.persistent[:] = persistent
    machine.pc = entry
    # The compiler's AST/IR arenas need more than the lower half of the
    # 16 MiB RAM image. Grow the stack down from the top so the full gap
    # above the linked image is available to malloc.
    machine.regs[14] = 0xfffff0
    machine.regs[13] = 0xfffff0
    _run(machine, 0xfffff0, max_steps, False)
    control = decode_control(machine.persistent)
    if control.status:
        raise RuntimeError(f"compiler failed: status={control.status}")
    record = machine.persistent[control.output_address:]
    output_size = int.from_bytes(record[:4], "big")
    if output_size > control.output_byte_length - 4:
        raise RuntimeError("compiler produced a truncated executable record")
    return load_size, output_size, machine.steps


def main():
    validate_cases()
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "example-benchmarks.md")
    parser.add_argument("--max-steps", type=int, default=2_000_000_000)
    parser.add_argument("--prefix", default=os.environ.get("SYMPHONY_GCC_PREFIX"))
    parser.add_argument("--build-gcc", action="store_true", help="build the local GCC toolchain when missing")
    args = parser.parse_args()
    default_prefix = ROOT / ".cache" / "symphony-gcc" / "build-stage1"
    args.prefix = args.prefix or default_prefix
    try:
        toolchain = Toolchain(args.prefix)
    except ToolchainNotBuilt:
        if not args.build_gcc:
            parser.error("GCC toolchain missing; rerun with --build-gcc or run tools/build_symphony_gcc.sh")
        subprocess.run([str(ROOT / "tools" / "build_symphony_gcc.sh")], check=True)
        toolchain = Toolchain(args.prefix)
    rows, notes = [], []
    with tempfile.TemporaryDirectory(prefix="symphony-example-bench-") as raw_tmp:
        tmp = Path(raw_tmp)
        for name, inputs in sorted(CASES.items()):
            path = ROOT / "examples" / name
            if not path.is_file():
                raise RuntimeError(f"benchmark source is missing: {path}")
            source = path.read_text()
            continuous = name in CONTINUOUS_CASES
            values = []
            for label, runner in (
                ("SSA", lambda: run_current(source, inputs, args.max_steps, continuous)),
                ("GCC -Os", lambda: run_gcc(toolchain, source, path.stem + "_os", inputs, "-Os", args.max_steps, tmp, continuous)),
                ("GCC -O2", lambda: run_gcc(toolchain, source, path.stem + "_o2", inputs, "-O2", args.max_steps, tmp, continuous)),
            ):
                try:
                    values.append(runner())
                except Exception as exc:  # report, never silently compare a failed build
                    values.append((None, None))
                    notes.append(f"- `{path.name}` {label}: `{type(exc).__name__}: {exc}`")
            rows.append((path.name, inputs, *values))

        persistent = selfhost_project_image()
        selfhost_rows = []
        for label, runner in (
            ("SSA", lambda: run_selfhost_benchmark(persistent, args.max_steps)),
            ("GCC -O0", lambda: run_gcc_selfhost(toolchain, persistent, "-O0", args.max_steps, tmp)),
            ("GCC -Os", lambda: run_gcc_selfhost(toolchain, persistent, "-Os", args.max_steps, tmp)),
            ("GCC -O2", lambda: run_gcc_selfhost(toolchain, persistent, "-O2", args.max_steps, tmp)),
        ):
            try:
                selfhost_rows.append((label, *runner()))
            except Exception as exc:
                selfhost_rows.append((label, None, None, None))
                notes.append(f"- self-host compiler {label}: `{concise_error(exc)}`")

    lines = [
        "# Example compiler benchmarks",
        "",
        "Generated by `tools/benchmark_examples.py`. Sizes are serialized Symphony load-image bytes; GCC sizes include extracted libgcc members and linker-generated trampolines. Steps are emulator instructions from entry to the termination loop. The intentionally continuous `hypercube.c` and `render.c` demos instead report a fixed 10,000,000-instruction sample. Static zero storage is treated as loader-zeroed BSS (`--bss=assume-zeroed` for dyncc), so startup clearing is excluded from both size and runtime. GCC uses the real same-ISA Symphony GCC toolchain at `SYMPHONY_GCC_PREFIX`.",
        "",
        "| Example | Inputs | SSA bytes | SSA steps | GCC -Os bytes | GCC -Os steps | GCC -O2 bytes | GCC -O2 steps |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, inputs, ssa, os_, o2 in rows:
        lines.append(f"| `{name}` | `{list(inputs)}` | {cell(ssa[0])} | {cell(ssa[1])} | {cell(os_[0])} | {cell(os_[1])} | {cell(o2[0])} | {cell(o2[1])} |")
    lines += [
        "",
        "## Self-host compiler benchmark",
        "",
        "Each row builds the complete self-hosted C compiler with that compiler, then runs it on the same packed compiler project to produce stage 1. Runtime is native-emulator target instruction steps, not host wall-clock time or a hardware cycle estimate. GCC sizes exclude zero-filled BSS, as in the example table.",
        "",
        "| Compiler build | Compiler bytes | Compiled output bytes | Compilation instructions |",
        "|---|---:|---:|---:|",
    ]
    for label, compiler_size, output_size, steps in selfhost_rows:
        lines.append(
            f"| {label} | {cell(compiler_size)} | {cell(output_size)} | {cell(steps)} |"
        )
    if notes:
        lines += ["", "## Failed measurements", "", *notes]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + "\n")
    print(args.output)


if __name__ == "__main__":
    try:
        main()
    except ToolchainNotBuilt as exc:
        raise SystemExit(f"benchmark unavailable: {exc}")
