/* GCC target header for Symphony/Dynphony.

   Hardware: 16 architectural registers (docs/isa.txt,
   symphony/targets/symphony/registers.py):
     zr=0 (hardwired zero), r1-r11, r12, r13, sp=14, flags=15.

   Software ABI (symphony/targets/symphony/abi.py, CONFIRMED, do not
   re-derive):
     argument/return registers: r1-r7 (SEVEN registers).
     link register: r13 (used by the link_call/link_return pseudo-ops).
     frame pointer: r11.
     pic base register: r12 (a distinct role from the frame pointer).
     scratch register: r7.
     caller-saved: r1-r7, flags.  callee-saved: r8-r12.

   There is no hardware multiply/divide -- the ALU only has
   NAND/OR/AND/NOR/ADD/SUB/XOR/LSL/LSR/ASR/CMP -- so libgcc must supply
   __mulsi3/__divsi3/__udivsi3/__modsi3/__umodsi3 etc. in software.

   All immediates in the immediate/label instruction forms are U16
   (0-65535); a full 32-bit constant costs three instructions (see
   symphony.md's "movsi" alternatives and symphony_output_move).  */

#ifndef GCC_SYMPHONY_H
#define GCC_SYMPHONY_H

/* This ISA's multi-byte fields (docs/isa.txt: aaaaaaaa aaaaaaaa for a
   16-bit immediate, etc.) are encoded most-significant-byte first. */
#define TARGET_BIG_ENDIAN_DEFAULT 1
#define BITS_BIG_ENDIAN 0
#define BYTES_BIG_ENDIAN 1
#define WORDS_BIG_ENDIAN 1

#define BITS_PER_WORD 32
#define UNITS_PER_WORD 4
#define POINTER_SIZE 32
#define PARM_BOUNDARY 32
#define STACK_BOUNDARY 32
#define FUNCTION_BOUNDARY 8
#define BIGGEST_ALIGNMENT 32
#define STRICT_ALIGNMENT 0

#define INT_TYPE_SIZE 32
#define SHORT_TYPE_SIZE 16
#define LONG_TYPE_SIZE 32
#define LONG_LONG_TYPE_SIZE 64
#define FLOAT_TYPE_SIZE 32
#define DOUBLE_TYPE_SIZE 32
#define LONG_DOUBLE_TYPE_SIZE 32
#define DEFAULT_SIGNED_CHAR 0

#undef SIZE_TYPE
#define SIZE_TYPE "unsigned int"
#undef PTRDIFF_TYPE
#define PTRDIFF_TYPE "int"
#undef WCHAR_TYPE
#define WCHAR_TYPE "unsigned int"
#undef WCHAR_TYPE_SIZE
#define WCHAR_TYPE_SIZE 32

/* Minimal CPU preprocessor hooks required by the C-family frontend. */
#define TARGET_CPU_CPP_BUILTINS()                                       \
  do                                                                    \
    {                                                                   \
      builtin_define ("__symphony__");                                 \
      builtin_define ("__SYMPHONY__");                                 \
      if (symphony_dynphony)                                           \
        builtin_define ("__dynphony__");                                \
    }                                                                   \
  while (0)

/* Register numbering, matching symphony/targets/symphony/registers.py
   exactly: zr=0, r1..r11=1..11, r12=12, r13=13, sp=14, flags=15. */
#define FIRST_PSEUDO_REGISTER 16

#define ZR_REGNUM 0
#define R1_REGNUM 1
#define R7_REGNUM 7
#define R11_REGNUM 11
#define R12_REGNUM 12
#define R13_REGNUM 13
#define SP_REGNUM 14
#define FLAGS_REGNUM 15

#define REGISTER_NAMES                                                  \
  { "zr", "r1", "r2", "r3", "r4", "r5", "r6", "r7",                     \
    "r8", "r9", "r10", "r11", "r12", "r13", "sp", "flags" }

