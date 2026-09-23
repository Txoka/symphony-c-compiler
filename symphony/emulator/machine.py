"""Small independent byte decoder for the instruction subset emitted by scc.

Comparisons and branches follow the hardware model literally: ``cmp`` is an
ordinary ALU operation whose result is the three-bit flag word (see ``flags``),
written to whatever register the encoding names, and a jump reads that word back
out of a register. Neither is special-cased, so an unconventional encoding
behaves exactly as the bits say it should.
"""

from collections import deque
from time import time_ns

from ..targets.symphony.config import Target

MASK = 0xFFFFFFFF


def signed(value):
    return value - 2**32 if value & 0x80000000 else value


def flags(a, b):
    """Pack the three-bit comparison word: equals, lower (unsigned), less (signed)."""
    return (a == b) | ((a < b) << 1) | ((signed(a) < signed(b)) << 2)


class Registers(list):
    """The register file. ``zr`` reads as zero because writes to it are dropped."""

    __slots__ = ()

    def __setitem__(self, index, value):
        if index:
            list.__setitem__(self, index, value)


class Machine:
    def __init__(
        self,
        binary,
        ram_size=16 * 1024 * 1024,
        load_address=0,
        *,
        inputs=(),
        keyboard_inputs=(),
        time_value=None,
        time_per_step_ns=0,
        time_frequency_hz=0,
        live_time=None,
        persistent_size=0,
        symphony=False,
    ):
        Target(
            ram_size=ram_size,
            load_address=load_address,
            persistent_size=persistent_size,
        ).validate()
        if len(binary) > ram_size or load_address % ram_size + len(binary) > ram_size:
            raise ValueError("image does not fit contiguously in RAM")
        self.memory = bytearray(ram_size)
        self.mask = ram_size - 1
        self.regs = Registers([0] * 16)
        self.pc = load_address
        self.steps = 0
        self.inputs = deque(value & MASK for value in inputs)
        self.keyboard_inputs = deque(value & MASK for value in keyboard_inputs)
        self.outputs = []
        self.screen_updates = []
        self.screen_update_callback = None
        self._screen_stop_requested = False
        self.live_time = time_value is None if live_time is None else live_time
        self.time_value = (0 if time_value is None else time_value) & 0xFFFFFFFFFFFFFFFF
        self.time_per_step_ns = time_per_step_ns & 0xFFFFFFFFFFFFFFFF
        self.time_frequency_hz = time_frequency_hz & 0xFFFFFFFFFFFFFFFF
        self.persistent = bytearray(persistent_size)
        self.persistent_mask = persistent_size - 1 if persistent_size else None
        self.symphony = symphony
        if symphony:
            self.step = self._step_symphony
        for i, b in enumerate(binary):
            self.memory[(load_address + i) & self.mask] = b

    def read(self, address, size):
        return int.from_bytes(
            bytes(self.memory[(address + i) & self.mask] for i in range(size)), "big"
        )

    def write(self, address, value, size):
        for i in range(size):
            self.memory[(address + i) & self.mask] = (
                value >> (8 * (size - i - 1))
            ) & 255

    def persistent_read(self, address):
        if self.persistent_mask is None:
            raise RuntimeError("persistent memory is not configured")
        return int.from_bytes(
            bytes(self.persistent[(address + i) & self.persistent_mask] for i in range(4)),
            "big",
        )

    def persistent_write(self, address, value):
        if self.persistent_mask is None:
            raise RuntimeError("persistent memory is not configured")
        for i in range(4):
            self.persistent[(address + i) & self.persistent_mask] = (
                value >> (8 * (3 - i))
            ) & 255

    def step(self):
        pc = self.pc
        op = self.read(pc, 1)
        r = self.regs
        next_pc = pc + 1
        if op == 0:
            pass
        elif op == 1:
            destination = self.read(pc + 1, 1) >> 4
            r[destination] = self.inputs.popleft() if self.inputs else 0
            next_pc = pc + 2
        elif op == 2:
            self.outputs.append(r[self.read(pc + 2, 1) & 15])
            next_pc = pc + 3
        elif op == 0x12:
            self.outputs.append(self.read(pc + 2, 2))
            next_pc = pc + 4
        elif op == 3:
            destination = self.read(pc + 1, 1) >> 4
            r[destination] = (
                self.keyboard_inputs.popleft() if self.keyboard_inputs else 0
            )
            next_pc = pc + 2
        elif op in (4, 0x14):
            setting = r[self.read(pc + 1, 1) & 15]
            immediate = op == 0x14
            value = (
                self.read(pc + 2, 2)
                if immediate
                else r[self.read(pc + 2, 1) & 15]
            )
            self.screen_updates.append((setting, value))
            callback = self.screen_update_callback
            if callback is not None and callback(setting, value, self.steps + 1):
                self._screen_stop_requested = True
            next_pc = pc + (4 if immediate else 3)
        elif op in (5, 6):
            destination = self.read(pc + 1, 1) >> 4
            if self.live_time:
                value = time_ns()
            elif self.time_frequency_hz:
                seconds, cycles = divmod(self.steps, self.time_frequency_hz)
                value = (
                    self.time_value
                    + seconds * 1_000_000_000
                    + cycles * 1_000_000_000 // self.time_frequency_hz
                )
            else:
                value = self.time_value + self.steps * self.time_per_step_ns
            r[destination] = (value if op == 5 else value >> 32) & MASK
            next_pc = pc + 2
        elif op == 7:
            destination = self.read(pc + 1, 1) >> 4
            r[destination] = pc
            next_pc = pc + 2
        elif 0x20 <= op <= 0x3A and (op & 15) <= 10:
            pair = self.read(pc + 1, 1)
            dst, left = pair >> 4, pair & 15
            immediate = bool(op & 16)
            right = self.read(pc + 2, 2) if immediate else r[self.read(pc + 2, 1) & 15]
            a = r[left]
            code = op & 15
            next_pc = pc + (4 if immediate else 3)
            operations = {
                0: lambda: ~(a & right),
                1: lambda: a | right,
                2: lambda: a & right,
                3: lambda: ~(a | right),
                4: lambda: a + right,
                5: lambda: a - right,
                6: lambda: a ^ right,
                7: lambda: (a << right) if right < 32 else 0,
                8: lambda: (a >> right) if right < 32 else 0,
                9: lambda: (signed(a) >> min(right, 32)),
                10: lambda: flags(a, right),
            }
            r[dst] = operations[code]() & MASK
        elif 0x40 <= op <= 0x5F:
            immediate = bool(op & 16)
            target = self.read(pc + 2, 2) if immediate else r[self.read(pc + 2, 1) & 15]
            next_pc = pc + (4 if immediate else 3)
            # The condition is the opcode's low nibble applied to the flag
            # register named by the second byte: mask the three condition bits
            # against the flags, reduce with OR, then invert if bit 3 is set.
            condition = op & 15
            source = r[self.read(pc + 1, 1) & 15]
            take = bool(condition & source & 7) ^ bool(condition & 8)
            if take:
                next_pc = target
        elif 0x60 <= op <= 0x77:
            immediate = bool(op & 16)
            code = op & 7
            operand = self.read(pc + 1, 1)
            address = (
                self.read(pc + 2, 2) if immediate else r[self.read(pc + 2, 1) & 15]
            )
            next_pc = pc + (4 if immediate else 3)
            if code == 3:
                r[operand >> 4] = self.persistent_read(address)
            elif code == 7:
                self.persistent_write(address, r[operand & 15])
            elif code < 4:
                size = (1, 2, 4)[code]
                r[operand >> 4] = self.read(address, size)
            else:
                size = (1, 2, 4)[code & 3]
                self.write(address, r[operand & 15], size)
        else:
            raise RuntimeError(f"unsupported opcode {op:#x} at {pc:#x}")
        self.pc = next_pc & MASK
        self.steps += 1

    def _step_symphony(self):
        pc = self.pc
        Machine.step(self)
        # Every Symphony instruction occupies four bytes, so round up from the
        # variable length ``step`` advanced by. A PC still inside this
        # instruction's four bytes means it fell through rather than branched.
        if 0 < (self.pc - pc) & MASK <= 4:
            self.pc = (pc + 4) & MASK

    def run(
        self,
        halt_address=None,
        max_steps=5_000_000,
        progress=None,
        progress_interval=250_000,
    ):
        self._screen_stop_requested = False
        next_progress = self.steps + progress_interval
        while self.steps < max_steps:
            if halt_address is not None and self.pc == halt_address:
                return self.regs[1]
            previous = self.pc
            self.step()
            if self._screen_stop_requested:
                return self.regs[1]
            if progress is not None and self.steps >= next_progress:
                progress(self)
                next_progress = self.steps + progress_interval
            if self.pc == previous:
                return self.regs[1]
        raise RuntimeError(
            f"execution limit exceeded ({max_steps} instructions), PC={self.pc:#x}"
        )
