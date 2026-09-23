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


FAR_STUB_BYTES = 16


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


def encode_width(data, fixed_width):
    """Same opcode-to-size mapping as symphony_as.py's encode_width -- pad
    to 4 bytes per sub-instruction for Symphony, or emit raw variable-length
    bytes for Dynphony. Used by _apply_reloc's abs32_la case, which
    re-encodes isa.constant() in place at link time and must produce
    exactly as many bytes as the assembler originally reserved for the
    placeholder it is overwriting."""
    return pad_fixed_width(data) if fixed_width else bytes(data)


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

    def _check_consistent_width(self):
        """All linked objects must share the same Symphony-vs-Dynphony
        encoding -- symphony_as.py records each object's mode (see
        ObjectFile.fixed_width) from whether -mdynphony was passed when it
        was assembled. Mixing the two would silently misinterpret byte
        boundaries (Symphony expects every sub-instruction padded to a
        4-byte slot; Dynphony packs them back-to-back), so this is checked
        explicitly rather than left to manifest as a mysterious runtime
        crash. Returns the single shared mode."""
        widths = {obj.unit_name: obj.fixed_width for obj in self.objects}
        modes = set(widths.values())
        if len(modes) > 1:
            symphony_objs = sorted(n for n, w in widths.items() if w)
            dynphony_objs = sorted(n for n, w in widths.items() if not w)
            raise ValueError(
                "cannot link a mix of Symphony (fixed-width) and Dynphony "
                "(variable-length) objects in one image -- Symphony: "
                f"{symphony_objs}, Dynphony: {dynphony_objs}")
        return modes.pop() if modes else True

    def link(self, entry_symbol=None):
        fixed_width = self._check_consistent_width()
        # A U16 branch cannot be expanded in place without either clobbering
        # the condition flags or changing a link_call's return address.  Put
        # far-target trampolines before all user text instead: the original
        # branch/call remains unchanged and jumps to its low-address stub;
        # the stub materializes the final 32-bit destination in flags and
        # jumps there.  The number of stubs changes code addresses, so solve
        # the monotone layout decision to a fixed point.
        if not fixed_width:
            far_stubs = set()
        else:
            far_stubs = set()
            while True:
                layout = self._layout(len(far_stubs) * FAR_STUB_BYTES)
                symtab = self._symbol_table(layout)
                newly_far = set()
                for obj in self.objects:
                    for reloc in obj.relocs:
                        if reloc.kind != "jump_u16":
                            continue
                        lookup = self._lookup_name(obj, reloc)
                        # Preserve the linker's normal undefined-symbol
                        # diagnostic; the relocation pass below reports it.
                        if lookup not in symtab:
                            continue
                        if symtab[lookup] + reloc.addend > 0xFFFF:
                            newly_far.add((lookup, reloc.addend))
                if newly_far <= far_stubs:
                    break
                far_stubs |= newly_far
        layout = self._layout(len(far_stubs) * FAR_STUB_BYTES)
        symtab = self._symbol_table(layout)
        # Lay out sections: all .text first, then .data, then .bss.
        text_base, data_base, bss_base, text_cursor, data_cursor, bss_cursor = layout
        self.load_size = data_cursor
        stub_addresses = {
            key: self.load_address + index * FAR_STUB_BYTES
            for index, key in enumerate(sorted(far_stubs))
        }
        if stub_addresses and max(stub_addresses.values()) > 0xFFFF:
            raise ValueError("far-branch trampoline area exceeds U16 range")

        # Synthesize a genuine end-of-image marker: the address right after
        # every object's .text/.data/.bss has been laid out, consuming no
        # bytes of its own. runtime/heap.c's bump allocator needs a real
        # "nothing statically allocated lives past this point" address to
        # start growing the heap from -- it used to rely on its own
        # __dyn_heap_anchor[7] BSS array being the last symbol in the whole
        # linked image, which only held by accident of link order and broke
        # (silently corrupting whatever global happened to land right after
        # it in BSS) as soon as any other object's BSS was placed after
        # heap.o. bss_cursor here is exactly the fully-general answer: it is
        # computed after ALL objects' sections are accounted for, so it is
        # correct regardless of link/object order.
        #
        # Only synthesize it if no object already defines it as a real
        # symbol -- an object built against the OLD heap.c (which defined
        # __dyn_heap_anchor as actual 7-byte BSS storage) must fail loudly
        # here rather than silently linking against a stale .o whose BSS
        # layout assumption this fix specifically removes. Rebuild that
        # object from the current runtime/heap.c instead of trying to link
        # it as-is.
        if "__dyn_heap_anchor" in symtab:
            raise ValueError(
                "__dyn_heap_anchor is defined as a real symbol by an input "
                "object (stale build against the old heap.c, which reserved "
                "BSS storage for it) -- the linker now synthesizes this "
                "symbol itself as a zero-size end-of-image marker; rebuild "
                "that object from the current runtime/heap.c")
        symtab["__dyn_heap_anchor"] = self.load_address + bss_cursor

        image = bytearray(bss_cursor)
        for key, address in stub_addresses.items():
            target = symtab[key[0]] + key[1]
            stub = (
                encode_width(isa.constant(Register.FLAGS, target), True)
                + encode_width(isa.jump("jmp", Register.FLAGS), True)
            )
            assert len(stub) == FAR_STUB_BYTES
            start = address - self.load_address
            image[start:start + FAR_STUB_BYTES] = stub
        for obj in self.objects:
            image[text_base[id(obj)]:text_base[id(obj)] + len(obj.text)] = obj.text
        for obj in self.objects:
            off = data_base[id(obj)]
            image[off:off + len(obj.data)] = obj.data

        for obj in self.objects:
            base = {"text": text_base[id(obj)], "data": data_base[id(obj)],
                    "bss": bss_base[id(obj)]}
            for reloc in obj.relocs:
                lookup = self._lookup_name(obj, reloc)
                if lookup not in symtab:
                    raise ValueError(
                        f"{obj.unit_name}: undefined symbol '{reloc.symbol}'")
                target = symtab[lookup] + reloc.addend
                if reloc.kind == "jump_u16":
                    target = stub_addresses.get((lookup, reloc.addend), target)
                site = base[reloc.section] + reloc.offset
                self._apply_reloc(image, site, target, reloc.kind, obj.unit_name,
                                   reloc.symbol, reloc.reg, fixed_width)

        entry_addr = None
        if entry_symbol is not None:
            if entry_symbol not in symtab:
                raise ValueError(f"undefined entry symbol: {entry_symbol}")
            entry_addr = symtab[entry_symbol]
        return bytes(image), symtab, entry_addr

    def _layout(self, stub_bytes):
        text_base, data_base, bss_base = {}, {}, {}
        text_cursor = stub_bytes
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
        return text_base, data_base, bss_base, text_cursor, data_cursor, bss_cursor

    def _symbol_table(self, layout):
        text_base, data_base, bss_base, *_ = layout
        symtab = {}
        for obj in self.objects:
            base = {
                "text": text_base[id(obj)],
                "data": data_base[id(obj)],
                "bss": bss_base[id(obj)],
            }
            for sym in obj.symbols:
                addr = self.load_address + base[sym.section] + sym.offset
                name = sym.name if sym.global_ else f"{obj.unit_name}::{sym.name}"
                if sym.global_ and name in symtab and symtab[name] != addr:
                    raise ValueError(f"duplicate global symbol: {sym.name}")
                symtab[name] = addr
        return symtab

    @staticmethod
    def _lookup_name(obj, reloc):
        """Resolve a relocation in the namespace of its own object only.

        Local symbol names are qualified by unit name in the combined table,
        but unit names are not unique in hand-built objects.  Looking merely
        for a qualified name in the global table can therefore bind an
        external relocation to another object's static label.
        """
        if any(not sym.global_ and sym.name == reloc.symbol
               for sym in obj.symbols):
            return f"{obj.unit_name}::{reloc.symbol}"
        return reloc.symbol

    @staticmethod
    def _apply_reloc(image, site, target, kind, unit_name, symbol, reg=0, fixed_width=True):
        if kind == "jump_u16":
            if not (0 <= target <= 0xFFFF):
                raise ValueError(
                    f"{unit_name}: relocation to '{symbol}' (address {target:#x}) "
                    "does not fit the U16 immediate jmp/link_call form -- "
                    "a materialized-address call sequence is needed for "
                    "programs whose code exceeds 64KiB (not yet implemented "
                    "in symphony_ld.py; fine for milestone-4-scale tests)")
            # The U16 immediate always occupies the last 2 bytes of the
            # 4-byte immediate-jump encoding (isa.jump(..., immediate=True)
            # is [opcode|0x10, 15] + u16(target), 4 bytes total) whether or
            # not that 4-byte form itself gets padded further -- it never
            # does, since it's already exactly 4 bytes raw. Same byte
            # offset in both Symphony and Dynphony, no mode branch needed.
            image[site + 2:site + 4] = target.to_bytes(2, "big")
            return
        if kind == "abs32_la":
            # Re-encode isa.constant() with the ACTUAL destination register
            # (recorded on the relocation by the assembler) and the now-
            # known target address -- isa.constant() always emits exactly
            # 3 sub-instructions regardless of value, so this is a
            # byte-for-byte drop-in replacement for the placeholder
            # sequence the assembler emitted at this site (padded to 12
            # bytes for Symphony, unpadded variable length -- but always
            # the same length isa.constant() itself produces -- for
            # Dynphony, matching whichever mode the assembler used to size
            # and reserve this site in the first place).
            encoded = encode_width(isa.constant(Register(reg), target), fixed_width)
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
