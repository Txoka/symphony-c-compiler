import json
import os
import random
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from symphony import (
    CompileError,
    Target as _Target,
    compile_source as _compile_source,
    compile_sources as _compile_sources,
)
from symphony.emulator import Machine as _Machine, native_available, native_run, signed
from symphony.frontend import parse, typecheck
from symphony.ir import lower
from symphony import isa
from symphony.targets.symphony.abi import ABI
from symphony.middle.ssa.optimizations import OPTIMIZATIONS

ROOT = Path(__file__).resolve().parents[1]
TEST_ISA = os.environ.get("SYMPHONY_TEST_ISA", "symphony")
TEST_PREAMBLE = """#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <symphony.h>
"""


def Target(*args, **kwargs):
    kwargs.setdefault("isa", TEST_ISA)
    return _Target(*args, **kwargs)


NATIVE_AVAILABLE = native_available(TEST_ISA == "symphony")


class _NativePreferringMachine(_Machine):
    """Run on the native emulator when it's built; fall back to the Python
    reference engine otherwise. Every test in this file calls ``run()``
    through a ``Machine`` built here, so this is the one place that needs to
    prefer native."""

    def run(self, halt_address=None, max_steps=5_000_000, progress=None, progress_interval=250_000):
        if NATIVE_AVAILABLE and progress is None:
            return native_run(self, halt_address, max_steps)
        return super().run(halt_address, max_steps, progress, progress_interval)


def Machine(*args, **kwargs):
    kwargs.setdefault("symphony", TEST_ISA == "symphony")
    return _NativePreferringMachine(*args, **kwargs)


def compile_source(source, filename="<input>", target=None):
    """Compile old focused snippets with their library dependencies declared."""
    return _compile_source(TEST_PREAMBLE + source, filename, target or Target())


def compile_sources(sources, target=None, **kwargs):
    return _compile_sources(sources, target or Target(), **kwargs)


def run(
    source,
    expected=None,
    pic=False,
    address=0,
    ram=1 << 20,
    inputs=(),
):
    source = source.replace("-2147483648", "(-2147483647-1)")
    target = Target(
        ram_size=ram,
        pic=pic,
        load_address=0 if pic else address,
    )
    result = compile_source(source, target=target)
    machine = Machine(result.image.binary, ram, address, inputs=inputs)
    halt = result.image.symbols["_halt"] + (address if pic else 0)
    actual = machine.run(halt)
    if expected is not None and actual != expected & 0xFFFFFFFF:
        raise AssertionError(
            f"expected {expected&0xffffffff:#x}, got {actual:#x}: {source}"
        )
    if machine.regs[14] != 0:
        raise AssertionError("stack pointer was not restored")
    return result, machine


