;; GCC machine description for Symphony/Dynphony.
;;
;; Real ISA encoding (docs/isa.txt, symphony/targets/symphony/isa.py).
;; This is NOT derived from moxie -- moxie's .md is used only earlier,
;; as a structural example of how a minimal target lays out
;; define_insn/define_expand; every mnemonic, operand form, and
;; encoding choice below comes from this ISA's own tables.
;;
;; ALU instructions have two operand-count forms in the real hardware
;; (docs/isa.txt): a 3-operand register form (`add a, b, c`), a
;; 3-operand immediate form (`add a, b, c:U16`), and destructive
;; 2-operand aliases (`add a, c` meaning `add a, a, c`).  GCC always
;; wants explicit 3-operand SSA-like RTL, so every pattern below emits
;; the explicit 3-operand assembly text; the assembler's encoder picks
;; the identical bytes either way (isa.alu's %a,%b,%c already matches
;; a==b constant-folding at the byte level), so there is no need to
;; special-case the 2-operand alias spelling here.
;;
;; All immediate operands in the ALU/branch immediate forms are U16
;; (0..65535).  A general 32-bit constant costs three instructions:
;;   mov r, hi16   (mov is `or r, zr, imm`, per isa.py)
;;   lsl r, r, 16
;;   or  r, r, lo16
;; symphony_output_move below picks the cheapest of: a single `mov`
;; (value or -value fits U16), or the full 3-instruction sequence.

(define_constants
  [(ZR_REG 0)
   (R7_REG 7)
   (R11_REG 11)
   (R13_REG 13)
   (SP_REG 14)
   (FLAGS_REG 15)])

;; -------------------------------------------------------------------
;; Constraints / predicates
;; -------------------------------------------------------------------

(define_constraint "I"
  "Unsigned 16-bit immediate, the only immediate form the ALU accepts
   directly."
  (and (match_code "const_int")
       (match_test "IN_RANGE (ival, 0, 65535)")))

(define_constraint "J"
  "A 16-bit immediate when negated (so `sub r,zr,-x` can materialize
   it in one instruction, matching isa.py's cheap_constant)."
  (and (match_code "const_int")
       (match_test "IN_RANGE (-ival, 0, 65535) && ival < 0")))

(define_predicate "symphony_u16_operand"
  (match_code "const_int")
{
  return IN_RANGE (INTVAL (op), 0, 65535);
})

;; Second ALU operand: a register, or a U16 immediate (the only
;; immediate the 3-operand register/immediate ALU encoding accepts).
(define_predicate "arith_operand"
  (ior (match_operand 0 "register_operand")
       (and (match_code "const_int")
            (match_test "IN_RANGE (INTVAL (op), 0, 65535)"))))

;; movsi's source operand: a register, a U16 immediate, or memory --
;; deliberately NOT plain "general_operand", which also accepts
;; symbol_ref/label_ref/out-of-range CONST_INT (anything CONSTANT_P).
;; Those must keep going through *movsi_full's 3-instruction
;; materialization sequence instead; letting general_operand widen
;; *movsi_reg's own predicate let recog match *movsi_reg for e.g. a
;; symbol_ref source with no alternative that could actually satisfy
;; it (neither "r" nor "I" accepts a symbol_ref), which is itself a
;; second, narrower way to hit the reload-insn-cap ICE (an insn recog
;; matches structurally but can never be constrained) -- caught by the
;; regression test added alongside this fix.
(define_predicate "movsi_src_operand"
  (ior (match_operand 0 "arith_operand")
       (match_operand 0 "memory_operand")))

