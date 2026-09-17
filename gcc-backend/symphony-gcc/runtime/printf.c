/* Text-screen printf family for Symphony/Dynphony, ported verbatim from
   symphony/runtime/intrinsics.py's SCREEN_SOURCE (the source there is
   already plain C, written to compile under any C compiler -- this is a
   straight copy, not a rewrite). ASCII 8 mode maps one byte to each of
   96 * 40 screen cells (3840 total).

   Kept in sync with intrinsics.py by hand; if SCREEN_SOURCE changes
   there, mirror the change here too. */

extern unsigned char __dyn_printf_framebuffer[3840];
extern unsigned int __dyn_printf_cursor;
extern unsigned int __dyn_printf_column;

unsigned int __dyn_printf_put(unsigned int value) {
    if (value == 10) {
        if (__dyn_printf_column) {
            __dyn_printf_cursor += 96 - __dyn_printf_column;
            if (__dyn_printf_cursor > 3840) __dyn_printf_cursor = 3840;
            __dyn_printf_column = 0;
        }
        return 1;
    }
    if (__dyn_printf_cursor >= 3840) return 0;
    __dyn_printf_framebuffer[__dyn_printf_cursor] = (unsigned char)value;
    __dyn_printf_cursor += 1;
    __dyn_printf_column += 1;
    if (__dyn_printf_column == 96) __dyn_printf_column = 0;
    return 1;
}

unsigned int __dyn_printf_write(const char *text, unsigned int count) {
    unsigned int written = 0, index = 0;
    while (index < count) {
        written += __dyn_printf_put((unsigned char)text[index]);
        index += 1;
    }
    return written;
}

unsigned int __dyn_printf_string(const char *text) {
    unsigned int written = 0, index = 0;
    while (text[index]) {
        written += __dyn_printf_put((unsigned char)text[index]);
        index += 1;
    }
    return written;
}

unsigned int __dyn_printf_unsigned(unsigned int value) {
    char digits[10];
    unsigned int count = 0, written = 0;
    if (!value) return __dyn_printf_put('0');
    while (value) {
        digits[count] = '0' + value % 10u;
        count += 1;
        value /= 10u;
    }
    while (count) { count -= 1; written += __dyn_printf_put(digits[count]); }
    return written;
}

unsigned int __dyn_printf_signed(int value) {
    unsigned int written = 0;
    unsigned int magnitude = (unsigned int)value;
    if (value < 0) { written += __dyn_printf_put('-'); magnitude = 0u - magnitude; }
    return written + __dyn_printf_unsigned(magnitude);
}

unsigned int __dyn_printf_hex(unsigned int value) {
    char digits[8];
    unsigned int count = 0, written = 0;
    if (!value) return __dyn_printf_put('0');
    while (value) {
        unsigned int digit = value & 15u;
        digits[count] = digit < 10 ? '0' + digit : 'a' + digit - 10;
        count += 1;
        value >>= 4;
    }
    while (count) { count -= 1; written += __dyn_printf_put(digits[count]); }
    return written;
}

char *screen_framebuffer(void) { return (char *)__dyn_printf_framebuffer; }

void screen_cursor(unsigned int x, unsigned int y) {
    if (x > 96) x = 96;
    if (y > 40) y = 40;
    if (y == 40) x = 0;
    __dyn_printf_cursor = y * 96u + x;
    __dyn_printf_column = x == 96 ? 0 : x;
}

/* printf(): dyncc's own frontend lowers a *call* to printf specially at
   compile time (format-string parsing happens in the frontend, see
   symphony/frontends/c/frontend.py's printf()/visit_ExprStmt -- it
   requires a string-literal format argument and rewrites the call into
   a sequence of __dyn_printf_put/string/unsigned/signed/hex calls, NOT
   a genuine runtime varargs call). There is no general runtime printf()
   to port for that reason -- dyncc never needed one.

   This target's GCC port has NOT implemented real C varargs support
   (no TARGET_SETUP_INCOMING_VARARGS -- GCC's generic va_start/va_arg
   assume a register-argument save area this port's fully custom
   prologue never establishes; confirmed broken by direct testing: a
   va_arg walk over register-passed arguments read uninitialized frame
   memory rather than the actual argument values). Implementing that
   properly is a real, separate piece of work (see README's Status/
   known-gaps section), not required for this milestone's bar. Rather
   than ship a printf() that silently breaks the moment it's called
   with more than the compile-time-fixed argument list below, this
   provides ONLY the same fixed small-arity forms
   symphony/frontends/c/frontend.py's compile-time printf lowering
   itself relies on (0-3 substitution arguments, one conversion each) --
   genuinely portable across both frontends' actual call shapes, not a
   real varargs implementation. A program needing more should call
   __dyn_printf_string/__dyn_printf_signed/__dyn_printf_unsigned/
   __dyn_printf_hex directly, exactly as dyncc's own lowering does. */

