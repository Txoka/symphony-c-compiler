"""Language/frontend-independent pipeline orchestration and public C shortcut."""

from dataclasses import dataclass
from .frontends.c import CFrontend
from .frontends.protocol import SourceFrontend
from .middle.passes.pipeline import lower_intrinsics
from .middle.ssa import construct, destruct, verify
from .targets.symphony import generate, Target
from .targets.symphony.legalize import legalize_runtime_arithmetic


@dataclass
class Compilation:
    parsed: object
    typed: object
    ir: object
    image: object


class Compiler:
    def __init__(self, frontend: SourceFrontend, target=None):
        self.frontend = frontend
        self.target = target or Target()

    def compile(self, source: str, filename: str = "<input>") -> Compilation:
        frontend = self.frontend.lower(source, filename)
        return self.finish(frontend)

    def compile_project(self, sources) -> Compilation:
        if not hasattr(self.frontend, "lower_project"):
            raise TypeError("this frontend does not support multiple translation units")
        return self.finish(self.frontend.lower_project(sources))

    def finish(self, frontend) -> Compilation:
        ir = frontend.ir
        for function in ir.functions:
            # Required legalization, not optimization: these intrinsics have
            # no C body, only a target-instruction lowering.
            lower_intrinsics(function)
            construct(function)
            verify(function)
            destruct(function)
        legalize_runtime_arithmetic(ir)
        return Compilation(
            frontend.parsed,
            frontend.typed,
            ir,
            generate(ir, self.target),
        )


def compile_source(source, filename="<input>", target=None):
    return Compiler(CFrontend(), target).compile(source, filename)


def compile_sources(sources, target=None, include_dirs=(), defines=()):
    """Compile ``[(filename, source), ...]`` as one linked C program."""
    frontend = CFrontend(include_dirs=include_dirs, defines=defines)
    return Compiler(frontend, target).compile_project(sources)
