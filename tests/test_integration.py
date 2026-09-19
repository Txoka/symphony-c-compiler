"""End-to-end compiler tests using complete, mixed-feature C programs.

These deliberately exercise the public ``compile_source`` API and execute the
raw binary in the reference machine.  Unit-level feature tests remain
in test_compiler.py; these tests protect the boundaries between compiler stages.
"""

import os
import unittest
from pathlib import Path

from symphony import Target as _Target, compile_source as _compile_source, compile_sources as _compile_sources
from symphony.emulator import Machine as _Machine, native_available, native_run


ROOT = Path(__file__).resolve().parents[1]
TEST_ISA = os.environ.get("SYMPHONY_TEST_ISA", "symphony")
TEST_PREAMBLE = """#include <stdio.h>
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
    reference engine otherwise. test_native_emulator_matches_python_reference
    below builds its own plain Machine() instances instead of using this
    wrapper, since it deliberately compares the two engines against each
    other and must not have both sides silently become native."""

    def run(self, halt_address=None, max_steps=5_000_000, progress=None, progress_interval=250_000):
        if NATIVE_AVAILABLE and progress is None:
            return native_run(self, halt_address, max_steps)
        return super().run(halt_address, max_steps, progress, progress_interval)


def Machine(*args, **kwargs):
    kwargs.setdefault("symphony", TEST_ISA == "symphony")
    return _NativePreferringMachine(*args, **kwargs)


def compile_source(source, filename="<input>", target=None):
    return _compile_source(TEST_PREAMBLE + source, filename, target or Target())


def compile_sources(sources, target=None, **kwargs):
    return _compile_sources(sources, target or Target(), **kwargs)


def compile_and_run(source, expected, *, target=None, load_address=0, inputs=()):
    target = target or Target(load_address=load_address)
    result = compile_source(source, "integration.c", target)
    machine = Machine(
        result.image.binary,
        target.ram_size,
        load_address,
        inputs=inputs,
        symphony=target.isa == "symphony",
    )
    halt = result.image.symbols["_halt"] + (load_address if target.pic else 0)
    actual = machine.run(halt)
    if actual != expected & 0xFFFFFFFF:
        raise AssertionError(f"expected {expected:#x}, got {actual:#x}")
    if machine.regs[14] != 0:
        raise AssertionError("program did not restore the stack pointer")
    return result, machine


MIXED_FEATURE_PROGRAM = r"""
enum { BIAS = 7 };

struct Totals {
    int sum;
    _Bool complete;
};

int values[] = {3, 5, 7};
int *start = values;

int finish(int value) {
    static int calls = 0;
    calls++;
    return value + BIAS + calls;
}

int collect(int count) {
    char odd[count];
    struct Totals totals = {0, 1};
    for (int i = 0; i < count; i++) {
        int value = start[i % 3];
        odd[i] = (char)(value & 1);
        totals.sum += value + odd[i];
    }
    if (!totals.complete) return 0;
    return finish(totals.sum);
}

int main(void) {
    int (*worker)(int) = collect;
    return worker(5);
}
"""