class ExecutionTests(unittest.TestCase):
    def test_basic(self):
        run("int main(void) { int x=3; int y=4; return x+y; }", 7)

    def test_types_and_promotions(self):
        cases = [
            ("char c=255; return c;", 255),
            ("signed char c=255; return c;", -1),
            ("short c=65535; return c;", -1),
            ("unsigned short c=65535; return c+1;", 65536),
            ("unsigned char c=255; return c+c;", 510),
            ("signed char c=-128; return c >> 2;", -32),
            ("unsigned int x=0x80000000u; return x >> 31;", 1),
            ("long x=-2; return x;", -2),
            ("unsigned long x=0xffffffff; return x>0;", 1),
            ("int x=-1; unsigned int y=1; return x<y;", 0),
            ("int x=-7; return (char)x;", 249),
            ("unsigned int x=0xffffffff; return (short)x;", -1),
            ("char c=255; c++; return c;", 0),
            ("signed char c=127; c+=1; return c;", -128),
            ("int x=5; x<<=2; x^=3; x|=8; x&=31; x>>=1; return x;", 15),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                run("int main(void){" + body + "}", expected)

    def test_comparisons(self):
        for typ, a, b in [
            ("int", -1, 1),
            ("int", -2147483648, 2147483647),
            ("unsigned int", 4294967295, 1),
            ("int", 3, 3),
        ]:
            for op, expected in [
                ("==", a == b),
                ("!=", a != b),
                ("<", a < b),
                ("<=", a <= b),
                (">", a > b),
                (">=", a >= b),
            ]:
                with self.subTest(typ=typ, a=a, b=b, op=op):
                    run(
                        f"int main(void){{ {typ} a={str(a)+'u' if typ.startswith('unsigned') else a},b={str(b)+'u' if typ.startswith('unsigned') else b}; return a {op} b; }}",
                        int(expected),
                    )

    def test_control_flow(self):
        run(
            """int main(void) {
            int n=0,s=0;
            while(n<10) { n++; if(n==4) continue; if(n==8) break; s+=n; }
            for(int i=0;i<3;i++) { for(int j=0;j<2;j++) s++; }
            do { s++; } while(s<35);
            if(s==35) return s; else return -1;
        }""",
            35,
        )
        run("int main(void){int i=0; for(;;) { if(++i==5) break; } return i;}", 5)
        run("int main(void){int i=0; do {i++; continue;} while(i<3); return i;}", 3)

    def test_switch_fallthrough_and_loop_breaks(self):
        run(
            """int main(void) {
                int total = 0;
                for (int i = 0; i < 3; i++) {
                    switch (i) {
                    case 0: total += 1; break;
                    case 1: total += 10;
                    case 2: total += 100; break;
                    default: return -1;
                    }
                    total += 1000;
                }
                return total;
            }""",
            3211,
        )

    def test_switch_promotions_nested_dispatch_and_no_default(self):
        run(
            """int main(void) {
                signed char selector = -1;
                int result = 0;
                switch (selector) {
                case -1:
                    switch (2) {
                    case 2: result = 40; break;
                    default: result = -100;
                    }
                    result += 2;
                    break;
                default: return -1;
                }
                switch (9) { case 1: return -2; }
                return result;
            }""",
            42,
        )

    def test_goto_forward_backward_and_vla_scope_exit(self):
        run(
            """int main(void) {
                int value = 0;
                goto ready;
                value = -1;
            ready:
                value += 2;
            again:
                value += 10;
                if (value < 42) goto again;
                return value;
            }""",
            42,
        )
        # The run helper also asserts that the stack pointer returns to zero.
        run(
            """int main(void) {
                int count = 3;
                {
                    int values[count];
                    values[0] = 9;
                    goto done;
                }
            done:
                return 42;
            }""",
            42,
        )

    def test_short_circuit_and_conditional(self):
        run(
            """int main(void){int x=0; int a=0 && ++x; int b=1 || ++x;
            int c=1 ? ++x : (x=100); int d=0 ? (x=100) : ++x;
            return x*100+a*10+b+c+d; }""",
            204,
        )
        run("int main(void){int a=0,b=0; return (a=3,b=a+4,b);}", 7)
        run("int main(void){int *p=0; return p && *p;}", 0)

    def test_recursion_and_nested_calls(self):
        run(
            """int add(int a,int b){return a+b;}
            int fib(int n){ if(n<2) return n; return fib(n-1)+fib(n-2); }
            int main(void){return add(fib(8),add(10,11));}""",
            42,
        )
        run(
            """int six(int a,int b,int c,int d,int e,int f) {return a+2*b+3*c+4*d+5*e+6*f;}
            int id(int x){return x;}
            int main(void){return six(id(1),id(2),id(3),id(4),id(5),id(6));}""",
            91,
        )

    def test_arrays_and_pointers(self):
        run(
            """int main(void){int a[4]={2,4}; int *p=a; p++; *p+=3;
            return a[0]+p[0]+a[2]+a[3]+(p-a); }""",
            10,
        )
        run(
            """int sum(int a[],int n){int s=0; for(int i=0;i<n;i++) s+=a[i]; return s;}
            int main(void){int a[]={2,3,4}; return sum(a,3);}""",
            9,
        )
        run(
            "int main(void){int a[2][3]={{1,2},{3,4,5}}; return a[0][2]+a[1][2]+sizeof(a);}",
            29,
        )
        run("int main(void){int a=4; int *p=&a; int **q=&p; **q=9; return a;}", 9)
        run("int main(void){int a[4]={0}; int *p=a+3; p-=2; return p-a;}", 1)
        run("int main(void){int a[2]={3,4}; int *p=a; *p+++=2; return a[0]*10+*p;}", 54)
        run('int main(void){char s[6]="Hi"; return s[0]+s[1]+s[2]+s[5];}', 177)
        run("int main(void){int a=0; return sizeof(a++)+a;}", 4)

    def test_unions_designated_and_brace_elided_initializers(self):
        run(
            """struct Triple { int pair[2]; int tail; };
            union Word { int value; unsigned char bytes[4]; };
            int main(void) {
                struct Triple source = { 4, 7, 9 };
                struct Triple copy = { .tail = 1, .pair = { [1] = 2, [0] = 3 } };
                int sparse[5] = { [3] = 8, [1] = 6 };
                union Word encoded = { .bytes = { 0, 0, 0, 5 } };
                union Word duplicate = { .value = 0 };
                copy = source;
                duplicate = encoded;
                return copy.pair[0] + copy.pair[1] + copy.tail
                    + sparse[1] + sparse[3] + duplicate.value;
            }""",
            39,
        )

    def test_union_local_initialized_from_union_lvalue(self):
        run(
            """union U { int a; unsigned char bytes[4]; };
            int main(void) {
                union U u1; u1.a = 5;
                union U u2 = u1;
                return u2.a;
            }""",
            5,
        )

    def test_switch_case_dedup_normalizes_to_promoted_type(self):
        # unsigned char promotes to int, so -1 and 255 are distinct case
        # values here (unlike if x's own type were used for normalization).
        run(
            """int main(void) {
                unsigned char x = 255;
                switch (x) {
                case -1: return 1;
                case 255: return 2;
                default: return 3;
                }
            }""",
            2,
        )

    def test_union_layout_static_initialization_and_assignment(self):
        run(
            """union Packet { unsigned int word; unsigned char bytes[4]; };
            static union Packet encoded = { .bytes = { 1, 2, 3, 4 } };
            int main(void) {
                union Packet copy = { 0 };
                copy = encoded;
                return sizeof(copy) + copy.bytes[0] + copy.bytes[3]
                    + (copy.word == 0x01020304u);
            }""",
            10,
        )

    def test_anonymous_struct_and_union_member_promotion(self):
        run(
            """struct Value {
                int type;
                union {
                    struct { int x; int y; };
                    struct { int r; int g; };
                };
            };
            int main(void) {
                struct Value value = { .type = 1, .x = 20, .y = 22 };
                struct Value *pointer = &value;
                pointer->r = 4;
                pointer->g = 7;
                return value.type + value.x + value.y + pointer->r + pointer->g;
            }""",
            23,
        )

    def test_anonymous_member_designator_brace_elision_continues_inside_member(self):
        run(
            """struct S { int tag; struct { int x; int y; }; int tail; };
            int main(void) {
                struct S value = { .x = 1, 2 };
                return value.x * 100 + value.y * 10 + value.tail;
            }""",
            120,
        )

    def test_anonymous_aggregate_members_example(self):
        source = (ROOT / "examples/anonymous_aggregate_members.c").read_text()
        run(source, 28)

    def test_structure_returns_use_caller_owned_result_storage(self):
        run(
            """struct Pair { int left; int right; };
            struct Pair make_pair(int value) {
                struct Pair result = { value, value + 1 };
                return result;
            }
            struct Pair twice(int value) { return make_pair(value + value); }
            int main(void) {
                struct Pair (*factory)(int) = make_pair;
                struct Pair first = twice(20);
                struct Pair second = factory(7);
                return first.left + first.right + second.left + second.right;
            }""",
            96,
        )

    def test_c_aggregate_compat_example(self):
        source = (ROOT / "examples/c_aggregate_compat.c").read_text()
        run(source, 41)

    def test_memory_runtime(self):
        result, _ = run(
            """int main(void) {
                unsigned char source[8] = {1,2,3,4,5,6,7,8};
                unsigned char destination[8] = {0};
                if (memcpy(destination, source, 8) != destination) return 1;
                memmove(destination + 2, destination, 6);
                memset(source, 9, 4);
                return destination[0] * 1000 + destination[1] * 100
                    + destination[2] * 10 + destination[7]
                    + (memcmp(source, source, 8) != 0);
            }""",
            1216,
        )
        self.assertNotIn("malloc", result.image.symbols)

    def test_heap_allocation_reuse_coalescing_and_alignment(self):
        source = """int main(void) {
            unsigned char *a = malloc(16), *b = malloc(16), *c = malloc(16), *d;
            if (!a || !b || !c) return 1;
            free(b); free(a);
            d = malloc(36);
            if (d != a) return 2;
            free(c); free(d);
            d = malloc(64);
            if (d != a) return 3;
            return (unsigned int)d & 3u;
        }"""
        for pic, address in ((False, 0), (False, 0x1003), (True, 0x1203)):
            with self.subTest(pic=pic, address=address):
                run(source, 0, pic, address, ram=1 << 14)

    def test_calloc_realloc_and_heap_exhaustion(self):
        run(
            """int main(void) {
                int *values = calloc(3, sizeof(int));
                int *grown;
                if (!values || values[0] || values[1] || values[2]) return 1;
                values[1] = 42;
                grown = realloc(values, 6 * sizeof(int));
                if (!grown) return 2;
                return grown[0] + grown[1] + grown[2];
            }""",
            42,
            ram=1 << 14,
        )
        run(
            "int main(void){return malloc(input()) == 0;}",
            1,
            ram=1 << 13,
            # Larger than the entire usable heap regardless of code-size and
            # frame-size improvements made by the optimizer.
            inputs=(8000,),
        )
        run(
            "int main(void){return calloc(0xffffffffu, 2) == 0;}",
            1,
            ram=1 << 13,
        )
        run(
            "int main(void){return malloc(0xfffffffcu) == 0;}",
            1,
            ram=1 << 13,
        )

    def test_heap_starts_after_text_framebuffer(self):
        result, _ = run(
            """int main(void) {
                unsigned char *framebuffer =
                    (unsigned char *)screen_framebuffer();
                unsigned char *allocation = malloc(4);
                return allocation != 0
                    && allocation >= framebuffer + 3840
                    && (((unsigned int)allocation & 3u) == 0);
            }""",
            1,
        )
        self.assertGreater(
            result.image.symbols["__dyn_heap_start"],
            result.image.symbols["__dyn_printf_framebuffer"],
        )

    def test_bool_type_and_char_arrays(self):
        run("bool global_flag = 9; int main(void) { return global_flag; }", 1)
        run(
            """_Bool nonzero(unsigned int value) { return value; }
            int main(void) {
                bool flags[3] = {0, 9, 0};
                char bytes[] = {4, 5, 6};
                flags[2] = nonzero(bytes[0] - 4);
                return sizeof(_Bool) * 100 + flags[0] * 10 + flags[1] * 5
                    + flags[2] + bytes[1];
            }""",
            110,
        )

    def test_variable_length_arrays(self):
        run(
            """int square_last(int n) {
                int values[n];
                values[n - 1] = n * n;
                return values[n - 1];
            }
            int main(void) { return square_last(input()); }""",
            49,
            inputs=(7,),
        )
        run(
            """int main(void) {
                int n = input();
                int total = 0;
                for (int i = 0; i < 4; i++) {
                    char row[n];
                    row[0] = i;
                    row[n - 1] = 10 + i;
                    if (i == 1) continue;
                    total += row[0] + row[n - 1];
                }
                return total;
            }""",
            40,
            inputs=(3,),
        )
        run(
            """int first_or_zero(int n) {
                int values[n];
                if (n == 1) return 7;
                values[0] = 9;
                return values[0];
            }
            int main(void) { return first_or_zero(input()); }""",
            7,
            inputs=(1,),
        )

    def test_printf_text_framebuffer(self):
        source = """int main(void) {
            int x = 42;
            printf("%d\\n", x);
            printf("x = %d\\n", x);
            printf("%s\\n", "hello");
            printf("%c\\n", 'A');
            return 0;
        }"""
        result, machine = run(source, 0)
        framebuffer = result.image.symbols["__dyn_printf_framebuffer"]
        self.assertLess(len(result.image.binary), 3600)
        self.assertEqual(machine.screen_updates, [(0, 0), (1, framebuffer)])
        self.assertEqual(bytes(machine.memory[framebuffer : framebuffer + 2]), b"42")
        self.assertEqual(bytes(machine.memory[framebuffer + 96 : framebuffer + 102]), b"x = 42")
        self.assertEqual(bytes(machine.memory[framebuffer + 192 : framebuffer + 197]), b"hello")
        self.assertEqual(machine.memory[framebuffer + 288], ord("A"))

        self.assertGreaterEqual(framebuffer, len(result.image.binary))

        result, machine = run(
            """int main(void) {
                char *fb = screen_framebuffer();
                screen_cursor(5, 2);
                printf("%c", 'Z');
                return fb[2 * 96 + 5];
            }""",
            90,
        )
        self.assertIn("__dyn_printf_framebuffer", result.image.symbols)
        self.assertEqual(machine.memory[result.image.symbols["__dyn_printf_framebuffer"] + 197], 90)

    def test_static_storage_sections_and_bss_startup_zeroing(self):
        source = """int initialized = 9;
            const int read_only = 4;
            int zeroes[256];
            static unsigned char bytes[3];
            int main(void) {
                static unsigned short local_zero;
                output((unsigned int)&initialized);
                output((unsigned int)&read_only);
                output((unsigned int)&zeroes);
                output((unsigned int)&bytes);
                output((unsigned int)&local_zero);
                return initialized + read_only + zeroes[255] + bytes[2] + local_zero;
            }"""
        result, _ = run(source, 13)
        globals_ = {global_.symbol.name: global_ for global_ in result.ir.globals}
        sections = {name: global_.section for name, global_ in globals_.items()}
        self.assertEqual(sections["initialized"], "data")
        self.assertEqual(sections["read_only"], "rodata")
        self.assertEqual(sections["zeroes"], "bss")
        self.assertEqual(sections["bytes"], "bss")
        self.assertEqual(sections["local_zero"], "bss")
        self.assertLess(result.image.symbols[globals_["initialized"].symbol.key], len(result.image.binary))
        self.assertLess(result.image.symbols[globals_["read_only"].symbol.key], len(result.image.binary))
        for name in ("zeroes", "bytes"):
            self.assertGreaterEqual(
                result.image.symbols[globals_[name].symbol.key], len(result.image.binary)
            )

        assumed = compile_source(source, target=Target(bss_mode="assume-zeroed"))
        self.assertLess(len(assumed.image.binary), len(result.image.binary))
        self.assertGreaterEqual(
            assumed.image.symbols["zeroes"], len(assumed.image.binary)
        )

        tiny = compile_source(
            "static unsigned char byte; int main(void) { output((unsigned int)&byte); return byte; }"
        )
        tiny_global = next(global_ for global_ in tiny.ir.globals if global_.symbol.name == "byte")
        self.assertEqual(tiny_global.section, "data")
        self.assertLess(tiny.image.symbols[tiny_global.symbol.key], len(tiny.image.binary))

        never = compile_source(source, target=Target(bss_mode="never"))
        never_zeroes = next(global_ for global_ in never.ir.globals if global_.symbol.name == "zeroes")
        self.assertEqual(never_zeroes.section, "data")

    def test_big_endian_and_unaligned(self):
        run(
            "int main(void){int x=0x12345678; unsigned char *p=(unsigned char*)&x; return p[0]*256+p[3];}",
            0x1278,
        )
        run(
            "int main(void){char a[6]={0}; int *p=(int*)(a+1); *p=0x12345678; return a[1]+a[4];}",
            0x8A,
        )

    def test_globals_strings_and_relocations(self):
        source = """static int a[3]={10,20,30}; int z[10]; int *p=&a[1];
            char text[]="abc"; char *s="de";
            int f(int x){return x+1;} int (*fn)(int)=f;
            int main(void){z[9]=4; return *p+text[2]+s[1]+z[9]+fn(5);}"""
        for pic, address in [(False, 0), (False, 0x12340), (True, 0), (True, 0x23451)]:
            with self.subTest(pic=pic, address=address):
                run(source, 230, pic, address)

    def test_dead_globals_and_their_relocation_targets_are_removed(self):
        baseline = compile_source("int main(void){return 6;}")
        result = compile_source(
            "int unused_values[4]={3,5,7,11}; "
            "int dead(int x){return x+99;} "
            "int (*unused_function)(int)=dead; "
            "int main(void){return 6;}"
        )
        self.assertEqual(result.image.binary, baseline.image.binary)
        self.assertEqual(set(result.image.symbols), {"_start", "_halt"})
        self.assertNotIn("dead", result.ir.dump())
        self.assertEqual(result.ir.globals, [])

    def test_live_global_relocations_retain_targets_transitively(self):
        source = (
            "int value=40; int *middle=&value; int **root=&middle; "
            "int dead=99; int *dead_pointer=&dead; "
            "int main(void){output((unsigned int)&root);return **root+2;}"
        )
        result, _ = run(source, 42)
        self.assertEqual(
            {global_.symbol.key for global_ in result.ir.globals},
            {"value", "middle", "root"},
        )
        self.assertNotIn("dead", result.image.symbols)
        self.assertNotIn("dead_pointer", result.image.symbols)

    def test_dead_last_relocation_removes_pic_startup_fixup(self):
        result = compile_source(
            "int dead=1; int *unused=&dead; int main(void){return 7;}",
            target=Target(pic=True),
        )
        self.assertNotIn("relocate_globals", result.ir.dump())
        self.assertEqual(result.ir.globals, [])
        machine = Machine(result.image.binary, 256, 0x40)
        self.assertEqual(machine.run(0x40 + result.image.symbols["_halt"]), 7)

    def test_immutable_scalar_array_and_pointer_loads_fold(self):
        cases = [
            ("int value=42; int main(void){return value;}", 42),
            ("int values[3]={10,20,30}; int main(void){return values[1];}", 20),
            (
                "int value=40; int *pointer=&value; "
                "int main(void){return *pointer+2;}",
                42,
            ),
            ("signed char value=255; int main(void){return value;}", -1),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                result, _ = run(source, expected)
                self.assertEqual(result.ir.globals, [])
                self.assertEqual(set(result.image.symbols), {"_start", "_halt"})

    def test_written_or_escaped_globals_are_not_folded(self):
        old = OPTIMIZATIONS["straight_line_memory_forwarding"]
        try:
            # Isolate immutable-global folding: the separate exact-address
            # forwarding pass may validly replace this store-followed load.
            OPTIMIZATIONS["straight_line_memory_forwarding"] = False
            result, _ = run(
                "int value=4; int main(void){value=input();return value;}",
                17,
                inputs=[17],
            )
        finally:
            OPTIMIZATIONS["straight_line_memory_forwarding"] = old
        self.assertIn("value", result.image.symbols)
        self.assertIn(" load ", result.ir.dump())
        result, _ = run(
            "int values[2]={4,5}; int main(void){int i=input();values[i]=9;return values[0];}",
            9,
            inputs=[0],
        )
        self.assertIn("values", result.image.symbols)

    def test_pic_binary_identical_and_reentry(self):
        source = "int x=13; int *p=&x; int main(void){return *p;}"
        a = compile_source(source, target=Target(pic=True))
        b = compile_source(source, target=Target(pic=True, load_address=0x10000))
        self.assertEqual(a.image.binary, b.image.binary)
        for base in (0, 128, 0x10003, 0x21000):
            m = Machine(a.image.binary, 1 << 20, base)
            self.assertEqual(m.run(base + a.image.symbols["_halt"]), 13)
            m.pc = base
            self.assertEqual(m.run(base + a.image.symbols["_halt"]), 13)

    def test_static_constant_conversions(self):
        run("int x=(signed char)255; int main(void){return x;}", -1)
        run("unsigned int x=(0xffffffffu+1u)/2u; int main(void){return x;}", 0)
        run("int x=-7/3; int y=-7%3; int main(void){return x*10+y;}", -21)
        run("int x=1 ? 7 : 1/0; int y=0 && 1/0; int main(void){return x+y;}", 7)
        run("int a[2+3]={1,2}; int main(void){return sizeof(a)+a[4];}", 20)

    def test_source_diagnostics_for_unsupported_semantics(self):
        for source in [
            "int f(int x){int x=3;return x;} int main(void){return f(1);}",
            "int main(void){volatile int x=3;return x;}",
            "const int x=3; int main(void){x=4;return x;}",
            "int main(void){const int x=3; const int *p=&x; *p=4; return x;}",
            "const int x=3; int main(void){int *p=&x;return *p;}",
        ]:
            with self.subTest(source=source), self.assertRaises(CompileError):
                compile_source(source)

    def test_function_pointers(self):
        run(
            """int inc(int x){return x+1;} int apply(int (*fn)(int),int x){return (*fn)(x);}
            int main(void){int (*p)(int)=inc; return apply(p,41);}""",
            42,
            True,
            0x10100,
        )

    def test_stack_passed_arguments(self):
        source = """
            int sum8(int a,int b,int c,int d,int e,int f,int g,int h) {
                return a+b+c+d+e+f+g+h;
            }
            int (*call8)(int,int,int,int,int,int,int,int)=sum8;
            int main(void) {
                return call8(1,2,3,4,5,6,7,8)
                     + call8(1,1,1,1,1,1,1,1);
            }
        """
        result, machine = run(source, 44)
        self.assertIn("sum8", result.image.symbols)
        self.assertEqual(machine.regs[14], 0)
        run(
            "int f(int a,int b,int c,int d,int e,int f,signed char g,unsigned short h)"
            "{return g+h;} int main(void){return f(0,0,0,0,0,0,255,65535);}",
            65534,
        )

    def test_parameter_register_moves_do_not_clobber_later_arguments(self):
        run(
            "int f(int a,int b,int c,int d,int e){"
            "if(a==0)return b; return a+a+a+c+d+e;}"
            "int main(void){return f(10,20,3,4,5);}",
            42,
        )

    def test_scope(self):
        run(
            "int x=2; int main(void){int x=3; {int x=4; x++;} for(int x=0;x<3;x++){} return x;}",
            3,
        )
        run(
            "typedef unsigned short word; word x=65535; int main(void){return x+1;}",
            65536,
        )

    def test_structs_enums_const_and_local_typedefs(self):
        run(
            """struct Pair { char a; int b; short c; };
            int main(void){struct Pair p; struct Pair *q=&p;
            p.a=2; q->b=30; p.c=4;
            return p.a+p.b+p.c+sizeof(struct Pair);}""",
            48,
        )
        run(
            """struct Node { int value; struct Node *next; };
            int main(void){struct Node n[2]; n[0].value=7; n[0].next=&n[1];
            n[0].next->value=9;
            return n[0].value+n[1].value+sizeof(struct Node);}""",
            24,
        )
        run(
            """struct Inner { short x; char y; };
            struct Outer { char tag; struct Inner inner; int values[2]; };
            struct Outer data={'A',{2,3},{4,5}};
            int main(void){return data.tag+data.inner.x+data.inner.y+
            data.values[0]+data.values[1]+sizeof(data);}""",
            95,
        )
        run(
            """enum Mode { MODE_A=3, MODE_B, MODE_C=MODE_B+5 };
            int main(void){enum Mode mode=MODE_C; return mode+sizeof(enum Mode);}""",
            13,
        )
        run(
            "typedef int word; int main(void){typedef unsigned char word; "
            "const word value=255; return value+sizeof(word);}",
            256,
        )
        run(
            "const int global=12; int main(void){const int local=30; "
            "const int *p=&local; return global+*p;}",
            42,
        )
        run(
            "int main(void){int a=20,b=22; const int *p=&a; p=&b; return a+*p;}",
            42,
        )
        run(
            "struct Item {int value;}; int main(void){struct Item outer; "
            "{struct Item {char value;}; struct Item inner; inner.value=3; "
            "if(sizeof(inner)!=1)return 0;} outer.value=4; return sizeof(outer);}",
            4,
        )
        run(
            "int next(void){static int value=10; return ++value;} "
            "int main(void){int a=next(); int b=next(); return a*100+b;}",
            1112,
        )

    def test_void(self):
        run(
            "int x; void f(int a){x=a; return;} int main(void){f(9); (void)x; return x;}",
            9,
        )

    def test_callee_saved_registers(self):
        result = compile_source(
            "int f(int n){if(n<2)return 1;return n*f(n-1);} int main(void){return f(6);}"
        )
        m = Machine(result.image.binary, 1 << 20)
        for r in range(8, 13):
            m.regs[r] = r * 19
        self.assertEqual(m.run(result.image.symbols["_halt"]), 720)
        for r in range(8, 13):
            self.assertEqual(m.regs[r], r * 19)
        self.assertEqual(m.regs[14], 0)

    def test_far_code_and_data(self):
        run("char pad[70000]; int value=37; int main(void){return value;}", 37)
        run("int main(void){return 0x12345678;}", 0x12345678, False, 0x23456)

    def test_software_arithmetic(self):
        rng = random.Random(917)
        for unsigned in (False, True):
            pairs = [(0, 1), (1, 1), (37, 5), (0x7FFFFFFF, 17)]
            pairs += (
                [
                    (0xFFFFFFFF, 0x80000000),
                    (0x80000000, 0xFFFFFFFF),
                    (0xFFFFFFFF, 0xFFFFFFFF),
                ]
                if unsigned
                else [
                    (-7, 3),
                    (7, -3),
                    (-7, -3),
                    (-2147483648, 2),
                    (2147483647, -2147483648),
                ]
            )
            pairs += [
                (
                    (
                        rng.randrange(0, 2**32)
                        if unsigned
                        else rng.randrange(-(2**31), 2**31)
                    ),
                    (
                        rng.randrange(1, 2**32)
                        if unsigned
                        else rng.choice([-1, 1]) * rng.randrange(1, 2**31)
                    ),
                )
                for _ in range(12)
            ]
            for a, b in pairs:
                q = (
                    a // b
                    if unsigned
                    else (abs(a) // abs(b)) * (-1 if (a < 0) != (b < 0) else 1)
                )
                for op, expected in [("*", a * b), ("/", q), ("%", a - q * b)]:
                    with self.subTest(unsigned=unsigned, a=a, b=b, op=op):
                        typ = "unsigned int" if unsigned else "int"
                        run(
                            f"int main(void){{{typ} a=({typ})input(),b=({typ})input();return a{op}b;}}",
                            expected,
                            inputs=(a & 0xFFFFFFFF, b & 0xFFFFFFFF),
                        )

    def test_c_escapes_and_adjacent_strings(self):
        run(
            r"""int main(void){char *s="a\?\n" "\x41\101"; return s[0]+s[1]+s[2]+s[3]+s[4]+s[5];}""",
            300,
        )
        run(r"""int main(void){return '\?'+'\n'+'\101';}""", 138)
        with self.assertRaises(CompileError):
            compile_source("int main(void){return 2147483648;}")
        with self.assertRaises(CompileError):
            compile_source("int main(void){unsigned signed int x; return 0;}")

    def test_comments_and_literals(self):
        run("/*head*/int main(void){// line\n return 010 + 0x10 + 'A';}", 89)
        run('int main(void){char *s="/*not a comment*/"; return s[0];}', 47)

    def test_preprocessor_headers_macros_and_conditionals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.h").write_text(
                "#pragma once\n#define SCALE(x) ((x) * FACTOR)\n"
                "#if FACTOR == 3\n#define OFFSET 4\n#else\n"
                "#error wrong factor\n#endif\n"
            )
            result = compile_sources(
                [
                    (
                        "main.c",
                        '#include <stdint.h>\n#include "config.h"\n'
                        "int main(void){uint32_t x=SCALE(5);return x+OFFSET;}\n",
                    )
                ],
                include_dirs=[root],
                defines=["FACTOR=3"],
            )
            machine = Machine(result.image.binary)
            self.assertEqual(machine.run(result.image.symbols["_halt"]), 19)

    def test_multidimensional_vla_sizeof_typedef_and_parameter_stride(self):
        run(
            """int inspect(unsigned int rows, unsigned int columns,
                           int values[rows][columns]) {
                   return sizeof(*values) + values[1][2];
               }
               int main(void) {
                   unsigned int rows=input(), columns=input();
                   typedef int Row[columns];
                   Row values[rows];
                   values[1][2]=37;
                   return sizeof(values)+inspect(rows,columns,values);
               }""",
            73,
            inputs=(2, 3),
        )

    def test_vla_overflow_enters_stack_overflow_trap(self):
        result = compile_source(
            "int main(void){unsigned int n=input(); "
            "int values[n][n]; return values[0][0];}"
        )
        machine = Machine(result.image.binary, inputs=(65536,))
        with self.assertRaisesRegex(RuntimeError, "execution limit"):
            machine.run(result.image.symbols["_halt"], max_steps=20000)
        self.assertNotEqual(machine.pc, result.image.symbols["_halt"])
        self.assertIn("_stack_overflow", result.image.symbols)


class DiagnosticTests(unittest.TestCase):
    def test_library_and_platform_names_require_headers(self):
        cases = [
            ("int main(void){return malloc(4)==0;}", "malloc"),
            ("int main(void){char x; memset(&x,0,1); return x;}", "memset"),
            ('int main(void){printf("x");return 0;}', "printf"),
            ("int main(void){return input();}", "input"),
            ("int main(void){bool value=1;return value;}", "before"),
        ]
        for source, name in cases:
            with self.subTest(name=name), self.assertRaisesRegex(CompileError, name):
                _compile_source(source, target=Target())

        accepted = """#include <stdbool.h>
            #include <stdio.h>
            #include <stdlib.h>
            #include <string.h>
            #include <symphony.h>
            int main(void){
                bool ok=1; char *p=malloc(1); memset(p,0,1);
                output(*p); printf("%d",ok); free(p); return input()+ok;
            }"""
        result = _compile_source(accepted, target=Target())
        machine = Machine(result.image.binary, inputs=[41])
        self.assertEqual(machine.run(result.image.symbols["_halt"]), 42)

    def test_rejections(self):
        cases = [
            ("int main(void){return missing;}", "undeclared"),
            ("int main(void){float x=1.0;return 0;}", "only integer"),
            ("int main(void){long long x;return 0;}", "64-bit"),
            ("int f(int a); int main(void){return f(1);}", "undefined symbol"),
            ("int main(void){int a[2]; a=0;return 0;}", "lvalue"),
            ("int main(void){break;return 0;}", "outside loop"),
            ("int f(int x){return x;} int main(void){return f();}", "arguments"),
            ("int main(void){int *p=2;return 0;}", "convert"),
            ("int main(void){int a[1]={1,2};return 0;}", "too many"),
            ("void main(void){}", "entry point"),
            ("int main(void){return __dyn_mul(1,2);}", "reserved"),
            ("int main(void){return 1.25;}", "floating"),
            ("int main(void){switch(1){case 1:return 0;case 1:return 1;}}", "duplicate case"),
            ("int main(void){switch(1){default:return 0;default:return 1;}}", "duplicate default"),
            ("int main(void){int a[2]={[2]=1};return 0;}", "outside the array"),
            ("int main(void){union U{int a;int b;};union U u={1,2};return 0;}", "exactly one"),
            (
                "int main(void){struct S{union{struct{int x;};struct{int x;};};};"
                "struct S value;return value.x;}",
                "duplicate member",
            ),
            (
                "struct Inner; struct Outer { struct Inner; int value; };"
                "int main(void){return sizeof(struct Outer);}",
                "complete object type",
            ),
            (
                "int main(void){struct S{const struct{int x;};};"
                "struct S value={{1}};value.x=2;return value.x;}",
                "modifiable lvalue",
            ),
            (
                "int main(void){struct S{volatile struct{int x;};};return 0;}",
                "unsupported type qualifier",
            ),
            ("int main(void){goto missing;return 0;}", "undefined label"),
            ("int main(void){x:return 0;x:return 1;}", "duplicate label"),
            (
                "int main(void){goto inside;{int n=2;int values[n];inside:return 0;}}",
                "enters the scope",
            ),
            (
                "int main(void){switch(1){int n=4;int values[n];case 1:return values[0];}return 0;}",
                "enters the scope",
            ),
            (
                "int main(void){switch(1){int n=4;int values[n];default:return values[0];}return 0;}",
                "enters the scope",
            ),
            (
                "int main(void){unsigned int x=0;"
                "switch(x){case -1:return 1;case 4294967295u:return 2;default:return 0;}}",
                "duplicate case",
            ),
        ]
        for source, message in cases:
            with self.subTest(source=source):
                with self.assertRaisesRegex(CompileError, message):
                    compile_source(source)

    def test_target_validation(self):
        for target in [
            Target(ram_size=12345),
            Target(persistent_size=13),
            Target(load_address=-1),
            Target(ram_size=4),
            Target(bss_mode="invalid"),
            Target(bss_mode="always"),
        ]:
            with self.subTest(target=target), self.assertRaises(CompileError):
                compile_source("int main(void){return 0;}", target=target)


class EncodingTests(unittest.TestCase):
    def test_branch_merge_example_beats_unoptimized_cfg(self):
        source = (ROOT / "examples" / "branch_merge.c").read_text()
        old = OPTIMIZATIONS["cfg_simplification"]
        try:
            OPTIMIZATIONS["cfg_simplification"] = False
            unoptimized = _compile_source(source, target=Target())
        finally:
            OPTIMIZATIONS["cfg_simplification"] = old
        optimized = _compile_source(source, target=Target())

        def execute(result, inputs):
            machine = Machine(result.image.binary, inputs=inputs)
            return machine.run(result.image.symbols["_halt"])

        for inputs in ((0,), (1,)):
            self.assertEqual(execute(unoptimized, inputs), 4)
            self.assertEqual(execute(optimized, inputs), 4)
        self.assertLess(len(optimized.image.binary), len(unoptimized.image.binary))

    def test_comparison_zero_test_example_beats_unoptimized_branch_form(self):
        source = (ROOT / "examples" / "comparison_zero_test.c").read_text()
        old = OPTIMIZATIONS["comparison_zero_test_fusion"]
        try:
            OPTIMIZATIONS["comparison_zero_test_fusion"] = False
            unoptimized = _compile_source(source, target=Target())
        finally:
            OPTIMIZATIONS["comparison_zero_test_fusion"] = old
        optimized = _compile_source(source, target=Target())

        def execute(result, inputs):
            machine = Machine(result.image.binary, inputs=inputs)
            return machine.run(result.image.symbols["_halt"])

        for inputs, expected in (((1, 2), 7), ((2, 1), 3)):
            self.assertEqual(execute(unoptimized, inputs), expected)
            self.assertEqual(execute(optimized, inputs), expected)
        self.assertLess(len(optimized.image.binary), len(unoptimized.image.binary))

    def test_paired_divmod_example_runs_one_software_division(self):
        source = (ROOT / "examples" / "divmod_pair.c").read_text()
        old = OPTIMIZATIONS["paired_divmod"]
        try:
            OPTIMIZATIONS["paired_divmod"] = False
            unoptimized = _compile_source(source, target=Target())
        finally:
            OPTIMIZATIONS["paired_divmod"] = old
        optimized = _compile_source(source, target=Target())

        def execute(result, value):
            machine = Machine(result.image.binary, inputs=(value,))
            machine.run(result.image.symbols["_halt"])
            return machine.outputs, machine.steps

        for value, expected in ((0, [0, 0]), (123456789, [12345678, 9])):
            self.assertEqual(execute(unoptimized, value)[0], expected)
            self.assertEqual(execute(optimized, value)[0], expected)
        self.assertLess(len(optimized.image.binary), len(unoptimized.image.binary))
        self.assertLess(execute(optimized, 123456789)[1], execute(unoptimized, 123456789)[1])

    def test_loop_pressure_guard_uses_live_at_call_and_configurable_budget(self):
        source = (ROOT / "examples" / "loop_helper_inlining.c").read_text()
        old = OPTIMIZATIONS["loop_pressure_aware_inlining"]
        old_budget = OPTIMIZATIONS["inlining_register_budget"]
        try:
            OPTIMIZATIONS["loop_pressure_aware_inlining"] = True
            OPTIMIZATIONS["inlining_register_budget"] = old_budget
            live_costed = _compile_source(source, target=Target())
            OPTIMIZATIONS["inlining_register_budget"] = 1
            guarded = _compile_source(source, target=Target())
        finally:
            OPTIMIZATIONS["loop_pressure_aware_inlining"] = old
            OPTIMIZATIONS["inlining_register_budget"] = old_budget
        self.assertNotIn("helper", live_costed.image.symbols)
        self.assertIn("helper", guarded.image.symbols)
        for result in (live_costed, guarded):
            machine = Machine(result.image.binary)
            self.assertEqual(machine.run(result.image.symbols["_halt"]), 316)

    def test_loop_fixed_point_has_a_configurable_hard_limit(self):
        old = OPTIMIZATIONS["loop_fixed_point_iteration_limit"]
        try:
            OPTIMIZATIONS["loop_fixed_point_iteration_limit"] = 0
            with self.assertRaisesRegex(RuntimeError, "did not converge"):
                _compile_source("int main(void) { return 0; }", target=Target())
        finally:
            OPTIMIZATIONS["loop_fixed_point_iteration_limit"] = old

    def test_alias_aware_divmod_improves_primes(self):
        source = (ROOT / "examples" / "primes.c").read_text()
        old_pair = OPTIMIZATIONS["paired_divmod"]
        old_alias = OPTIMIZATIONS["alias_aware_divmod_pairing"]
        old_guard = OPTIMIZATIONS["loop_pressure_aware_inlining"]
        try:
            OPTIMIZATIONS["paired_divmod"] = False
            OPTIMIZATIONS["alias_aware_divmod_pairing"] = False
            OPTIMIZATIONS["loop_pressure_aware_inlining"] = False
            baseline = _compile_source(source, target=Target())
            OPTIMIZATIONS["paired_divmod"] = True
            OPTIMIZATIONS["alias_aware_divmod_pairing"] = True
            optimized = _compile_source(source, target=Target())
        finally:
            OPTIMIZATIONS["paired_divmod"] = old_pair
            OPTIMIZATIONS["alias_aware_divmod_pairing"] = old_alias
            OPTIMIZATIONS["loop_pressure_aware_inlining"] = old_guard

        def execute(result):
            machine = Machine(result.image.binary)
            return machine.run(result.image.symbols["_halt"]), machine.outputs, machine.steps

        baseline_result = execute(baseline)
        optimized_result = execute(optimized)
        self.assertEqual(optimized_result[:2], baseline_result[:2])
        self.assertLess(optimized_result[2], baseline_result[2])
        self.assertLess(len(optimized.image.binary), len(baseline.image.binary))

    def test_expensive_expression_cse_avoids_repeated_multiply(self):
        source = (ROOT / "examples" / "common_subexpression.c").read_text()
        old = OPTIMIZATIONS["expensive_expression_cse"]
        try:
            OPTIMIZATIONS["expensive_expression_cse"] = False
            baseline = _compile_source(source, target=Target())
            OPTIMIZATIONS["expensive_expression_cse"] = True
            optimized = _compile_source(source, target=Target())
        finally:
            OPTIMIZATIONS["expensive_expression_cse"] = old

        def execute(result, value):
            machine = Machine(result.image.binary, inputs=(value,))
            return machine.run(result.image.symbols["_halt"]), machine.steps

        for value in (0, 7, 65537):
            self.assertEqual(execute(optimized, value)[0], execute(baseline, value)[0])
        self.assertLess(execute(optimized, 65537)[1], execute(baseline, 65537)[1])
        self.assertLess(len(optimized.image.binary), len(baseline.image.binary))

    def test_self_reduction_loop_improves_demo(self):
        source = (ROOT / "examples" / "demo.c").read_text()
        old = OPTIMIZATIONS["self_reduction_loop_lowering"]
        try:
            OPTIMIZATIONS["self_reduction_loop_lowering"] = False
            baseline = _compile_source(source, target=Target())
            OPTIMIZATIONS["self_reduction_loop_lowering"] = True
            optimized = _compile_source(source, target=Target())
        finally:
            OPTIMIZATIONS["self_reduction_loop_lowering"] = old

        def execute(result):
            machine = Machine(result.image.binary)
            returned = machine.run(result.image.symbols["_halt"])
            framebuffer = result.image.symbols["__dyn_printf_framebuffer"]
            text = bytes(machine.memory[framebuffer:framebuffer + 11])
            return returned, text, machine.steps

        baseline_result = execute(baseline)
        optimized_result = execute(optimized)
        self.assertEqual(optimized_result[:2], baseline_result[:2])
        self.assertEqual(optimized_result[0], 146)
        self.assertEqual(optimized_result[1], b"Result: 146")
        self.assertLess(optimized_result[2], baseline_result[2])
        self.assertLess(len(optimized.image.binary), len(baseline.image.binary))

    def test_self_reduction_requires_multiplicative_identity(self):
        result = _compile_source(
            "int f(int n){if(n<2)return 2;return n*f(n-1);}"
            "int main(void){return f(3);}",
            target=Target(),
        )
        machine = Machine(result.image.binary)
        self.assertEqual(machine.run(result.image.symbols["_halt"]), 12)

    def test_self_reduction_loop_handles_additive_parameter_expression(self):
        source = (
            "#include <symphony.h>\n"
            "int f(int n){if(n<2)return 0;return n*n+f(n-1);}"
            "int main(void){return f((int)input());}"
        )
        old = OPTIMIZATIONS["self_reduction_loop_lowering"]
        try:
            OPTIMIZATIONS["self_reduction_loop_lowering"] = False
            recursive = _compile_source(source, target=Target())
            OPTIMIZATIONS["self_reduction_loop_lowering"] = True
            looped = _compile_source(source, target=Target())
        finally:
            OPTIMIZATIONS["self_reduction_loop_lowering"] = old

        def execute(result, value):
            machine = Machine(result.image.binary, inputs=(value,))
            return machine.run(result.image.symbols["_halt"])

        for value in (-4, 0, 1, 2, 5, 10):
            self.assertEqual(execute(looped, value), execute(recursive, value))

    def test_straight_line_memory_forwarding_improves_printing_loop(self):
        source = (
            '#include <stdio.h>\n'
            'int main(void){for(int i=0;i<12;i++)'
            'printf("value=%d\\n",i);return 0;}'
        )
        old = OPTIMIZATIONS["straight_line_memory_forwarding"]
        try:
            OPTIMIZATIONS["straight_line_memory_forwarding"] = False
            baseline = _compile_source(source, target=Target())
            OPTIMIZATIONS["straight_line_memory_forwarding"] = True
            optimized = _compile_source(source, target=Target())
        finally:
            OPTIMIZATIONS["straight_line_memory_forwarding"] = old

        def execute(result):
            machine = Machine(result.image.binary)
            returned = machine.run(result.image.symbols["_halt"])
            framebuffer = result.image.symbols["__dyn_printf_framebuffer"]
            rows = bytes(machine.memory[framebuffer:framebuffer + 12 * 96])
            return returned, rows, machine.steps

        baseline_result = execute(baseline)
        optimized_result = execute(optimized)
        self.assertEqual(optimized_result[:2], baseline_result[:2])
        self.assertLess(optimized_result[2], baseline_result[2])
        self.assertLess(len(optimized.image.binary), len(baseline.image.binary))

    def test_bounded_constant_call_evaluates_general_pure_loop(self):
        source = (
            "int sum_squares(int n){int total=0;"
            "for(int i=0;i<n;i++)total+=i*i;return total;}"
            "int main(void){return sum_squares(9);}"
        )
        old = OPTIMIZATIONS["bounded_constant_call_evaluation"]
        try:
            OPTIMIZATIONS["bounded_constant_call_evaluation"] = False
            runtime_loop = _compile_source(source, target=Target())
            OPTIMIZATIONS["bounded_constant_call_evaluation"] = True
            evaluated = _compile_source(source, target=Target())
        finally:
            OPTIMIZATIONS["bounded_constant_call_evaluation"] = old

        def execute(result):
            machine = Machine(result.image.binary)
            return machine.run(result.image.symbols["_halt"]), machine.steps

        self.assertEqual(execute(runtime_loop)[0], 204)
        self.assertEqual(execute(evaluated)[0], 204)
        self.assertLess(execute(evaluated)[1], execute(runtime_loop)[1])
        self.assertLess(len(evaluated.image.binary), len(runtime_loop.image.binary))

        inline = _compile_source(
            "int main(void){int acc=1;"
            "for(int i=5;i>2;i--)acc*=i;return acc;}",
            target=Target(),
        )
        returned, steps = execute(inline)
        self.assertEqual(returned, 60)
        self.assertEqual(steps, 1)
        self.assertEqual(len(inline.image.binary), 8)

    def test_bounded_constant_call_rejects_observable_function(self):
        result = _compile_source(
            "#include <symphony.h>\n"
            "int noisy(int x){output(x);return x+1;}"
            "int main(void){return noisy(4);}",
            target=Target(),
        )
        machine = Machine(result.image.binary)
        self.assertEqual(machine.run(result.image.symbols["_halt"]), 5)
        self.assertEqual(machine.outputs, [4])

    def test_bounded_constant_call_evaluates_tower_before_inlining(self):
        source = (
            "#include <symphony.h>\n"
            "int leaf(int x){return x+1;}"
            "int middle(int x){return leaf(x)+2;}"
            "int runtime(int x){return leaf(x)+4;}"
            "int main(void){return middle(5)+runtime(input());}"
        )
        old = OPTIMIZATIONS["bounded_constant_call_evaluation"]
        try:
            OPTIMIZATIONS["bounded_constant_call_evaluation"] = False
            unfolded = _compile_source(source, target=Target())
            OPTIMIZATIONS["bounded_constant_call_evaluation"] = True
            folded = _compile_source(source, target=Target())
        finally:
            OPTIMIZATIONS["bounded_constant_call_evaluation"] = old

        measurements = []
        for result in (unfolded, folded):
            machine = Machine(result.image.binary, inputs=(10,))
            returned = machine.run(result.image.symbols["_halt"])
            self.assertEqual(returned, 23)
            measurements.append(machine.steps)
        self.assertIn("direct_call", unfolded.ir.dump())
        self.assertNotIn("direct_call", folded.ir.dump())
        self.assertLess(len(folded.image.binary), len(unfolded.image.binary))
        self.assertLess(measurements[1], measurements[0])

    def test_bounded_constant_call_budget_is_configurable(self):
        source = (
            "#include <symphony.h>\n"
            "int leaf(int x){return x+1;}"
            "int middle(int x){return leaf(x)+2;}"
            "int runtime(int x){return leaf(x)+4;}"
            "int main(void){return middle(5)+runtime(input());}"
        )
        key = "bounded_constant_call_instruction_limit"
        old = OPTIMIZATIONS[key]
        try:
            OPTIMIZATIONS[key] = 1
            limited = _compile_source(source, target=Target())
            OPTIMIZATIONS[key] = 1024
            evaluated = _compile_source(source, target=Target())
        finally:
            OPTIMIZATIONS[key] = old

        for result in (limited, evaluated):
            machine = Machine(result.image.binary, inputs=(10,))
            self.assertEqual(machine.run(result.image.symbols["_halt"]), 23)
        self.assertGreater(len(limited.image.binary), len(evaluated.image.binary))

    def test_constant_loop_evaluation_improves_demo_array_sum(self):
        source = (ROOT / "examples" / "demo.c").read_text()
        old = OPTIMIZATIONS["constant_loop_evaluation"]
        try:
            OPTIMIZATIONS["constant_loop_evaluation"] = False
            runtime_loop = _compile_source(source, target=Target())
            OPTIMIZATIONS["constant_loop_evaluation"] = True
            evaluated = _compile_source(source, target=Target())
        finally:
            OPTIMIZATIONS["constant_loop_evaluation"] = old

        def execute(result):
            machine = Machine(result.image.binary)
            returned = machine.run(result.image.symbols["_halt"])
            framebuffer = result.image.symbols["__dyn_printf_framebuffer"]
            text = bytes(machine.memory[framebuffer:framebuffer + 11])
            return returned, text, machine.steps

        baseline = execute(runtime_loop)
        optimized = execute(evaluated)
        self.assertEqual(optimized[:2], baseline[:2])
        self.assertEqual(optimized[:2], (146, b"Result: 146"))
        self.assertLess(optimized[2], baseline[2])
        self.assertLess(len(evaluated.image.binary), len(runtime_loop.image.binary))

        mutable = _compile_source(
            "#include <symphony.h>\n"
            "int values[2]={1,2};int main(void){values[input()]=9;"
            "int total=0;for(int i=0;i<2;i++)total+=values[i];return total;}",
            target=Target(),
        )
        for index in (0, 1):
            machine = Machine(mutable.image.binary, inputs=(index,))
            self.assertEqual(machine.run(mutable.image.symbols["_halt"]), 11 if index == 0 else 10)

    def test_constant_loop_evaluation_reconstructs_break_live_out(self):
        source = (
            "#include <symphony.h>\n"
            "int main(void){int x=3;for(int i=0;i<6;i++){"
            "if(i==2){x=i+7;break;}x++;}output(x);return x;}"
        )
        old_evaluate = OPTIMIZATIONS["constant_loop_evaluation"]
        old_unroll = OPTIMIZATIONS["known_trip_full_unrolling"]
        try:
            OPTIMIZATIONS["known_trip_full_unrolling"] = False
            OPTIMIZATIONS["constant_loop_evaluation"] = False
            looped = _compile_source(source, target=Target())
            OPTIMIZATIONS["constant_loop_evaluation"] = True
            evaluated = _compile_source(source, target=Target())
        finally:
            OPTIMIZATIONS["constant_loop_evaluation"] = old_evaluate
            OPTIMIZATIONS["known_trip_full_unrolling"] = old_unroll

        def execute(result):
            machine = Machine(result.image.binary)
            returned = machine.run(result.image.symbols["_halt"])
            return returned, machine.outputs, machine.steps

        self.assertEqual(execute(evaluated)[:2], execute(looped)[:2])
        self.assertEqual(execute(evaluated)[:2], (9, [9]))
        self.assertLess(execute(evaluated)[2], execute(looped)[2])
        self.assertLess(len(evaluated.image.binary), len(looped.image.binary))

    def test_known_trip_unrolling_obeys_final_code_growth_policy(self):
        source = (
            "#include <symphony.h>\n"
            "int main(void){for(int i=0;i<2;i++)output(input()+i);return 7;}"
        )
        old_unroll = OPTIMIZATIONS["known_trip_full_unrolling"]
        old_guard = OPTIMIZATIONS["unroll_no_code_growth"]
        try:
            OPTIMIZATIONS["known_trip_full_unrolling"] = False
            looped = _compile_source(source, target=Target())
            OPTIMIZATIONS["known_trip_full_unrolling"] = True
            OPTIMIZATIONS["unroll_no_code_growth"] = True
            unrolled = _compile_source(source, target=Target())
        finally:
            OPTIMIZATIONS["known_trip_full_unrolling"] = old_unroll
            OPTIMIZATIONS["unroll_no_code_growth"] = old_guard

        def execute(result):
            machine = Machine(result.image.binary, inputs=(10, 20, 30))
            returned = machine.run(result.image.symbols["_halt"])
            return returned, machine.outputs, machine.steps

        self.assertEqual(execute(unrolled)[:2], execute(looped)[:2])
        self.assertEqual(execute(unrolled)[:2], (7, [10, 21]))
        self.assertLess(execute(unrolled)[2], execute(looped)[2])
        self.assertLess(len(unrolled.image.binary), len(looped.image.binary))

        speed_source = source.replace("i<2", "i<3")
        try:
            OPTIMIZATIONS["known_trip_full_unrolling"] = True
            OPTIMIZATIONS["unroll_no_code_growth"] = True
            guarded = _compile_source(speed_source, target=Target())
            OPTIMIZATIONS["unroll_no_code_growth"] = False
            speed = _compile_source(speed_source, target=Target())
        finally:
            OPTIMIZATIONS["known_trip_full_unrolling"] = old_unroll
            OPTIMIZATIONS["unroll_no_code_growth"] = old_guard
        self.assertGreater(len(speed.image.binary), len(guarded.image.binary))
        self.assertLess(execute(speed)[2], execute(guarded)[2])

    def test_known_trip_unrolling_traces_conditional_loop_body(self):
        source = (
            "#include <symphony.h>\n"
            "int main(void){int i=0;while(i<4){i++;"
            "if(i&1)output(input()+i);}return i;}"
        )
        old_unroll = OPTIMIZATIONS["known_trip_full_unrolling"]
        old_guard = OPTIMIZATIONS["unroll_no_code_growth"]
        try:
            OPTIMIZATIONS["known_trip_full_unrolling"] = False
            looped = _compile_source(source, target=Target())
            OPTIMIZATIONS["known_trip_full_unrolling"] = True
            OPTIMIZATIONS["unroll_no_code_growth"] = True
            unrolled = _compile_source(source, target=Target())
        finally:
            OPTIMIZATIONS["known_trip_full_unrolling"] = old_unroll
            OPTIMIZATIONS["unroll_no_code_growth"] = old_guard

        def execute(result):
            machine = Machine(result.image.binary, inputs=(10, 20, 30, 40))
            returned = machine.run(result.image.symbols["_halt"])
            return returned, machine.outputs, machine.steps

        self.assertEqual(execute(looped)[:2], (4, [11, 23]))
        self.assertEqual(execute(unrolled)[:2], execute(looped)[:2])
        self.assertLess(execute(unrolled)[2], execute(looped)[2])
        self.assertLess(len(unrolled.image.binary), len(looped.image.binary))

    def test_known_trip_unrolling_reconstructs_known_break_live_out(self):
        source = (
            "#include <symphony.h>\n"
            "int main(void){int x=3;for(int i=0;i<4;i++){"
            "if(i==2){x=i+7;break;}x++;output(input()+x);}return x;}"
        )
        old_unroll = OPTIMIZATIONS["known_trip_full_unrolling"]
        old_guard = OPTIMIZATIONS["unroll_no_code_growth"]
        try:
            OPTIMIZATIONS["known_trip_full_unrolling"] = False
            looped = _compile_source(source, target=Target())
            OPTIMIZATIONS["known_trip_full_unrolling"] = True
            OPTIMIZATIONS["unroll_no_code_growth"] = True
            unrolled = _compile_source(source, target=Target())
        finally:
            OPTIMIZATIONS["known_trip_full_unrolling"] = old_unroll
            OPTIMIZATIONS["unroll_no_code_growth"] = old_guard

        def execute(result):
            machine = Machine(result.image.binary, inputs=(10, 20))
            returned = machine.run(result.image.symbols["_halt"])
            return returned, machine.outputs, machine.steps

        self.assertEqual(execute(looped)[:2], (9, [14, 25]))
        self.assertEqual(execute(unrolled)[:2], execute(looped)[:2])
        self.assertLess(execute(unrolled)[2], execute(looped)[2])
        self.assertLess(len(unrolled.image.binary), len(looped.image.binary))

    def test_known_trip_unrolling_clones_data_dependent_cfg(self):
        source = (
            "#include <symphony.h>\n"
            "int main(void){int x=0;for(int i=0;i<3;i++){"
            "if(input())x+=i+1;else x+=2;output(x);}return x;}"
        )
        old_unroll = OPTIMIZATIONS["known_trip_full_unrolling"]
        old_guard = OPTIMIZATIONS["unroll_no_code_growth"]
        try:
            OPTIMIZATIONS["known_trip_full_unrolling"] = False
            looped = _compile_source(source, target=Target())
            OPTIMIZATIONS["known_trip_full_unrolling"] = True
            OPTIMIZATIONS["unroll_no_code_growth"] = True
            guarded = _compile_source(source, target=Target())
            OPTIMIZATIONS["unroll_no_code_growth"] = False
            unrolled = _compile_source(source, target=Target())
        finally:
            OPTIMIZATIONS["known_trip_full_unrolling"] = old_unroll
            OPTIMIZATIONS["unroll_no_code_growth"] = old_guard

        def execute(result, inputs):
            machine = Machine(result.image.binary, inputs=inputs)
            returned = machine.run(result.image.symbols["_halt"])
            return returned, machine.outputs, machine.steps

        for inputs in ((1, 0, 1), (0, 1, 0)):
            self.assertEqual(execute(unrolled, inputs)[:2], execute(looped, inputs)[:2])
        self.assertEqual(execute(unrolled, (1, 0, 1))[:2], (6, [1, 3, 6]))
        self.assertEqual(len(guarded.image.binary), len(looped.image.binary))
        self.assertLess(execute(unrolled, (1, 0, 1))[2], execute(looped, (1, 0, 1))[2])

    def test_pointer_limit_loop_uses_scaled_derived_recurrence(self):
        source = (
            "#include <symphony.h>\n"
            "int values[4]={2,4,6,8};int main(void){int n=input(),total=0;"
            "for(int i=0;i<n;i++){total+=values[i];output(total);}return total;}"
        )
        old = OPTIMIZATIONS["pointer_limit_loops"]
        old_budget = OPTIMIZATIONS["loop_register_budget"]
        try:
            # Isolate pointer-limit lowering from the independently tested
            # scaled-induction pressure policy that supplies its cursor.
            OPTIMIZATIONS["loop_register_budget"] = 64
            OPTIMIZATIONS["pointer_limit_loops"] = False
            indexed = _compile_source(source, target=Target())
            OPTIMIZATIONS["pointer_limit_loops"] = True
            pointer_limited = _compile_source(source, target=Target())
        finally:
            OPTIMIZATIONS["pointer_limit_loops"] = old
            OPTIMIZATIONS["loop_register_budget"] = old_budget

        def execute(result):
            machine = Machine(result.image.binary, inputs=(4,))
            returned = machine.run(result.image.symbols["_halt"])
            return returned, machine.outputs, machine.steps

        self.assertEqual(execute(pointer_limited)[:2], execute(indexed)[:2])
        self.assertEqual(execute(pointer_limited)[:2], (20, [2, 6, 12, 20]))
        self.assertLess(execute(pointer_limited)[2], execute(indexed)[2])
        self.assertLessEqual(len(pointer_limited.image.binary), len(indexed.image.binary))

    def test_loop_pipeline_reaches_fixed_point_after_self_tail_lowering(self):
        source = (ROOT / "examples" / "insertion_sort.c").read_text()
        inputs = (15, 3, 9, 0, 14, 2, 8, 1, 13, 4, 12, 5, 11, 6, 10, 7)
        old = OPTIMIZATIONS["loop_optimization_fixed_point"]
        try:
            OPTIMIZATIONS["loop_optimization_fixed_point"] = False
            single_pass = _compile_source(source, target=Target(bss_mode="assume-zeroed"))
            OPTIMIZATIONS["loop_optimization_fixed_point"] = True
            fixed_point = _compile_source(source, target=Target(bss_mode="assume-zeroed"))
        finally:
            OPTIMIZATIONS["loop_optimization_fixed_point"] = old

        def execute(result):
            machine = Machine(result.image.binary, inputs=inputs)
            returned = machine.run(result.image.symbols["_halt"])
            return returned, machine.outputs, machine.steps

        self.assertEqual(execute(fixed_point)[:2], execute(single_pass)[:2])
        self.assertLessEqual(len(fixed_point.image.binary), len(single_pass.image.binary))
        self.assertLess(execute(fixed_point)[2], execute(single_pass)[2])

    def test_named_registers_and_abi_roles(self):
        self.assertEqual(isa.Register.ZR, 0)
        self.assertEqual(isa.Register.SP, 14)
        self.assertEqual(isa.Register.FLAGS, 15)
        self.assertEqual(isa.register_name(isa.Register.R7), "r7")
        self.assertEqual(isa.parse_register("sp"), isa.Register.SP)
        self.assertEqual(ABI.argument_registers, tuple(range(1, 8)))
        self.assertEqual(ABI.return_registers, tuple(range(1, 8)))
        self.assertEqual(ABI.return_register, isa.Register.R1)
        self.assertEqual(ABI.status_register, isa.Register.FLAGS)
        self.assertEqual(ABI.link_register, isa.Register.R13)
        self.assertEqual(ABI.frame_pointer, isa.Register.R11)
        self.assertEqual(ABI.pic_base_register, isa.Register.R12)

    def test_r12_is_allocatable_only_without_pic(self):
        # Four values live across g() fill every non-PIC callee-saved home,
        # including r12. Both functions have multiple callers so inlining does
        # not erase the call boundary this test is deliberately exercising.
        source = """int g(int x) { return x + input(); }
            int f(int x) {
                int a=x+1,b=x+2,c=x+3,d=x+4;
                return g(x)+a+b+c+d;
            }
            int main(void) { return f(9)+f(2)+g(1); }"""
        for pic in (False, True):
            with self.subTest(pic=pic):
                result = compile_source(source, target=Target(pic=pic, ram_size=4096))
                start = result.image.symbols["f"]
                end = min(
                    (offset for offset in result.image.symbols.values() if offset > start),
                    default=len(result.image.binary),
                )
                function = result.image.binary[start:end]
                self.assertEqual(isa.push(12) in function, not pic)

    def test_lowerer_emits_canonical_calls_and_fallthrough(self):
        module = lower(
            typecheck(
                parse(
                    "int f(int x){if(x)return x;return 0;} "
                    "int main(void){return f(3);}"
                )
            )
        )
        dump = module.dump()
        self.assertIn("direct_call (", dump)
        self.assertIn("branch_if (", dump)
        self.assertNotIn("global_addr () f", dump)
        self.assertNotIn("  branch (", dump)

    def test_constant_folding_example_is_one_instruction_before_halt(self):
        source = (ROOT / "examples/constant_folding.c").read_text()
        result = compile_source(source)
        halt = result.image.symbols["_halt"]
        self.assertEqual(halt, len(isa.cheap_constant(1, 1466)))
        self.assertEqual(result.image.binary[:halt], isa.cheap_constant(1, 1466))
        self.assertNotIn("main", result.image.symbols)
        self.assertNotIn("__dyn_mul", result.image.symbols)
        machine = Machine(result.image.binary, 256)
        self.assertEqual(machine.run(halt), 1466)
        self.assertEqual(machine.steps, 1)

    def test_interprocedural_constant_folding_example_is_one_instruction(self):
        source = (ROOT / "examples/interprocedural_constant_folding.c").read_text()
        result = compile_source(source)
        halt = result.image.symbols["_halt"]
        self.assertEqual(halt, len(isa.cheap_constant(1, 1466)))
        self.assertEqual(result.image.binary[:halt], isa.cheap_constant(1, 1466))
        self.assertEqual(set(result.image.symbols), {"_start", "_halt"})
        self.assertNotIn("function foo", result.ir.dump())
        self.assertNotIn("function main", result.ir.dump())
        self.assertNotIn("__dyn_mul", result.image.symbols)
        machine = Machine(result.image.binary, 256)
        self.assertEqual(machine.run(halt), 1466)
        self.assertEqual(machine.steps, 1)

    def test_towers_of_hanoi_example(self):
        source = (ROOT / "examples/towers_of_hanoi.c").read_text()
        result = compile_source(source)
        machine = Machine(result.image.binary, inputs=[2, 0, 2, 1])
        self.assertEqual(machine.run(result.image.symbols["_halt"]), 0)
        moves = [(0, 2), (0, 1), (2, 1), (0, 2), (1, 0), (1, 2), (0, 2)]
        expected = []
        for source_location, destination_location in moves:
            expected.extend([source_location, 5, destination_location, 5])
        self.assertEqual(machine.outputs, expected)
        self.assertNotIn("move_one", result.image.symbols)
        self.assertIn("cbranch_if", result.ir.dump())
        self.assertIn("direct_call", result.ir.dump())
        self.assertNotIn("direct_tailcall", result.ir.dump())
        self.assertIn("label () move_pile.entry", result.ir.dump())
        self.assertNotIn("global_addr () move_pile", result.ir.dump())
        self.assertNotIn("main", result.image.symbols)
        self.assertEqual(result.image.frames["_start"], 0)
        self.assertLessEqual(
            len(result.image.binary), 320 if TEST_ISA == "symphony" else 260
        )
        self.assertLessEqual(machine.steps, 272)

    def test_arena_allocator_example(self):
        source = (ROOT / "examples/arena_allocator.c").read_text()
        result = compile_source(source)
        machine = Machine(result.image.binary, 1 << 16)
        self.assertEqual(machine.run(result.image.symbols["_halt"]), 1)

    def test_scalar_locals_fold_to_constant_return(self):
        result = compile_source(
            "int main(void){int a=12345; int b=6789; return a+b;}"
        )
        self.assertNotIn("main", result.image.symbols)
        self.assertEqual(result.image.frames["_start"], 0)
        halt = len(isa.cheap_constant(1, 19134))
        self.assertEqual(result.image.symbols["_halt"], halt)
        self.assertEqual(result.image.binary[:halt], isa.cheap_constant(1, 19134))
        machine = Machine(result.image.binary, 256)
        self.assertEqual(machine.run(result.image.symbols["_halt"]), 19134)
        self.assertEqual(machine.steps, 1)

    def test_readonly_parameters_stay_in_input_registers(self):
        result = compile_source(
            "int add(int a,int b){return a+b;} int (*keep)(int,int)=add; "
            "int main(void){return input()?keep(20,22):add(20,22);}"
        )
        address = result.image.symbols["add"]
        self.assertIn("direct_call", result.ir.dump())
        self.assertNotIn("global_addr () add", result.ir.dump())
        self.assertEqual(result.image.frames["add"], 0)
        self.assertEqual(
            result.image.binary[address : address + 3], isa.alu("add", 1, 1, 2)
        )
        machine = Machine(result.image.binary, 1024)
        self.assertEqual(machine.run(result.image.symbols["_halt"]), 42)

    def test_promoted_parameters_are_permuted_safely_for_tailcalls(self):
        old = OPTIMIZATIONS["bounded_constant_call_evaluation"]
        try:
            # Keep pair's second call site alive so this specifically tests
            # tail-call argument permutation rather than constant evaluation.
            OPTIMIZATIONS["bounded_constant_call_evaluation"] = False
            result = compile_source(
                "int pair(int a,int b){return a*100+b;} "
                "int flip(int a,int b){return pair(b,a);} "
                "int other(void){return pair(8,9);}"
                "int (*keep)(int,int)=flip; "
                "int main(void){return input()?keep(1,2):other();}"
            )
        finally:
            OPTIMIZATIONS["bounded_constant_call_evaluation"] = old
        self.assertIn("direct_tailcall", result.ir.dump())
        for value, expected in ((0, 809), (1, 201)):
            with self.subTest(input=value):
                machine = Machine(result.image.binary, 4096, inputs=[value])
                self.assertEqual(machine.run(result.image.symbols["_halt"]), expected)

    def test_promoted_loop_parameter_survives_the_backedge(self):
        result = compile_source(
            "unsigned at(unsigned *p,unsigned item,unsigned pos){"
            "while(pos && item != 0xffffffffu){item=p[item];pos--;}"
            "if(item==0xffffffffu)return 99;return p[item];}"
            "int other(unsigned *p){return at(p,0,0);}"
            "unsigned (*keep)(unsigned*,unsigned,unsigned)=at;"
            "int main(void){unsigned x[4]={1,2,3,42};"
            "return input()?keep(x,0,3):other(x);}"
        )
        for value, expected in ((0, 1), (1, 42)):
            with self.subTest(input=value):
                machine = Machine(result.image.binary, 4096, inputs=[value])
                self.assertEqual(machine.run(result.image.symbols["_halt"]), expected)

    def test_address_taken_parameter_uses_safe_stack_path(self):
        result = compile_source(
            "int f(int a){int *p=&a; *p+=1; return a;} "
            "int (*keep)(int)=f; int main(void){return input()?keep(41):f(41);}"
        )
        self.assertGreater(result.image.frames["f"], 4)
        machine = Machine(result.image.binary, 2048)
        self.assertEqual(machine.run(result.image.symbols["_halt"]), 42)

    def test_optimized_leaf_and_halt(self):
        result = compile_source("int main(void){return 42;}")
        binary = result.image.binary
        halt = result.image.symbols["_halt"]
        self.assertNotIn("main", result.image.symbols)
        self.assertEqual(result.image.frames["_start"], 0)
        self.assertEqual(binary[:halt], isa.cheap_constant(1, 42))
        self.assertEqual(binary[halt : halt + 4], isa.jump("jmp", halt, True))
        self.assertNotIn("__dyn_mul", result.image.symbols)
        self.assertNotIn("__dyn_udiv", result.image.symbols)

        machine = Machine(binary, 256)
        self.assertEqual(machine.run(halt), 42)
        machine.step()
        self.assertEqual(machine.pc, halt)

    def test_global_fixed_point_prunes_branch_and_relocates_single_caller(self):
        result = compile_source(
            "int dead(void){return 99;} "
            "int add3(int x){return x+3;} "
            "int main(void){if(0)return dead(); return add3(5);}"
        )
        self.assertEqual(set(result.image.symbols), {"_start", "_halt"})
        self.assertNotIn("function dead", result.ir.dump())
        self.assertNotIn("function add3", result.ir.dump())
        self.assertNotIn("function main", result.ir.dump())
        machine = Machine(result.image.binary, 256)
        self.assertEqual(machine.run(result.image.symbols["_halt"]), 8)

    def test_sccp_propagates_promoted_locals_across_cfg_joins(self):
        source = """int main(void) {
            int value = 4;
            if (input()) value = 4; else value = 4;
            return value * 3;
        }"""
        result = compile_source(source)
        self.assertNotIn(" branch_", result.ir.dump())
        self.assertNotIn(" binary ", result.ir.dump())
        for input_value in (0, 1):
            machine = Machine(result.image.binary, inputs=[input_value])
            self.assertEqual(machine.run(result.image.symbols["_halt"]), 12)

    def test_sccp_removes_infeasible_edge_and_callee(self):
        result = compile_source(
            "int unused(void){return input();} "
            "int main(void){int x=1;if(x)x=6;else return unused();return x+1;}"
        )
        self.assertNotIn("unused", result.image.symbols)
        machine = Machine(result.image.binary)
        self.assertEqual(machine.run(result.image.symbols["_halt"]), 7)

    def test_runtime_arithmetic_is_explicit_call_ir(self):
        result = compile_source(
            "int main(void){unsigned int a=input(),b=input();return a/b;}"
        )
        self.assertNotRegex(result.ir.dump(), r"binary .* / ")
        self.assertNotIn("__dyn_udiv", result.image.symbols)
        machine = Machine(result.image.binary, inputs=[100, 7])
        self.assertEqual(machine.run(result.image.symbols["_halt"]), 14)

    def test_single_caller_tail_recursion_becomes_relocated_loop(self):
        result = compile_source(
            "int sum(int n,int total){if(!n)return total;return sum(n-1,total+n);} "
            "int main(void){return sum(input(),0);}"
        )
        self.assertNotIn("function sum", result.ir.dump())
        self.assertNotIn("sum", result.image.symbols)
        self.assertNotIn("direct_tailcall", result.ir.dump())
        machine = Machine(result.image.binary, inputs=[10])
        self.assertEqual(machine.run(result.image.symbols["_halt"]), 55)

    def test_mutual_tail_recursion_is_not_repeatedly_inlined(self):
        result = compile_source(
            "int odd(int n); "
            "int even(int n){if(!n)return 1;return odd(n-1);} "
            "int odd(int n){if(!n)return 0;return even(n-1);} "
            "int main(void){return even(input());}"
        )
        machine = Machine(result.image.binary, inputs=[7])
        self.assertEqual(machine.run(result.image.symbols["_halt"]), 0)

    def test_no_growth_comparison_and_division_identities(self):
        result = compile_source(
            "int main(void){int x=input();return (x==x)+(x!=x)+(x/1)+(x%1);}"
        )
        self.assertNotIn("__dyn_sdiv", result.image.symbols)
        self.assertNotIn("__dyn_smod", result.image.symbols)
        machine = Machine(result.image.binary, inputs=[41])
        self.assertEqual(machine.run(result.image.symbols["_halt"]), 42)

    def test_tier_one_algebraic_identities_reach_fixed_point(self):
        result = compile_source(
            "int main(void){int x=37; return (((x+0)|0)^0) + (x-x) + (x&0);}"
        )
        self.assertEqual(set(result.image.symbols), {"_start", "_halt"})
        self.assertNotIn("binary", result.ir.dump())
        machine = Machine(result.image.binary, 256)
        self.assertEqual(machine.run(result.image.symbols["_halt"]), 37)

    def test_pic_halt_is_one_repeated_register_jump(self):
        result = compile_source("int main(void){return 42;}", target=Target(pic=True))
        halt = result.image.symbols["_halt"]
        self.assertEqual(result.image.binary[halt : halt + 3], isa.jump("jmp", 7))
        machine = Machine(result.image.binary, 1024, 0x80)
        runtime_halt = 0x80 + halt
        self.assertEqual(machine.run(runtime_halt), 42)
        machine.step()
        self.assertEqual(machine.pc, runtime_halt)

    def test_power_of_two_strength_reduction_prunes_multiply(self):
        result = compile_source(
            "int main(void){int a[4]={1,2,3,4}; int i=3; return a[i];}"
        )
        self.assertNotIn("__dyn_mul", result.image.symbols)
        machine = Machine(result.image.binary, 4096)
        self.assertEqual(machine.run(result.image.symbols["_halt"]), 4)

    def test_golden_bytes(self):
        self.assertEqual(isa.alu("add", 1, 2, 3), bytes.fromhex("24 12 03"))
        self.assertEqual(isa.alu("sub", 14, 14, 4, True), bytes.fromhex("35 ee 00 04"))
        self.assertEqual(isa.alu("cmp", 15, 1, 2), bytes.fromhex("2a f1 02"))
        self.assertEqual(isa.counter(13), bytes.fromhex("07 d0"))
        self.assertEqual(isa.mov(3, 4), bytes.fromhex("21 30 04"))
        self.assertEqual(isa.mov(3, 0x1234, True), bytes.fromhex("31 30 12 34"))
        self.assertEqual(isa.load(2, 1, 2), bytes.fromhex("61 10 02"))
        self.assertEqual(isa.store(4, 2, 1), bytes.fromhex("66 01 02"))
        self.assertEqual(isa.jump("jl", 7), bytes.fromhex("44 0f 07"))
        self.assertEqual(
            isa.call(7),
            bytes.fromhex("07 f0 34 ff 00 10 35 ee 00 04 66 0f 0e 48 0f 07"),
        )
        self.assertEqual(
            isa.call(0x1234, True),
            bytes.fromhex(
                "07 f0 34 ff 00 11 35 ee 00 04 66 0f 0e 58 0f 12 34"
            ),
        )
        self.assertEqual(isa.ret(), bytes.fromhex("62 f0 0e 34 ee 00 04 48 0f 0f"))
        self.assertEqual(
            isa.link_call(7), bytes.fromhex("07 d0 34 dd 00 09 48 0f 07")
        )
        self.assertEqual(
            isa.link_call(0x1234, True),
            bytes.fromhex("07 d0 34 dd 00 0a 58 0f 12 34"),
        )
        self.assertEqual(isa.link_return(), bytes.fromhex("48 0f 0d"))

    def test_io_and_persistent_intrinsics(self):
        source = """int main(void) {
            unsigned int a = input();
            unsigned int k = keyboard();
            output(a + k);
            screen(2, a);
            persistent_store(4, a + k);
            return time() ^ time_high() ^ persistent_load(4);
        }"""
        result = compile_source(source, target=Target(ram_size=4096, persistent_size=256))
        self.assertNotIn("call", "\n".join(
            line for line in result.ir.dump().splitlines() if "__dyn_" not in line
        ))
        machine = Machine(
            result.image.binary,
            4096,
            inputs=[7],
            keyboard_inputs=[5],
            time_value=0x1122334455667788,
            persistent_size=256,
        )
        actual = machine.run(result.image.symbols["_halt"])
        self.assertEqual(machine.outputs, [12])
        self.assertEqual(machine.screen_updates, [(2, 7)])
        self.assertEqual(machine.persistent_read(4), 12)
        self.assertEqual(actual, 0x55667788 ^ 0x11223344 ^ 12)

    def test_intrinsic_immediate_encodings_and_reserved_names(self):
        result = compile_source(
            "int main(void){output(0x1234); screen(3, 0xabcd); return 0;}"
        )
        self.assertIn(isa.output(0x1234, True), result.image.binary)
        self.assertIn(isa.screen(1, 0xABCD, True), result.image.binary)
        with self.assertRaisesRegex(CompileError, "reserved device intrinsic"):
            compile_source("unsigned int input(void){return 1;} int main(void){return 0;}")

    def test_encoders_against_supplied_spec(self):
        lines = (ROOT / "docs/isa.txt").read_text().splitlines()
        definitions = {
            line: lines[i + 1]
            for i, line in enumerate(lines[:-1])
            if lines[i + 1] and set(lines[i + 1].replace(" ", "")) <= set("01abcdv")
        }

        def encode(signature, **fields):
            bits = definitions[signature].replace(" ", "")
            for key, value in fields.items():
                digits = iter(f"{value:0{bits.count(key)}b}")
                bits = "".join(next(digits) if c == key else c for c in bits)
            return int(bits, 2).to_bytes(len(bits) // 8, "big")

        for op in isa.ALU:
            if op == "cmp":
                continue
            for immediate in (False, True):
                suffix = "%c:U16(immediate | label)" if immediate else "%c(register)"
                signature = f"{op} %a(register), %b(register), {suffix}"
                value = 0x1234 if immediate else 9
                self.assertEqual(
                    isa.alu(op, 3, 5, value, immediate),
                    encode(signature, a=3, b=5, c=value),
                )
        for op in isa.JUMP:
            self.assertEqual(isa.jump(op, 6), encode(f"{op} %a(register)", a=6))
        self.assertEqual(isa.call(6), encode("call %a(register)", a=6))
        self.assertEqual(
            isa.call(0x1234, True),
            encode("call %a:U16(immediate | label)", a=0x1234),
        )
        self.assertEqual(isa.ret(), encode("ret"))
        self.assertEqual(isa.mov(3, 4), encode("mov %a(register), %b(register)", a=3, b=4))
        self.assertEqual(isa.mov(3, 0x1234, True), encode("mov %a(register), %b:U16(immediate | label)", a=3, b=0x1234))
        self.assertEqual(isa.input_(3), encode("in %a(register)", a=3))
        self.assertEqual(isa.output(4), encode("out %b(register)", b=4))
        self.assertEqual(isa.output(0x1234, True), encode("out %a:U16(immediate)", a=0x1234))
        self.assertEqual(isa.keyboard(5), encode("keyboard %a(register)", a=5))
        self.assertEqual(isa.screen(2, 6), encode("screen %a(register), %b(register)", a=2, b=6))
        self.assertEqual(isa.screen(2, 0x1234, True), encode("screen %a(register), %b:U16(immediate)", a=2, b=0x1234))
        self.assertEqual(isa.time(0, 7), encode("time_0 %a(register)", a=7))
        self.assertEqual(isa.time(1, 8), encode("time_1 %a(register)", a=8))
        self.assertEqual(isa.persistent_load(3, 4), encode("pload %dest(register), [%adr(register)]", d=3, a=4))
        self.assertEqual(isa.persistent_store(4, 3), encode("pstore [%adr(register)], %value(register)", a=4, v=3))

    def test_machine_memory_and_zero_register(self):
        m = Machine(isa.constant(0, 123) + isa.constant(1, 0xFFFFFFFF), 256)
        m.write(255, 0x12345678, 4)
        self.assertEqual(m.read(255, 4), 0x12345678)
        m = Machine(isa.constant(0, 123), 256)
        for _ in range(3):
            m.step()
        self.assertEqual(m.regs[0], 0)

        # Fixed-width execution skips non-semantic padding bytes.  Keeping the
        # original bytes in memory also makes code inspection target-faithful.
        fixed = bytes([0x01, 0x10, 0x08, 0x7F, 0x34, 0x11, 0, 1, 0x58, 0x0F, 0, 8])
        m = Machine(fixed, 256, inputs=[41], symphony=True)
        self.assertEqual(m.run(), 42)
        self.assertEqual(m.steps, 3)
        self.assertEqual(m.read(2, 2), 0x087F)

    def test_status_flag_branches_without_comparison(self):
        for status, branch in ((1, "je"), (0, "jne")):
            with self.subTest(status=status, branch=branch):
                binary = (
                    isa.mov(isa.Register.FLAGS, status, True)
                    + isa.jump(branch, 12, True)
                    + isa.cheap_constant(1, 99)
                    + isa.cheap_constant(1, 42)
                )
                machine = Machine(binary, 256)
                self.assertEqual(machine.run(16), 42)

    def test_costed_casted_scaled_induction_improves_long_array_loops(self):
        source = """
            unsigned values[402];
            int main(void) {
                unsigned seed = input();
                for (unsigned i = 0; i < 402; i++) values[i] = seed + i;
                unsigned total = 0;
                for (unsigned i = 0; i < 402; i++) total += values[i];
                return total;
            }
        """
        keys = (
            "representation_preserving_induction_casts",
            "loop_profitability",
        )
        old = {key: OPTIMIZATIONS[key] for key in keys}
        try:
            for key in keys:
                OPTIMIZATIONS[key] = False
            baseline = compile_source(
                source, target=Target(bss_mode="assume-zeroed")
            )
            for key in keys:
                OPTIMIZATIONS[key] = True
            optimized = compile_source(
                source, target=Target(bss_mode="assume-zeroed")
            )
        finally:
            OPTIMIZATIONS.update(old)

        def execute(result):
            machine = Machine(result.image.binary, inputs=[7])
            returned = machine.run(result.image.symbols["_halt"])
            return returned, machine.steps

        baseline_result = execute(baseline)
        optimized_result = execute(optimized)
        self.assertEqual(baseline_result[0], 83415)
        self.assertEqual(optimized_result[0], baseline_result[0])
        self.assertLess(optimized_result[1], baseline_result[1])

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            source = path / "demo.c"
            source.write_text("int main(void){return 146;}\n")
            cmd = [
                sys.executable,
                "-m",
                "symphony",
                str(source),
                "-o",
                str(path / "demo.bin"),
                "--pic",
                "--bss=assume-zeroed",
                "--run",
                "--run-address",
                "0x12345",
                "--hz-meter",
                "--map",
                str(path / "map.json"),
                "--emit-ir",
                str(path / "demo.ir"),
            ]
            result = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("returned 146", result.stdout)
            self.assertIn(" Hz; PC=", result.stdout)
            self.assertTrue((path / "demo.bin").stat().st_size > 0)
            self.assertTrue(
                json.loads((path / "map.json").read_text())["target"]["pic"]
            )
            self.assertTrue(
                json.loads((path / "map.json").read_text())["target"]["bss_mode"]
                == "assume-zeroed"
            )
            self.assertEqual(
                json.loads((path / "map.json").read_text())["target"]["isa"],
                "symphony",
            )
            self.assertIn("function _start", (path / "demo.ir").read_text())

            for command, expected in (("scc", "symphony"), ("dcc", "dynphony")):
                target_map = path / f"{command}.json"
                script = (
                    f"from symphony.cli import {command}; "
                    f"raise SystemExit({command}({[str(source), '--map', str(target_map)]!r}))"
                )
                result = subprocess.run(
                    [sys.executable, "-c", script],
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(target_map.read_text())["target"]["isa"], expected
                )


if __name__ == "__main__":
    unittest.main()
