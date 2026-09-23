"""Shared maintained-example benchmark inventory.

This lives in the installed package so repository coverage tests also work in
cibuildwheel's isolated test environment, where development-only ``tools/``
modules are deliberately absent.
"""

from pathlib import Path


# Inputs are deliberately modest, deterministic terminating workloads. They
# are provenance for the comparison, not an attempt to represent every use.
# Animated and interactive examples use framebuffer-presentation cadence.
CASES = {
    "anonymous_aggregate_members.c": (),
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
    "insertion_sort.c": (15, 3, 9, 0, 14, 2, 8, 1, 13, 4, 12, 5, 11, 6, 10, 7),
    "interprocedural_constant_folding.c": (),
    "julia.c": (),
    "loop_helper_inlining.c": (),
    "mandelbrot.c": (),
    "pi.c": (),
    "primes.c": (),
    "towers_of_hanoi.c": (2, 0, 2, 1),
    "unbounded/game_of_life.c": (),
    "unbounded/hypercube.c": (),
    "unbounded/pong.c": (),
    "unbounded/sphere.c": (),
}

CONTINUOUS_CASES = frozenset({
    "unbounded/game_of_life.c",
    "unbounded/hypercube.c",
    "unbounded/pong.c",
    "unbounded/sphere.c",
})
FRAME_WARMUP_COUNT = 3
FRAME_SAMPLE_COUNT = 60


def validate_cases(examples_directory):
    """Require every maintained example to have explicit benchmark inputs."""
    root = Path(examples_directory)
    examples = {path.relative_to(root).as_posix() for path in root.rglob("*.c")}
    configured = set(CASES)
    if examples != configured:
        missing = ", ".join(sorted(examples - configured)) or "none"
        stale = ", ".join(sorted(configured - examples)) or "none"
        raise RuntimeError(
            f"benchmark cases are incomplete (missing: {missing}; stale: {stale})"
        )


__all__ = [
    "CASES",
    "CONTINUOUS_CASES",
    "FRAME_SAMPLE_COUNT",
    "FRAME_WARMUP_COUNT",
    "validate_cases",
]
