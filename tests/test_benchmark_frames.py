import unittest

from symphony import Target, compile_source
from symphony.emulator import Machine, native_available, native_run
from tools.benchmark_examples import FrameSample, run_frame_sample


FRAME_PROGRAM = r"""
#include <symphony.h>

int main(void) {
    unsigned int frame;

    /* Initial framebuffer selection is configuration, not a completed frame. */
    screen(1u, 100u);
    screen(2u, 64u);
    screen(0u, 3u);

    frame = 0u;
    while (frame < 70u) {
        screen(2u, frame); /* Unrelated screen setting: never a boundary. */
        screen(1u, 100u + (frame & 1u) * 400u);
        frame += 1u;
    }
    return 9;
}
"""


class FrameSampleTests(unittest.TestCase):
    def test_third_frame_starts_exact_sixty_frame_interval(self):
        sample = FrameSample(warmup=3, count=60)
        self.assertFalse(sample(1, 100, 5))
        for frame in range(1, 63):
            self.assertFalse(sample(2, frame, frame * 10))
            self.assertFalse(sample(1, 100 + frame % 2, frame * 10))
        self.assertTrue(sample(1, 101, 630))
        self.assertEqual(sample.frames, 63)
        self.assertEqual(sample.start_step, 30)
        self.assertEqual(sample.instructions, 600)

    def test_zero_warmup_starts_at_framebuffer_baseline(self):
        sample = FrameSample(warmup=0, count=2)
        self.assertFalse(sample(1, 100, 5))
        self.assertFalse(sample(1, 101, 11))
        self.assertTrue(sample(1, 100, 19))
        self.assertEqual(sample.start_step, 5)
        self.assertEqual(sample.instructions, 14)

    def test_invalid_frame_sample_counts_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "warmup"):
            FrameSample(warmup=-1)
        with self.assertRaisesRegex(ValueError, "sample count"):
            FrameSample(count=0)

    def test_python_emulator_stops_on_exact_frame_boundary(self):
        result = compile_source(FRAME_PROGRAM, target=Target(ram_size=1 << 16))
        machine = Machine(result.image.binary, ram_size=1 << 16, symphony=True)
        sample = FrameSample()
        machine.screen_update_callback = sample
        machine.run(result.image.symbols["_halt"], max_steps=100_000)
        post_mode = machine.screen_updates[3:]
        self.assertEqual(sum(setting == 1 for setting, _ in post_mode), 63)
        self.assertEqual(sum(setting == 2 for setting, _ in post_mode), 63)
        self.assertGreater(sample.instructions, 0)

    @unittest.skipUnless(native_available(True), "native emulator is not built")
    def test_native_emulator_callback_matches_python(self):
        result = compile_source(FRAME_PROGRAM, target=Target(ram_size=1 << 16))
        halt = result.image.symbols["_halt"]
        reference = Machine(result.image.binary, ram_size=1 << 16, symphony=True)
        expected = run_frame_sample(reference, halt, 100_000)

        native = Machine(result.image.binary, ram_size=1 << 16, symphony=True)
        sample = FrameSample()
        native.screen_update_callback = sample
        native_run(native, halt, 100_000)
        self.assertEqual(sample.instructions, expected)
        self.assertEqual(native.steps, reference.steps)
        self.assertEqual(native.screen_updates, reference.screen_updates)

    @unittest.skipUnless(native_available(True), "native emulator is not built")
    def test_native_callback_cannot_resize_live_memory(self):
        result = compile_source(FRAME_PROGRAM, target=Target(ram_size=1 << 16))
        for attribute in ("memory", "persistent"):
            with self.subTest(attribute=attribute):
                machine = Machine(
                    result.image.binary,
                    ram_size=1 << 16,
                    persistent_size=16,
                    symphony=True,
                )

                def resize_memory(setting, value, step):
                    del setting, value, step
                    getattr(machine, attribute).append(0)
                    return False

                machine.screen_update_callback = resize_memory
                with self.assertRaises(BufferError):
                    native_run(machine, result.image.symbols["_halt"], 100_000)

    @unittest.skipUnless(native_available(True), "native emulator is not built")
    def test_insufficient_budget_reports_observed_frames(self):
        result = compile_source(FRAME_PROGRAM, target=Target(ram_size=1 << 16))
        machine = Machine(result.image.binary, ram_size=1 << 16, symphony=True)
        with self.assertRaisesRegex(
            RuntimeError, r"frame sample incomplete: observed \d+ of 63 presentations"
        ):
            run_frame_sample(machine, result.image.symbols["_halt"], 20)

    def test_live_time_is_unix_epoch_nanoseconds(self):
        result = compile_source(
            "#include <symphony.h>\nint main(void) { return time_high() != 0u; }",
            target=Target(ram_size=4096),
        )
        fixed = Machine(
            result.image.binary, ram_size=4096, symphony=True, time_value=0
        )
        live = Machine(result.image.binary, ram_size=4096, symphony=True)
        self.assertEqual(fixed.run(result.image.symbols["_halt"]), 0)
        self.assertEqual(live.run(result.image.symbols["_halt"]), 1)

    def test_frequency_clock_accumulates_fractional_nanoseconds_exactly(self):
        machine = Machine(
            bytes((0x05, 0x10)),
            ram_size=4096,
            time_value=123,
            time_frequency_hz=15_000_000,
        )
        machine.steps = 15_000_001
        machine.step()
        self.assertEqual(machine.regs[1], 1_000_000_189)

    @unittest.skipUnless(native_available(True), "native emulator is not built")
    def test_frequency_clock_matches_in_native_emulator(self):
        options = {
            "ram_size": 4096,
            "time_value": 123,
            "time_frequency_hz": 15_000_000,
        }
        reference = Machine(bytes((0x05, 0x10)), **options)
        native = Machine(bytes((0x05, 0x10)), **options)
        reference.steps = native.steps = 15_000_001
        reference.step()
        with self.assertRaisesRegex(RuntimeError, "execution limit exceeded"):
            native_run(native, None, native.steps + 1)
        self.assertEqual(native.regs[1], reference.regs[1])

    @unittest.skipUnless(native_available(True), "native emulator is not built")
    def test_instruction_clock_matches_in_both_emulators(self):
        result = compile_source(
            "#include <symphony.h>\n"
            "int main(void) { output(7u); return time_low(); }",
            target=Target(ram_size=4096),
        )
        options = {
            "ram_size": 4096,
            "symphony": True,
            "time_value": 1_000_000,
            "time_per_step_ns": 7,
        }
        reference = Machine(result.image.binary, **options)
        native = Machine(result.image.binary, **options)
        halt = result.image.symbols["_halt"]
        expected = reference.run(halt)
        actual = native_run(native, halt)
        self.assertGreater(expected, 1_000_000)
        self.assertEqual(actual, expected)
        self.assertEqual(native.steps, reference.steps)


if __name__ == "__main__":
    unittest.main()
