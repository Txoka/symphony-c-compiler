import os
import unittest
from pathlib import Path

from symphony import Target
from symphony.emulator import Machine as _Machine, native_available
from symphony.project import (
    Project,
    ProjectFile,
    decode_control,
    make_persistent_image as _make_persistent_image,
    project_from_directory,
)

from selfhost.tools.bootstrap import build_stage0, run_machine, run_stage0


TEST_ISA = os.environ.get("SYMPHONY_TEST_ISA", "symphony")


def Machine(*args, **kwargs):
    kwargs.setdefault("symphony", TEST_ISA == "symphony")
    return _Machine(*args, **kwargs)


def make_persistent_image(*args, **kwargs):
    kwargs.setdefault("symphony", TEST_ISA == "symphony")
    return _make_persistent_image(*args, **kwargs)


class BootstrapCompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.compiler = build_stage0(Target(isa=TEST_ISA))

    def test_compiles_and_runs_example(self):
        source = Path("selfhost/examples/answer.c").read_text()
        status, compiler, binary, control = run_stage0(
            self.compiler, source.encode("ascii")
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(binary), 16)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 52)

    @unittest.skipUnless(native_available(), "requires native emulator")
    def test_compile_then_run_mode_transfers_to_generated_program(self):
        persistent_size = 1 << 24
        load_address = 0x80000
        project = Project((ProjectFile(
            "main.c", b"int main(void){output(77);return 42;}"
        ),))
        persistent = make_persistent_image(
            project, persistent_size=persistent_size,
            program_load_address=load_address, run_after_compile=True,
        )
        machine = Machine(
            self.compiler.image.binary, persistent_size=persistent_size
        )
        machine.persistent[:] = persistent
        result = run_machine(
            machine, self.compiler.image.symbols["_halt"], 60_000_000
        )
        control = decode_control(machine.persistent)
        self.assertEqual(control.status, 0)
        self.assertEqual(machine.outputs, [77])
        self.assertEqual(result, 42)
        self.assertGreaterEqual(machine.pc, load_address)

    def test_precedence_literals_unary_and_comments(self):
        source = "int main(void){/* fold */ return ~0 & (0x20 + 010 * 2); }"
        status, compiler, binary, control = run_stage0(
            self.compiler, source.encode("ascii")
        )
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 48)

    def test_reports_parse_and_semantic_errors(self):
        for source, expected in (
            ("int main(void){return nope;}", 4),
            ("int main(void){return 1/0;}", 5),
            ("enum Bad { SAME, SAME }; int main(void){return 0;}", 4),
            ("typedef int same; typedef char same; int main(void){return 0;}", 4),
        ):
            with self.subTest(source=source):
                status, compiler, binary, control = run_stage0(
                    self.compiler, source.encode("ascii")
                )
                self.assertEqual(status, expected)
                self.assertEqual(binary, b"")
                self.assertEqual(control.output_byte_length, 8)

    def test_character_comparison_logical_conditional_and_comma(self):
        source = r"""int main(void) {
            return (0 && (1 / 0)), (1 || (1 / 0))
                ? ('A' == 65 && 9 >= 8 && !(3 != 3) ? 77u : 2)
                : 1;
        }"""
        status, compiler, binary, control = run_stage0(
            self.compiler, source.encode("ascii")
        )
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 77)

    def test_generated_image_honors_arbitrary_load_address(self):
        status, compiler, binary, control = run_stage0(
            self.compiler,
            b"int main(void){return 23;}",
            load_address=0x12340,
        )
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 23)

    def test_runtime_locals_assignment_control_flow_and_io(self):
        source = b"""int main(void) {
            int x = input();
            unsigned int total = 0;
            while (x > 0) {
                total = total + x;
                x = x - 1;
            }
            if (total > 10) output(total); else output(0);
            return total;
        }"""
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(
            binary, load_address=control.program_load_address, inputs=[5]
        )
        self.assertEqual(program.run(), 15)
        self.assertEqual(program.outputs, [15])

    def test_for_do_break_continue_and_increment(self):
        source = b"""int main(void) {
            int i = 0;
            int total = 0;
            for (i = 0; i < 6; i++) {
                if (i == 2) continue;
                if (i == 5) break;
                total += i;
            }
            do { total += 1; } while (total < 9);
            output(total);
            return total;
        }"""
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 9)
        self.assertEqual(program.outputs, [9])

    def test_runtime_software_multiply_divide_and_remainder(self):
        source = b"""int main(void) {
            unsigned int value = input();
            unsigned int product = value * 7;
            unsigned int quotient = product / 3;
            unsigned int remainder = product % 3;
            unsigned int result = quotient + remainder;
            output(result);
            return result;
        }"""
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(
            binary, load_address=control.program_load_address, inputs=[13]
        )
        self.assertEqual(program.run(max_steps=100_000), 31)
        self.assertEqual(program.outputs, [31])

    def test_scalar_declarators_casts_and_sizeof(self):
        source = b"""int main(void) {
            const char value = (char)input();
            unsigned long *pointer = 0;
            return sizeof(char) + sizeof(value) + sizeof(pointer)
                + (unsigned int)value;
        }"""
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(
            binary, load_address=control.program_load_address, inputs=[4]
        )
        self.assertEqual(program.run(), 10)

    def test_runtime_device_intrinsics(self):
        source = b"""unsigned int read_value(void) { return 12; }
        int main(void) {
            unsigned int key = keyboard();
            persistent_store(4, read_value());
            screen(2, key);
            return persistent_load(4) ^ time_high();
        }"""
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(
            binary,
            load_address=control.program_load_address,
            keyboard_inputs=[7],
            time_value=0x1122334400000005,
            persistent_size=256,
        )
        self.assertEqual(program.run(), 12 ^ 0x11223344)
        self.assertEqual(program.persistent_read(4), 12)
        self.assertEqual(program.screen_updates, [(2, 7)])

    def test_stack_backed_locals_and_width_correct_storage(self):
        source = b"""int main(void) {
            char a = 258;
            short b = 2;
            int c = 3;
            int d = 4;
            int e = 5;
            int f = 6;
            int g = 7;
            int h = 8;
            return a + b + c + d + e + f + g + h;
        }"""
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 37)

    def test_function_scope_static_scalar_lifetime(self):
        source = b'''int next(void) {
            static int value = 10;
            static int *pointer = &value;
            static int *empty = 0;
            if (empty) return 0;
            *pointer += 1;
            return *pointer;
        }
        int other(void) {
            static int value = 20;
            static char *label = "A";
            return ++value + label[0] - 'A';
        }
        int main(void) {
            int first = next();
            int second = next();
            return first * 100 + second + other() - 21;
        }'''
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 1112)

        status, _, binary, _ = run_stage0(
            self.compiler,
            b"int main(void){int x=1; static int bad=x; return bad;}",
        )
        self.assertEqual(status, 4)
        self.assertEqual(binary, b"")

    def test_function_scope_static_array_storage(self):
        source = b'''int accumulate(void) {
            static int values[3] = {10, 20};
            values[2] += 1;
            return values[0] + values[1] + values[2];
        }
        int main(void) {
            return accumulate() + accumulate() - 21;
        }'''
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 42)

    def test_compiles_symphony_fixed_width_image(self):
        status, _, binary, control = run_stage0(
            self.compiler,
            b"int twice(int x){return x+x;} int main(void){return twice(21);}",
            symphony=True,
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(binary) % 4, 0)
        program = Machine(
            binary,
            load_address=control.program_load_address,
            symphony=True,
        )
        self.assertEqual(program.run(), 42)

    def test_bool_conversion_storage_returns_and_stack_parameters(self):
        source = b'''typedef _Bool bool;
        bool global_flag = 9;
        _Bool nonzero(unsigned int value) { return value; }
        _Bool seventh(
            int a, int b, int c, int d, int e, int f, _Bool value
        ) { return value; }
        unsigned char seventh_char(
            int a, int b, int c, int d, int e, int f, unsigned char value
        ) { return value; }
        struct Item { _Bool flag; };
        int main(void) {
            bool flags[3];
            struct Item item;
            _Bool *pointer = &flags[0];
            flags[0] = 0;
            flags[1] = 9;
            flags[2] = 0;
            *pointer = 8;
            flags[2] = nonzero(0);
            item.flag = (_Bool)42;
            return sizeof(_Bool) * 100 + global_flag * 10
                + flags[0] * 5 + flags[1] * 3 + flags[2]
                + item.flag + seventh(0, 0, 0, 0, 0, 0, 9)
                + seventh_char(0, 0, 0, 0, 0, 0, 0x1234);
        }'''
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 172)

        status, _, binary, _ = run_stage0(
            self.compiler,
            b"int main(void){int n=3; static int bad[n]; return 0;}",
        )
        self.assertEqual(status, 4)
        self.assertEqual(binary, b"")

    def test_functions_parameters_nested_calls_and_recursion(self):
        source = b"""unsigned int factorial(unsigned int value) {
            if (value <= 1) return 1;
            return value * factorial(value - 1);
        }
        unsigned int combine(unsigned int a, unsigned int b) {
            return factorial(a) + factorial(b);
        }
        int main(void) {
            return combine(input(), 3);
        }"""
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(
            binary, load_address=control.program_load_address, inputs=[5]
        )
        self.assertEqual(program.run(max_steps=1_000_000), 126)

    def test_postfix_increment_preserves_old_value(self):
        source = b"""int main(void) {
            int value = 3;
            int old = value++;
            return old * 10 + value;
        }"""
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 34)

    def test_void_function_bare_return(self):
        source = b'''int value;
        int pick(int input, int output) { return input + output; }
        void *identity(void *pointer) { return pointer; }
        void no_operation(void) {}
        void set_value(int next) {
            value = next;
            return;
        }
        int main(void) {
            no_operation();
            int next = pick(40, 2);
            int *pointer = identity(&next);
            set_value(*pointer);
            return value;
        }'''
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 42)

    def test_nested_division_spills_to_low_scratch_registers(self):
        source = b"int main(void) { return 1 + (82 / 2); }"
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 42)

    def test_deep_subscript_scaling_spills_to_low_scratch_registers(self):
        source = b'''struct Triple { int a; int b; int c; };
        int main(void) {
            struct Triple values[2];
            values[1].c = 39;
            return 1 + (2 + values[1].c);
        }'''
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 42)

    def test_forward_prototype_and_seven_argument_abi(self):
        source = b"""int sum(int a, int b, int c, int d, int e, int f, int g);
        int main(void) { return sum(1, 2, 3, 4, 5, 6, 7); }
        int sum(int a, int b, int c, int d, int e, int f, int g) {
            return a + b + c + d + e + f + g;
        }"""
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 28)

    def test_stack_passed_arguments(self):
        source = b'''int sum8(int a, int b, int c, int d,
            int e, int f, int g, int h) {
            return a + b + c + d + e + f + g + h;
        }
        int main(void) {
            return sum8(1, 2, 3, 4, 5, 6, 7, 8)
                + sum8(1, 1, 1, 1, 1, 1, 1, 1);
        }'''
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 44)

    def test_project_links_multiple_translation_units(self):
        project = Project((
            ProjectFile("helper.c", b'''int add(int left, int right) {
                return left + right;
            }'''),
            ProjectFile("main.c", b'''int add(int left, int right);
            int main(void) { return add(19, 23); }'''),
        ))
        persistent = make_persistent_image(
            project, persistent_size=1 << 16, program_load_address=8192
        )
        machine = Machine(self.compiler.image.binary, persistent_size=1 << 16)
        machine.persistent[:] = persistent
        status = run_machine(
            machine, self.compiler.image.symbols["_halt"], 50_000_000
        )
        control = decode_control(machine.persistent)
        self.assertEqual(status, 0)
        self.assertEqual(control.status, 0)
        record = machine.persistent[
            control.output_address:
            control.output_address + control.output_byte_length
        ]
        image_length = int.from_bytes(record[:4], "big")
        binary = bytes(record[4:4 + image_length])
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 42)

    def test_project_preprocesses_relative_and_rooted_includes(self):
        project = Project(
            (
                ProjectFile(
                    "include/constants.h",
                    b'''#ifndef CONSTANTS_H
                    #define CONSTANTS_H
                    #define BASE_VALUE 40
                    #endif''',
                    2,
                ),
                ProjectFile(
                    "src/local.h", b"int local_value(void);", 2
                ),
                ProjectFile(
                    "src/main.c",
                    b'''#include <constants.h>
                    #include <constants.h>
                    #include "local.h"
                    int local_value(void) { return PROJECT_OFFSET; }
                    int main(void) {
                        char *text = "BASE_VALUE";
                        return BASE_VALUE + local_value() + (text[0] == 'B');
                    }''',
                ),
            ),
            ("include",),
            ("PROJECT_OFFSET=1",),
        )
        persistent = make_persistent_image(
            project, persistent_size=1 << 16, program_load_address=8192
        )
        machine = Machine(self.compiler.image.binary, persistent_size=1 << 16)
        machine.persistent[:] = persistent
        status = run_machine(
            machine, self.compiler.image.symbols["_halt"], 50_000_000
        )
        control = decode_control(machine.persistent)
        self.assertEqual(status, 0)
        self.assertEqual(control.status, 0)
        record = machine.persistent[
            control.output_address:
            control.output_address + control.output_byte_length
        ]
        image_length = int.from_bytes(record[:4], "big")
        binary = bytes(record[4:4 + image_length])
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 42)

    def test_preprocessor_once_undef_redefinition_and_error(self):
        project = Project((
            ProjectFile(
                "value.h",
                b'''#pragma once
                #define VALUE 1
                #undef VALUE
                #define VALUE 42
                int selected(void) { return VALUE; }''',
                2,
            ),
            ProjectFile(
                "main.c",
                b'''#define ADD(left, right) ((left) + (right))
                #define ZERO() 0
                #define BASE FORTY
                #define FORTY 40
                #define OFFSET \
                    2
                #define WITH_OFFSET(value) ((value) + \
                    OFFSET)
                #define LEVEL 3
                #if defined(LEVEL) && ((LEVEL * 10 + 12) == 42)
                #define CONDITIONAL 42
                #elif LEVEL == 3
                #error wrong conditional branch
                #else
                #error wrong fallback branch
                #endif
                #include "value.h"
                #include "value.h"
                int main(void) {
                    return WITH_OFFSET(BASE) + ZERO()
                        + selected() + CONDITIONAL - 84;
                }''',
            ),
        ))
        persistent = make_persistent_image(
            project, persistent_size=1 << 16, program_load_address=8192
        )
        machine = Machine(self.compiler.image.binary, persistent_size=1 << 16)
        machine.persistent[:] = persistent
        self.assertEqual(run_machine(
            machine, self.compiler.image.symbols["_halt"], 50_000_000
        ), 0)
        control = decode_control(machine.persistent)
        self.assertEqual(control.status, 0)
        record = machine.persistent[control.output_address:]
        image_length = int.from_bytes(record[:4], "big")
        program = Machine(
            bytes(record[4:4 + image_length]),
            load_address=control.program_load_address,
        )
        self.assertEqual(program.run(), 42)

        status, _, binary, _ = run_stage0(
            self.compiler, b"#error deliberate failure\nint main(void){return 0;}"
        )
        self.assertEqual(status, 4)
        self.assertEqual(binary, b"")

        status, _, binary, _ = run_stage0(
            self.compiler,
            b"#if (1 + )\n#endif\nint main(void){return 0;}",
        )
        self.assertEqual(status, 4)
        self.assertEqual(binary, b"")

        status, _, binary, _ = run_stage0(
            self.compiler,
            b"#define ONE(value) value\nint main(void){return ONE(1, 2);}",
        )
        self.assertEqual(status, 4)
        self.assertEqual(binary, b"")

    def test_preprocessor_rejects_recursive_include(self):
        project = Project((
            ProjectFile("loop.h", b'#include "loop.h"', 2),
            ProjectFile(
                "main.c",
                b'#include "loop.h"\nint main(void){return 0;}',
            ),
        ))
        persistent = make_persistent_image(
            project, persistent_size=1 << 16, program_load_address=8192
        )
        machine = Machine(self.compiler.image.binary, persistent_size=1 << 16)
        machine.persistent[:] = persistent
        self.assertEqual(run_machine(
            machine, self.compiler.image.symbols["_halt"], 50_000_000
        ), 4)
        self.assertEqual(decode_control(machine.persistent).status, 4)

    def test_fixed_arrays_address_dereference_and_subscript(self):
        source = b"""int main(void) {
            int values[5];
            int index = 0;
            while (index < 5) {
                values[index] = index * index;
                index++;
            }
            int *pointer = &values[3];
            *pointer += 4;
            return values[3] + *pointer + values[4];
        }"""
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(max_steps=500_000), 42)

    def test_nested_scope_shadowing(self):
        source = b"""int main(void) {
            int value = 2;
            { int value = 5; output(value); }
            output(value);
            return value;
        }"""
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 2)
        self.assertEqual(program.outputs, [5, 2])

    def test_global_scalars_arrays_and_static_data(self):
        source = b"""static int seed = 7;
        unsigned int values[4];
        int update(int index) {
            values[index] = seed + index;
            seed += 1;
            return values[index];
        }
        int main(void) {
            return update(2) + update(1) + values[2] + seed;
        }"""
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 36)

    def test_string_literal_pooling_escapes_and_concatenation(self):
        source = b'''int main(void) {
            char *text = "ab" "c\\n";
            return text[0] + text[1] + text[2] + text[3];
        }'''
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 304)

    def test_static_pointer_relocations(self):
        source = b'''int value = 40;
        int *pointer = &value;
        char *text = "az";
        int main(void) { return *pointer + text[1]; }'''
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 162)

    def test_static_array_initializer_data(self):
        source = b'''static int values[4] = {10, 20, 30};
        static char bytes[3] = {1, 2, 255};
        int main(void) {
            return values[0] + values[2] + values[3]
                + bytes[0] + bytes[1] + bytes[2];
        }'''
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 298)

    def test_scaled_pointer_arithmetic(self):
        source = b'''int main(void) {
            int values[4];
            int *pointer = values;
            *(pointer + 2) = 40;
            pointer++;
            *(pointer + 2) = 2;
            return values[2] + values[3] + (*(2 + values) - 40);
        }'''
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 42)

    def test_pointer_cast_changes_arithmetic_stride(self):
        source = b'''int main(void) {
            unsigned int words[2];
            unsigned char *bytes = (unsigned char *)words;
            return (unsigned int)(bytes + 3) - (unsigned int)words;
        }'''
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 3)

    def test_selfhost_runtime_memory_and_heap(self):
        runtime = "\n".join(
            line for line in Path("selfhost/src/runtime.c").read_text().splitlines()
            if not line.startswith("#include")
        )
        source = (runtime + r'''
        unsigned char __dyn_heap_anchor[7];
        int main(void) {
            unsigned char *a = malloc(16u);
            unsigned char *b = malloc(16u);
            unsigned char *c = malloc(16u);
            unsigned char *joined;
            unsigned char *zeroed;
            unsigned char *grown;
            unsigned int index = 0u;
            if (!a || !b || !c) return 1;
            while (index < 16u) { a[index] = (unsigned char)index; index += 1u; }
            memcpy(b, a, 16u);
            if (memcmp(a, b, 16u)) return 2;
            memmove(b + 2u, b, 10u);
            if (b[2] != 0u || b[11] != 9u) return 3;
            memset(c, 0x5a, 16u);
            if (c[0] != 0x5a || c[15] != 0x5a) return 4;
            free(b);
            free(a);
            joined = malloc(40u);
            if (joined != a) return 5;
            free(joined);
            free(c);
            zeroed = calloc(8u, 1u);
            if (!zeroed) return 6;
            index = 0u;
            while (index < 8u) {
                if (zeroed[index]) return 7;
                zeroed[index] = (unsigned char)(index + 1u);
                index += 1u;
            }
            grown = realloc(zeroed, 24u);
            if (!grown) return 8;
            index = 0u;
            while (index < 8u) {
                if (grown[index] != (unsigned char)(index + 1u)) return 9;
                index += 1u;
            }
            return 42;
        }
        ''').encode("ascii")
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 42)

    def test_sibling_blocks_may_reuse_local_names(self):
        source = b'''int main(void) {
            int total = 0;
            if (1) {
                int value = 19;
                total += value;
            }
            {
                int value = 23;
                total += value;
            }
            return total;
        }'''
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 42)

    def test_enum_definitions_and_constant_expressions(self):
        source = b'''enum Token {
            TOKEN_ZERO,
            TOKEN_START = 7,
            TOKEN_NEXT,
            TOKEN_MASK = (TOKEN_NEXT << 2) | 1,
        };
        int main(void) {
            int value = TOKEN_MASK;
            return TOKEN_ZERO + TOKEN_START + TOKEN_NEXT + value;
        }'''
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 48)

    def test_enum_typed_globals_parameters_and_locals(self):
        source = b'''enum Mode { MODE_A = 3, MODE_B = MODE_A + 6 };
        enum Mode selected = MODE_B;
        enum Mode choose(enum Mode mode) {
            enum Mode local = mode;
            return local;
        }
        int main(void) {
            enum Mode outer = MODE_A;
            {
                enum Mode { MODE_A = 38 };
                enum Mode inner = MODE_A;
                outer += inner;
            }
            return outer + sizeof(enum Mode) + MODE_A - 6;
        }'''
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 42)

        status, _, binary, _ = run_stage0(
            self.compiler,
            b"int main(void){enum Missing value; return 0;}",
        )
        self.assertEqual(status, 4)
        self.assertEqual(binary, b"")

    def test_scalar_typedefs_in_globals_parameters_and_locals(self):
        source = b'''typedef unsigned int word;
        typedef char byte;
        word base = 30;
        word add(byte left, word right) {
            word result = left + right;
            return result;
        }
        int main(void) { return add(12, base); }'''
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 42)

    def test_block_scope_typedef_shadowing_and_lifetime(self):
        source = b'''typedef int word;
        int main(void) {
            word outer = 40;
            {
                typedef unsigned char word;
                const word inner = 255;
                if (sizeof(word) != 1 || inner != 255) return 0;
            }
            {
                typedef short word;
                word delta = 2;
                outer += delta;
            }
            return outer + sizeof(word) - 4;
        }'''
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 42)

        status, _, binary, _ = run_stage0(
            self.compiler,
            b'''int main(void) {
                typedef int duplicate;
                typedef char duplicate;
                return 0;
            }''',
        )
        self.assertEqual(status, 4)
        self.assertEqual(binary, b"")

    def test_struct_layout_members_pointers_and_nesting(self):
        source = b'''struct Pair;
        typedef struct Pair *PairPointer;
        struct Pair {
            char tag;
            int value;
            short delta;
            PairPointer next;
        };
        struct Box {
            struct Pair pair;
            struct Pair *link;
            int values[3];
        };
        typedef struct Pair Pair;
        struct Pair global_pair;
        int main(void) {
            Pair local;
            Pair items[2];
            struct Box box;
            PairPointer pointer = &local;
            pointer->tag = 2;
            pointer->value = 30;
            local.delta = 4;
            box.pair.value = 5;
            box.link = pointer;
            box.values[1] = 1;
            global_pair.value = 7;
            local.next = &global_pair;
            pointer = items;
            pointer++;
            pointer->value = 9;
            return box.link->tag + box.link->value + local.delta
                + box.pair.value + box.values[1] + local.next->value
                + sizeof(struct Pair) + items[1].value;
        }'''
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 74)

    def test_multidimensional_fixed_arrays_and_strides(self):
        source = b'''int global_values[2][3];
        struct Bytes { char values[2][3]; };
        int main(void) {
            int local_values[2][3];
            struct Bytes bytes;
            global_values[1][2] = 10;
            local_values[1][2] = 20;
            bytes.values[1][2] = 12;
            return global_values[1][2] + local_values[1][2]
                + bytes.values[1][2];
        }'''
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 42)

    def test_runtime_multidimensional_vla(self):
        source = b'''int main(void) {
            unsigned int rows = input();
            unsigned int columns = input();
            int values[rows][columns];
            values[1][2] = 37;
            values[0][1] = 5;
            return values[1][2] + values[0][1];
        }'''
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(
            binary, load_address=control.program_load_address, inputs=[2, 3]
        )
        self.assertEqual(program.run(), 42)

    def test_vla_sizeof_and_array_parameter_stride(self):
        source = b'''unsigned int row_size(
            unsigned int rows, unsigned int columns,
            int values[rows][columns]
        ) {
            return sizeof(*values);
        }
        int main(void) {
            unsigned int rows = 2;
            unsigned int columns = 3;
            int values[rows][columns];
            values[1][2] = 6;
            return sizeof(values) + row_size(rows, columns, values)
                + values[1][2];
        }'''
        status, compiler, binary, control = run_stage0(self.compiler, source)
        self.assertEqual(status, 0)
        program = Machine(binary, load_address=control.program_load_address)
        self.assertEqual(program.run(), 42)

    @unittest.skipUnless(native_available(), "requires native emulator")
    def test_compiler_reproduces_itself_byte_for_byte(self):
        persistent_size = 1 << 24
        load_address = 8192
        project = project_from_directory(
            "selfhost", exclude=("build", "examples", "tests")
        )
        persistent = make_persistent_image(
            project, persistent_size=persistent_size,
            program_load_address=load_address,
        )
        stage1 = Machine(
            self.compiler.image.binary, persistent_size=persistent_size
        )
        stage1.persistent[:] = persistent
        # The two ISA encodings execute the same compiler workload. Keep one
        # budget for both so this test measures bootstrap correctness rather
        # than an arbitrary target-specific cutoff.
        stage_limit = 2_000_000_000
        self.assertEqual(run_machine(
            stage1, self.compiler.image.symbols["_halt"], stage_limit
        ), 0)
        control1 = decode_control(stage1.persistent)
        self.assertEqual(control1.status, 0)
        record1 = stage1.persistent[control1.output_address:]
        length1 = int.from_bytes(record1[:4], "big")
        stage2_binary = bytes(record1[4:4 + length1])

        stage2 = Machine(
            stage2_binary, load_address=load_address,
            persistent_size=persistent_size,
        )
        stage2.persistent[:] = persistent
        self.assertEqual(
            run_machine(stage2, None, stage_limit), 0
        )
        control2 = decode_control(stage2.persistent)
        self.assertEqual(control2.status, 0)
        record2 = stage2.persistent[control2.output_address:]
        length2 = int.from_bytes(record2[:4], "big")
        stage3_binary = bytes(record2[4:4 + length2])
        self.assertEqual(stage3_binary, stage2_binary)


if __name__ == "__main__":
    unittest.main()
