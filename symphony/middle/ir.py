"""Explicit control flow, virtual values, memory operations and function calls.

Virtual values are immutable except for 'copy' at control-flow joins. Addresses
are ordinary values. The backend never sees parser nodes or C expression trees.
"""

from dataclasses import dataclass, field
from .model import INT, UINT, VOID, CompileError, Node, Symbol, Type, pointer, common


@dataclass
class Instruction:
    op: str
    dst: int | None = None
    args: tuple = ()
    type: Type = VOID
    extra: object = None


TERMINATORS = {
    "jump",
    "branch_if",
    "cbranch_if",
    "return",
    "tailcall",
    "direct_tailcall",
    "halt",
}


@dataclass
class BasicBlock:
    """A maximal straight-line instruction sequence, identified by a
    persistent ``label`` assigned once at creation. ``instructions`` never
    includes the block's own canonical leading label -- that is the block's
    identity, not its content -- though it may still contain extra ``label``
    instructions for additional names that resolve to this same block (the
    lowerer sometimes marks two names with nothing lowered in between, e.g.
    an empty ``if`` arm)."""

    label: str
    instructions: list = field(default_factory=list)
    successors: list = field(default_factory=list)
    predecessors: list = field(default_factory=list)

    def terminator(self):
        return self.instructions[-1] if self.instructions and self.instructions[-1].op in TERMINATORS else None

    def add_successor(self, label, fallthrough=False):
        if label in self.successors:
            return
        if fallthrough:
            self.successors.insert(0, label)
        else:
            self.successors.append(label)


def _split_into_blocks(name, instructions):
    """Split a flat, label-delimited instruction list into ``BasicBlock``s
    with stable synthetic identity, and a map from every label spelled in
    the stream (including extra aliases beyond a block's first) to its
    block's canonical label."""
    if not instructions:
        return [], {}
    leaders = {0}
    for index, instruction in enumerate(instructions):
        if instruction.op == "label":
            leaders.add(index)
        if instruction.op in TERMINATORS and index + 1 < len(instructions):
            leaders.add(index + 1)
    starts = sorted(leaders)
    blocks = []
    label_to_block = {}
    anon_id = 0
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(instructions)
        items = instructions[start:end]
        names = [item.extra for item in items if item.op == "label"]
        if names:
            canonical = names[0]
        else:
            anon_id += 1
            canonical = f"{name}.entry" if position == 0 else f"{name}.B{anon_id}"
        for label_name in names:
            label_to_block[label_name] = canonical
        blocks.append(BasicBlock(canonical, list(items)))
    return blocks, label_to_block


def _referenced_labels(blocks):
    """Every label name any block's terminator explicitly jumps/branches to."""
    referenced = set()
    for block in blocks:
        last = block.terminator()
        if last is None:
            continue
        if last.op == "jump":
            referenced.add(last.extra)
        elif last.op in ("branch_if", "cbranch_if"):
            referenced.add(last.extra[1])
    return referenced


def _flatten_blocks(blocks):
    """The inverse of ``_split_into_blocks``: linearize blocks back into a
    flat instruction stream in their current list order, synthesizing a
    leading ``label`` instruction for any block that (a) isn't already
    spelled out by one of its own ``label`` instructions and (b) is actually
    the target of some explicit jump/branch elsewhere in the function.

    A block with neither is pure internal bookkeeping -- nothing in the
    function can ever reach it except by fallthrough from its immediate
    predecessor in this same list -- so it never needs a label spelled into
    the instruction stream the backend sees."""
    out = []
    referenced = _referenced_labels(blocks)
    for block in blocks:
        has_own_label = any(
            item.op == "label" and item.extra == block.label for item in block.instructions
        )
        if not has_own_label and block.label in referenced:
            out.append(Instruction("label", extra=block.label))
        out.extend(block.instructions)
    return out


@dataclass
class FunctionIR:
    name: str
    params: list
    locals: list
    blocks: list = field(default_factory=list)
    values: int = 0

    @property
    def instructions(self):
        return _flatten_blocks(self.blocks)

    @instructions.setter
    def instructions(self, value):
        self.blocks, _ = _split_into_blocks(self.name, list(value))


