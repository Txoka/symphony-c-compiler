/* Raw hardware opcode intrinsics for Symphony/Dynphony (docs/isa.txt).

   I/O on this ISA is opcode-based, not memory-mapped (confirmed against
   symphony/emulator/native_emulator.c's L_OUT_R/L_SCREEN_R handlers) --
   there is no MMIO address range a normal C load/store could hit, so
   these operations only exist as inline asm wrapping the raw opcodes.
   GCC's generic __asm__ support works for any well-formed target .s
   output; no dedicated builtins are needed. gcc-backend/symphony-gcc/
   tools/symphony_as.py recognizes each mnemonic used below directly
   (input/output/keyboard/time_0/time_1/screen/counter).

   Naming matches symphony/runtime/intrinsics.py's PROTOTYPES so C code
   written against dyncc's own frontend needs no changes to also build
   under this real GCC port. */

unsigned int input(void) {
    unsigned int r;
    __asm__ volatile ("input\t%0" : "=r"(r));
    return r;
}

void output(unsigned int value) {
    __asm__ volatile ("output\t%0" : : "r"(value));
}

unsigned int keyboard(void) {
    unsigned int r;
    __asm__ volatile ("keyboard\t%0" : "=r"(r));
    return r;
}

/* setting/value match isa.py's screen(setting, value): setting selects
   which screen register is being written (0 = init/mode, 1 = pixel/cell
   write, 2 = cursor, etc. -- see symphony/targets/symphony/backend.py's
   emit sites for the setting values dyncc's own codegen uses), value is
   the data written to it. */
void screen(unsigned int setting, unsigned int value) {
    __asm__ volatile ("screen\t%0, %1" : : "r"(setting), "r"(value));
}

unsigned int time_low(void) {
    unsigned int r;
    __asm__ volatile ("time_0\t%0" : "=r"(r));
    return r;
}

unsigned int time_high(void) {
    unsigned int r;
    __asm__ volatile ("time_1\t%0" : "=r"(r));
    return r;
}

unsigned int time(void) {
    /* Matches symphony/runtime/intrinsics.py's `time()`: the low half is
       the commonly-used value (see how dyncc's own frontend lowers a bare
       `time()` call), time_low()/time_high() are available separately
       for callers that need the full 64-bit counter. */
    return time_low();
}

/* Minimal atexit(): a real `int main(void){...}` function compiled by
   this GCC port ALWAYS gets an implicit `link_call __main` inserted at
   entry by expand_main_function (GCC's generic behaviour, not something
   this target's .md/.cc opted into or can opt out of -- confirmed: it
   survives -ffreestanding too, since this target defines neither
   HAS_INIT_SECTION nor its own NAME__MAIN override). libgcc2.c's __main
   -> __do_global_ctors always calls `atexit(__do_global_dtors)`
   unconditionally, even when __CTOR_LIST__/__DTOR_LIST__ are the
   trivial empty two-element arrays libgcc itself default-defines (no
   real global constructors anywhere in this runtime or any program
   compiled against it) -- so without a real `atexit` symbol, EVERY
   program with a `main()` fails to link with "undefined symbol
   'atexit'", not just ones that reference it directly. There is no
   corresponding real `exit()` in this freestanding runtime (no OS to
   return control to), so the registered callback table only needs to
   exist and accept registrations up to a small fixed capacity --
   nothing here ever walks or invokes it, matching the fact that
   __do_global_dtors itself is dead code whenever __DTOR_LIST__ is
   empty (the only case that occurs in practice for this runtime). */
#define ATEXIT_MAX 8
static void (*__dyn_atexit_fns[ATEXIT_MAX])(void);
static unsigned int __dyn_atexit_count;

int atexit(void (*function)(void)) {
    if (__dyn_atexit_count >= ATEXIT_MAX) return 1;
    __dyn_atexit_fns[__dyn_atexit_count] = function;
    __dyn_atexit_count += 1;
    return 0;
}
