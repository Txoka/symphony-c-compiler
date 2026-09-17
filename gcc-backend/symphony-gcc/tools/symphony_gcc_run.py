#!/usr/bin/env python3
"""One-command build-and-run for a single C source file against the real
Symphony GCC toolchain: xgcc -> symphony_as.py -> symphony_ld.py's Linker
(against libgcc.a and, unless --no-runtime is given, the full runtime
library) -> symphony.emulator's Machine.

This is the "easy to use" entry point the README's step-by-step build
recipe deliberately keeps separate from: that recipe explains what each
stage actually does and why, for anyone extending the port itself; this
script exists for the common case of "I just want to compile and run one
C file" without hand-writing the Linker/ObjectFile calls each time.

Requires a built toolchain -- see README.md's "Build recipe" section --
and SYMPHONY_GCC_PREFIX (or --prefix) pointing at the build-stage1
directory, exactly like the test suite's SYMPHONY_GCC_PREFIX gate.

Examples:
    python3 symphony_gcc_run.py hello.c
    python3 symphony_gcc_run.py hello.c -O2
    python3 symphony_gcc_run.py hello.c -O2 -o hello.bin  # write the binary, then run it
    python3 symphony_gcc_run.py lib_only.s --no-runtime --entry _start
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
TESTS_DIR = TOOLS_DIR.parent / "tests"
sys.path.insert(0, str(TESTS_DIR))

from toolchain import Toolchain, ToolchainNotBuilt, CompileError, screen_text  # noqa: E402


def main(argv):
    parser = argparse.ArgumentParser(
        description="Compile, link, and run one C program on the Symphony emulator."
    )
    parser.add_argument("source", help="C source file to compile and run")
    parser.add_argument(
        "-O", dest="optimize", default="-O0", metavar="LEVEL",
        help="optimization level passed to xgcc as -OLEVEL (default: -O0; "
             "-O1/-O2 may hit the known reload-insns ICE on some programs, "
             "see README.md's Known gaps section)",
    )
    parser.add_argument(
        "--entry", default="main",
        help="entry symbol to run from (default: main)",
    )
    parser.add_argument(
        "--no-runtime", action="store_true",
        help="don't link the runtime library (intrinsics.c/heap.c/printf.c) "
             "-- only valid for programs that don't call printf/malloc/etc. "
             "and don't define main() (main always needs intrinsics.c's "
             "atexit, see toolchain.py's build_and_run docstring)",
    )
    parser.add_argument(
        "-o", dest="out_bin", metavar="PATH",
        help="also write the linked flat binary to this path",
    )
    parser.add_argument(
        "--max-steps", type=int, default=2_000_000,
        help="emulator step ceiling before giving up (default: 2000000; "
             "raise this for long-running programs -- hundreds of millions "
             "to low billions of steps is normal, not a sign of a hang)",
    )
    parser.add_argument(
        "--prefix", default=os.environ.get("SYMPHONY_GCC_PREFIX"),
        help="toolchain build-stage1 directory (default: $SYMPHONY_GCC_PREFIX)",
    )
    parser.add_argument(
        "--screen", action="store_true",
        help="also print the printf-family text-screen framebuffer contents "
             "after the program halts",
    )
    args = parser.parse_args(argv)

    if not args.prefix:
        parser.error(
            "no toolchain prefix given -- set SYMPHONY_GCC_PREFIX or pass --prefix "
            "(see README.md's Build recipe for how to produce one)"
        )

    optimize_flag = args.optimize if args.optimize.startswith("-O") else f"-O{args.optimize}"

    try:
        toolchain = Toolchain(args.prefix)
    except ToolchainNotBuilt as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="symphony-gcc-run-") as tmp:
        tmp_path = Path(tmp)
        try:
            source_text = Path(args.source).read_text()
            result, machine, symtab = toolchain.build_and_run(
                source_text, tmp_path,
                name=Path(args.source).stem,
                optimize=optimize_flag,
                with_runtime=not args.no_runtime,
                entry_symbol=args.entry,
                max_steps=args.max_steps,
            )
        except CompileError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        if args.out_bin:
            image, _symtab, _entry = toolchain.link(
                [toolchain.compile_and_assemble(
                    source_text, tmp_path, name=Path(args.source).stem,
                    optimize=optimize_flag,
                )] + ([] if args.no_runtime else toolchain.full_runtime_objects(tmp_path)),
                entry_symbol=args.entry,
            )
            Path(args.out_bin).write_bytes(image)
            print(f"wrote {len(image)} bytes -> {args.out_bin}", file=sys.stderr)

        print(f"halted after {machine.steps} steps, r1 (return value) = "
              f"{result:#x} ({result})")
        if args.screen:
            print("--- screen framebuffer ---")
            print(screen_text(machine, symtab))

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
