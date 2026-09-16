/* GCC target hooks for Symphony/Dynphony.

   Calling convention summary (see symphony.h and symphony/targets/
   symphony/abi.py, the authoritative source):
     - r1-r7 carry the first seven word-sized arguments and the
       (first) return value.
     - r13 is the link register.  Calls use the cheap link_call/
       link_return pseudo-op pair (docs/isa.txt) rather than the
       stack-based call/ret pair: `counter r13; add r13,r13,N; jmp
       target` / `jmp r13`.  A function that itself calls out (a
       non-leaf function) must save/restore its OWN incoming r13
       across those nested calls, exactly like any other callee-saved
       register it clobbers -- this is what symphony_expand_prologue/
       epilogue do below, driven by crtl->is_leaf via
       symphony_call_is_leaf().
     - Frame pointer is r11 (NOT r12 -- r12 is the PIC base register,
       a different role; the ISA/ABI stub this replaces conflated the
       two, which was a real bug). */

#define IN_TARGET_CODE 1

#include "config.h"
#include "system.h"
#include "coretypes.h"
#include "backend.h"
#include "target.h"
#include "rtl.h"
#include "tree.h"
#include "stringpool.h"
#include "attribs.h"
#include "memmodel.h"
#include "tm_p.h"
#include "emit-rtl.h"
#include "explow.h"
#include "expr.h"
#include "function.h"
#include "regs.h"
#include "df.h"
#include "output.h"
#include "diagnostic-core.h"
#include "stor-layout.h"
#include "calls.h"
#include "varasm.h"
#include "builtins.h"
#include "target-def.h"

/* The ABI has SEVEN argument/return registers: r1-r7. */
static const unsigned symphony_arg_regs[] = { 1, 2, 3, 4, 5, 6, 7 };

static rtx
symphony_function_arg (cumulative_args_t cum_v, const function_arg_info &arg)
{
  CUMULATIVE_ARGS *cum = get_cumulative_args (cum_v);
  unsigned words = (arg.mode == BLKmode
                    ? (int_size_in_bytes (arg.type) + 3) / 4
                    : (GET_MODE_SIZE (arg.mode) + 3) / 4);
  if (words == 1 && cum->words < ARRAY_SIZE (symphony_arg_regs))
    return gen_rtx_REG (arg.mode, symphony_arg_regs[cum->words]);
  return NULL_RTX;
}

static void
symphony_function_arg_advance (cumulative_args_t cum_v,
                                const function_arg_info &arg)
{
  CUMULATIVE_ARGS *cum = get_cumulative_args (cum_v);
  unsigned words = (arg.mode == BLKmode
                    ? (int_size_in_bytes (arg.type) + 3) / 4
                    : (GET_MODE_SIZE (arg.mode) + 3) / 4);
  cum->words += MAX (1u, words);
}

static bool
symphony_return_in_memory (const_tree type, const_tree)
{
  return int_size_in_bytes (type) > 4;
}

static rtx
symphony_function_value (const_tree ret_type, const_tree, bool)
{
  machine_mode mode = TYPE_MODE (ret_type);
  return gen_rtx_REG (mode, 1);
}

static rtx
symphony_libcall_value (machine_mode mode, const_rtx)
{
  return gen_rtx_REG (mode, 1);
}

static bool
symphony_function_value_regno_p (const unsigned int regno)
{
  return regno == 1;
}

bool
symphony_legitimate_address_p (machine_mode, rtx x, bool strict,
                                code_helper)
{
  if (REG_P (x))
    return !strict || REGNO (x) < FIRST_PSEUDO_REGISTER;
  if (CONST_INT_P (x))
    return IN_RANGE (INTVAL (x), 0, 65535);
  return false;
}

rtx
symphony_legitimize_address (rtx x, rtx, machine_mode)
{
  if (symphony_legitimate_address_p (Pmode, x, false, ERROR_MARK))
    return x;
  return force_reg (Pmode, x);
}

static bool
symphony_frame_pointer_required (void)
{
  return cfun->calls_alloca || crtl->has_nonlocal_goto;
}

HOST_WIDE_INT
symphony_initial_elimination_offset (int from, int to)
{
  gcc_assert (to == STACK_POINTER_REGNUM);
  if (from == FRAME_POINTER_REGNUM || from == ARG_POINTER_REGNUM)
    return get_frame_size () + crtl->outgoing_args_size;
  gcc_unreachable ();
}

/* True when this function can use the cheap link_call/link_return
   convention without saving r13, i.e. it makes no calls of its own
   (crtl->is_leaf, computed by GCC's leaf-function analysis) and so
   never clobbers its own incoming link value. */
