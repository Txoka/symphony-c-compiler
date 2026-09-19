#!/usr/bin/env bash
# Build the real same-ISA GCC toolchain used by benchmark_examples.py.
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cache="$root/.cache/symphony-gcc"
jobs=$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)
skip_deps=false

while (($#)); do
  case $1 in
    --cache) cache=$2; shift 2 ;;
    --jobs) jobs=$2; shift 2 ;;
    --skip-deps) skip_deps=true; shift ;;
    *) echo "usage: $0 [--cache DIR] [--jobs N] [--skip-deps]" >&2; exit 2 ;;
  esac
done

if ! "$skip_deps" && command -v apt-get >/dev/null; then
  runner=()
  if [[ $EUID -ne 0 ]]; then runner=(sudo); fi
  "${runner[@]}" apt-get update
  "${runner[@]}" apt-get install -y build-essential bison flex git python3 \
    libgmp-dev libmpfr-dev libmpc-dev texinfo
fi

source_dir="$cache/gcc-src"
build_dir="$cache/build-stage1"
install_dir="$cache/install"
backend="$root/gcc-backend/symphony-gcc"

mkdir -p "$cache"
if [[ ! -d $source_dir/.git ]]; then
  git clone --depth 1 -b releases/gcc-14 https://github.com/gcc-mirror/gcc.git "$source_dir"
fi
if git -C "$source_dir" apply --check "$backend/gcc-src.patch" 2>/dev/null; then
  git -C "$source_dir" apply "$backend/gcc-src.patch"
fi
mkdir -p "$source_dir/gcc/config/symphony" "$source_dir/libgcc/config/symphony" "$build_dir"
for file in "$backend/config/symphony/"*; do ln -sfn "$file" "$source_dir/gcc/config/symphony/"; done
for file in "$backend/libgcc-config/symphony/"*; do ln -sfn "$file" "$source_dir/libgcc/config/symphony/"; done

if [[ ! -f $build_dir/Makefile ]]; then
  (cd "$build_dir" && "$source_dir/configure" \
    --target=symphony-elf --prefix="$install_dir" --enable-languages=c \
    --without-headers --with-newlib --disable-shared --disable-threads \
    --disable-libssp --disable-libquadmath --disable-libgomp --disable-libatomic \
    --disable-libstdcxx --disable-bootstrap --disable-nls --disable-gcov \
    --with-insnemit-partitions=1)
fi

make -C "$build_dir" -j"$jobs" all-gcc
# libgcc configure needs a real target assembler; GCC's generated stub is not one.
printf '%s\n' '#!/bin/sh' "exec python3 '$backend/tools/symphony_as.py' \"\$@\"" > "$build_dir/gcc/as"
chmod +x "$build_dir/gcc/as"
PATH="$install_dir/symphony-elf/bin:$PATH" make -C "$build_dir" -j"$jobs" all-target-libgcc

printf 'SYMPHONY_GCC_PREFIX=%s\n' "$build_dir"
