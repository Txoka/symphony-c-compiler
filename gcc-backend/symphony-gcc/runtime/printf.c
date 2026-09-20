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

unsigned int __dyn_printf_put(unsigned int value);
unsigned int __dyn_printf_string(const char *text);

int putchar(int character) {
    __dyn_printf_put((unsigned int)(unsigned char)character);
    return (unsigned char)character;
}

int puts(const char *text) {
    __dyn_printf_string(text);
    __dyn_printf_put(10);
    return 0;
}

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
/* RESOLVED (was: "KNOWN BUG, unresolved, real: printf3() ... hangs ...
   preceded by two or more malloc() calls"). Root-caused via emulator
   single-stepping, not a printf3/register-allocation bug at all: it was
   a heap layout bug in runtime/heap.c's malloc(). malloc() used to
   compute "start of heap" as the address right after its own
   __dyn_heap_anchor[7] BSS array, relying on __dyn_heap_anchor being the
   last symbol in the whole linked image -- true only by accident of
   link/object order. As soon as any other object's BSS symbol happened
   to be placed after heap.o's (completely ordinary, unavoidable in a
   multi-file link), the first malloc() call's returned block silently
   overlapped that neighboring global's storage instead of real free
   memory: the second sequential struct-field store inside malloc()
   (`block->size = size; block->next = 0;`) corrupted it. In the
   traced case that neighbor was __dyn_heap_end itself; a few calls
   later a corrupted __dyn_heap_end value (which happened to look like
   a code address) got treated as a jump target somewhere downstream,
   landing execution in garbage memory well after the point of actual
   corruption -- which is why it looked like a printf3-specific,
   argument-count-specific hang (needs 2+ preceding malloc() calls to
   accumulate enough heap growth to actually collide with something,
   and needs enough call depth afterward for the corruption to surface
   as a visible crash). printf3()/__dyn_printf_n's own r1-r5 argument
   handling was never at fault and needed no changes.

   Fixed at the root: symphony_ld.py's Linker.link() now synthesizes
   __dyn_heap_anchor itself, as a zero-size marker for the address right
   after ALL objects' sections are laid out (computed last, so it is
   correct regardless of link order) -- heap.c no longer defines it as
   real BSS storage, just `extern unsigned char __dyn_heap_anchor[];`.
   Verified via the emulator: the original repro (two malloc() calls
   then printf3()) now halts cleanly instead of hanging (see the linker
   change's commit for the exact before/after emulator trace). */
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
