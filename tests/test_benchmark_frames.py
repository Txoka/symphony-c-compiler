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
        self.assertFalse(sample(0, 3, 10))
        for frame in range(1, 63):
            self.assertFalse(sample(2, frame, frame * 10))
            self.assertFalse(sample(1, 100 + frame % 2, frame * 10))
        self.assertTrue(sample(1, 101, 630))
        self.assertEqual(sample.frames, 63)
        self.assertEqual(sample.start_step, 30)
        self.assertEqual(sample.instructions, 600)

    def test_python_emulator_stops_on_exact_frame_boundary(self):
        result = compile_source(FRAME_PROGRAM, target=Target(ram_size=1 << 16))
        machine = Machine(result.image.binary, ram_size=1 << 16, symphony=True)
        sample = FrameSample()
        machine.screen_callback = sample
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
        native.screen_callback = sample
        native_run(native, halt, 100_000)
        self.assertEqual(sample.instructions, expected)
        self.assertEqual(native.steps, reference.steps)
        self.assertEqual(native.screen_updates, reference.screen_updates)

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