/* zr is hardwired zero (never allocatable). sp is the stack pointer.
   flags is caller-clobbered and used transiently by cmp/branch
   sequences and register-shuffle scratch, so it is not allocated as a
   general pseudo home either -- GCC treats it like a fixed register
   with no persistent value across statements.  r13 (the ABI link
   register) IS allocatable: leaf functions never touch it, and
   non-leaf functions that need it save/restore it explicitly in the
   prologue/epilogue (see symphony_expand_prologue/epilogue), matching
   how symphony/targets/symphony/backend.py treats r13 as an ordinary
   value that just happens to also serve as the call-return slot.

   r11 (HARD_FRAME_POINTER_REGNUM) MUST be fixed here.
   symphony_frame_pointer_required() (symphony.cc) unconditionally
   returns true -- this target never allows r11 to be reused as a
   general register, matching the documented FIXED_REGISTERS contract
   in GCC's own tm.texi ("the frame pointer, except on machines where
   that can be used as a general register when no frame pointer is
   needed"). An earlier version of this port left r11 UNFIXED, which
   let IRA/LRA treat it as an ordinary GENERAL_REGS value eligible for
   copy-propagation into pseudos -- e.g. ivopts/move-loop-invariants
   hoisting a loop-invariant copy of r11 into a pseudo compared every
   loop iteration. Since r11 is in fact permanently pinned to the
   frame-pointer role and this target's REG_CLASS_CONTENTS gives LRA
   no alternative register class to fall back to, LRA's equivalence/
   reload-substitution loop for that pseudo could never converge,
   hitting reload's "maximum number of generated reload insns per insn
   achieved (90)" internal compiler error on perfectly ordinary user
   code (no 64-bit arithmetic, no VLA, no recursion needed --
   confirmed via a minimal reproducer: a fill loop + insertion-sort
   loop + sum loop over a 16-byte char array at -O2, and gdb-traced
   into lra_constraints at lra-constraints.cc:5392, where
   original_insn was exactly "(set (reg N) (reg 11 r11))" and
   subsequent retries kept minting fresh pseudo copies of r11 without
   ever converging). Marking r11 fixed here stops GCC from ever
   placing a general pseudo's value there or substituting it as an
   equivalence target, removing that trigger.  Root-cause note for
   whoever continues this investigation: fixing r11 measurably helps
   (confirmed: the exact reproducer above, which never compiled at any
   optimization level above -Os before this fix, now compiles and
   runs correctly at -O2) but is NOT a complete fix for the "reload
   insns" ICE class documented in this README's Known gaps section --
   -O1 still ICEs on the same reproducer, and a second, independently
   confirmed trigger (examples/towers_of_hanoi.c's move_pile, a plain
   recursive function with no loops at all, passing highest_disk - 1
   as an argument across its own recursive call) ICEs at every
   optimization level including -Os both before AND after this fix.
   Traced via gdb the same way: a spilled pseudo (register allocator
   ran out of the only 4 callee-saved hard registers, r8-r10 and r12,
   and had to spill one candidate to a stack slot) whose reload before
   a subsequent use is re-emitted over and over
   ("(set (reg N)(reg M))" with M incrementing every retry) without
   ever being accepted as satisfying constraints board-wide. This is
   LRA failing to converge on an ordinary register-pressure spill/
   reload, not something r11-specific -- a distinct manifestation of
   the same underlying "this target's flat, single-class register file
   gives LRA very little room to maneuver once genuinely register-
   starved" structural issue, but NOT solved by the r11 fix above and
   NOT solved by a register-class-based fix tried and reverted during
   this investigation (see the git history / prior comment on this
   line for the attempt: reserving r12 as a dedicated address-reload
   scratch class measurably REGRESSED this same reproducer at -O2,
   because it reduces the callee-saved register pool from 4 to 3,
   increasing spill pressure faster than it relieves address-reload
   pressure -- a net loss, confirmed by A/B rebuilding and testing
   both variants against the same reproducer set). Whoever picks this
   up next should look at why LRA's per-insn retry loop
   (lra_constraints's curr_insn/original_insn bookkeeping around
   lra-constraints.cc:5375-5392) fails to terminate for a spilled
   pseudo's reload specifically when it's read again after a
   subsequent call, rather than assuming another register-class tweak
   will fix it -- the evidence above suggests this needs a genuine
   LRA-level or spill-strategy fix, not just a target-description
   register-class rebalancing, since taking a register away to hand
   LRA more "room to maneuver" this way made things worse, not
   better. */
#define FIXED_REGISTERS \
  { 1,0,0,0,0,0,0,0, 0,0,0,1, 0,0,1,1 }

/* 1 means the ABI allows an ordinary call to clobber this register
   (i.e. the caller must not assume it survives a call): r1-r7 and
   flags are caller-saved: r8-r12 are callee-saved.  r13 is treated as
   caller-saved from the allocator's point of view -- a function that
   uses it as the link register saves/restores it itself when needed,
   the same way it would save any other register it clobbers. */
