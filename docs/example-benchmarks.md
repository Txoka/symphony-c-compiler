# Example compiler benchmarks

Sizes are final Symphony binary bytes. Steps are native-emulator instructions
from entry through the first termination instruction, using the deterministic
input vectors below. The `main` comparison is a one-time reference measurement;
`tools/benchmark_examples.py` intentionally regenerates only current SSA versus
real GCC `-Os` and `-O2`.

| Example | Inputs | SSA bytes | SSA steps | main bytes | main steps | GCC -Os | GCC -O2 |
|---|---|---:|---:|---:|---:|---|---|
| `arena_allocator.c` | `[]` | 1,356 | 569 | 1,252 | 553 | pending | pending |
| `bigprime.c` | `[]` | 10,832 | 791,403,917 | 10,344 | 967,067,881 | pending | pending |
| `constant_folding.c` | `[]` | 8 | 1 | 8 | 1 | pending | pending |
| `demo.c` | `[]` | 3,172 | 6,499 | 3,068 | 6,896 | pending | pending |
| `dynamic_sensor_report.c` | `[8, 4, -2, 4, 9, 0, -2, 7, 1]` | 13,356 | 23,374 | 12,960 | 19,788 | pending | pending |
| `insertion_sort.c` | `[15, 3, 9, 0, 14, 2, 8, 1, 13, 4, 12, 5, 11, 6, 10, 7]` | 968 | 6,506 | 864 | 7,185 | pending | pending |
| `interprocedural_constant_folding.c` | `[]` | 8 | 1 | 8 | 1 | pending | pending |
| `pi.c` | `[]` | 4,852 | 5,867,967,561 | 4,582 | 5,225,253,518 | pending | pending |
| `primes.c` | `[]` | 3,420 | 5,953,388 | 3,428 | 6,962,874 | pending | pending |
| `towers_of_hanoi.c` | `[2, 0, 2, 1]` | 308 | 269 | 312 | 272 | pending | pending |

## GCC regeneration

```sh
python tools/benchmark_examples.py --build-gcc --max-steps 20000000000
```

The command builds the GCC target when necessary and replaces the GCC columns
with same-ISA measurements. At this revision the checked GCC toolchain exposes
two blockers before a valid full table can be produced: target C/platform
headers are not staged, and full-runtime linking does not yet resolve the
`__dyn_heap_anchor + 3` relocation expression. Those are recorded as pending,
not benchmark results.