;; -------------------------------------------------------------------
;; Moves
;; -------------------------------------------------------------------
;; Register/register and register/small-immediate moves are one `mov`
;; (an alias for `or d, zr, source`, see isa.py's mov()).  A general
;; 32-bit immediate costs three instructions.  Memory operands go
;; through explicit load_32/store_32.

;; The ISA has no store-immediate form (store_32 always stores a
;; register value) and no memory-to-memory move, so movsi is a real
;; define_expand: whenever the destination is memory and the source
;; isn't already a register, force the source into a register first
;; (exactly what a real hardware store instruction requires here).
;; Register-destination moves are left to the define_insn below
;; unchanged (including its own further split for out-of-U16
;; constants).
(define_expand "movsi"
  [(set (match_operand:SI 0 "general_operand" "")
        (match_operand:SI 1 "general_operand" ""))]
  ""
{
  if (MEM_P (operands[0]) && !REG_P (operands[1]))
    operands[1] = force_reg (SImode, operands[1]);
})

;; *movsi_reg carries real "m" (memory) alternatives alongside the
;; register/register and register/immediate ones, deliberately -- NOT
;; split into a separate always-register-only pattern plus standalone
;; *load_si/*store_si patterns the way the very first port of this
;; target had it.  That split was a real, previously-undiagnosed
;; source of the "maximum number of generated reload insns per insn
;; achieved" ICE class (see the reload-ICE investigation notes in
;; README.md's "Known gaps" section): when *movsi_reg's only
;; alternatives are register-to-register, LRA can NEVER present a
;; spilled pseudo's memory location directly to this insn's own
;; constraint matching -- there is no alternative that would accept
;; it.  Instead of reloading the operand in place (an ordinary,
;; convergent operation LRA's constraint machinery is built for), LRA
;; falls back to its generic equivalence/inheritance substitution path
;; -- repeatedly minting a fresh temporary pseudo to stand in for the
;; spilled one -- and under high register pressure (recursive
;; functions, the worst case: every live parameter must additionally
;; survive the function's own call) those fresh pseudos can ALSO fail
;; to get a hard register, so the substitution never terminates and
;; hits the reload-insn retry cap.  Confirmed via gdb: examples/
;; towers_of_hanoi.c's move_pile (plain recursion, 4 live parameters,
;; only 4 callee-saved hard registers) looped forever emitting exactly
;; this shape, `(set (reg N) (reg M))` with M a fresh pseudo each
;; retry, before this fix.
;;
;; The memory operand's address is constrained to a bare register
;; ("r" inside the mem, matching symphony_legitimate_address_p exactly
;; -- this ISA has no base+offset addressing mode at all).  A spill
;; slot's natural address as GCC/LRA constructs it is frame-relative,
;; `(mem (plus (reg r11) (const_int N)))`, which is NOT a legitimate
;; address here -- but that is fine and expected: LRA's own generic
;; process_address_1 (lra-constraints.cc) already knows how to
;; legitimize exactly this shape by materializing the sum into a fresh
;; scratch pseudo via an ADD before the load/store, whenever the insn
;; it's reloading actually HAS a memory alternative for
;; process_address_1 to run against. That's the missing piece this
;; fix supplies -- not a new hook, just letting the existing generic
;; mechanism see the memory operand it was always able to legitimize.
(define_insn "*movsi_reg"
  [(set (match_operand:SI 0 "nonimmediate_operand"  "=r,r,r,m")
        (match_operand:SI 1 "movsi_src_operand"     "r,I,m,r"))]
  "!MEM_P (operands[0]) || REG_P (operands[1])"
{
  switch (which_alternative)
    {
    case 0:
    case 1:
      return "mov\t%0, %1";
    case 2:
      return "load_32\t%0, %1";
    case 3:
      return "store_32\t%0, %1";
    default:
      gcc_unreachable ();
    }
}
  [(set_attr "length" "3")])

(define_split
  [(set (match_operand:SI 0 "register_operand" "")
        (match_operand:SI 1 "const_int_operand" ""))]
  "!satisfies_constraint_I (operands[1])"
  [(const_int 0)]
{
  emit_move_insn (operands[0], operands[1]);
  DONE;
})

;; The general 32-bit-constant materialization used by the split above
;; and by symbol/label address loads (both are CONST/SYMBOL_REF, which
;; are not "arith_operand" so they never match movsi's alternatives
;; above and always come through here instead).
(define_insn "*movsi_full"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (match_operand:SI 1 "immediate_operand" "i"))]
  "!arith_operand (operands[1], SImode)"
  { return symphony_output_move (operands, SImode); }
  [(set_attr "length" "9")])

;; Explicit store: the memory operand is a real (mem ...) so %0 always
;; goes through TARGET_PRINT_OPERAND_ADDRESS (bracketed address), never
;; the bare-register path -- matching load_32/store_32's ISA encoding,
;; which always takes an address (register or U16), never a register
;; plus offset (this ISA has no base+displacement addressing mode).
(define_insn "*store_si"
  [(set (mem:SI (match_operand:SI 0 "register_operand" "r"))
        (match_operand:SI 1 "register_operand" "r"))]
  ""
  "store_32\t[%0], %1"
  [(set_attr "length" "3")])

(define_expand "movhi"
  [(set (match_operand:HI 0 "general_operand" "")
        (match_operand:HI 1 "general_operand" ""))]
  ""
{
  if (MEM_P (operands[0]) && !REG_P (operands[1]))
    operands[1] = force_reg (HImode, operands[1]);
})

(define_insn "*movhi_reg"
  [(set (match_operand:HI 0 "register_operand" "=r,r")
        (match_operand:HI 1 "arith_operand"    "r,I"))]
  ""
  "mov\t%0, %1"
  [(set_attr "length" "3")])

(define_insn "*store_hi"
  [(set (mem:HI (match_operand:SI 0 "register_operand" "r"))
        (match_operand:HI 1 "register_operand" "r"))]
  ""
  "store_16\t[%0], %1"
  [(set_attr "length" "3")])

(define_expand "movqi"
  [(set (match_operand:QI 0 "general_operand" "")
        (match_operand:QI 1 "general_operand" ""))]
  ""
{
  if (MEM_P (operands[0]) && !REG_P (operands[1]))
    operands[1] = force_reg (QImode, operands[1]);
})

(define_insn "*movqi_reg"
  [(set (match_operand:QI 0 "register_operand" "=r,r")
        (match_operand:QI 1 "arith_operand"    "r,I"))]
  ""
  "mov\t%0, %1"
  [(set_attr "length" "3")])

(define_insn "*store_qi"
  [(set (mem:QI (match_operand:SI 0 "register_operand" "r"))
        (match_operand:QI 1 "register_operand" "r"))]
  ""
  "store_8\t[%0], %1"
  [(set_attr "length" "3")])

;; Explicit loads (memory source with a register or U16 address).
(define_insn "*load_si"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (mem:SI (match_operand:SI 1 "register_operand" "r")))]
  ""
  "load_32\t%0, [%1]"
  [(set_attr "length" "3")])

;; Plain same-mode loads (the movhi/movqi expand's memory-source case, and
;; whatever else GCC needs a bare HImode/QImode value for without also
;; widening it to SImode -- e.g. libgcc2.c's __clz_tab[] byte lookups).
(define_insn "*load_hi"
  [(set (match_operand:HI 0 "register_operand" "=r")
        (mem:HI (match_operand:SI 1 "register_operand" "r")))]
  ""
  "load_16\t%0, [%1]"
  [(set_attr "length" "3")])

(define_insn "*load_qi"
  [(set (match_operand:QI 0 "register_operand" "=r")
        (mem:QI (match_operand:SI 1 "register_operand" "r")))]
  ""
  "load_8\t%0, [%1]"
  [(set_attr "length" "3")])

;; Zero-extending SImode loads: the ISA's load_16/load_8 already zero-fill
;; the upper bits of the destination register (docs/isa.txt), so these are
;; free -- no separate extension instruction needed, matching how
;; load_16/load_8 are used for narrow-to-SI loads throughout normal
;; (non-libgcc) codegen too.
(define_insn "*zero_extendqisi2_mem"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (zero_extend:SI (mem:QI (match_operand:SI 1 "register_operand" "r"))))]
  ""
  "load_8\t%0, [%1]"
  [(set_attr "length" "3")])

(define_insn "*zero_extendhisi2_mem"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (zero_extend:SI (mem:HI (match_operand:SI 1 "register_operand" "r"))))]
  ""
  "load_16\t%0, [%1]"
  [(set_attr "length" "3")])

;; Zero-extending a value already in a register (no memory operand): the
;; ISA has no dedicated extend instruction, so mask with an AND immediate.
(define_insn "zero_extendqisi2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (zero_extend:SI (match_operand:QI 1 "register_operand" "r")))]
  ""
  "and\t%0, %1, 255"
  [(set_attr "length" "3")])

(define_insn "zero_extendhisi2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (zero_extend:SI (match_operand:HI 1 "register_operand" "r")))]
  ""
  "and\t%0, %1, 65535"
  [(set_attr "length" "3")])

;; -------------------------------------------------------------------
;; Arithmetic / logic (register form: `op d, a, b`)
;; -------------------------------------------------------------------

(define_insn "addsi3"
  [(set (match_operand:SI 0 "register_operand" "=r,r")
        (plus:SI (match_operand:SI 1 "register_operand" "r,r")
                 (match_operand:SI 2 "arith_operand"     "r,I")))]
  ""
  "add\t%0, %1, %2"
  [(set_attr "length" "3")])

(define_insn "subsi3"
  [(set (match_operand:SI 0 "register_operand" "=r,r")
        (minus:SI (match_operand:SI 1 "register_operand" "r,r")
                  (match_operand:SI 2 "arith_operand"     "r,I")))]
  ""
  "sub\t%0, %1, %2"
  [(set_attr "length" "3")])

(define_insn "andsi3"
  [(set (match_operand:SI 0 "register_operand" "=r,r")
        (and:SI (match_operand:SI 1 "register_operand" "r,r")
                (match_operand:SI 2 "arith_operand"     "r,I")))]
  ""
  "and\t%0, %1, %2"
  [(set_attr "length" "3")])

(define_insn "iorsi3"
  [(set (match_operand:SI 0 "register_operand" "=r,r")
        (ior:SI (match_operand:SI 1 "register_operand" "r,r")
                (match_operand:SI 2 "arith_operand"     "r,I")))]
  ""
  "or\t%0, %1, %2"
  [(set_attr "length" "3")])

(define_insn "xorsi3"
  [(set (match_operand:SI 0 "register_operand" "=r,r")
        (xor:SI (match_operand:SI 1 "register_operand" "r,r")
                (match_operand:SI 2 "arith_operand"     "r,I")))]
  ""
  "xor\t%0, %1, %2"
  [(set_attr "length" "3")])

(define_insn "one_cmplsi2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (not:SI (match_operand:SI 1 "register_operand" "r")))]
  ""
  "nor\t%0, zr, %1"
  [(set_attr "length" "3")])

(define_insn "negsi2"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (neg:SI (match_operand:SI 1 "register_operand" "r")))]
  ""
  "sub\t%0, zr, %1"
  [(set_attr "length" "3")])

(define_insn "ashlsi3"
  [(set (match_operand:SI 0 "register_operand" "=r,r")
        (ashift:SI (match_operand:SI 1 "register_operand" "r,r")
                   (match_operand:SI 2 "arith_operand"     "r,I")))]
  ""
  "lsl\t%0, %1, %2"
  [(set_attr "length" "3")])

(define_insn "lshrsi3"
  [(set (match_operand:SI 0 "register_operand" "=r,r")
        (lshiftrt:SI (match_operand:SI 1 "register_operand" "r,r")
                     (match_operand:SI 2 "arith_operand"     "r,I")))]
  ""
  "lsr\t%0, %1, %2"
  [(set_attr "length" "3")])

(define_insn "ashrsi3"
  [(set (match_operand:SI 0 "register_operand" "=r,r")
        (ashiftrt:SI (match_operand:SI 1 "register_operand" "r,r")
                     (match_operand:SI 2 "arith_operand"     "r,I")))]
  ""
  "asr\t%0, %1, %2"
  [(set_attr "length" "3")])

;; No hardware multiply/divide -- these are intentionally left
;; undefined so GCC emits libcalls to libgcc's __mulsi3/__divsi3/
;; __udivsi3/__modsi3/__umodsi3 (built from the target's soft
;; implementations, see t-symphony/LIB2FUNCS).

;; -------------------------------------------------------------------
;; Comparison / branch
;; -------------------------------------------------------------------
;; `cmp a, b` writes a 3-bit result word to the flags register (docs/
;; isa.txt); conditional jumps read it back.  GCC's cbranch expander
;; keeps the compare and branch fused into one RTL pattern since the
;; flags register is not otherwise modeled as a GCC condition-code
;; register class member here (kept simple: CCmode compare feeding an
;; immediately following branch, never separated by scheduling).

(define_insn "cbranchsi4"
  [(set (pc)
        (if_then_else
          (match_operator 0 "comparison_operator"
            [(match_operand:SI 1 "register_operand" "r")
             (match_operand:SI 2 "arith_operand"     "rI")])
          (label_ref (match_operand 3 "" ""))
          (pc)))]
  ""
  { return symphony_output_cbranch (operands, false); }
  [(set_attr "length" "6")])

;; -------------------------------------------------------------------
;; Unconditional control flow
;; -------------------------------------------------------------------

(define_insn "jump"
  [(set (pc) (label_ref (match_operand 0 "" "")))]
  ""
  "jmp\t%l0"
  [(set_attr "length" "3")])

(define_insn "indirect_jump"
  [(set (pc) (match_operand:SI 0 "register_operand" "r"))]
  ""
  "jmp\t%0"
  [(set_attr "length" "3")])

;; -------------------------------------------------------------------
;; Calls
;; -------------------------------------------------------------------
;; The PRIMARY calling convention is the cheap link_call/link_return
;; pseudo-op pair (docs/isa.txt bottom, isa.py's link_call/
;; link_return): `counter r13; add r13,r13,N; jmp target` to call,
;; `jmp r13` to return.  It touches neither the stack nor flags.  A
;; non-leaf function preserves ITS OWN incoming r13 explicitly in the
;; prologue/epilogue (symphony_expand_prologue/epilogue in
;; symphony.cc) rather than this pattern doing anything stack-based
;; per call -- so every call site, leaf or not, looks identical here.

;; The callee address's own DECL_RTL is already (mem (symbol_ref)) for
;; a direct call; TARGET_LEGITIMATE_ADDRESS_P deliberately rejects
;; SYMBOL_REF (this ISA has no PC-relative/absolute-symbol addressing
;; mode of its own -- link_call takes a register or, for a direct call
;; to a known symbol, the assembler resolves the label itself, see
;; symphony_output_move's "la" pseudo-op for the register case), so
;; without an expand here generic call-expansion legitimizes the
;; address by wrapping it in a SECOND mem, producing unmatchable RTL.
;; Direct calls to a SYMBOL_REF are passed straight through as the
;; symbol operand (matched by the "i" alternative below, printed
;; literally as the label -- the assembler resolves it); anything else
;; (a computed function pointer) is forced into a register first.
(define_expand "call"
  [(call (match_operand:SI 0 "" "")
         (match_operand 1 "" ""))]
  ""
{
  rtx addr = XEXP (operands[0], 0);
  if (!REG_P (addr) && !(SYMBOL_REF_P (addr) || GET_CODE (addr) == LABEL_REF))
    addr = force_reg (SImode, addr);
  emit_call_insn (gen_call_internal (addr, operands[1]));
  DONE;
})

(define_expand "call_value"
  [(set (match_operand 0 "" "")
        (call (match_operand:SI 1 "" "")
              (match_operand 2 "" "")))]
  ""
{
  rtx addr = XEXP (operands[1], 0);
  if (!REG_P (addr) && !(SYMBOL_REF_P (addr) || GET_CODE (addr) == LABEL_REF))
    addr = force_reg (SImode, addr);
  emit_call_insn (gen_call_value_internal (operands[0], addr, operands[2]));
  DONE;
})

(define_insn "call_internal"
  [(call (mem:SI (match_operand:SI 0 "general_operand" "r,i"))
         (match_operand 1 "" ""))
   (clobber (reg:SI R13_REG))]
  ""
  "@
   link_call\t%0
   link_call\t%0"
  [(set_attr "length" "9")])

(define_insn "call_value_internal"
  [(set (match_operand 0 "register_operand" "=r,r")
        (call (mem:SI (match_operand:SI 1 "general_operand" "r,i"))
              (match_operand 2 "" "")))
   (clobber (reg:SI R13_REG))]
  ""
  "@
   link_call\t%1
   link_call\t%1"
  [(set_attr "length" "9")])

(define_expand "prologue"
  [(const_int 0)]
  ""
  { symphony_expand_prologue (); DONE; })

(define_expand "epilogue"
  [(const_int 0)]
  ""
  { symphony_expand_epilogue (); DONE; })

;; `return` here means "jump through the link register"; the actual
;; save/restore of r13 (when needed) is already emitted by the
;; epilogue expander above, so this pattern is just the final jump.
(define_insn "return"
  [(return)]
  ""
  "link_return"
  [(set_attr "length" "3")])

;; Raw single-register push/pop, used by the prologue/epilogue
;; expanders to save/restore r13 and any callee-saved registers GCC's
;; generic frame code decides to spill.  These match the ISA's own
;; push/pop pseudo-ops (docs/isa.txt) byte for byte via the
;; downstream assembler.
(define_insn "movsi_push"
  [(set (mem:SI (pre_dec:SI (reg:SI SP_REG)))
        (match_operand:SI 0 "register_operand" "r"))]
  ""
  "push\t%0"
  [(set_attr "length" "6")])

(define_insn "movsi_pop"
  [(set (match_operand:SI 0 "register_operand" "=r")
        (mem:SI (post_inc:SI (reg:SI SP_REG))))]
  ""
  "pop\t%0"
  [(set_attr "length" "6")])

;; The ISA's own true no-op opcode (docs/isa.txt: `nop` = 00000000),
;; used by GCC's generic alignment/padding and delay-slot-filling
;; infrastructure (cfgrtl.cc's gen_nop()).
(define_insn "nop"
  [(const_int 0)]
  ""
  "nop"
  [(set_attr "length" "1")])

(define_attr "length" "" (const_int 3))
