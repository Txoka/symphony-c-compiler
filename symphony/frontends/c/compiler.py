"""C-specific source preparation and lowering into common IR."""

import re

from pycparser import c_ast

from ..protocol import FrontendResult
from ...middle.ir import lower
from ...middle.model import INT, CompileError, Program
from ...runtime import (
    NAMES as INTRINSIC_NAMES,
    PROTOTYPES,
    LIBRARY_PROTOTYPES,
    SCREEN_SOURCE,
    SOURCE,
    TEXT_SCREEN_NAMES,
)
from .frontend import typecheck
from .parser import parse, strip_comments
from .preprocessor import Preprocessor


def readonly_object(type_):
    """Whether the object itself, rather than only a pointed-to value, is const."""
    return "const" in type_.qualifiers or (
        type_.kind == "array" and readonly_object(type_.base)
    )


def assign_static_sections(program):
    """Classify live-independent static storage before IR lowering.

    All-zero, non-relocatable mutable objects are BSS candidates.  The backend
    chooses BSS or serialized DATA after dead-global pruning, using the target
    BSS policy. Pointer and function-address initializers always stay in DATA.
    """
    for global_ in program.globals:
        if global_.section == "rodata" or readonly_object(global_.symbol.type):
            global_.section = "rodata"
        elif not global_.relocations and not any(global_.data):
            global_.section = "zero"
        else:
            global_.section = "data"


def compatible_types(a, b, seen=None):
    """C-compatible cross-unit type comparison, including recursive structs."""
    seen = set() if seen is None else seen
    pair = (id(a), id(b))
    if pair in seen:
        return True
    seen.add(pair)
    if a.kind != b.kind:
        return (
            a.kind in ("array", "vla")
            and b.kind in ("array", "vla")
            and compatible_types(a.base, b.base, seen)
        )
    if a.qualifiers != b.qualifiers:
        return False
    if a.kind in ("int", "bool", "void"):
        return a.size == b.size and a.signed == b.signed
    if a.kind == "pointer":
        return compatible_types(a.base, b.base, seen)
    if a.kind == "array":
        return a.count == b.count and compatible_types(a.base, b.base, seen)
    if a.kind == "vla":
        return compatible_types(a.base, b.base, seen)
    if a.kind == "function":
        return (
            compatible_types(a.base, b.base, seen)
            and len(a.params) == len(b.params)
            and all(
                compatible_types(x, y, seen)
                for x, y in zip(a.params, b.params)
            )
        )
    if a.kind == "struct":
        return (
            a.record.tag == b.record.tag
            and a.size == b.size
            and len(a.record.members) == len(b.record.members)
            and all(
                xn == yn and xo == yo and compatible_types(xt, yt, seen)
                for (xn, xt, xo), (yn, yt, yo) in zip(
                    a.record.members, b.record.members
                )
            )
        )
    return a == b


def link_programs(programs):
    """Resolve independently checked translation units into one typed program."""
    globals_ = []
    functions = []
    symbols = {}
    definitions = {}
    for program in programs:
        for key, symbol in program.symbols.items():
            previous = symbols.get(key)
            if previous is not None and not compatible_types(
                previous.type, symbol.type
            ):
                raise CompileError(f"conflicting declarations of {symbol.name}")
            symbols.setdefault(key, symbol)
        for global_ in program.globals:
            key = global_.symbol.key
            if key in definitions:
                raise CompileError(f"multiple definitions of {global_.symbol.name}")
            definitions[key] = global_.symbol
            globals_.append(global_)
        for function in program.functions:
            key = function.symbol.key
            if key in definitions:
                raise CompileError(f"multiple definitions of {function.symbol.name}")
            definitions[key] = function.symbol
            functions.append(function)
    main = definitions.get("main")
    if main is None or main.storage != "function":
        raise CompileError("a definition of main is required")
    if main.type.params or main.type.base != INT:
        raise CompileError("entry point must be int main(void)")
    return Program(globals_, functions, symbols)


class CFrontend:
    def __init__(self, include_dirs=(), defines=()):
        self.include_dirs = tuple(include_dirs)
        self.defines = tuple(defines)

    def lower(self, source: str, filename: str = "<input>") -> FrontendResult:
        return self.lower_project([(filename, source)])

    def lower_project(self, sources) -> FrontendResult:
        parsed_units = []
        programs = []
        for index, (filename, source) in enumerate(sources):
            if re.search(r"\b__dyn_\w*", strip_comments(source)):
                raise CompileError("identifiers beginning __dyn_ are reserved for the runtime")
            processor = Preprocessor(self.include_dirs, self.defines)
            processed = processor.process(source, filename)
            if re.search(r"\b__dyn_\w*", processed):
                raise CompileError(
                    "identifiers beginning __dyn_ are reserved for the runtime"
                )
            parsed = parse(processed, filename)
            parsed_units.append(parsed)
            programs.append(typecheck(parsed, require_main=False, namespace=str(index)))
        for parsed in parsed_units:
            for item in parsed.ext:
                if isinstance(item, c_ast.FuncDef) and item.decl.name in (
                    INTRINSIC_NAMES | TEXT_SCREEN_NAMES
                ):
                    raise CompileError(
                        f"{item.decl.name} is a reserved device intrinsic"
                    )

        runtime_source = (
            "typedef _Bool bool;\n"
            + PROTOTYPES
            + LIBRARY_PROTOTYPES
            + SOURCE
            + SCREEN_SOURCE
        )
        runtime_tree = parse(runtime_source, "<symphony-runtime>")
        programs.append(typecheck(runtime_tree, require_main=False, namespace="runtime"))
        typed = link_programs(programs)

        assign_static_sections(typed)
        parsed = parsed_units[0] if len(parsed_units) == 1 else parsed_units
        return FrontendResult(parsed, typed, lower(typed))


__all__ = ["CFrontend"]
