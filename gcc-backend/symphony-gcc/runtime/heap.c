/* memcpy/memmove/memset/memcmp and a first-fit + coalescing malloc/free/
   calloc/realloc heap allocator, ported verbatim from
   symphony/runtime/helpers.py's SOURCE (already plain C, no rewrite
   needed for these routines specifically).

   Deliberately EXCLUDED from this port (per the project's explicit
   scope decision, see gcc-backend/symphony-gcc/README.md): helpers.py's
   own soft-arithmetic helpers (__dyn_mul, __dyn_udivmod, __dyn_udiv,
   __dyn_umod, __dyn_sdiv, __dyn_smod, __dyn_checked_mul). Those are
   dyncc's OWN backend's internal helpers, called directly by
   symphony/targets/symphony/backend.py's binary-op lowering for
   `*`/`/`/`%` -- NOT something this GCC-targeting runtime should
   duplicate. Real libgcc (built in an earlier milestone: __mulsi3,
   __divsi3, __udivsi3, __modsi3, __umodsi3) covers the equivalent need
   for code compiled by this GCC port instead.

   dyncc's own build backs __dyn_heap_remaining with a compiler
   intrinsic (see symphony/targets/symphony/backend.py: `and address,
   address, mask; and sp_copy, sp, mask` then subtract) that computes
   space remaining between a heap address and the CURRENT STACK POINTER
   at the call site, masked to the configured RAM size -- i.e. growing
   the heap upward can never collide with the (downward-growing) stack,
   whatever the stack currently happens to be using. This port
   reproduces the same semantics directly (stack-pointer-relative, not
   a fixed ceiling) via inline asm reading sp, so heap growth remains
   safe against actual stack usage exactly as it does under dyncc's own
   backend. RAM size matches symphony/targets/symphony/config.py's
   Target default (16 MiB); a program running under a differently
   configured emulator instance should override SYMPHONY_RAM_SIZE. */

#ifndef SYMPHONY_RAM_SIZE
#define SYMPHONY_RAM_SIZE (16u * 1024u * 1024u)
#endif

unsigned int __dyn_heap_remaining(void *address) {
    unsigned int mask = SYMPHONY_RAM_SIZE - 1u;
    unsigned int here = (unsigned int)address & mask;
    unsigned int sp;
    __asm__ volatile ("mov\t%0, sp" : "=r"(sp));
    sp &= mask;
    return sp > here ? sp - here : 0;
}

void *memcpy(void *destination, const void *source, unsigned int count) {
    unsigned char *to = destination;
    const unsigned char *from = source;
    unsigned int index = 0;
    while (index < count) {
        to[index] = from[index];
        index += 1;
    }
    return destination;
}

void *memmove(void *destination, const void *source, unsigned int count) {
    unsigned char *to = destination;
    const unsigned char *from = source;
    if ((unsigned int)to < (unsigned int)from) {
        unsigned int index = 0;
        while (index < count) {
            to[index] = from[index];
            index += 1;
        }
    } else if ((unsigned int)to > (unsigned int)from) {
        while (count) {
            count -= 1;
            to[count] = from[count];
        }
    }
    return destination;
}

void *memset(void *destination, int value, unsigned int count) {
    unsigned char *bytes = destination;
    unsigned int index = 0;
    while (index < count) {
        bytes[index] = (unsigned char)value;
        index += 1;
    }
    return destination;
}

int memcmp(const void *left, const void *right, unsigned int count) {
    const unsigned char *a = left;
    const unsigned char *b = right;
    unsigned int index = 0;
    while (index < count) {
        if (a[index] != b[index]) return (int)a[index] - (int)b[index];
        index += 1;
    }
    return 0;
}

struct __dyn_heap_block {
    unsigned int size;
    struct __dyn_heap_block *next;
};

/* helpers.py declares `extern unsigned char __dyn_heap_anchor[7]` and
   relies on dyncc's own linker to place it as a marker just past the
   program's other globals (see backend.py's memory layout). This
   freestanding GCC port has no equivalent linker convention, so the
   anchor is a genuine array defined here instead -- the heap simply
   starts right after it, same effect, no special linker cooperation
   needed. */
unsigned char __dyn_heap_anchor[7];
unsigned char *__dyn_heap_end;
static struct __dyn_heap_block *__dyn_heap_free_list;

