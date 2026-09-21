"""Target types and typed syntax. No parser or machine encoding dependencies."""

from dataclasses import dataclass, field
from typing import Any


class CompileError(Exception):
    """A source or target configuration diagnostic safe to show to a user."""


@dataclass(eq=False)
class Record:
    tag: str
    kind: str = "struct"
    members: tuple = ()
    promoted_members: tuple = ()
    size: int = 0
    alignment: int = 1
    complete: bool = False


@dataclass(frozen=True, init=False)
class Type:
    kind: str
    width: int
    signed: bool
    base: "Type | None"
    count: int
    params: tuple
    record: Record | None
    qualifiers: frozenset[str]
    bound: Any = field(default=None, compare=False)

    def __init__(
        self,
        kind="int",
        size=4,
        signed=True,
        base=None,
        count=0,
        params=(),
        record=None,
        qualifiers=(),
        bound=None,
    ):
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "width", size)
        object.__setattr__(self, "signed", signed)
        object.__setattr__(self, "base", base)
        object.__setattr__(self, "count", count)
        object.__setattr__(self, "params", tuple(params))
        object.__setattr__(self, "record", record)
        object.__setattr__(self, "qualifiers", frozenset(qualifiers))
        object.__setattr__(self, "bound", bound)

    @property
    def size(self):
        return self.record.size if self.kind in ("struct", "union") else self.width

    @property
    def align(self):
        if self.kind in ("array", "vla"):
            return self.base.align
        if self.kind in ("struct", "union"):
            return self.record.alignment
        return min(max(self.size, 1), 4)

    @property
    def integer(self):
        return self.kind in ("int", "bool")

    def decay(self):
        return (
            pointer(self.base)
            if self.kind in ("array", "vla")
            else pointer(self) if self.kind == "function" else self
        )

    def promote(self):
        return INT if self.integer and self.size < 4 else self.decay()

    def qualified(self, *names):
        return Type(
            self.kind,
            self.width,
            self.signed,
            self.base,
            self.count,
            self.params,
            self.record,
            self.qualifiers | set(names),
            self.bound,
        )

    def unqualified(self):
        return Type(
            self.kind,
            self.width,
            self.signed,
            self.base,
            self.count,
            self.params,
            self.record,
            bound=self.bound,
        )

    def __str__(self):
        prefix = "const " if "const" in self.qualifiers else ""
        if self.kind == "pointer":
            return f"{prefix}{self.base}*"
        if self.kind == "array":
            return f"{prefix}{self.base}[{self.count}]"
        if self.kind == "vla":
            return f"{prefix}{self.base}[*]"
        if self.kind == "function":
            return f'{prefix}{self.base}({", ".join(map(str, self.params))})'
        if self.kind in ("struct", "union"):
            return f"{prefix}{self.kind} {self.record.tag or '<anonymous>'}"
        if self.kind == "void":
            return f"{prefix}void"
        if self.kind == "bool":
            return f"{prefix}_Bool"
        return f'{prefix}{"i" if self.signed else "u"}{self.size * 8}'


INT = Type()
UINT = Type(signed=False)
CHAR = Type(size=1, signed=False)
BOOL = Type("bool", size=1, signed=False)
VOID = Type("void", 0, False)


def pointer(base):
    return Type("pointer", 4, False, base)


def array(base, count):
    return Type("array", base.size * count, False, base, count)


def vla(base, bound=None):
    """A runtime-sized array; ``bound`` is a saved declaration-time value."""
    return Type("vla", 0, False, base, bound=bound)


def common(a, b):
    a, b = a.promote(), b.promote()
    if not a.integer or not b.integer:
        raise CompileError("integer operands required")
    return UINT if not a.signed or not b.signed else INT


@dataclass
class Symbol:
    name: str
    type: Type
    storage: str  # global, function, local, parameter
    key: str
    constant: int | None = None


@dataclass
class Node:
    op: str
    type: Type = VOID
    children: list["Node"] = field(default_factory=list)
    value: Any = None
    location: str = ""
    lvalue: bool = False


@dataclass
class Global:
    symbol: Symbol
    data: bytearray
    relocations: list[tuple[int, str, int]] = field(default_factory=list)
    section: str = "data"  # rodata, data, zero candidate, or bss


@dataclass
class Function:
    symbol: Symbol
    params: list[Symbol]
    locals: list[Symbol]
    body: Node


@dataclass
class Program:
    globals: list[Global]
    functions: list[Function]
    symbols: dict[str, Symbol] = field(default_factory=dict)


def align_up(value, alignment):
    return (value + alignment - 1) // alignment * alignment