bool
symphony_call_is_leaf (void)
{
  return crtl->is_leaf;
}

void
symphony_expand_prologue (void)
{
  HOST_WIDE_INT size = get_frame_size () + crtl->outgoing_args_size;
  if (!symphony_call_is_leaf ())
    {
      /* Save the incoming link register (r13) before any nested call
         can clobber it -- push is the ISA's own U16-immediate
         pseudo-op (docs/isa.txt), always exactly usable here since a
         single register push never exceeds it. */
      rtx r13 = gen_rtx_REG (SImode, R13_REGNUM);
      emit_insn (gen_movsi_push (r13));
    }
  if (size)
    {
      /* addsi3's "I" constraint only accepts an UNSIGNED 16-bit
         immediate (the ALU's actual immediate encoding, per
         isa.py) -- so a downward adjustment must be emitted as
         subsi3 with a positive constant, not addsi3 with a
         negative one, or the RTL never matches any alternative.
         For sizes that don't fit U16 either way, force the
         constant into a register first (movsi's full 3-insn
         constant-materialization path handles that). */
      if (IN_RANGE (size, 0, 65535))
        emit_insn (gen_subsi3 (stack_pointer_rtx, stack_pointer_rtx,
                                GEN_INT (size)));
      else
        {
          rtx tmp = force_reg (SImode, GEN_INT (size));
          emit_insn (gen_subsi3 (stack_pointer_rtx, stack_pointer_rtx, tmp));
        }
    }
}

void
symphony_expand_epilogue (void)
{
  HOST_WIDE_INT size = get_frame_size () + crtl->outgoing_args_size;
  if (size)
    {
      if (IN_RANGE (size, 0, 65535))
        emit_insn (gen_addsi3 (stack_pointer_rtx, stack_pointer_rtx,
                                GEN_INT (size)));
      else
        {
          rtx tmp = force_reg (SImode, GEN_INT (size));
          emit_insn (gen_addsi3 (stack_pointer_rtx, stack_pointer_rtx, tmp));
        }
    }
  if (!symphony_call_is_leaf ())
    {
      rtx r13 = gen_rtx_REG (SImode, R13_REGNUM);
      emit_insn (gen_movsi_pop (r13));
    }
  emit_jump_insn (gen_return ());
}

void
symphony_print_operand (FILE *file, rtx x, int code)
{
  if (code == 0)
    {
      if (REG_P (x))
        fputs (reg_names[REGNO (x)], file);
      else if (CONST_INT_P (x))
        fprintf (file, HOST_WIDE_INT_PRINT_DEC, INTVAL (x));
      else
        output_addr_const (file, x);
      return;
    }
  output_operand_lossage ("unsupported Symphony operand modifier");
}

void
symphony_print_operand_address (FILE *file, machine_mode, rtx addr)
{
  if (REG_P (addr))
    fprintf (file, "[%s]", reg_names[REGNO (addr)]);
  else if (CONST_INT_P (addr))
    fprintf (file, "[" HOST_WIDE_INT_PRINT_DEC "]", INTVAL (addr));
  else
    {
      fputc ('[', file);
      output_addr_const (file, addr);
      fputc (']', file);
    }
}

static bool
symphony_hard_regno_mode_ok (unsigned int regno, machine_mode mode)
{
  if (regno == ZR_REGNUM)
    return false;
  return GET_MODE_SIZE (mode) <= 4;
}

static unsigned int
symphony_hard_regno_nregs (unsigned int, machine_mode mode)
{
  return (GET_MODE_SIZE (mode) + 3) / 4;
}

static bool
symphony_can_eliminate (const int from ATTRIBUTE_UNUSED, const int to)
{
  return to == STACK_POINTER_REGNUM;
}

/* Emit the cheapest sequence that materializes a general 32-bit
   constant, symbol, or label address into operands[0].  Mirrors
   isa.py's cheap_constant()/constant(): a value (or its negation)
   that fits U16 is one `mov`/`sub`; everything else (including
   relocatable symbol/label addresses, which the assembler resolves
   after this text is emitted) is the fixed 3-instruction hi16/lsl16/
   or-lo16 sequence so the encoded length never depends on the actual
   resolved value -- required for symbols, whose value is unknown at
   compile time. */
