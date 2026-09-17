#!/usr/bin/env python3
"""Linker for the real Symphony GCC toolchain: combines symphony_obj.py
objects (and members pulled from `ar` archives, e.g. libgcc.a) into one
flat Symphony/Dynphony binary matching the layout dyncc's own backend
already produces on this repo's emulator (symphony/emulator).

Standard archive-linking semantics: an archive member is pulled in only if
it defines a symbol that is still undefined among objects already
included, applied to a fixed point (pulling in a member can introduce new
undefined symbols, e.g. __divsi3 needing __udivsi3).

Layout: .text sections first (concatenated in link order), then .data,
then .bss (space reserved, zero-filled at load by the emulator's own
convention -- consistent with dyncc's own flat-image output). All symbol
addresses are absolute byte offsets from a configurable load_address
(default 0, matching dyncc's default).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from symphony_obj import ObjectFile
from symphony_ar import read_archive

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from symphony.targets.symphony import isa
from symphony.targets.symphony.registers import Register


def pad_fixed_width(data):
    data = bytes(data)
    out = bytearray()
    position = 0
    while position < len(data):
        opcode = data[position]
        if opcode in (0, 8):
            size = 1
        elif opcode in (1, 3, 5, 6, 7):
            size = 2
        elif opcode in (2, 4) or 0x20 <= opcode <= 0x77:
            size = 4 if opcode & 0x10 else 3
        elif opcode in (0x12, 0x14):
            size = 4
        else:
            raise ValueError(f"cannot pad unknown Symphony opcode {opcode:#x}")
        instruction = data[position:position + size]
        out.extend(instruction)
        out.extend(bytes(4 - size))
        position += size
    return bytes(out)


class Linker:
    def __init__(self, load_address=0):
        self.load_address = load_address
        self.objects = []  # list of ObjectFile actually included

    def add_object(self, obj):
        self.objects.append(obj)

    def add_archive(self, path):
        """Pull in only the members currently needed (standard archive
        semantics), iterating to a fixed point since a pulled-in member can
        introduce further undefined symbols."""
        members = read_archive(path)
        parsed = []
        for name, payload in members:
            try:
                import json
                obj = ObjectFile.from_dict(json.loads(payload.decode()))
            except Exception:
                continue  # not one of our objects (e.g. the ranlib symbol index)
            parsed.append(obj)

        changed = True
        included_names = set()
        while changed:
            changed = False
            defined = self._defined_symbols()
            needed = self._undefined_symbols() - defined
            for obj in parsed:
                if obj.unit_name in included_names:
                    continue
                obj_defs = {s.name for s in obj.symbols if s.global_}
                if obj_defs & needed:
                    self.objects.append(obj)
                    included_names.add(obj.unit_name)
                    changed = True

    def _defined_symbols(self):
        out = set()
        for obj in self.objects:
            out |= {s.name for s in obj.symbols if s.global_}
        return out

    def _undefined_symbols(self):
        out = set()
        for obj in self.objects:
            out |= obj.undefined
        return out

    def link(self, entry_symbol=None):
        # Lay out sections: all .text first, then .data, then .bss.
        text_base = {}
        data_base = {}
        bss_base = {}
        text_cursor = 0
        for obj in self.objects:
            text_base[id(obj)] = text_cursor
            text_cursor += len(obj.text)
        data_cursor = text_cursor
        for obj in self.objects:
            data_base[id(obj)] = data_cursor
            data_cursor += len(obj.data)
        bss_cursor = data_cursor
        for obj in self.objects:
            bss_base[id(obj)] = bss_cursor
            bss_cursor += obj.bss_size

        symtab = {}
        for obj in self.objects:
            base = {"text": text_base[id(obj)], "data": data_base[id(obj)],
                    "bss": bss_base[id(obj)]}
            for sym in obj.symbols:
                addr = self.load_address + base[sym.section] + sym.offset
                if sym.global_:
                    if sym.name in symtab and symtab[sym.name] != addr:
                        raise ValueError(f"duplicate global symbol: {sym.name}")
                    symtab[sym.name] = addr
                else:
                    # Local labels: still need addresses for this object's
                    # own relocations, but must not collide across objects
                    # -- namespace by unit.
                    symtab[f"{obj.unit_name}::{sym.name}"] = addr

        image = bytearray(bss_cursor)
        for obj in self.objects:
            image[text_base[id(obj)]:text_base[id(obj)] + len(obj.text)] = obj.text
        for obj in self.objects:
            off = data_base[id(obj)]
            image[off:off + len(obj.data)] = obj.data

        for obj in self.objects:
            base = {"text": text_base[id(obj)], "data": data_base[id(obj)],
                    "bss": bss_base[id(obj)]}
            local_names = {s.name for s in obj.symbols if not s.global_}
            for reloc in obj.relocs:
                lookup = (f"{obj.unit_name}::{reloc.symbol}"
                          if reloc.symbol in local_names else reloc.symbol)
                if lookup not in symtab:
                    raise ValueError(
                        f"{obj.unit_name}: undefined symbol '{reloc.symbol}'")
                target = symtab[lookup] + reloc.addend
                site = base[reloc.section] + reloc.offset
                self._apply_reloc(image, site, target, reloc.kind, obj.unit_name,
                                   reloc.symbol, reloc.reg)

        entry_addr = None
        if entry_symbol is not None:
            if entry_symbol not in symtab:
                raise ValueError(f"undefined entry symbol: {entry_symbol}")
            entry_addr = symtab[entry_symbol]
        return bytes(image), symtab, entry_addr

    @staticmethod
    def _apply_reloc(image, site, target, kind, unit_name, symbol, reg=0):
        if kind == "jump_u16":
            if not (0 <= target <= 0xFFFF):
                raise ValueError(
                    f"{unit_name}: relocation to '{symbol}' (address {target:#x}) "
                    "does not fit the U16 immediate jmp/link_call form -- "
                    "a materialized-address call sequence is needed for "
                    "programs whose code exceeds 64KiB (not yet implemented "
                    "in symphony_ld.py; fine for milestone-4-scale tests)")
            image[site + 2:site + 4] = target.to_bytes(2, "big")
            return
        if kind == "abs32_la":
            # Re-encode isa.constant() with the ACTUAL destination register
            # (recorded on the relocation by the assembler) and the now-
            # known target address -- isa.constant() always emits exactly
            # 3 sub-instructions regardless of value, so this is a
            # byte-for-byte drop-in replacement for the placeholder
            # sequence the assembler emitted at this site.
            encoded = pad_fixed_width(isa.constant(Register(reg), target))
            image[site:site + len(encoded)] = encoded
            return
        if kind in ("data1", "data2", "data4"):
            width = int(kind[-1])
            # Big-endian, matching symphony_as.py's data-directive encoding
            # and Machine.read()'s big-endian multi-byte memory reads.
            image[site:site + width] = (target & ((1 << (8 * width)) - 1)).to_bytes(
                width, "big")
            return
        raise ValueError(f"unknown relocation kind: {kind}")


def main(argv):
    out_path = None
    entry = None
    inputs = []
    archives = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-o":
            out_path = argv[i + 1]
            i += 2
            continue
        if a == "--entry":
            entry = argv[i + 1]
            i += 2
            continue
        if a.endswith(".a"):
            archives.append(a)
        else:
            inputs.append(a)
        i += 1

    linker = Linker()
    for p in inputs:
        linker.add_object(ObjectFile.load(p))
    for p in archives:
        linker.add_archive(p)

    image, symtab, entry_addr = linker.link(entry_symbol=entry)
    Path(out_path).write_bytes(image)
    print(f"linked {len(image)} bytes -> {out_path}")
    if entry is not None:
        print(f"entry {entry} @ {entry_addr:#x}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