#define CALL_USED_REGISTERS \
  { 1,1,1,1,1,1,1,1, 0,0,0,0, 0,1,1,1 }

enum reg_class
{
  NO_REGS,
  GENERAL_REGS,
  ALL_REGS,
  LIM_REG_CLASSES
};

#define N_REG_CLASSES ((int) LIM_REG_CLASSES)
#define REG_CLASS_NAMES { "NO_REGS", "GENERAL_REGS", "ALL_REGS" }
#define REG_CLASS_CONTENTS \
  { { 0x0000 }, { 0xffff }, { 0xffff } }
#define REGNO_REG_CLASS(R) GENERAL_REGS
#define BASE_REG_CLASS GENERAL_REGS
#define INDEX_REG_CLASS NO_REGS
#define REGNO_OK_FOR_BASE_P(R) ((R) < FIRST_PSEUDO_REGISTER)
#define REGNO_OK_FOR_INDEX_P(R) 0
#define MAX_REGS_PER_ADDRESS 1

#define STACK_POINTER_REGNUM SP_REGNUM
/* The frame pointer is r11 per abi.py -- r12 is a DIFFERENT role (the
   PIC base register).  The old dynphony.h stub conflated these; fixed
   here. */
#define HARD_FRAME_POINTER_REGNUM R11_REGNUM
#define FRAME_POINTER_REGNUM R11_REGNUM
#define ARG_POINTER_REGNUM R11_REGNUM
#define STATIC_CHAIN_REGNUM R7_REGNUM

#define ELIMINABLE_REGS \
  {{ FRAME_POINTER_REGNUM, STACK_POINTER_REGNUM },                      \
   { ARG_POINTER_REGNUM, STACK_POINTER_REGNUM }}

#define INITIAL_ELIMINATION_OFFSET(FROM, TO, OFFSET) \
  ((OFFSET) = symphony_initial_elimination_offset ((FROM), (TO)))

#define DEFAULT_PCC_STRUCT_RETURN 0

/* Argument-passing state: the ABI has SEVEN argument/return registers
   (r1-r7), not six -- the old dynphony.cc stub used a 6-entry table,
   which was a real bug relative to abi.py. */
typedef struct symphony_args
{
  unsigned int words;
} CUMULATIVE_ARGS;

#define INIT_CUMULATIVE_ARGS(CUM, FNTYPE, LIBNAME, FNDECL, N_NAMED_ARGS) \
  ((CUM).words = 0)

#define FUNCTION_ARG_REGNO_P(N) ((N) >= 1 && (N) <= 7)

#define STACK_GROWS_DOWNWARD 1
#define FRAME_GROWS_DOWNWARD 1
#define ACCUMULATE_OUTGOING_ARGS 0

#define RETURN_ADDR_RTX(COUNT, FRAME) \
  ((COUNT) == 0 ? gen_rtx_REG (Pmode, R13_REGNUM) : NULL_RTX)

/* Stack-passed arguments (the eighth word-sized argument onward) start
   immediately at the incoming argument pointer -- there is no fixed
   register-save area ahead of them to skip, since the ABI passes only
   r1-r7 in registers and spills the rest directly. */
#define FIRST_PARM_OFFSET(FNDECL) 0

/* No profiling support target-side; -pg is simply unsupported here. */
#define FUNCTION_PROFILER(FILE, LABELNO) \
  do { } while (0)

#define MOVE_MAX 4
#define MOVE_RATIO(SPEED) 2
#define SLOW_BYTE_ACCESS 0
#define LOAD_EXTEND_OP(MEM) ZERO_EXTEND

#define Pmode SImode
#define FUNCTION_MODE SImode
#define CASE_VECTOR_MODE SImode

#define TRAMPOLINE_SIZE 0
#define TRAMPOLINE_ALIGNMENT 32

/* No debugger targets this ISA and the custom assembler+linker does not
   understand any debug-info format, so debug-info generation is simply
   off (GCC still requires SOME PREFERRED_DEBUGGING_TYPE to be nameable
   even when unused, since elfos.h -- which normally supplies this --
   is intentionally not included by this freestanding, non-ELF
   target). */
#define PREFERRED_DEBUGGING_TYPE NO_DEBUG

