#!/usr/bin/env python3
"""Real assembler for GCC's Symphony `.s` output (gcc/config/symphony/symphony.md).

Unlike gcc-backend/tools/gcc_assembler.py (the older moxie-hijack single-file
assembler for a different, Dynphony-flavoured `.s` dialect with no object-file/
relocation concept at all), this assembler supports genuine separate
compilation: each translation unit becomes its own relocatable object (see
symphony_obj.py for the format), with undefined symbols left as relocations
for symphony_ld.py to resolve when linking multiple objects (a user program,
libgcc.a, and eventually runtime/libc.a) into one flat Symphony/Dynphony
binary. This is what stands in for `as` when GCC's own build (including
libgcc's stage1->libgcc->stage2 bootstrap) invokes `as -o out.o in.s`.

Encoding is delegated to symphony/targets/symphony/isa.py + registers.py --
the exact same byte-level encoder dyncc's own backend uses -- so GCC-emitted
code and dyncc-emitted code are byte-for-byte identical for the same
mnemonic/operand shape.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from symphony.targets.symphony import isa
from symphony.targets.symphony.registers import parse_register, Register

from symphony_obj import ObjectFile, Reloc, Symbol

COND_JUMP = {
    "je": "je", "jne": "jne", "jb": "jb", "jae": "jae", "jbe": "jbe",
    "ja": "ja", "jl": "jl", "jge": "jge", "jle": "jle", "jg": "jg",
}


def pad_fixed_width(data):
    """Symphony pads every raw sub-instruction to 4 bytes; mirrors
    Assembler.emit in symphony/targets/symphony/assembler.py exactly, so
    GCC-driven and dyncc-driven code share identical instruction boundaries.
    """
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
        if len(instruction) != size:
            raise ValueError("truncated instruction while padding Symphony")
        out.extend(instruction)
        out.extend(bytes(4 - size))
        position += size
    return bytes(out)


INSN_SLOT = 4  # every Symphony sub-instruction occupies exactly 4 bytes
LINK_CALL_SLOTS = 3    # counter, add-immediate, jmp -> 12 bytes
LA_SLOTS = 3            # mov-hi16, lsl, or-lo16 -> 12 bytes (isa.constant)


def parse_register_operand(tok):
    return parse_register(tok.strip())


def parse_mem_operand(tok):
    """`[reg]` -> Register."""
    tok = tok.strip()
    m = re.fullmatch(r"\[\s*([A-Za-z0-9]+)\s*\]", tok)
    if not m:
        raise ValueError(f"expected [reg] memory operand, got {tok!r}")
    return parse_register(m.group(1))


def parse_int_or_symbol(tok):
    tok = tok.strip()
    try:
        return int(tok, 0)
    except ValueError:
        pass
    if tok.startswith("-"):
        try:
            return int(tok, 0)
        except ValueError:
            pass
    return tok  # symbol name


class Assembler:
    """Two-pass assembler: pass 1 lays out sizes/labels, pass 2 encodes and
    records relocations for anything referencing a symbol not yet known to
    be a fixed numeric value within this translation unit."""

    def __init__(self, unit_name):
        self.unit_name = unit_name
        self.sections = {"text": bytearray(), "data": bytearray(), "bss_size": 0}
        self.cur_section = "text"
        self.labels = {}       # name -> (section, offset)
        self.globals = set()
        self.pending_bss = []  # (name, size, align) emitted while in .bss
        self.relocs = []       # (section, offset, symbol, addend, kind)
        self._local_counter = 0

    # ---- directive/line parsing -------------------------------------

    @staticmethod
    def split_line(raw):
        line = raw.split("#", 1)[0].rstrip()
        line = line.strip()
        return line

    def assemble(self, text):
        # Pass 1: compute instruction sizes and section layout, recording
        # label offsets as we go (labels must be known before pass 2 can
        # decide immediate-vs-materialized forms for backward refs, and
        # ARE needed for forward refs too -- since this is single-unit
        # layout, not whole-program layout, any symbol not defined in this
        # unit becomes a relocation in pass 2 regardless).
        parsed_lines = []
        self.cur_section = "text"
        offsets = {"text": 0, "data": 0}
        for raw in text.splitlines():
            line = self.split_line(raw)
            if not line:
                continue
            label_match = re.match(r"^([A-Za-z_.$][\w.$]*):\s*$", line)
            if label_match:
                name = label_match.group(1)
                if self.cur_section == "bss":
                    self._pending_bss_label = name
                else:
                    self.labels[name] = (self.cur_section, offsets[self.cur_section])
                parsed_lines.append(("label", name))
                continue
            if line.startswith("."):
                self._handle_directive(line, offsets, parsed_lines)
                continue
            # instruction
            mnem, operands = self._split_insn(line)
            size = self._insn_size(mnem, operands)
            parsed_lines.append(("insn", mnem, operands, size))
            offsets[self.cur_section] += size

        # Pass 2: encode, emitting relocations for any symbol not defined
        # in this translation unit (checked against self.labels).
        self.cur_section = "text"
        cursors = {"text": 0, "data": 0}
        for entry in parsed_lines:
            if entry[0] == "label":
                continue
            if entry[0] == "section":
                self.cur_section = entry[1]
                continue
            _, mnem, operands, size = entry
            encoded = self._encode_insn(mnem, operands, cursors[self.cur_section])
            assert len(encoded) == size, (mnem, operands, len(encoded), size)
            self.sections[self.cur_section].extend(encoded)
            cursors[self.cur_section] += size

    def _split_insn(self, line):
        parts = line.split(None, 1)
        mnem = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        operands = [o.strip() for o in rest.split(",")] if rest else []
        return mnem, operands

    def _handle_directive(self, line, offsets, parsed_lines):
        parts = line.split(None, 1)
        name = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        args = [a.strip() for a in rest.split(",")] if rest else []

        if name == ".text":
            self.cur_section = "text"
            parsed_lines.append(("section", "text"))
            return
        if name == ".data":
            self.cur_section = "data"
            parsed_lines.append(("section", "data"))
            return
        if name == ".bss":
            self.cur_section = "bss"
            self._pending_bss_label = None
            return
        if name == ".section":
            # ELF-style `.section NAME,"flags",@type` -- only NAME matters
            # here (no real ELF section semantics for this target). Bucket
            # by conventional prefix.
            sect_name = args[0].strip('"') if args else ".text"
            if sect_name.startswith(".bss"):
                self.cur_section = "bss"
                self._pending_bss_label = None
            elif sect_name.startswith(".data"):
                self.cur_section = "data"
                parsed_lines.append(("section", "data"))
            else:
                # .rodata, .text.*, etc: fold into data/text respectively.
                if sect_name.startswith(".text"):
                    self.cur_section = "text"
                    parsed_lines.append(("section", "text"))
                else:
                    self.cur_section = "data"
                    parsed_lines.append(("section", "data"))
            return
        if name == ".global" or name == ".globl":
            self.globals.add(args[0])
            return
        if name in (".p2align", ".align", ".balign"):
            align = int(args[0], 0)
            if name == ".p2align":
                align = 1 << align
            if self.cur_section == "bss":
                return  # bss alignment handled by the linker via symbol align
            cur = offsets[self.cur_section]
            pad = (-cur) % align
            if pad:
                self.sections[self.cur_section].extend(bytes(pad))
                offsets[self.cur_section] += pad
            return
        if name == ".zero" or name == ".skip":
            size = int(args[0], 0)
            if self.cur_section == "bss":
                label = getattr(self, "_pending_bss_label", None)
                if label:
                    self.pending_bss.append((label, size))
                    self._pending_bss_label = None
                else:
                    self.sections["bss_size"] += size
                return
            self.sections[self.cur_section].extend(bytes(size))
            offsets[self.cur_section] += size
            return
        if name in (".byte", ".2byte", ".short", ".hword", ".4byte", ".long", ".word"):
            width = {".byte": 1, ".2byte": 2, ".short": 2, ".hword": 2,
                      ".4byte": 4, ".long": 4, ".word": 4}[name]
            for a in args:
                v = parse_int_or_symbol(a)
                if isinstance(v, int):
                    # Symphony is big-endian throughout (isa.py's u16()
                    # uses to_bytes(2, "big"), and Machine.read() in
                    # symphony/emulator/machine.py assembles multi-byte
                    # memory reads big-endian) -- data directives must
                    # match, or GCC-initialized globals silently read back
                    # byte-swapped.
                    self.sections[self.cur_section].extend(
                        (v & ((1 << (8 * width)) - 1)).to_bytes(width, "big"))
                else:
                    off = offsets[self.cur_section]
                    self.relocs.append((self.cur_section, off, v, 0, f"data{width}", 0))
                    self.sections[self.cur_section].extend(bytes(width))
                offsets[self.cur_section] += width
            return
        # Unrecognized directive (e.g. .file, .ident, .size, .type,
        # .cfi_*): safe to ignore -- no debug info, no ELF metadata.

    # ---- instruction sizing -------------------------------------------

    def _insn_size(self, mnem, operands):
        if mnem == "link_call":
            return INSN_SLOT * LINK_CALL_SLOTS
        if mnem == "link_return":
            return INSN_SLOT
        if mnem == "la":
            return INSN_SLOT * LA_SLOTS
        if mnem in ("push", "pop"):
            return INSN_SLOT * 2  # sub/add sp,4 + store/load
        if mnem == "cmp":
            return INSN_SLOT
        if mnem in COND_JUMP or mnem == "jmp":
            return INSN_SLOT
        if mnem in ("store_32", "store_16", "store_8", "load_32", "load_16", "load_8"):
            return INSN_SLOT
        if mnem == "nop":
            return INSN_SLOT
        if mnem == "mov":
            return INSN_SLOT
        # ALU 3-operand forms: add/sub/and/or/xor/lsl/lsr/asr/nor/nand
        return INSN_SLOT

    # ---- instruction encoding -------------------------------------------

    def _resolve_or_reloc(self, tok, section, offset, kind, addend=0):
        """Return an int if resolvable now (numeric literal or defined-in-unit
        label whose value we already know from pass 1), else record a
        relocation with placeholder 0 and return 0."""
        v = parse_int_or_symbol(tok)
        if isinstance(v, int):
            return v
        # Whether v is a same-unit label or a genuinely external symbol,
        # always emit a relocation -- the linker resolves ALL symbols
        # (local labels included) uniformly once it has assigned final
        # section base addresses.
        self.relocs.append((section, offset, v, addend, kind, 0))
        return 0

    def _encode_insn(self, mnem, operands, offset):
        section = self.cur_section

        if mnem == "mov":
            d, s = operands
            dr = parse_register_operand(d)
            try:
                sr = parse_register_operand(s)
                return pad_fixed_width(isa.mov(dr, sr))
            except ValueError:
                imm = parse_int_or_symbol(s)
                if isinstance(imm, int):
                    if imm < 0:
                        imm &= 0xFFFF
                    return pad_fixed_width(isa.mov(dr, imm, True))
                raise ValueError(f"mov with unresolved symbol operand not supported: {s}")

        alu_names = {"add", "sub", "and", "or", "xor", "lsl", "lsr", "asr", "nor", "nand"}
        if mnem in alu_names:
            d, a, b = operands
            dr = parse_register_operand(d)
            ar = parse_register_operand(a)
            try:
                br = parse_register_operand(b)
                return pad_fixed_width(isa.alu(mnem, dr, ar, br, False))
            except ValueError:
                imm = parse_int_or_symbol(b)
                if not isinstance(imm, int):
                    raise ValueError(f"ALU immediate operand must be a literal: {b}")
                if imm < 0:
                    imm &= 0xFFFF
                return pad_fixed_width(isa.alu(mnem, dr, ar, imm, True))

        if mnem == "cmp":
            a, b = operands
            ar = parse_register_operand(a)
            try:
                br = parse_register_operand(b)
                return pad_fixed_width(isa.alu("cmp", Register.FLAGS, ar, br, False))
            except ValueError:
                imm = parse_int_or_symbol(b)
                if imm < 0:
                    imm &= 0xFFFF
                return pad_fixed_width(isa.alu("cmp", Register.FLAGS, ar, imm, True))

        if mnem == "jmp" or mnem in COND_JUMP:
            (t,) = operands
            name = "jmp" if mnem == "jmp" else COND_JUMP[mnem]
            try:
                tr = parse_register_operand(t)
                return pad_fixed_width(isa.jump(name, tr))
            except ValueError:
                val = self._resolve_or_reloc(t, section, offset, "jump_u16")
                return pad_fixed_width(isa.jump(name, val & 0xFFFF, True))

        if mnem in ("store_32", "store_16", "store_8"):
            size = {"store_32": 4, "store_16": 2, "store_8": 1}[mnem]
            mem, val = operands
            addr_reg = parse_mem_operand(mem)
            val_reg = parse_register_operand(val)
            return pad_fixed_width(isa.store(size, addr_reg, val_reg))

        if mnem in ("load_32", "load_16", "load_8"):
            size = {"load_32": 4, "load_16": 2, "load_8": 1}[mnem]
            dst, mem = operands
            dst_reg = parse_register_operand(dst)
            addr_reg = parse_mem_operand(mem)
            return pad_fixed_width(isa.load(size, dst_reg, addr_reg))

        if mnem == "push":
            (r,) = operands
            return pad_fixed_width(isa.push(parse_register_operand(r)))

        if mnem == "pop":
            (r,) = operands
            return pad_fixed_width(isa.pop(parse_register_operand(r)))

        if mnem == "nop":
            return pad_fixed_width(isa.mov(Register.ZR, Register.ZR))

        if mnem == "link_return":
            return pad_fixed_width(isa.link_return())

        if mnem == "link_call":
            (t,) = operands
            try:
                tr = parse_register_operand(t)
                return pad_fixed_width(isa.link_call(tr, return_offset=12))
            except ValueError:
                # Symbol/label target: counter + add-immediate + jmp-immediate,
                # matching symphony/targets/symphony/assembler.py's own
                # fixed_instruction_width return_offset=12. The jmp's target
                # is a relocation resolved at link time (jump_u16 kind: the
                # linker will need the symbol to land in U16 range, or we'd
                # need a materialized-address call form -- see note in
                # symphony_ld.py about this known limitation).
                counter_bytes = pad_fixed_width(isa.counter(Register.R13))
                add_bytes = pad_fixed_width(
                    isa.alu("add", Register.R13, Register.R13, 12, True))
                jmp_offset = offset + len(counter_bytes) + len(add_bytes)
                val = self._resolve_or_reloc(t, section, jmp_offset, "jump_u16")
                jmp_bytes = pad_fixed_width(isa.jump("jmp", val & 0xFFFF, True))
                return counter_bytes + add_bytes + jmp_bytes

        if mnem == "la":
            d, s = operands
            dr = parse_register_operand(d)
            # isa.constant() always emits exactly 3 sub-instructions
            # (mov-hi16, lsl, or-lo16), independent of the value -- so we
            # can encode a correctly-shaped placeholder now and record one
            # relocation covering the whole 12-byte sequence; the linker
            # rewrites it in place once the symbol's address is known
            # (see symphony_ld.py's "abs32_la" reloc handling).
            self.relocs.append((section, offset, s, 0, "abs32_la", int(dr)))
            return pad_fixed_width(isa.constant(dr, 0))

        raise ValueError(f"unsupported Symphony GCC mnemonic: {mnem} {operands}")

    # ---- object emission -------------------------------------------

    def to_object(self):
        obj = ObjectFile(unit_name=self.unit_name)
        obj.text = bytes(self.sections["text"])
        obj.data = bytes(self.sections["data"])
        bss_total = self.sections["bss_size"]
        bss_offset = 0
        bss_symbol_offsets = {}
        for label, size in self.pending_bss:
            bss_symbol_offsets[label] = bss_offset
            bss_offset += size
        bss_total += bss_offset
        obj.bss_size = bss_total

        for name, (sect, off) in self.labels.items():
            obj.symbols.append(Symbol(name=name, section=sect, offset=off,
                                        global_=(name in self.globals)))
        for name, off in bss_symbol_offsets.items():
            obj.symbols.append(Symbol(name=name, section="bss", offset=off,
                                        global_=(name in self.globals)))

        defined = {s.name for s in obj.symbols}
        for section, off, sym, addend, kind, reg in self.relocs:
            obj.relocs.append(Reloc(section=section, offset=off, symbol=sym,
                                      addend=addend, kind=kind, reg=reg))
            if sym not in defined:
                obj.undefined.add(sym)
        return obj


def assemble_file(path):
    text = Path(path).read_text()
    unit_name = Path(path).stem
    asm = Assembler(unit_name)
    asm.assemble(text)
    return asm.to_object()


def main(argv):
    # GNU-as-compatible enough calling convention for GCC's own driver:
    # `as -o OUT.o IN.s` (possibly with other flags GCC passes that we
    # can safely ignore, e.g. --traditional-format).
    out_path = None
    in_path = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-o":
            out_path = argv[i + 1]
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        in_path = a
        i += 1
    if in_path is None or out_path is None:
        print("usage: symphony_as.py [-o OUT.o] IN.s", file=sys.stderr)
        return 1
    obj = assemble_file(in_path)
    obj.save(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