/* Not `static`: a `static` definition of these two helpers triggers a
   genuine GCC backend bug on this target -- an internal compiler error
   ("maximum number of generated reload insns per insn achieved") during
   reload, reproduced in isolation down to a single trivial one-line
   caller (`int printf(const char *format) { return printf_n(format, 0,
   0, 0, 0); }`) calling a `static` printf_n defined in the same
   translation unit; the same call compiles fine once printf_n has
   external linkage instead, or when it's genuinely external (declared
   but not defined in this TU). Root cause not fully isolated (some
   interaction between -O1's IPA/static-function handling and this
   port's restricted calling convention/register classes), same general
   "reload insns" ICE class as the documented 64-bit-arithmetic gap in
   libgcc-config/symphony/t-symphony -- flagged here as a real, still-
   open backend limitation for whoever picks this up next, worked
   around rather than blocking this milestone. */
/* KNOWN BUG, unresolved, real: printf3() (a 4-argument call passing
   through to __dyn_printf_n's 5-argument r1-r5 call) hangs -- format[i]
   is read incorrectly partway through the loop once i advances past
   the first few characters -- specifically and ONLY when preceded by
   two or more malloc() calls earlier in the SAME function (a single
   malloc() before printf3 is fine; any number of malloc()/free() calls
   before printf1() or printf2() is fine, verified). Narrowed via
   emulator instrumentation to: the loop's read of format[i] eventually
   returns garbage instead of terminating at the NUL, but the exact
   faulting register/stack-slot was not pinned down before time ran out
   on this investigation -- confirmed NOT a printf3-alone issue (works
   with zero or one preceding malloc calls) and NOT a free()/coalescing
   issue (reproduces with malloc() alone, no free() at all). Suspect
   area: something specific to a 5-live-argument-register call chain
   (r1-r5) combined with malloc's own call tree, but this was not
   proven. printf(), printf1() and printf2() (0-2 conversions) are
   fully verified correct in combination with malloc/free of any count;
   printf3() should be treated as unverified/possibly broken until this
   is root-caused. Flagged here rather than silently shipped. */
unsigned int __dyn_printf_emit_one(const char *format, unsigned int i,
                              unsigned int *consumed_conversion,
                              int has_arg, unsigned int arg) {
    unsigned int written = 0;
    if (format[i] != '%') {
        return __dyn_printf_put((unsigned char)format[i]);
    }
    *consumed_conversion = 1;
    switch (format[i + 1]) {
        case 'd': return has_arg ? __dyn_printf_signed((int)arg) : 0;
        case 'u': return has_arg ? __dyn_printf_unsigned(arg) : 0;
        case 'x': return has_arg ? __dyn_printf_hex(arg) : 0;
        case 's': return has_arg ? __dyn_printf_string((const char *)arg) : 0;
        case 'c': return has_arg ? __dyn_printf_put(arg) : 0;
        case '%': *consumed_conversion = 0; return __dyn_printf_put('%');
        default: written += __dyn_printf_put('%');
                 written += __dyn_printf_put((unsigned char)format[i + 1]);
                 return written;
    }
}

int __dyn_printf_n(const char *format, unsigned int argc, unsigned int a0,
                     unsigned int a1, unsigned int a2) {
    unsigned int written = 0, i = 0, arg_index = 0;
    unsigned int args[3];
    args[0] = a0; args[1] = a1; args[2] = a2;
    while (format[i]) {
        unsigned int consumed_conversion = 0;
        int has_arg = format[i] == '%' && arg_index < argc;
        written += __dyn_printf_emit_one(format, i, &consumed_conversion,
                             has_arg, has_arg ? args[arg_index] : 0);
        if (consumed_conversion) {
            if (has_arg) arg_index += 1;
            i += 2;
        } else {
            i += 1;
        }
    }
    return (int)written;
}

int printf(const char *format) { return __dyn_printf_n(format, 0, 0, 0, 0); }
int printf1(const char *format, unsigned int a0) { return __dyn_printf_n(format, 1, a0, 0, 0); }
int printf2(const char *format, unsigned int a0, unsigned int a1) { return __dyn_printf_n(format, 2, a0, a1, 0); }
int printf3(const char *format, unsigned int a0, unsigned int a1, unsigned int a2) { return __dyn_printf_n(format, 3, a0, a1, a2); }

unsigned char __dyn_printf_framebuffer[3840];
unsigned int __dyn_printf_cursor;
unsigned int __dyn_printf_column;
