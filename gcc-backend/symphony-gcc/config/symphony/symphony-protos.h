/* Prototypes for Symphony/Dynphony GCC target support functions.  */

#ifndef GCC_SYMPHONY_PROTOS_H
#define GCC_SYMPHONY_PROTOS_H

extern HOST_WIDE_INT symphony_initial_elimination_offset (int, int);
extern void symphony_expand_prologue (void);
extern void symphony_expand_epilogue (void);
extern void symphony_print_operand (FILE *, rtx, int);
extern void symphony_print_operand_address (FILE *, machine_mode, rtx);
extern bool symphony_legitimate_address_p (machine_mode, rtx, bool, code_helper);
extern rtx symphony_legitimize_address (rtx, rtx, machine_mode);
extern bool symphony_call_is_leaf (void);
extern const char *symphony_output_move (rtx *, machine_mode);
extern const char *symphony_output_cbranch (rtx *, bool);

#endif /* GCC_SYMPHONY_PROTOS_H */
