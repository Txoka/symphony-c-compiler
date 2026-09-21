"""Symphony and Dynphony instruction selection, ABI lowering, and frame construction."""

from . import isa
from .abi import ABI
from .assembler import Assembler
from .config import Image, Target
from ...middle.model import CompileError, align_up
from ...middle.ssa.allocate import copy_coalescing_groups, interference_graph


class Backend:
    """r1/r2 operands, r7 scratch, r11 frame pointer, r12 PIC base.

    Straight-line leaf values use incoming/caller-saved registers. Across control
    flow, frequently used values can live in r8-r11; a function saves only the
    callee-saved registers it actually receives. Remaining live values have frame
    slots. Calls place their continuation in r13; PIC startup owns r12.
    """

    def __init__(self, module, target):
        self.module = module
        self.target = target
        self.a = Assembler(target)
        self.frames = {}
        self.private_id = 0
        self.needs_stack_overflow = False
        self.bss_clear_unroll = None
        self.select_bss_sections()

    def unique(self):
        self.private_id += 1
        return f"@backend.{self.private_id}"

    def bss_globals(self):
        return [global_ for global_ in self.module.globals if global_.section == "bss"]

    def bss_size(self, globals_=None):
        size = 0
        for global_ in self.bss_globals() if globals_ is None else globals_:
            size = align_up(size, global_.symbol.type.align)
            size += len(global_.data)
        # The heap begins after BSS, so final alignment padding is available
        # to the clear routine. This permits word-only clearing.
        return align_up(size, 4)

    def instruction_size(self, data):
        if not self.target.fixed_instruction_width:
            return len(data)
        # Every instruction emitted here occupies one Symphony slot; the
        # encodings passed to this helper never contain multiple instructions.
        return 4

    def clear_plan_size(self, words, unroll):
        """Serialized size for a word-clear plan, including its address load."""
        if not words:
            return 0
        address = 12 + (4 if self.target.pic else 0)
        store = self.instruction_size(isa.store(4, 1, 0))
        add = self.instruction_size(isa.alu("add", 1, 1, 4, True))
        if unroll is None:
            return address + words * store + (words - 1) * add
        groups, tail = divmod(words, unroll)
        if groups < 2:
            return None
        counter = len(isa.cheap_constant(2, groups))
        body = unroll * (store + add)
        branch = (
            address + self.instruction_size(isa.jump("jne", 7))
            if self.target.pic
            # The clear loop's final address within the image isn't known yet
            # (layout hasn't happened), so bound it by the worst case the
            # relaxer could still resolve to a short branch for: the target
            # RAM's highest addressable byte. Matches Assembler.relax_controls,
            # which picks a short branch only when load_address + offset <= 0xFFFF.
            else (4 if self.target.load_address + self.target.ram_size - 1 <= 0xFFFF else
                  (16 if self.target.fixed_instruction_width else 15))
        )
        control = (
            self.instruction_size(isa.alu("sub", 2, 2, 1, True))
            + self.instruction_size(isa.alu("cmp", 15, 2, 0))
            + branch
        )
        tail_code = tail * store + max(tail - 1, 0) * add
        return address + counter + body + control + tail_code

    def clear_plans(self, size):
        words = size // 4
        return [
            (unroll, cost)
            for unroll in (None, 1, 2, 4, 8)
            if (cost := self.clear_plan_size(words, unroll)) is not None
        ]

    def select_bss_sections(self):
        candidates = [g for g in self.module.globals if g.section == "zero"]
        if not candidates:
            return
        if self.target.bss_mode == "assume-zeroed":
            section = "bss"
        elif self.target.bss_mode == "never":
            section = "data"
        else:
            bss_size = self.bss_size(candidates)
            data_size = sum(len(global_.data) for global_ in candidates)
            eligible = [
                unroll for unroll, cost in self.clear_plans(bss_size)
                if cost < data_size
            ]
            section = "bss" if eligible else "data"
            if eligible:
                # More unrolling executes fewer loop-control instructions;
                # choose the fastest plan that still beats DATA on size.
                self.bss_clear_unroll = max(
                    (unroll for unroll in eligible if unroll is not None),
                    default=None,
                )
        for global_ in candidates:
            global_.section = section

    def needs_heap_start(self):
        return any(
            instruction.op == "stack_alloc"
            or (
                instruction.op == "global_addr"
                and instruction.extra == "__dyn_heap_start"
            )
            for function in self.module.functions
            for instruction in function.instructions
        )

    def needs_text_screen(self):
        """Whether the retained program uses the text-screen runtime."""
        return any(
            global_.symbol.key == "__dyn_printf_framebuffer"
            for global_ in self.module.globals
        )

    def emit_zero_bss(self):
        """Clear the virtual BSS range with wide stores and exact-size tails."""
        globals_ = self.bss_globals()
        if not globals_ or self.target.bss_mode == "assume-zeroed":
            return
        size = self.bss_size(globals_)
        a = self.a
        # Clear backward from the end of the virtual BSS range.
        a.address(1, "@bss.end")
        words = size // 4
        unroll = self.bss_clear_unroll
        if unroll:
            groups, words = divmod(words, unroll)
            a.emit(isa.cheap_constant(2, groups))
            loop = self.unique()
            a.label(loop)
            for _ in range(unroll):
                a.emit(isa.alu("sub", 1, 1, 4, True))
                a.emit(isa.store(4, 1, 0))
            a.emit(isa.alu("sub", 2, 2, 1, True))
            a.emit(isa.alu("cmp", 15, 2, 0))
            a.branch("jne", loop)
        for _ in range(words):
            a.emit(isa.alu("sub", 1, 1, 4, True))
            a.emit(isa.store(4, 1, 0))

    def emit_text_screen_setup(self):
        """Bind the retained runtime framebuffer to the text-screen device."""
        a = self.a
        a.emit(isa.screen(0, 0, True))
        a.emit(isa.constant(2, 1))
        a.address(1, "__dyn_printf_framebuffer")
        a.emit(isa.screen(2, 1))

    def emit_relocations(self):
        """Lower the startup IR operation that rebases static pointers."""
        if not self.target.pic:
            return
        for global_ in self.module.globals:
            for offset, symbol, addend in global_.relocations:
                self.a.address(1, global_.symbol.key, offset)
                self.a.address(2, symbol, addend)
                self.a.emit(isa.store(4, 1, 2))

    @staticmethod
    def straight_leaf_eligible(f):
        if len(f.params) > len(ABI.argument_registers):
            return False
        allowed = {
            "param",
            "const",
            "copy",
            "cast",
            "unary",
            "binary",
            "intrinsic",
            "return",
        }
        machine_binary = {"+", "-", "&", "|", "^", "<<", ">>"}
        instructions = f.instructions
        for instruction in instructions:
            if instruction.op not in allowed:
                return False
            if instruction.op == "binary" and instruction.extra not in machine_binary:
                return False
        return True

    def emit_straight_leaf(self, f):
        """Local register allocation for one straight-line, call-free block."""
        if not self.straight_leaf_eligible(f):
            return False

        a = self.a
        instructions = f.instructions
        remaining_uses = {}
        definitions = {}
        for instruction in instructions:
            if instruction.dst is not None:
                definitions[instruction.dst] = instruction
            for value in instruction.args:
                remaining_uses[value] = remaining_uses.get(value, 0) + 1

        locations = {}
        register_values = {}
        for instruction in instructions:
            if instruction.op == "param":
                register = instruction.extra[0]
                locations[instruction.dst] = register
                register_values[register] = instruction.dst

        def release(value):
            remaining_uses[value] -= 1
            if remaining_uses[value] == 0 and value in locations:
                register_values.pop(locations[value], None)

        def free_register(avoid=()):
            for register in range(1, 8):
                if register not in register_values and register not in avoid:
                    return register
            return None

        def materialize(value, avoid=()):
            if value in locations:
                return locations[value]
            definition = definitions[value]
            if definition.op != "const":
                return None
            register = free_register(avoid)
            if register is None:
                return None
            a.emit(isa.cheap_constant(register, definition.extra))
            return register

        def destination(operands):
            for value, register in operands:
                if remaining_uses[value] == 1:
                    return register
            return free_register()

        for instruction in instructions:
            op = instruction.op
            if op in ("param", "const"):
                continue
            if op == "return":
                if instruction.args:
                    value = instruction.args[0]
                    register = materialize(value)
                    if register is None:
                        return False
                    if register != 1:
                        a.emit(isa.mov(1, register))
                    release(value)
                a.emit(isa.link_return())
                return True

            if op == "intrinsic":
                name = instruction.extra
                argument_registers = []
                immediate = None
                if name in ("output", "persistent_load"):
                    definition = definitions.get(instruction.args[0])
                    if definition is not None and definition.op == "const":
                        value = definition.extra & 0xFFFFFFFF
                        if value <= 0xFFFF:
                            immediate = value
                elif name == "screen":
                    definition = definitions.get(instruction.args[1])
                    if definition is not None and definition.op == "const":
                        value = definition.extra & 0xFFFFFFFF
                        if value <= 0xFFFF:
                            immediate = value

                for index, value in enumerate(instruction.args):
                    if immediate is not None and (
                        (name in ("output", "persistent_load") and index == 0)
                        or (name == "screen" and index == 1)
                    ):
                        argument_registers.append(None)
                        continue
                    register = materialize(
                        value, [r for r in argument_registers if r is not None]
                    )
                    if register is None:
                        return False
                    argument_registers.append(register)

                target = None
                if name in ("input", "keyboard", "time", "time_low", "time_high", "persistent_load"):
                    target = free_register(
                        [r for r in argument_registers if r is not None]
                    )
                    if target is None:
                        return False
                if name == "input":
                    a.emit(isa.input_(target))
                elif name == "output":
                    a.emit(
                        isa.output(
                            immediate if immediate is not None else argument_registers[0],
                            immediate is not None,
                        )
                    )
                elif name == "keyboard":
                    a.emit(isa.keyboard(target))
                elif name == "screen":
                    a.emit(
                        isa.screen(
                            argument_registers[0],
                            immediate if immediate is not None else argument_registers[1],
                            immediate is not None,
                        )
                    )
                elif name in ("time", "time_low", "time_high"):
                    a.emit(isa.time(name == "time_high", target))
                elif name == "persistent_load":
                    a.emit(
                        isa.persistent_load(
                            target,
                            immediate if immediate is not None else argument_registers[0],
                            immediate is not None,
                        )
                    )
                elif name == "persistent_store":
                    a.emit(
                        isa.persistent_store(
                            argument_registers[0], argument_registers[1]
                        )
                    )
                elif name == "jump":
                    a.emit(isa.jump("jmp", argument_registers[0]))
                elif name == "__dyn_heap_remaining":
                    return False
                else:
                    return False
                for value in instruction.args:
                    release(value)
                if target is not None and instruction.type.kind != "void":
                    locations[instruction.dst] = target
                    register_values[target] = instruction.dst
                continue

            operands = []
            for value in instruction.args:
                register = materialize(value, [item[1] for item in operands])
                if register is None:
                    return False
                operands.append((value, register))
            target = destination(operands)
            if target is None:
                return False

            if op in ("copy", "cast"):
                source = operands[0][1]
                if target != source:
                    a.emit(isa.mov(target, source))
                if op == "cast":
                    self.normalize(target, instruction.type)
            elif op == "unary":
                source = operands[0][1]
                a.emit(
                    isa.alu(
                        "sub" if instruction.extra == "-" else "nor",
                        target,
                        0,
                        source,
                    )
                )
            elif op == "binary":
                left, right = operands
                name = {
                    "+": "add",
                    "-": "sub",
                    "&": "and",
                    "|": "or",
                    "^": "xor",
                    "<<": "lsl",
                    ">>": "asr" if instruction.type.signed else "lsr",
                }[instruction.extra]
                a.emit(isa.alu(name, target, left[1], right[1]))
            else:
                return False

            for value, _ in operands:
                release(value)
            locations[instruction.dst] = target
            register_values[target] = instruction.dst

        return False

    def slot_address(self, offset, r=7):
        if offset <= 0xFFFF:
            self.a.emit(isa.alu("sub", r, ABI.frame_pointer, offset, True))
        else:
            self.a.emit(isa.cheap_constant(r, offset))
            self.a.emit(isa.alu("sub", r, ABI.frame_pointer, r))

    def get(self, v, r):
        if v in self.register_values:
            source = self.register_values[v]
            if source != r:
                self.a.emit(isa.mov(r, source))
            return
        definition = self.rematerialized.get(v)
        if definition:
            if definition.op == "const":
                self.a.emit(isa.cheap_constant(r, definition.extra))
            elif definition.op == "global_addr":
                self.a.address(r, definition.extra)
            elif definition.op == "local_addr":
                self.slot_address(self.locals[definition.extra], r)
            return
        self.slot_address(self.temp_offsets[v])
        self.a.emit(isa.load(4, r, 7))

    def put(self, v, r=1):
        if v in self.register_values:
            destination = self.register_values[v]
            if destination != r:
                self.a.emit(isa.mov(destination, r))
            return
        if v in self.temp_offsets:
            self.slot_address(self.temp_offsets[v])
            self.a.emit(isa.store(4, 7, r))

    def u16_constant(self, value):
        seen = set()
        while value not in seen:
            seen.add(value)
            definition = self.value_definitions.get(value)
            if definition is None:
                return None
            if definition.op == "const":
                constant = definition.extra & 0xFFFFFFFF
                return constant if constant <= 0xFFFF else None
            if definition.op not in ("copy", "cast"):
                return None
            value = definition.args[0]
        return None

    def intrinsic(self, instruction):
        """Emit a device operation. Value-producing instructions return in r1."""
        a = self.a
        name = instruction.extra
        args = instruction.args
        if name == "input":
            a.emit(isa.input_(1))
        elif name == "output":
            immediate = self.u16_constant(args[0])
            if immediate is None:
                self.get(args[0], 1)
                a.emit(isa.output(1))
            else:
                a.emit(isa.output(immediate, True))
        elif name == "keyboard":
            a.emit(isa.keyboard(1))
        elif name == "screen":
            self.get(args[0], 1)
            immediate = self.u16_constant(args[1])
            if immediate is None:
                self.get(args[1], 2)
                a.emit(isa.screen(1, 2))
            else:
                a.emit(isa.screen(1, immediate, True))
        elif name in ("time", "time_low", "time_high"):
            a.emit(isa.time(name == "time_high", 1))
        elif name == "persistent_load":
            immediate = self.u16_constant(args[0])
            if immediate is None:
                self.get(args[0], 1)
                a.emit(isa.persistent_load(1, 1))
            else:
                a.emit(isa.persistent_load(1, immediate, True))
        elif name == "persistent_store":
            immediate = self.u16_constant(args[0])
            self.get(args[1], 1)
            if immediate is None:
                self.get(args[0], 2)
                a.emit(isa.persistent_store(2, 1))
            else:
                a.emit(isa.persistent_store(immediate, 1, True))
        elif name == "jump":
            self.get(args[0], 1)
            a.emit(isa.jump("jmp", 1))
        elif name == "__dyn_heap_remaining":
            self.get(args[0], 1)
            mask = self.target.ram_size - 1
            if mask <= 0xFFFF:
                a.emit(isa.alu("and", 1, 1, mask, True))
                a.emit(isa.alu("and", 2, 14, mask, True))
            else:
                a.emit(isa.cheap_constant(7, mask))
                a.emit(isa.alu("and", 1, 1, 7))
                a.emit(isa.alu("and", 2, 14, 7))
            unavailable = self.unique()
            done = self.unique()
            a.emit(isa.alu("cmp", 15, 2, 1))
            a.branch("jb", unavailable)
            a.emit(isa.alu("sub", 1, 2, 1))
            a.branch("jmp", done)
            a.label(unavailable)
            a.emit(isa.mov(1, 0))
            a.label(done)
        else:
            raise AssertionError(f"unknown intrinsic {name}")

    def place_call_arguments(self, arguments):
        """Place register arguments and push arguments seven onward right-to-left."""
        register_count = len(ABI.argument_registers)
        register_arguments = arguments[:register_count]
        stack_arguments = arguments[register_count:]
        for value in reversed(stack_arguments):
            self.get(value, 1)
            self.a.emit(isa.push(1))

        homes = [self.register_values.get(value) for value in register_arguments]
        # SSA entry parameters may still live in *any* argument register.
        # Treat them all as simultaneous sources: otherwise materializing an
        # early argument can overwrite a later one (for example, a tail call
        # passing ``(b, a)`` when ``a`` and ``b`` reside in r1 and r2).
        caller_homes = any(
            home is not None and home in ABI.argument_registers for home in homes
        )
        if not caller_homes:
            for register, value in enumerate(register_arguments, 1):
                self.get(value, register)
            return len(stack_arguments)

        if all(home is not None for home in homes):
            pending = [
                [register, home]
                for register, home in enumerate(homes, 1)
                if register != home
            ]
            while pending:
                selected = next(
                    (
                        index
                        for index, (target, _) in enumerate(pending)
                        if target
                        not in {
                            source
                            for other, source in pending
                            if other != target
                        }
                    ),
                    None,
                )
                if selected is None:
                    # flags is caller-clobbered and is not an argument register.
                    self.a.emit(isa.mov(ABI.status_register, pending[0][1]))
                    pending[0][1] = ABI.status_register
                    continue
                target, source = pending.pop(selected)
                self.a.emit(isa.mov(target, source))
            return len(stack_arguments)

        # Mixed computed/register arguments need storage so materializing one
        # cannot overwrite a later caller-register source.
        for value in register_arguments:
            self.get(value, 1)
            self.a.emit(isa.push(1))
        for register in range(len(register_arguments), 0, -1):
            self.a.emit(isa.pop(register))
        return len(stack_arguments)

    def discard_stack_arguments(self, count):
        if not count:
            return
        size = count * 4
        if size <= 0xFFFF:
            self.a.emit(isa.alu("add", 14, 14, size, True))
        else:
            self.a.emit(isa.cheap_constant(7, size))
            self.a.emit(isa.alu("add", 14, 14, 7))

    def load_stack_parameter(
        self, position, type_, saved_register_count, link_saved, destination=1
    ):
        # The frame pointer follows its saved predecessor, any saved value
        # registers, and an optional saved r13. Argument eight is at the entry SP.
        # The caller always pushes a full word regardless of the parameter's
        # width; on this big-endian target the value occupies that word's
        # low-order bytes, so a narrower load must start further into it.
        offset = 4 * (position - 7 + saved_register_count + int(link_saved)) + (
            4 - type_.size
        )
        if offset <= 0xFFFF:
            self.a.emit(isa.alu("add", 7, ABI.frame_pointer, offset, True))
        else:
            self.a.emit(isa.cheap_constant(7, offset))
            self.a.emit(isa.alu("add", 7, ABI.frame_pointer, 7))
        self.a.emit(isa.load(type_.size, destination, 7))
        self.normalize(destination, type_)

    def normalize(self, r, t):
        if t.kind == "int" and t.size < 4:
            shift = 32 - t.size * 8
            self.a.emit(isa.alu("lsl", r, r, shift, True))
            self.a.emit(isa.alu("asr" if t.signed else "lsr", r, r, shift, True))

    def comparison(self, op, signed, immediate=None):
        jumps = {
            "==": "je",
            "!=": "jne",
            "<": "jl" if signed else "jb",
            "<=": "jle" if signed else "jbe",
            ">": "jg" if signed else "ja",
            ">=": "jge" if signed else "jae",
        }
        yes, end = self.unique(), self.unique()
        self.a.emit(
            isa.alu(
                "cmp",
                15,
                1,
                immediate if immediate is not None else 2,
                immediate is not None,
            )
        )
        self.a.branch(jumps[op], yes)
        self.a.emit(isa.constant(1, 0))
        self.a.branch("jmp", end)
        self.a.label(yes)
        self.a.emit(isa.constant(1, 1))
        self.a.label(end)

    @staticmethod
    def condition_jump(op, signed):
        return {
            "==": "je",
            "!=": "jne",
            "<": "jl" if signed else "jb",
            "<=": "jle" if signed else "jbe",
            ">": "jg" if signed else "ja",
            ">=": "jge" if signed else "jae",
        }[op]

    def prepare_frame(self, f):
        """Lay out addressable objects and spill slots for live values."""
        offset = 0
        self.locals = {}
        instructions = f.instructions
        promoted = {
            instruction.extra[1]
            for instruction in instructions
            if instruction.op == "param"
        }
        addressed = {
            instruction.extra
            for instruction in instructions
            if instruction.op == "local_addr"
        }
        # An unused parameter has no local_addr (the lowerer only emits one on
        # actual use), but the prologue below still stores its incoming
        # argument register somewhere before anything has proven it unused.
        addressed |= {symbol.key for symbol in f.params}
        for symbol in f.params + f.locals:
            if symbol.key in promoted or symbol.key not in addressed:
                continue
            offset = align_up(offset + symbol.type.size, symbol.type.align)
            self.locals[symbol.key] = offset
        self.temp_base = align_up(offset, 4)

        definitions = {}
        writes = {}
        uses = {}
        for index, instruction in enumerate(instructions):
            if instruction.dst is not None:
                definitions.setdefault(instruction.dst, []).append(instruction)
                writes.setdefault(instruction.dst, []).append(index)
            for value in instruction.args:
                uses[value] = max(uses.get(value, -1), index)

        self.value_definitions = {
            value: items[0]
            for value, items in definitions.items()
            if len(items) == 1
        }
        self.rematerialized = {
            value: instruction
            for value, instruction in self.value_definitions.items()
            if instruction.op in ("const", "global_addr", "local_addr")
        }

        # Give each materialized live value its own slot. Reusing intervals needs
        # control-flow liveness: a textual interval is unsound across loop edges.
        live_values = sorted(
            value
            for value in uses
            if value not in self.rematerialized and value in writes
        )
        # A small global allocation across the complete CFG. Values live over a
        # real call use callee-saved homes; ordinary branches preserve r3-r6.
        # Saving only selected registers keeps low-pressure functions inexpensive.
        scores = {
            value: sum(len(item.args) for item in definitions.get(value, ()))
            + sum(
                instruction.args.count(value)
                for instruction in instructions
            )
            for value in live_values
        }
        definition_indexes = {}
        use_indexes = {}
        call_indexes = []
        for index, instruction in enumerate(instructions):
            if instruction.dst is not None:
                definition_indexes.setdefault(instruction.dst, []).append(index)
            for value in instruction.args:
                use_indexes.setdefault(value, []).append(index)
            if instruction.op in (
                "call",
                "direct_call",
                "tailcall",
                "direct_tailcall",
            ) or (
                instruction.op == "binary"
                and instruction.extra in ("*", "/", "%")
            ):
                call_indexes.append(index)

        pinned = {}
        for index, instruction in enumerate(instructions):
            if instruction.op != "halt" or not instruction.args:
                continue
            value = instruction.args[0]
            if (
                index > 0
                and instructions[index - 1].op in ("call", "direct_call")
                and instructions[index - 1].dst == value
            ):
                # The ABI already leaves this immediately consumed result in r1.
                pinned[value] = 1

        graph, across_calls = interference_graph(f)
        groups = copy_coalescing_groups(f, live_values, graph, pinned)
        group_for = {
            value: index for index, group in enumerate(groups) for value in group
        }
        group_graph = {index: set() for index in range(len(groups))}
        for value, neighbours in graph.items():
            if value not in group_for:
                continue
            group = group_for[value]
            group_graph[group].update(
                group_for[other]
                for other in neighbours
                if other in group_for and group_for[other] != group
            )
        group_scores = {
            index: sum(scores[value] for value in group)
            for index, group in enumerate(groups)
        }
        ordered = sorted(
            range(len(groups)),
            key=lambda group: (
                -len(group_graph[group]), -group_scores[group], min(groups[group])
            ),
        )
        self.register_values = dict(pinned)
        callee_registers = [8, 9, 10]
        if not self.target.pic:
            callee_registers.append(12)
        for group in ordered:
            values = groups[group]
            assigned = {
                self.register_values[value]
                for value in values
                if value in self.register_values
            }
            if assigned:
                register = next(iter(assigned))
                self.register_values.update((value, register) for value in values)
                continue
            forbidden = {
                self.register_values.get(other)
                for neighbour in group_graph[group]
                for other in groups[neighbour]
            }
            choices = (
                callee_registers
                if any(value in across_calls for value in values)
                else [3, 4, 5, 6, *callee_registers]
            )
            register = next((item for item in choices if item not in forbidden), None)
            if register is not None:
                self.register_values.update((value, register) for value in values)
        live_values = [
            value for value in live_values if value not in self.register_values
        ]
        self.temp_offsets = {}
        for slot, value in enumerate(live_values):
            self.temp_offsets[value] = self.temp_base + 4 * (slot + 1)

        return self.temp_base + 4 * len(live_values)

    def function(self, f):
        a = self.a
        a.label(f.name)
        instructions = f.instructions
        for instruction in instructions:
            if instruction.op == "init_pic":
                if self.target.pic:
                    a.emit(isa.counter(ABI.pic_base_register))
            elif instruction.op == "init_stack":
                if self.target.pic:
                    a.emit(isa.mov(14, 0))
            elif instruction.op == "zero_bss":
                self.emit_zero_bss()
            elif instruction.op == "relocate_globals":
                self.emit_relocations()
            else:
                break
        if f.name == "_start" and self.needs_text_screen():
            self.emit_text_screen_setup()
        leaf_start = len(a.code)
        if self.emit_straight_leaf(f):
            self.frames[f.name] = 0
            return
        # The allocator is allowed to decline when local pressure exceeds its
        # register set. It emits no lasting partial fast path in that case.
        del a.code[leaf_start:]
        frame = self.prepare_frame(f)
        uses_frame = frame > 0 or len(f.params) > len(ABI.argument_registers)
        returns_to_caller = any(
            instruction.op in ("return", "tailcall", "direct_tailcall")
            for instruction in instructions
        )
        saves_link = returns_to_caller and any(
            instruction.op in ("call", "direct_call")
            or (
                instruction.op == "binary"
                and instruction.extra in ("*", "/", "%")
            )
            for instruction in instructions
        )
        saved_registers = sorted({
            register for register in self.register_values.values() if register >= 8
        })
        staged_seventh = len(f.params) >= len(ABI.argument_registers)
        self.frames[f.name] = (
            frame + 4 * len(saved_registers)
            + (4 if uses_frame else 0) + (4 if saves_link else 0)
            + (4 if staged_seventh else 0)
        )
        if self.frames[f.name] >= self.target.ram_size:
            raise CompileError(f"{f.name}: stack frame exceeds RAM")
        if saves_link:
            a.emit(isa.push(ABI.link_register))
        for register in saved_registers:
            a.emit(isa.push(register))
        if uses_frame:
            a.emit(isa.push(ABI.frame_pointer))
            a.emit(isa.mov(ABI.frame_pointer, 14))
            if frame <= 0xFFFF:
                a.emit(isa.alu("sub", 14, 14, frame, True))
            else:
                a.emit(isa.cheap_constant(7, frame))
                a.emit(isa.alu("sub", 14, 14, 7))
        promoted = {
            instruction.extra[1]: instruction
            for instruction in instructions
            if instruction.op == "param"
        }
        if staged_seventh:
            # r7 is also the backend address scratch. Preserve its incoming
            # argument until the other parameter homes have been established.
            a.emit(isa.push(ABI.argument_registers[-1]))
        # Store every register parameter that needs a frame slot before moving
        # any promoted parameter into its allocated home.  A move such as
        # r1 -> r4 must not destroy the still-unhandled fourth argument.
        register_moves = []
        for r, sym in enumerate(f.params[:len(ABI.argument_registers) - 1], 1):
            if sym.key in promoted:
                instruction = promoted[sym.key]
                destination = self.register_values.get(instruction.dst)
                if destination is None:
                    self.put(instruction.dst, r)
                elif destination != r:
                    register_moves.append([destination, r])
            else:
                self.slot_address(self.locals[sym.key])
                a.emit(isa.store(sym.type.size, 7, r))

        # Resolve the incoming-register permutation in parallel. r7 now carries
        # an argument, so use caller-clobbered flags to break cycles.
        while register_moves:
            selected = next(
                (
                    index
                    for index, (target, _) in enumerate(register_moves)
                    if target
                    not in {
                        source
                        for other, source in register_moves
                        if other != target
                    }
                ),
                None,
            )
            if selected is None:
                a.emit(isa.mov(ABI.status_register, register_moves[0][1]))
                register_moves[0][1] = ABI.status_register
                continue
            target, source = register_moves.pop(selected)
            a.emit(isa.mov(target, source))

        if staged_seventh:
            sym = f.params[len(ABI.argument_registers) - 1]
            a.emit(isa.pop(ABI.status_register))
            if sym.key in promoted:
                self.put(promoted[sym.key].dst, ABI.status_register)
            else:
                self.slot_address(self.locals[sym.key])
                a.emit(isa.store(sym.type.size, 7, ABI.status_register))

        for r, sym in enumerate(
            f.params[len(ABI.argument_registers):], len(ABI.argument_registers) + 1
        ):
            self.load_stack_parameter(
                r, sym.type, len(saved_registers), saves_link
            )
            if sym.key in promoted:
                self.put(promoted[sym.key].dst, 1)
            else:
                self.slot_address(self.locals[sym.key])
                a.emit(isa.store(sym.type.size, 7, 1))
        epilogue = self.unique()
        terminated = False
        for instruction_index, i in enumerate(instructions):
            op = i.op
            result_register = 1
            if op in ("param", "init_pic", "init_stack", "zero_bss", "relocate_globals"):
                continue
            if op == "label":
                a.label(i.extra)
                continue
            if op == "jump":
                a.branch("jmp", i.extra)
                continue
            if op == "const":
                if i.dst in self.rematerialized:
                    continue
                result_register = self.register_values.get(i.dst, 1)
                a.emit(isa.cheap_constant(result_register, i.extra))
            elif op == "global_addr":
                if i.dst in self.rematerialized:
                    continue
                result_register = self.register_values.get(i.dst, 1)
                a.address(result_register, i.extra)
            elif op == "local_addr":
                if i.dst in self.rematerialized:
                    continue
                result_register = self.register_values.get(i.dst, 1)
                self.slot_address(self.locals[i.extra], result_register)
            elif op == "stack_mark":
                a.emit(isa.mov(1, 14))
            elif op == "stack_alloc":
                self.get(i.args[0], 1)
                self.needs_stack_overflow = True
                a.emit(isa.alu("cmp", 15, 1, 0))
                a.branch("je", "_stack_overflow")
                if self.target.ram_size < 2**32:
                    a.emit(isa.cheap_constant(7, self.target.ram_size))
                    a.emit(isa.alu("cmp", 15, 1, 7))
                    a.branch("jae", "_stack_overflow")
                a.emit(isa.alu("sub", 2, 14, 1))
                mask = self.target.ram_size - 1
                if mask <= 0xFFFF:
                    a.emit(isa.alu("and", 2, 2, mask, True))
                else:
                    a.emit(isa.cheap_constant(7, mask))
                    a.emit(isa.alu("and", 2, 2, 7))
                heap = next(
                    (g.symbol.key for g in self.module.globals
                     if g.symbol.name == "__dyn_heap_end"),
                    None,
                )
                if heap is not None:
                    a.address(7, heap)
                    a.emit(isa.load(4, 1, 7))
                    have_heap = self.unique()
                    a.emit(isa.alu("cmp", 15, 1, 0))
                    a.branch("jne", have_heap)
                a.address(1, "__dyn_heap_start")
                a.emit(isa.alu("add", 1, 1, 3, True))
                a.emit(isa.cheap_constant(7, -4))
                a.emit(isa.alu("and", 1, 1, 7))
                if heap is not None:
                    a.label(have_heap)
                a.emit(isa.alu("cmp", 15, 2, 1))
                a.branch("jb", "_stack_overflow")
                a.emit(isa.mov(14, 2))
                a.emit(isa.mov(1, 2))
            elif op == "stack_restore":
                self.get(i.args[0], 14)
                continue
            elif op == "zero_bss":
                self.emit_zero_bss()
                continue
            elif op in ("cast", "copy"):
                result_register = self.register_values.get(i.dst, 1)
                self.get(i.args[0], result_register)
                if op == "cast":
                    self.normalize(result_register, i.type)
            elif op == "load":
                result_register = self.register_values.get(i.dst, 1)
                self.get(i.args[0], result_register)
                a.emit(isa.load(i.type.size, result_register, result_register))
                self.normalize(result_register, i.type)
            elif op == "store":
                self.get(i.args[0], 1)
                self.get(i.args[1], 2)
                a.emit(isa.store(i.type.size, 1, 2))
                continue
            elif op == "zero":
                self.get(i.args[0], 1)
                a.emit(isa.cheap_constant(2, i.extra))
                loop, end = self.unique(), self.unique()
                a.label(loop)
                a.emit(isa.alu("cmp", 15, 2, 0))
                a.branch("je", end)
                a.emit(isa.store(1, 1, 0))
                a.emit(isa.alu("add", 1, 1, 1, True))
                a.emit(isa.alu("sub", 2, 2, 1, True))
                a.branch("jmp", loop)
                a.label(end)
                continue
            elif op == "unary":
                result_register = self.register_values.get(i.dst, 1)
                self.get(i.args[0], result_register)
                if i.extra == "!":
                    # comparison() currently materializes its Boolean in r1.
                    if result_register != 1:
                        a.emit(isa.mov(1, result_register))
                    a.emit(isa.mov(2, 0))
                    self.comparison("==", False)
                    result_register = 1
                else:
                    a.emit(isa.alu(
                        "sub" if i.extra == "-" else "nor",
                        result_register,
                        0,
                        result_register,
                    ))
            elif op == "binary":
                left, right = i.args
                immediate = None
                if i.extra not in ("*", "/", "%"):
                    definition = self.rematerialized.get(right)
                    if definition and definition.op == "const":
                        value = definition.extra & 0xFFFFFFFF
                        if value <= 0xFFFF:
                            immediate = value
                    if (
                        i.extra in ("+", "&", "|", "^", "==", "!=")
                        and immediate is None
                    ):
                        definition = self.rematerialized.get(left)
                        if definition and definition.op == "const":
                            value = definition.extra & 0xFFFFFFFF
                            if value <= 0xFFFF:
                                left, right = right, left
                                immediate = value
                if i.extra in ("==", "!=", "<", "<=", ">", ">="):
                    self.get(left, 1)
                    if immediate is None:
                        self.get(right, 2)
                    self.comparison(i.extra, i.type.signed, immediate)
                elif i.extra in ("*", "/", "%"):
                    self.get(left, 1)
                    self.get(right, 2)
                    helper = (
                        "__dyn_mul"
                        if i.extra == "*"
                        else "__dyn_"
                        + ("s" if i.type.signed else "u")
                        + ("div" if i.extra == "/" else "mod")
                    )
                    a.call(helper)
                else:
                    result_register = self.register_values.get(i.dst, 1)
                    commutative = i.extra in ("+", "&", "|", "^")
                    if (
                        immediate is None
                        and commutative
                        and self.register_values.get(right) == result_register
                    ):
                        left, right = right, left
                    preserve_right = (
                        immediate is None
                        and left != right
                        and self.register_values.get(right) == result_register
                    )
                    if preserve_right:
                        self.get(right, 2)
                    self.get(left, result_register)
                    if immediate is None and not preserve_right:
                        self.get(right, 2)
                    name = {
                        "+": "add",
                        "-": "sub",
                        "&": "and",
                        "|": "or",
                        "^": "xor",
                        "<<": "lsl",
                        ">>": "asr" if i.type.signed else "lsr",
                    }[i.extra]
                    a.emit(
                        isa.alu(
                            name,
                            result_register,
                            result_register,
                            immediate if immediate is not None else 2,
                            immediate is not None,
                        )
                    )
            elif op == "call":
                arguments = i.args[1:]
                target = self.rematerialized.get(i.args[0])
                symbolic = (
                    target is not None
                    and target.op == "global_addr"
                    and not self.target.pic
                )
                if not symbolic:
                    # Preserve an indirect target before argument placement can
                    # overwrite its allocated caller-saved register.
                    self.get(i.args[0], ABI.call_target_register)
                stack_arguments = self.place_call_arguments(arguments)
                if symbolic:
                    a.call(target.extra)
                else:
                    a.call_register(ABI.call_target_register)
                self.discard_stack_arguments(stack_arguments)
                self.normalize(1, i.type)
            elif op == "direct_call":
                stack_arguments = self.place_call_arguments(i.args)
                a.call(i.extra)
                self.discard_stack_arguments(stack_arguments)
                self.normalize(1, i.type)
            elif op == "tailcall":
                target = self.rematerialized.get(i.args[0])
                symbolic = (
                    target is not None
                    and target.op == "global_addr"
                    and not self.target.pic
                )
                if not symbolic:
                    self.get(i.args[0], ABI.call_target_register)
                self.place_call_arguments(i.args[1:])
                if uses_frame:
                    a.emit(isa.mov(14, ABI.frame_pointer))
                    a.emit(isa.pop(ABI.frame_pointer))
                for register in reversed(saved_registers):
                    a.emit(isa.pop(register))
                if saves_link:
                    a.emit(isa.pop(ABI.link_register))
                if symbolic:
                    a.branch("jmp", target.extra, ABI.call_target_register)
                else:
                    a.emit(isa.jump("jmp", ABI.call_target_register))
                continue
            elif op == "direct_tailcall":
                self.place_call_arguments(i.args)
                if uses_frame:
                    a.emit(isa.mov(14, ABI.frame_pointer))
                    a.emit(isa.pop(ABI.frame_pointer))
                for register in reversed(saved_registers):
                    a.emit(isa.pop(register))
                if saves_link:
                    a.emit(isa.pop(ABI.link_register))
                a.branch("jmp", i.extra, ABI.call_target_register)
                continue
            elif op == "cbranch_if":
                operator, target_label = i.extra
                left, right = i.args
                immediate = self.u16_constant(right)
                if immediate is None and operator in ("==", "!="):
                    left_immediate = self.u16_constant(left)
                    if left_immediate is not None:
                        left, right = right, left
                        immediate = left_immediate
                self.get(left, 1)
                if immediate is None:
                    self.get(right, 2)
                a.emit(
                    isa.alu(
                        "cmp",
                        15,
                        1,
                        immediate if immediate is not None else 2,
                        immediate is not None,
                    )
                )
                a.branch(
                    self.condition_jump(operator, i.type.signed), target_label
                )
                continue
            elif op == "intrinsic":
                self.intrinsic(i)
                self.normalize(1, i.type)
            elif op == "branch_if":
                truthy, target_label = i.extra
                self.get(i.args[0], 1)
                a.emit(isa.alu("cmp", 15, 1, 0))
                a.branch("jne" if truthy else "je", target_label)
                continue
            elif op == "return":
                if i.args:
                    self.get(i.args[0], 1)
                if instruction_index + 1 != len(instructions):
                    a.branch("jmp", epilogue)
                continue
            elif op == "halt":
                if i.args:
                    self.get(i.args[0], 1)
                if uses_frame:
                    a.emit(isa.mov(14, ABI.frame_pointer))
                    a.emit(isa.pop(ABI.frame_pointer))
                for register in reversed(saved_registers):
                    a.emit(isa.pop(register))
                if saves_link:
                    a.emit(isa.pop(ABI.link_register))
                if not self.target.pic:
                    a.label("_halt")
                    a.branch("jmp", "_halt")
                else:
                    a.address(7, "_halt")
                    a.label("_halt")
                    a.emit(isa.jump("jmp", 7))
                terminated = True
                # A graph rewrite may serialize another reachable block after
                # a halt block (notably an explicit critical-edge trampoline
                # appended by SSA destruction). Halt has no fallthrough, but
                # those later labels still have to be emitted for incoming
                # branches. `terminated` suppresses only the final epilogue.
                continue
            else:
                raise AssertionError(f"unhandled IR opcode {op}")
            self.put(i.dst, result_register)
        if terminated:
            return
        a.label(epilogue)
        if uses_frame:
            a.emit(isa.mov(14, ABI.frame_pointer))
            a.emit(isa.pop(ABI.frame_pointer))
        for register in reversed(saved_registers):
            a.emit(isa.pop(register))
        if saves_link:
            a.emit(isa.pop(ABI.link_register))
        a.emit(isa.link_return())

    def build(self):
        self.target.validate()
        a = self.a
        for f in self.module.functions:
            self.function(f)
        if self.needs_stack_overflow:
            a.label("_stack_overflow")
            overflow_loop = self.unique()
            a.branch("jmp", overflow_loop)
            a.label(overflow_loop)
            a.branch("jmp", "_stack_overflow")
        for section in ("rodata", "data"):
            for g in self.module.globals:
                if g.section != section:
                    continue
                a.emit_data(bytes(align_up(len(a.code), g.symbol.type.align) - len(a.code)))
                a.label(g.symbol.key)
                a.emit_data(g.data)
        # Relax branches/calls before assigning addresses to reservations that
        # live immediately after the serialized image.
        a.relax_controls()
        virtual_end = len(a.code)
        bss = self.bss_globals()
        if bss:
            virtual_end = align_up(
                virtual_end, max(4, max(g.symbol.type.align for g in bss))
            )
            a.labels["@bss.start"] = virtual_end
        for g in bss:
            virtual_end = align_up(virtual_end, g.symbol.type.align)
            a.labels[g.symbol.key] = virtual_end
            virtual_end += len(g.data)
        if bss:
            virtual_end = align_up(virtual_end, 4)
            a.labels["@bss.end"] = virtual_end
        if self.needs_heap_start():
            a.labels["__dyn_heap_start"] = virtual_end
            # PIC images may load at any byte address, so reserve the largest
            # possible gap introduced by runtime four-byte alignment.
            virtual_end += 3
        a.finish()
        for g in self.module.globals:
            for offset, symbol, addend in g.relocations:
                if symbol not in a.labels:
                    raise CompileError(f"undefined symbol: {symbol}")
                value = (
                    0
                    if self.target.pic
                    else (self.target.load_address + a.labels[symbol] + addend)
                    & 0xFFFFFFFF
                )
                start = a.labels[g.symbol.key] + offset
                a.code[start : start + 4] = value.to_bytes(4, "big")
        size = len(a.code)
        start = self.target.load_address % self.target.ram_size
        if virtual_end > self.target.ram_size or start + virtual_end > self.target.ram_size:
            raise CompileError(
                "image does not fit contiguously in configured RAM at load address"
            )
        if virtual_end + max(self.frames.values(), default=0) > self.target.ram_size - start:
            raise CompileError(
                "image leaves insufficient RAM for the largest single stack frame"
            )
        symbols = {
            k: v + (0 if self.target.pic else self.target.load_address)
            for k, v in a.labels.items()
        }
        return Image(bytes(a.code), symbols, self.target, self.frames)


def generate(module, target=None):
    return Backend(module, target or Target()).build()
