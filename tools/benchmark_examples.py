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
from symphony.emulator import Machine, native_available, native_run
from toolchain import Toolchain, ToolchainNotBuilt


# Inputs are deliberately modest, deterministic terminating workloads.  They
# are part of the result's provenance, not an attempt to represent every use.
CASES = {
    "arena_allocator.c": (),
    "bigprime.c": (),
    "branch_merge.c": (1,),
    "c_aggregate_compat.c": (),
    "common_subexpression.c": (123456789,),
    "comparison_zero_test.c": (7, 3),
    "constant_folding.c": (),
    "demo.c": (),
    "divmod_pair.c": (123456789,),
    "dynamic_sensor_report.c": (8, 4, -2, 4, 9, 0, -2, 7, 1),
    "hypercube.c": (),
    "insertion_sort.c": (15, 3, 9, 0, 14, 2, 8, 1, 13, 4, 12, 5, 11, 6, 10, 7),
    "interprocedural_constant_folding.c": (),
    "loop_helper_inlining.c": (),
    "pi.c": (),
    "primes.c": (),
    "render.c": (),
    "towers_of_hanoi.c": (2, 0, 2, 1),
}

# Interactive renderers deliberately have no terminating path. Benchmark a
# deterministic prefix rather than misreporting their expected nontermination
# as a failure. Other examples must still reach their halt normally.
CONTINUOUS_CASES = {"hypercube.c", "render.c"}
CONTINUOUS_STEPS = 10_000_000


def validate_cases():
    """Require every maintained example to have explicit benchmark inputs."""
    examples = {path.name for path in (ROOT / "examples").glob("*.c")}
    configured = set(CASES)
    if examples != configured:
        missing = ", ".join(sorted(examples - configured)) or "none"
        stale = ", ".join(sorted(configured - examples)) or "none"
        raise RuntimeError(
            f"benchmark cases are incomplete (missing: {missing}; stale: {stale})"
        )


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
    load_size = sum(len(obj.text) + len(obj.data) for obj in objects)
    image, _symbols, entry = toolchain.link(objects, entry_symbol="main")
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

    lines = [
        "# Example compiler benchmarks",
        "",
        "Generated by `tools/benchmark_examples.py`. Sizes are serialized Symphony load-image bytes; steps are emulator instructions from entry to the termination loop. The intentionally continuous `hypercube.c` and `render.c` demos instead report a fixed 10,000,000-instruction sample. Static zero storage is treated as loader-zeroed BSS (`--bss=assume-zeroed` for dyncc), so startup clearing is excluded from both size and runtime. GCC uses the real same-ISA Symphony GCC toolchain at `SYMPHONY_GCC_PREFIX`.",
        "",
        "| Example | Inputs | SSA bytes | SSA steps | GCC -Os bytes | GCC -Os steps | GCC -O2 bytes | GCC -O2 steps |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, inputs, ssa, os_, o2 in rows:
        lines.append(f"| `{name}` | `{list(inputs)}` | {cell(ssa[0])} | {cell(ssa[1])} | {cell(os_[0])} | {cell(os_[1])} | {cell(o2[0])} | {cell(o2[1])} |")
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
