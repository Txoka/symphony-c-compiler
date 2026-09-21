"""Built-in C declarations lowered directly to target opcodes."""

PROTOTYPES = r"""
unsigned int input(void);
void output(unsigned int value);
unsigned int keyboard(void);
void screen(unsigned int setting, unsigned int value);
unsigned int time(void);
unsigned int time_low(void);
unsigned int time_high(void);
unsigned int persistent_load(unsigned int address);
void persistent_store(unsigned int address, unsigned int value);
void jump(unsigned int address);
unsigned int __dyn_heap_remaining(void *address);
"""

LIBRARY_PROTOTYPES = r"""
void *memcpy(void *destination, const void *source, unsigned int count);
void *memmove(void *destination, const void *source, unsigned int count);
void *memset(void *destination, int value, unsigned int count);
int memcmp(const void *left, const void *right, unsigned int count);
void *malloc(unsigned int size);
void free(void *pointer);
void *calloc(unsigned int count, unsigned int size);
void *realloc(void *pointer, unsigned int size);
int printf(const char *format);
char *screen_framebuffer(void);
void screen_cursor(unsigned int x, unsigned int y);
"""

# Text-screen runtime. ASCII 8 mode maps one byte to each of 96 * 40 cells.
# Its functions and globals are pruned like every other unused runtime symbol.
SCREEN_SOURCE = r"""
unsigned char __dyn_printf_framebuffer[3840];
unsigned int __dyn_printf_cursor;
unsigned int __dyn_printf_column;

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
"""

NAMES = frozenset(
    {
        "input",
        "output",
        "keyboard",
        "screen",
        "time",
        "time_low",
        "time_high",
        "persistent_load",
        "persistent_store",
        "jump",
        "__dyn_heap_remaining",
    }
)

TEXT_SCREEN_NAMES = frozenset({"printf", "screen_framebuffer", "screen_cursor"})