class CompilerIntegrationTests(unittest.TestCase):
    def test_multiple_translation_units_and_internal_linkage(self):
        sources = [
            (
                "main.c",
                "extern int shared; int add(int,int); "
                "static int local(void){return 6;} "
                "int main(void){return add(shared,local());}",
            ),
            (
                "math.c",
                "int shared=5; static int local(void){return 99;} "
                "int add(int a,int b){return a+b+(local()==99?0:1000);}",
            ),
        ]
        result = compile_sources(sources)
        machine = Machine(result.image.binary)
        self.assertEqual(machine.run(result.image.symbols["_halt"]), 11)

    @unittest.skipUnless(native_available(), "native emulator is not built")
    def test_native_emulator_matches_python_reference(self):
        source = """int main(void) {
            unsigned int value = input();
            unsigned int key = keyboard();
            int values[3] = {1, 2, 3};
            values[1] = value;
            output(values[0] + values[1] + values[2]);
            screen(7, key);
            persistent_store(4, value ^ key);
            return persistent_load(4) ^ time_high();
        }"""
        for target_isa in ("dynphony", "symphony"):
            with self.subTest(target=target_isa):
                self.assertTrue(native_available(target_isa == "symphony"))
                target = Target(
                    ram_size=1 << 16,
                    persistent_size=256,
                    pic=True,
                    isa=target_isa,
                )
                result = compile_source(source, "native-integration.c", target)
                address = 0x1203
                halt = address + result.image.symbols["_halt"]
                options = {
                    "inputs": [38],
                    "keyboard_inputs": [9],
                    "time_value": 0x1234567800000000,
                    "persistent_size": 256,
                    "symphony": target_isa == "symphony",
                }
                # Plain _Machine here, not the native-preferring Machine()
                # wrapper: this test's whole point is comparing the Python
                # reference engine against native, so "reference" must stay
                # on the Python engine regardless of what's installed.
                reference = _Machine(
                    result.image.binary, target.ram_size, address, **options
                )
                native = _Machine(
                    result.image.binary, target.ram_size, address, **options
                )
                expected = reference.run(halt)
                actual = native_run(native, halt)
                self.assertEqual(actual, expected)
                for attribute in (
                    "pc",
                    "steps",
                    "regs",
                    "outputs",
                    "screen_updates",
                    "memory",
                    "persistent",
                ):
                    self.assertEqual(
                        getattr(native, attribute),
                        getattr(reference, attribute),
                        attribute,
                    )

    def test_symphony_fixed_width_calls_globals_and_pic(self):
        target = Target(ram_size=1 << 20, isa="symphony")
        result, machine = compile_and_run(
            MIXED_FEATURE_PROGRAM, 36, target=target
        )
        self.assertTrue(all(address % 4 == 0 for address in result.image.symbols.values()))
        self.assertEqual(machine.regs[14], 0)
        compile_and_run(
            MIXED_FEATURE_PROGRAM,
            36,
            target=Target(ram_size=1 << 20, pic=True, isa="symphony"),
            load_address=0x12345,
        )

    def test_insertion_sort_demo(self):
        source = (ROOT / "examples/insertion_sort.c").read_text()
        values = [12, 240, 7, 7, 99, 0, 180, 42, 3, 1, 8, 255, 25, 6, 2, 11]
        _, machine = compile_and_run(source, 0, inputs=values)
        self.assertEqual(machine.outputs, sorted(values))

    def test_dynamic_sensor_report_demo(self):
        source = (ROOT / "examples/dynamic_sensor_report.c").read_text()
        samples = [12, -3, 7, 12, 25, 7, 0, -3, 18]
        result, machine = compile_and_run(
            source,
            6,
            inputs=[len(samples), *samples],
        )
        self.assertEqual(
            machine.outputs,
            [
                9,
                6,
                0,
                0xFFFFFFFD,
                25,
                12,
                9,
                1,
                0,
                1,
                1,
                0,
                1,
                0,
                1,
                1,
                0,
            ],
        )
        self.assertIn("__dyn_heap_anchor", result.image.symbols)

    def test_mixed_language_features_at_fixed_and_pic_addresses(self):
        # values sum to 23; all are odd, so collect gives 28; finish adds 7 + 1.
        compile_and_run(MIXED_FEATURE_PROGRAM, 36)
        compile_and_run(MIXED_FEATURE_PROGRAM, 36, load_address=0x2400)
        compile_and_run(
            MIXED_FEATURE_PROGRAM,
            36,
            target=Target(pic=True),
            load_address=0x2403,
        )

    def test_printf_and_framebuffer_modes(self):
        source = r'''int main(void) {
            char *framebuffer = screen_framebuffer();
            unsigned int value = 0x2a;
            printf("value=%x", value);
            screen_cursor(0, 1);
            printf("%c", 'A');
            return framebuffer[0] + framebuffer[5] + framebuffer[96];
        }'''
        for include_framebuffer in (False, True):
            with self.subTest(include_framebuffer=include_framebuffer):
                result, machine = compile_and_run(
                    source,
                    ord("v") + ord("=") + ord("A"),
                    target=Target(include_framebuffer=include_framebuffer),
                )
                framebuffer = result.image.symbols["__dyn_printf_framebuffer"]
                self.assertEqual(machine.screen_updates, [(0, 0), (1, framebuffer)])
                self.assertEqual(
                    bytes(machine.memory[framebuffer : framebuffer + 8]), b"value=2a"
                )
                self.assertEqual(machine.memory[framebuffer + 96], ord("A"))
                if include_framebuffer:
                    self.assertLess(framebuffer, len(result.image.binary))
                else:
                    self.assertGreaterEqual(framebuffer, len(result.image.binary))

    def test_division_and_multiply_helper_edge_cases(self):
        """Regression coverage for __dyn_udivmod's algorithm swap (fixed
        32-iteration loop -> libgcc's own shift-align-then-subtract
        __udivmodsi4 shape) and __dyn_mul: exercises small divisors (the
        common case the new algorithm is faster for), divisors >= 2**31
        (the specific edge case the old algorithm's docstring called out
        by name), signed division/modulo sign handling, and division by
        zero's defined-zero-result behavior -- all run through the real
        compiler and emulator, not just the algorithm in isolation."""
        cases = [
            ("unsigned int a=100,b=7; return a/b;", 100 // 7),
            ("unsigned int a=100,b=7; return a%7;", 100 % 7),
            ("unsigned int a=0xFFFFFFFFu,b=0x80000001u; return a/b;", 1),
            ("unsigned int a=0xFFFFFFFFu,b=0x80000001u; return a%b;",
             0xFFFFFFFF % 0x80000001),
            ("unsigned int a=5,b=0x90000000u; return a/b;", 0),
            ("unsigned int a=5,b=0x90000000u; return a%b;", 5),
            ("int a=-17,b=5; return a/b;", -3),
            ("int a=-17,b=5; return a%b;", -2),
            ("int a=17,b=-5; return a/b;", -3),
            ("unsigned int a=7,b=0; return a/b;", 0),
            ("unsigned int a=7,b=0; return a%b;", 0),
            ("unsigned int a=0x12345,b=0x6789; return a*b;",
             (0x12345 * 0x6789) & 0xFFFFFFFF),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                compile_and_run(
                    f"int main(void) {{ {body} }}", expected & 0xFFFFFFFF
                )
