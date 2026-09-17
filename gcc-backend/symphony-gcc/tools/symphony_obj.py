"""Minimal relocatable object-file format for the real Symphony GCC toolchain.

Not ELF (deliberately -- see gcc-backend/symphony-gcc/README.md): there is
no OS, no dynamic linking, no need for section flags/alignment metadata
beyond what this target's own assembler+linker actually use. Just enough
structure to support real separate compilation: named sections (text/data/
bss), a symbol table (global vs. local, which section+offset each symbol
lives at), and relocation records so symphony_ld.py can combine many objects
(a user program's .o files, libgcc.a's members, eventually runtime/libc.a)
into one flat binary.

Encoded as JSON for simplicity/inspectability during bring-up (milestone 4);
nothing about the design prevents swapping in a tighter binary encoding
later without changing the assembler/linker's own logic, since all reads/
writes go through this module.
"""
import base64
import json
from dataclasses import dataclass, field


@dataclass
class Symbol:
    name: str
    section: str   # "text" | "data" | "bss"
    offset: int
    global_: bool = False


@dataclass
class Reloc:
    section: str   # section the fixup site lives in
    offset: int    # byte offset within that section
    symbol: str
    addend: int = 0
    # "jump_u16": rewrite a 2-byte U16 immediate field (jmp/link_call's
    #   final jmp sub-instruction) -- target must fit 0..65535 or the
    #   linker raises (see symphony_ld.py).
    # "abs32_la": rewrite a full isa.constant() 3-sub-instruction (12-byte)
    #   sequence in place with the resolved 32-bit address.
    # "data4"/"data2"/"data1": a plain big-endian data-section literal
    #   (from .4byte symbol, .2byte symbol, .byte symbol) -- Symphony is
    #   big-endian throughout (isa.py's u16(), Machine.read()'s multi-byte
    #   memory reads), matching symphony_as.py's data-directive encoding
    #   and symphony_ld.py's _apply_reloc.
    kind: str = "jump_u16"
    reg: int = 0  # destination register, "abs32_la" only


@dataclass
class ObjectFile:
    unit_name: str
    text: bytes = b""
    data: bytes = b""
    bss_size: int = 0
    symbols: list = field(default_factory=list)
    relocs: list = field(default_factory=list)
    undefined: set = field(default_factory=set)
    # True (default): Symphony fixed 4-byte-padded encoding. False: Dynphony
    # variable-length encoding of the identical instruction set (see
    # symphony/targets/symphony/assembler.py's Assembler.emit -- this
    # mirrors target.fixed_instruction_width exactly). Objects assembled in
    # different modes must never be linked together -- symphony_ld.py
    # checks this explicitly rather than silently mixing encodings.
    fixed_width: bool = True

    def to_dict(self):
        return {
            "unit_name": self.unit_name,
            "text": base64.b64encode(self.text).decode(),
            "data": base64.b64encode(self.data).decode(),
            "bss_size": self.bss_size,
            "symbols": [
                {"name": s.name, "section": s.section, "offset": s.offset,
                 "global": s.global_}
                for s in self.symbols
            ],
            "relocs": [
                {"section": r.section, "offset": r.offset, "symbol": r.symbol,
                 "addend": r.addend, "kind": r.kind, "reg": r.reg}
                for r in self.relocs
            ],
            "undefined": sorted(self.undefined),
            "fixed_width": self.fixed_width,
        }

    @classmethod
    def from_dict(cls, d):
        obj = cls(unit_name=d["unit_name"])
        obj.text = base64.b64decode(d["text"])
        obj.data = base64.b64decode(d["data"])
        obj.bss_size = d["bss_size"]
        obj.symbols = [
            Symbol(name=s["name"], section=s["section"], offset=s["offset"],
                   global_=s["global"])
            for s in d["symbols"]
        ]
        obj.relocs = [
            Reloc(section=r["section"], offset=r["offset"], symbol=r["symbol"],
                  addend=r["addend"], kind=r["kind"], reg=r.get("reg", 0))
            for r in d["relocs"]
        ]
        obj.undefined = set(d["undefined"])
        # Default True for older object files saved before this field
        # existed (all pre-existing objects were Symphony fixed-width).
        obj.fixed_width = d.get("fixed_width", True)
        return obj

    def save(self, path):
        with open(path, "w") as f:
            json.dump(self.to_dict(), f)

    @classmethod
    def load(cls, path):
        with open(path) as f:
            return cls.from_dict(json.load(f))
