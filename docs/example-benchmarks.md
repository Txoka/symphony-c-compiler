# Example compiler benchmarks

Regenerate with:

```sh
SYMPHONY_GCC_PREFIX=/path/to/gcc-build \
  python tools/benchmark_examples.py
```

The generator replaces this file with current-checkout SSA versus real GCC
`-Os` and `-O2` same-ISA measurements. It records final binary bytes and
emulator instruction steps using deterministic input vectors.