@dataclass
class ModuleIR:
    globals: list
    functions: list[FunctionIR]

    def dump(self):
        lines = []
        for f in self.functions:
            lines.append(f'function {f.name}({", ".join(p.key for p in f.params)}):')
            for i in f.instructions:
                dst = f"%{i.dst} = " if i.dst is not None else ""
                lines.append(
                    f'  {dst}{i.op} {i.args} {i.extra if i.extra is not None else ""} : {i.type}'
                )
        return "\n".join(lines) + "\n"


class Lowerer:
    """Builds one function's IR as a flat, label-delimited instruction
    stream (the same shape C control flow naturally produces one
    ``label``/``jump``/``branch_if`` at a time), then splits it into
    ``FunctionIR.blocks`` once lowering finishes. Instructions accumulate in
    ``self.raw`` rather than ``self.f.instructions`` directly: the latter is
    a property backed by ``self.f.blocks``, so repeated ``.append()`` calls
    during lowering would silently be lost against a freshly flattened list
    each time.
    """

    def __init__(self, function):
        self.aggregate_return = function.symbol.type.base if function.symbol.type.base.kind == "struct" else None
        self.sret = (
            Symbol(
                "__sret",
                pointer(self.aggregate_return),
                "parameter",
                f"{function.symbol.key}.__sret",
            )
            if self.aggregate_return is not None
            else None
        )
        self.f = FunctionIR(
            function.symbol.key,
            ([self.sret] if self.sret is not None else []) + function.params,
            function.locals,
        )
        self.raw = []
        self.label_id = 0
        self.temporary_id = 0
        self.loops = []
        self.breaks = []
        self.case_labels = {}
        self.scopes = []
        self.dynamic_locals = {}

    def finish(self):
        """Split the accumulated flat stream into ``self.f.blocks``."""
        self.f.instructions = self.raw

    def value(self):
        v = self.f.values
        self.f.values += 1
        return v

    def emit(self, op, args=(), type_=VOID, extra=None, result=True):
        dst = self.value() if result else None
        self.raw.append(Instruction(op, dst, tuple(args), type_, extra))
        return dst

    def label(self):
        self.label_id += 1
        return f"{self.f.name}.L{self.label_id}"

    def mark(self, label):
        self.emit("label", extra=label, result=False)

    def jump(self, label):
        self.emit("jump", extra=label, result=False)

    def branch(self, value, target, truthy=True):
        """Branch to target on the requested truth value; otherwise fall through."""
        self.emit(
            "branch_if", (value,), extra=(truthy, target), result=False
        )

    def const(self, value, t=INT):
        return self.emit("const", type_=t, extra=value)

    def cast(self, v, t):
        return self.emit("cast", (v,), t)

    def store(self, address, value, t):
        self.emit("store", (address, value), t, result=False)

    def aggregate_copy(self, destination, source, size):
        """Copy through virtual values so partially overlapping objects are safe."""
        chunks = []
        offset = 0
        while offset < size:
            width = min(4, size - offset)
            type_ = Type(size=width, signed=False)
            src = self.binary("+", source, self.const(offset, UINT), UINT) if offset else source
            chunks.append((offset, type_, self.emit("load", (src,), type_)))
            offset += width
        for offset, type_, value in chunks:
            dst = self.binary("+", destination, self.const(offset, UINT), UINT) if offset else destination
            self.store(dst, value, type_)

    def aggregate_temporary(self, t):
        """Reserve caller-owned storage for a structure result."""
        self.temporary_id += 1
        symbol = Symbol(
            "__aggregate_result",
            t,
            "local",
            f"{self.f.name}.__aggregate_result.{self.temporary_id}",
        )
        self.f.locals.append(symbol)
        return self.emit("local_addr", type_=pointer(t), extra=symbol.key)

    def aggregate_call(self, n):
        destination = self.aggregate_temporary(n.type)
        target = n.children[0]
        arguments = [destination, *[self.expr(argument) for argument in n.children[1:]]]
        if (
            target.op == "address"
            and target.children[0].op == "var"
            and target.children[0].value.storage == "function"
        ):
            self.emit("direct_call", arguments, VOID, target.children[0].value.key, result=False)
        else:
            self.emit("call", [self.expr(target), *arguments], VOID, result=False)
        return destination

    def address(self, n):
        if n.op == "aggregate_call":
            return self.aggregate_call(n)
        if n.op == "var":
            sym = n.value
            if sym.key in self.dynamic_locals:
                return self.dynamic_locals[sym.key]
            return self.emit(
                (
                    "global_addr"
                    if sym.storage in ("global", "function")
                    else "local_addr"
                ),
                type_=pointer(sym.type),
                extra=sym.key,
            )
        if n.op == "deref":
            return self.expr(n.children[0])
        if n.op == "member":
            base = self.expr(n.children[0])
            return (
                self.binary("+", base, self.const(n.value), UINT)
                if n.value
                else base
            )
        raise AssertionError(f"non-addressable typed node: {n.op}")

    def binary(self, op, a, b, t):
        return self.emit("binary", (a, b), t, op)

    def scaled(self, a, b, scale):
        if isinstance(scale, Node):
            scale_value = self.expr(scale)
            b = self.binary("*", b, scale_value, INT)
        elif scale != 1:
            scale_value = self.const(scale)
            b = self.binary("*", b, scale_value, INT)
        return self.binary("+", a, b, UINT)

    def expr(self, n):
        op = n.op
        if op == "const":
            return self.const(n.value, n.type)
        if op in ("var", "deref", "member"):
            return self.emit("load", (self.address(n),), n.type)
        if op == "address":
            return self.address(n.children[0])
        if op == "aggregate_call":
            return self.aggregate_call(n)
        if op == "cast":
            return self.cast(self.expr(n.children[0]), n.type)
        if op == "bool_cast":
            return self.binary("!=", self.expr(n.children[0]), self.const(0), n.type)
        if op == "unary":
            return self.emit("unary", (self.expr(n.children[0]),), n.type, n.value)
        if op == "pointer_add":
            return self.scaled(
                self.expr(n.children[0]), self.expr(n.children[1]), n.value
            )
        if op == "pointer_diff":
            d = self.binary(
                "-", self.expr(n.children[0]), self.expr(n.children[1]), INT
            )
            scale = self.expr(n.value) if isinstance(n.value, Node) else self.const(n.value)
            return self.binary("/", d, scale, INT)
        if op == "binary":
            if n.value in ("&&", "||"):
                result = self.value()
                rhs, short, end = self.label(), self.label(), self.label()
                a = self.expr(n.children[0])
                # The short-circuit block is laid out next. Jump only when the
                # right operand must be evaluated.
                self.branch(a, rhs, n.value == "&&")
                self.mark(short)
                v = self.const(int(n.value == "||"))
                self.raw.append(Instruction("copy", result, (v,), INT))
                self.jump(end)
                self.mark(rhs)
                b = self.expr(n.children[1])
                v = self.binary("!=", b, self.const(0), UINT)
                self.raw.append(Instruction("copy", result, (v,), INT))
                self.mark(end)
                return result
            return self.binary(
                n.value,
                self.expr(n.children[0]),
                self.expr(n.children[1]),
                n.children[0].type,
            )
        if op == "checked_mul":
            return self.emit(
                "direct_call",
                (self.expr(n.children[0]), self.expr(n.children[1])),
                UINT,
                "__dyn_checked_mul",
            )
        if op == "assign":
            addr = self.address(n.children[0])
            value = self.expr(n.children[1])
            self.store(addr, value, n.type)
            return value
        if op == "aggregate_assign":
            destination = self.address(n.children[0])
            source = self.address(n.children[1])
            self.aggregate_copy(destination, source, n.type.size)
            return destination
        if op == "compound_assign":
            lhs, rhs = n.children
            addr = self.address(lhs)
            a = self.emit("load", (addr,), lhs.type)
            b = self.expr(rhs)
            if lhs.type.kind == "pointer":
                value = self.scaled(
                    a, b, lhs.type.base.size * (1 if n.value == "+" else -1)
                )
            else:
                t = (
                    lhs.type.promote()
                    if n.value in ("<<", ">>")
                    else common(lhs.type, rhs.type)
                )
                value = self.binary(
                    n.value,
                    self.cast(a, t),
                    self.cast(b, rhs.type.promote() if n.value in ("<<", ">>") else t),
                    t,
                )
            value = self.cast(value, lhs.type)
            self.store(addr, value, lhs.type)
            return value
        if op == "increment":
            lhs = n.children[0]
            operator, step = n.value
            addr = self.address(lhs)
            old = self.emit("load", (addr,), lhs.type)
            if isinstance(step, Node):
                step = self.expr(step)
            else:
                step = self.const(step)
            value = self.binary(
                "+" if "+" in operator else "-",
                old,
                step,
                lhs.type.promote(),
            )
            value = self.cast(value, lhs.type)
            self.store(addr, value, lhs.type)
            return old if operator.startswith("p") else value
        if op == "call":
            target = n.children[0]
            arguments = [self.expr(x) for x in n.children[1:]]
            if (
                target.op == "address"
                and target.children[0].op == "var"
                and target.children[0].value.storage == "function"
            ):
                return self.emit(
                    "direct_call",
                    arguments,
                    n.type,
                    target.children[0].value.key,
                )
            return self.emit("call", [self.expr(target), *arguments], n.type)
        if op == "printf":
            format_ = self.expr(n.children[0])
            arguments = iter(n.children[1:])
            result = self.const(0, INT)
            for token in n.value:
                if token[0] == "literal":
                    _, offset, count = token
                    text = (
                        self.binary("+", format_, self.const(offset, UINT), UINT)
                        if offset
                        else format_
                    )
                    written = self.emit(
                        "direct_call",
                        (text, self.const(count, UINT)),
                        UINT,
                        "__dyn_printf_write",
                    )
                else:
                    helper = {
                        "d": "__dyn_printf_signed",
                        "u": "__dyn_printf_unsigned",
                        "x": "__dyn_printf_hex",
                        "c": "__dyn_printf_put",
                        "s": "__dyn_printf_string",
                    }[token[0]]
                    written = self.emit(
                        "direct_call", (self.expr(next(arguments)),), UINT, helper
                    )
                result = self.binary("+", result, written, INT)
            return result
        if op == "comma":
            for child in n.children:
                result = self.expr(child)
            return result
        if op == "select":
            result = self.value()
            yes, no, end = self.label(), self.label(), self.label()
            self.branch(self.expr(n.children[0]), no, False)
            self.mark(yes)
            a = self.expr(n.children[1])
            self.raw.append(Instruction("copy", result, (a,), n.type))
            self.jump(end)
            self.mark(no)
            b = self.expr(n.children[2])
            self.raw.append(Instruction("copy", result, (b,), n.type))
            self.mark(end)
            return result
        raise AssertionError(f"unhandled typed expression {op}")

    def statement(self, n):
        op = n.op
        if op == "block":
            dynamic = any(
                child.op == "declare" and child.value[3] is not None
                for child in n.children
            )
            marker = self.emit("stack_mark", type_=UINT) if dynamic else None
            self.scopes.append(marker)
            for child in n.children:
                self.statement(child)
            self.scopes.pop()
            if marker is not None:
                self.emit("stack_restore", (marker,), result=False)
        elif op == "expression":
            self.expr(n.children[0])
        elif op == "vla_bounds":
            for saved, expression in n.value:
                address = self.emit("local_addr", type_=pointer(UINT), extra=saved.key)
                self.store(address, self.expr(expression), UINT)
        elif op == "declare":
            sym, entries, bounds, size = n.value
            for saved, expression in bounds:
                address = self.emit("local_addr", type_=pointer(UINT), extra=saved.key)
                self.store(address, self.expr(expression), UINT)
            if size is not None:
                bytes_ = self.expr(size)
                align = sym.type.align
                if align > 1:
                    bytes_ = self.binary(
                        "+", bytes_, self.const(align - 1, UINT), UINT
                    )
                    bytes_ = self.binary(
                        "&", bytes_, self.const(-(align), UINT), UINT
                    )
                self.dynamic_locals[sym.key] = self.emit(
                    "stack_alloc", (bytes_,), pointer(sym.type)
                )
                return
            if entries is not None:
                addr = self.emit("local_addr", type_=pointer(sym.type), extra=sym.key)
                if sym.type.kind in ("array", "struct", "union"):
                    self.emit("zero", (addr,), extra=sym.type.size, result=False)
                for off, t, value in entries:
                    p = self.binary("+", addr, self.const(off), UINT) if off else addr
                    self.store(p, self.expr(value), t)
        elif op == "return":
            if self.aggregate_return is not None and n.children:
                destination = self.emit(
                    "load",
                    (self.emit("local_addr", type_=pointer(pointer(self.aggregate_return)), extra=self.sret.key),),
                    pointer(self.aggregate_return),
                )
                self.aggregate_copy(destination, self.address(n.children[0]), self.aggregate_return.size)
                result = None
            else:
                result = self.expr(n.children[0]) if n.children else None
            for marker in reversed(self.scopes):
                if marker is not None:
                    self.emit("stack_restore", (marker,), result=False)
            self.emit(
                "return",
                (result,) if result is not None else (),
                result=False,
            )
        elif op == "if":
            yes, no, end = self.label(), self.label(), self.label()
            self.branch(self.expr(n.children[0]), no, False)
            self.mark(yes)
            self.statement(n.children[1])
            self.jump(end)
            self.mark(no)
            self.statement(n.children[2])
            self.mark(end)
        elif op == "switch":
            cases = []

            def collect(node):
                if node.op == "switch":
                    return
                if node.op in ("case", "default"):
                    cases.append(node)
                for child in node.children:
                    collect(child)

            collect(n.children[1])
            values = set()
            default = None
            for case in cases:
                if case.op == "default":
                    if default is not None:
                        raise CompileError("duplicate default label")
                    default = case
                elif case.value in values:
                    raise CompileError("duplicate case label")
                else:
                    values.add(case.value)
            end = self.label()
            labels = {id(case): self.label() for case in cases}
            self.case_labels.update(labels)
            selector = self.expr(n.children[0])
            for case in cases:
                if case.op == "case":
                    comparison = self.binary(
                        "==", selector, self.const(case.value, n.children[0].type), INT
                    )
                    self.branch(comparison, labels[id(case)])
            self.jump(labels[id(default)] if default is not None else end)
            self.breaks.append((end, len(self.scopes)))
            self.statement(n.children[1])
            self.breaks.pop()
            for case in cases:
                self.case_labels.pop(id(case), None)
            self.mark(end)
        elif op in ("while", "for", "do"):
            test, body, step, end = (
                self.label(),
                self.label(),
                self.label(),
                self.label(),
            )
            loop_marker = None
            if op == "for" and n.children[0].op == "declare" and n.children[0].value[3] is not None:
                loop_marker = self.emit("stack_mark", type_=UINT)
                self.scopes.append(loop_marker)
            if op == "for":
                self.statement(n.children[0])
                cond = n.children[1]
                stmt = n.children[3]
            else:
                cond, stmt = n.children
            self.loops.append((end, step, len(self.scopes) - (1 if loop_marker is not None else 0)))
            self.breaks.append((end, len(self.scopes) - (1 if loop_marker is not None else 0)))
            if op == "do":
                self.jump(body)
            self.mark(test)
            self.branch(self.expr(cond), end, False)
            self.mark(body)
            self.statement(stmt)
            self.mark(step)
            if op == "for":
                self.statement(n.children[2])
            self.jump(test)
            self.mark(end)
            self.loops.pop()
            self.breaks.pop()
            if loop_marker is not None:
                self.scopes.pop()
                self.emit("stack_restore", (loop_marker,), result=False)
        elif op == "break":
            end, scope_start = self.breaks[-1]
            for marker in reversed(self.scopes[scope_start:]):
                if marker is not None:
                    self.emit("stack_restore", (marker,), result=False)
            self.jump(end)
        elif op == "continue":
            _, step, scope_start = self.loops[-1]
            for marker in reversed(self.scopes[scope_start + 1 :]):
                if marker is not None:
                    self.emit("stack_restore", (marker,), result=False)
            self.jump(step)
        elif op in ("case", "default"):
            self.mark(self.case_labels[id(n)])
            for child in n.children:
                self.statement(child)
        else:
            raise AssertionError(f"unhandled typed statement {op}")


def lower(program):
    functions = []
    for f in program.functions:
        l = Lowerer(f)
        l.statement(f.body)
        # Deterministic fallthrough; only main's implicit return is specified by C.
        l.emit(
            "return",
            () if f.symbol.type.base in (VOID,) or f.symbol.type.base.kind == "struct" else (l.const(0),),
            result=False,
        )
        l.finish()
        functions.append(l.f)

    main = next(function for function in program.functions if function.symbol.key == "main")
    startup = FunctionIR("_start", [], [])
    raw = [
        Instruction("init_pic"),
        Instruction("init_stack"),
        *([Instruction("zero_bss")] if any(g.section == "zero" for g in program.globals) else []),
        Instruction("relocate_globals"),
    ]
    result = startup.values
    startup.values += 1
    raw.append(
        Instruction("direct_call", result, (), main.symbol.type.base, "main")
    )
    raw.append(Instruction("halt", args=(result,)))
    startup.instructions = raw
    return ModuleIR(program.globals, [startup, *functions])