const char *
symphony_output_move (rtx *operands, machine_mode mode ATTRIBUTE_UNUSED)
{
  static char buf[256];
  rtx dest = operands[0];
  rtx src = operands[1];

  if (CONST_INT_P (src))
    {
      unsigned HOST_WIDE_INT value = UINTVAL (src) & 0xffffffff;
      if (value <= 0xffff)
        {
          snprintf (buf, sizeof buf, "mov\t%%0, %u", (unsigned) value);
          return buf;
        }
      unsigned HOST_WIDE_INT magnitude = (-value) & 0xffffffff;
      if (magnitude <= 0xffff)
        {
          snprintf (buf, sizeof buf, "sub\t%%0, zr, %u", (unsigned) magnitude);
          return buf;
        }
      snprintf (buf, sizeof buf,
                "mov\t%%0, %u\n\tlsl\t%%0, %%0, 16\n\tor\t%%0, %%0, %u",
                (unsigned) (value >> 16), (unsigned) (value & 0xffff));
      return buf;
    }

  /* Symbol/label address: value unknown until link time.  Emit a
     single pseudo-mnemonic `la dest, symbol` that the downstream
     assembler+linker (gcc_assembler.py) recognizes and expands to the
     fixed 3-instruction constant-materialization sequence with a
     32-bit relocation against the symbol -- exactly mirroring how
     symphony/targets/symphony/assembler.py's Assembler.address()
     emits isa.constant() plus a fixup for any symbolic value. Kept as
     one pseudo-op here (rather than spelling out hi16/lo16 text)
     because the addend and relocation bookkeeping belongs with the
     assembler's existing fixup machinery, not duplicated in GCC. */
  snprintf (buf, sizeof buf, "la\t%%0, %%1");
  return buf;
}

/* comparison_operator codes -> (signed jump, unsigned jump) mnemonic,
   matching symphony/targets/symphony/backend.py's identical table. */
const char *
symphony_output_cbranch (rtx *operands, bool inverted)
{
  static char buf[64];
  enum rtx_code code = GET_CODE (operands[0]);
  if (inverted)
    code = reverse_condition (code);
  const char *mnem;
  switch (code)
    {
    case EQ: mnem = "je"; break;
    case NE: mnem = "jne"; break;
    case LT: mnem = "jl"; break;
    case LE: mnem = "jle"; break;
    case GT: mnem = "jg"; break;
    case GE: mnem = "jge"; break;
    case LTU: mnem = "jb"; break;
    case LEU: mnem = "jbe"; break;
    case GTU: mnem = "ja"; break;
    case GEU: mnem = "jae"; break;
    default:
      gcc_unreachable ();
    }
  snprintf (buf, sizeof buf, "cmp\t%%1, %%2\n\t%s\t%%l3", mnem);
  return buf;
}

#undef TARGET_FUNCTION_ARG
#define TARGET_FUNCTION_ARG symphony_function_arg
#undef TARGET_FUNCTION_ARG_ADVANCE
#define TARGET_FUNCTION_ARG_ADVANCE symphony_function_arg_advance
#undef TARGET_RETURN_IN_MEMORY
#define TARGET_RETURN_IN_MEMORY symphony_return_in_memory
#undef TARGET_FUNCTION_VALUE
#define TARGET_FUNCTION_VALUE symphony_function_value
#undef TARGET_LIBCALL_VALUE
#define TARGET_LIBCALL_VALUE symphony_libcall_value
#undef TARGET_FUNCTION_VALUE_REGNO_P
#define TARGET_FUNCTION_VALUE_REGNO_P symphony_function_value_regno_p
#undef TARGET_FRAME_POINTER_REQUIRED
#define TARGET_FRAME_POINTER_REQUIRED symphony_frame_pointer_required
#undef TARGET_CAN_ELIMINATE
#define TARGET_CAN_ELIMINATE symphony_can_eliminate
#undef TARGET_LEGITIMATE_ADDRESS_P
#define TARGET_LEGITIMATE_ADDRESS_P symphony_legitimate_address_p
#undef TARGET_LEGITIMIZE_ADDRESS
#define TARGET_LEGITIMIZE_ADDRESS symphony_legitimize_address
#undef TARGET_PRINT_OPERAND
#define TARGET_PRINT_OPERAND symphony_print_operand
#undef TARGET_PRINT_OPERAND_ADDRESS
#define TARGET_PRINT_OPERAND_ADDRESS symphony_print_operand_address
#undef TARGET_HARD_REGNO_MODE_OK
#define TARGET_HARD_REGNO_MODE_OK symphony_hard_regno_mode_ok
#undef TARGET_HARD_REGNO_NREGS
#define TARGET_HARD_REGNO_NREGS symphony_hard_regno_nregs

struct gcc_target targetm = TARGET_INITIALIZER;