/* Forward -mdynphony straight through to `as` (symphony_as.py), the
   standard GCC mechanism for a target flag that changes assembler
   behavior but no codegen decision (see e.g. moxie's `%{mel:-EL}` for
   the analogous case).  symphony_as.py records the resulting encoding
   mode (Symphony fixed-width padding vs. Dynphony variable-length, see
   symphony/targets/symphony/assembler.py's Assembler.emit for the
   reference logic this mirrors) into each object file it produces, so
   symphony_ld.py never needs its own separate flag -- it just reads the
   mode off the objects being linked and fails loudly on a mismatch
   instead of silently mixing encodings. */
#define ASM_SPEC "%{mdynphony:-mdynphony}"

/* --- Assembler output ---

   GCC never invokes a real `as`/`ld` for this target: the driver only
   ever runs it as `-S` (see symphony-gcc's SPECs below), and the
   resulting .s is consumed by gcc-backend/tools/gcc_assembler.py, a
   from-scratch assembler+linker.  Syntax below is chosen for that
   tool's convenience, not for GNU as compatibility. */

#define ASM_COMMENT_START "#"
#define ASM_APP_ON ""
#define ASM_APP_OFF ""

#define TEXT_SECTION_ASM_OP "\t.text"
#define DATA_SECTION_ASM_OP "\t.data"
#define BSS_SECTION_ASM_OP "\t.bss"

#define GLOBAL_ASM_OP "\t.global\t"

#define ASM_OUTPUT_ALIGN(FILE, LOG) \
  do { if ((LOG) != 0) fprintf ((FILE), "\t.p2align\t%d\n", (LOG)); } while (0)

#define ASM_OUTPUT_LABEL(FILE, NAME) \
  do { assemble_name ((FILE), (NAME)); fputs (":\n", (FILE)); } while (0)

#define ASM_OUTPUT_LABELREF(FILE, NAME) \
  asm_fprintf ((FILE), "%U%s", (NAME))

/* Internal (compiler-generated, non-global) label names: ".<PREFIX><NUM>",
   e.g. ".L7" -- matches the local-label convention gcc_assembler.py
   already normalizes/namespaces (see namespace_local_labels() there),
   so compiler-generated labels from multiple translation units never
   collide once concatenated. No '*' escape (a GNU-as-specific "don't
   prefix this symbol" marker) is needed since this target's assembler
   is entirely custom and never applies such a prefix in the first
   place. */
#define ASM_GENERATE_INTERNAL_LABEL(LABEL, PREFIX, NUM)                 \
  sprintf ((LABEL), "*.%s%ld", (PREFIX), (long) (NUM))

#define ASM_OUTPUT_SKIP(FILE, SIZE) \
  fprintf ((FILE), "\t.zero\t" HOST_WIDE_INT_PRINT_UNSIGNED "\n", (SIZE))

#define ASM_OUTPUT_ALIGNED_DECL_LOCAL(FILE, DECL, NAME, SIZE, ALIGN)    \
  do                                                                     \
    {                                                                   \
      switch_to_section (bss_section);                                  \
      ASM_OUTPUT_ALIGN ((FILE), floor_log2 ((ALIGN) / BITS_PER_UNIT));  \
      ASM_OUTPUT_LABEL ((FILE), (NAME));                                \
      ASM_OUTPUT_SKIP ((FILE), (SIZE) ? (SIZE) : 1);                    \
    }                                                                   \
  while (0)

#define ASM_OUTPUT_ALIGNED_BSS(FILE, DECL, NAME, SIZE, ALIGN) \
  asm_output_aligned_bss ((FILE), (DECL), (NAME), (SIZE), (ALIGN))

/* No real "common symbol" merging concept exists for this target (no
   ELF, no linker with COMM-section semantics) -- tentative
   definitions simply become ordinary bss allocations, identical to
   ASM_OUTPUT_ALIGNED_DECL_LOCAL above. */
#define ASM_OUTPUT_ALIGNED_COMMON(FILE, NAME, SIZE, ALIGN)              \
  do                                                                     \
    {                                                                   \
      switch_to_section (bss_section);                                  \
      ASM_OUTPUT_ALIGN ((FILE), floor_log2 ((ALIGN) / BITS_PER_UNIT));  \
      ASM_OUTPUT_LABEL ((FILE), (NAME));                                \
      ASM_OUTPUT_SKIP ((FILE), (SIZE) ? (SIZE) : 1);                    \
    }                                                                   \
  while (0)

#endif /* GCC_SYMPHONY_H */