void *__dyn_heap_take_free(unsigned int size) {
    struct __dyn_heap_block *previous = 0;
    struct __dyn_heap_block *block = __dyn_heap_free_list;
    while (block) {
        if (block->size >= size) {
            unsigned int spare = block->size - size;
            if (spare >= sizeof(struct __dyn_heap_block) + 4u) {
                struct __dyn_heap_block *rest =
                    (struct __dyn_heap_block *)((unsigned char *)block
                    + sizeof(struct __dyn_heap_block) + size);
                rest->size = spare - sizeof(struct __dyn_heap_block);
                rest->next = block->next;
                if (previous) previous->next = rest;
                else __dyn_heap_free_list = rest;
                block->size = size;
            } else {
                if (previous) previous->next = block->next;
                else __dyn_heap_free_list = block->next;
            }
            block->next = 0;
            return (unsigned char *)block + sizeof(struct __dyn_heap_block);
        }
        previous = block;
        block = block->next;
    }
    return 0;
}

void *malloc(unsigned int size) {
    struct __dyn_heap_block *block;
    void *reused;
    unsigned int required;
    if (!size) return 0;
    if (size > 0xfffffff4u) return 0;
    size = (size + 3u) & ~3u;
    reused = __dyn_heap_take_free(size);
    if (reused) return reused;
    if (!__dyn_heap_end) {
        __dyn_heap_end = (unsigned char *)(((unsigned int)
            (__dyn_heap_anchor + sizeof(__dyn_heap_anchor)) + 3u) & ~3u);
    }
    required = sizeof(struct __dyn_heap_block) + size;
    if (__dyn_heap_remaining(__dyn_heap_end) < required) return 0;
    block = (struct __dyn_heap_block *)__dyn_heap_end;
    block->size = size;
    block->next = 0;
    __dyn_heap_end += required;
    return (unsigned char *)block + sizeof(struct __dyn_heap_block);
}

void free(void *pointer) {
    struct __dyn_heap_block *block;
    struct __dyn_heap_block *previous = 0;
    struct __dyn_heap_block *next = __dyn_heap_free_list;
    if (!pointer) return;
    block = (struct __dyn_heap_block *)((unsigned char *)pointer
        - sizeof(struct __dyn_heap_block));
    while (next && (unsigned int)next < (unsigned int)block) {
        previous = next;
        next = next->next;
    }
    block->next = next;
    if (previous) previous->next = block;
    else __dyn_heap_free_list = block;
    if (next && (unsigned char *)block + sizeof(struct __dyn_heap_block)
            + block->size == (unsigned char *)next) {
        block->size += sizeof(struct __dyn_heap_block) + next->size;
        block->next = next->next;
    }
    if (previous && (unsigned char *)previous + sizeof(struct __dyn_heap_block)
            + previous->size == (unsigned char *)block) {
        previous->size += sizeof(struct __dyn_heap_block) + block->size;
        previous->next = block->next;
    }
}

void *calloc(unsigned int count, unsigned int size) {
    unsigned int total;
    void *pointer;
    /* The overflow check is deliberately split across two statements
       (materializing the division into `limit` first) rather than the
       single-expression `size > 0xffffffffu / count` helpers.py uses --
       this target's GCC port hits a genuine backend bug when a libcall
       result (division has no hardware instruction here, so it's a
       call to __udivsi3) feeds directly into a comparison's RTL
       expansion at -O0: "maximum number of generated reload insns per
       insn achieved". Reproduced in isolation; splitting into a
       temporary (forcing the division's result into its own pseudo
       before the compare) avoids it entirely and is semantically
       identical. Flagged here as a real, separate backend limitation
       from the already-documented 64-bit-arithmetic reload ICE (see
       libgcc-config/symphony/t-symphony) -- worked around rather than
       blocking this milestone. */
    if (count) {
        unsigned int limit = 0xffffffffu / count;
        if (size > limit) return 0;
    }
    total = count * size;
    pointer = malloc(total);
    if (pointer) memset(pointer, 0, total);
    return pointer;
}

void *realloc(void *pointer, unsigned int size) {
    struct __dyn_heap_block *block;
    void *replacement;
    unsigned int copy_size;
    if (!pointer) return malloc(size);
    if (!size) { free(pointer); return 0; }
    block = (struct __dyn_heap_block *)((unsigned char *)pointer
        - sizeof(struct __dyn_heap_block));
    if (block->size >= size) return pointer;
    replacement = malloc(size);
    if (!replacement) return 0;
    copy_size = block->size < size ? block->size : size;
    memcpy(replacement, pointer, copy_size);
    free(pointer);
    return replacement;
}
