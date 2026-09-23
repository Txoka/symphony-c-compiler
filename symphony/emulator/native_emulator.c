#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

/* Symphony vs. Dynphony is a per-run flag (State.is_symphony), checked once
 * per decode below. decoded_at() caches the result per PC (see below), so
 * this branch runs once per cold decode, not once per executed step -- the
 * hot dispatch loop never re-derives next_pc. */
#define DYN_NEXT_PC(pc, size) ((s)->is_symphony ? ((pc) + 4u) : ((pc) + (size)))

#if defined(__GNUC__) || defined(__clang__)
#  define DYN_LIKELY(x)   __builtin_expect(!!(x), 1)
#  define DYN_UNLIKELY(x) __builtin_expect(!!(x), 0)
#else
#  define DYN_LIKELY(x)   (x)
#  define DYN_UNLIKELY(x) (x)
#endif

/*
 * Optimized Dynphony/Symphony interpreter. Both ISAs are served by one
 * binary; State.is_symphony selects between them at decode time (see
 * DYN_NEXT_PC above), so the hot dispatch loop itself never branches on it.
 *
 * Main differences from the original implementation:
 *   - dedicated byte/BE16/BE32 memory helpers
 *   - lazy predecode cache indexed by guest PC
 *   - specialized micro-ops (RR vs RI, load/store width, branch condition, etc.)
 *   - direct-threaded dispatch (computed goto) on GCC/Clang
 *   - portable switch-dispatch fallback for MSVC
 *   - precise decode-cache invalidation for self-modifying stores
 *
 * Python objects remain only at the boundary and for guest I/O instructions.
 */

typedef enum {
    U_INVALID = 0,
    U_NOP,
    U_IN,
    U_OUT_R,
    U_OUT_I,
    U_KEY,
    U_SCREEN_R,
    U_SCREEN_I,
    U_TIME_LO,
    U_TIME_HI,
    U_GETPC,

    U_NAND_RR, U_OR_RR, U_AND_RR, U_NOR_RR, U_ADD_RR, U_SUB_RR,
    U_XOR_RR, U_SHL_RR, U_SHR_RR, U_SAR_RR, U_CMP_RR,
    U_NAND_RI, U_OR_RI, U_AND_RI, U_NOR_RI, U_ADD_RI, U_SUB_RI,
    U_XOR_RI, U_SHL_RI, U_SHR_RI, U_SAR_RI, U_CMP_RI,
    /* U_CMP_* stay adjacent to the ALU block: cmp is ALU code 10. */

    U_BRANCH_R, U_BRANCH_I,

    U_LOAD8_R, U_LOAD16_R, U_LOAD32_R, U_PLOAD32_R,
    U_STORE8_R, U_STORE16_R, U_STORE32_R, U_PSTORE32_R,
    U_LOAD8_I, U_LOAD16_I, U_LOAD32_I, U_PLOAD32_I,
    U_STORE8_I, U_STORE16_I, U_STORE32_I, U_PSTORE32_I,

    /* Keep optional device behavior out of the original hot handlers. */
    U_SCREEN_CALLBACK_R, U_SCREEN_CALLBACK_I,
    U_TIME_STEPPED_LO, U_TIME_STEPPED_HI,
    U_TIME_FREQUENCY_LO, U_TIME_FREQUENCY_HI,
    U_TIME_LIVE_LO, U_TIME_LIVE_HI,

    U_COUNT
} UOp;

typedef struct {
    uint8_t valid;
    uint8_t uop;
    uint8_t a;
    uint8_t b;
    uint8_t c;
    uint8_t opcode;
    uint16_t _pad;
    uint32_t imm;
    uint32_t next_pc;
    uint32_t pc_tag;
} Decoded;

typedef struct {
    PyObject *machine;               /* borrowed */
    PyObject *regs_obj;              /* owned */
    PyObject *inputs;                /* owned */
    PyObject *keyboard_inputs;       /* owned */
    PyObject *outputs;               /* owned */
    PyObject *screen_updates;        /* owned */
    PyObject *screen_update_callback; /* owned */
    PyObject *persistent_obj;        /* owned */
    Py_buffer memory_view;
    Py_buffer persistent_view;

    uint8_t *memory;
    uint8_t *persistent;
    /* Slot 16 is the sink for writes to zr: destination 0 is redirected there,
     * so reads of regs[0] always see zero without branching on every write.
     * Sized to a power of two to keep the surrounding struct layout aligned. */
    uint32_t regs[32];
    uint32_t mask;
    uint32_t persistent_mask;
    uint32_t pc;
    uint64_t steps;
    uint64_t time_value;
    uint64_t time_per_step_ns;
    uint64_t time_frequency_hz;
    int has_persistent;
    int is_symphony;
    int live_time;

    Decoded *decode;
    size_t decode_count;
} State;

static inline uint8_t mem8(const uint8_t *m, uint32_t mask, uint32_t a) {
    return m[a & mask];
}

static inline uint16_t mem16be(const uint8_t *m, uint32_t mask, uint32_t a) {
    uint32_t p = a & mask;
    if (DYN_LIKELY(p <= mask - 1u)) {
        return (uint16_t)(((uint16_t)m[p] << 8) | m[p + 1u]);
    }
    return (uint16_t)(((uint16_t)m[p] << 8) | m[(a + 1u) & mask]);
}

static inline uint32_t mem32be(const uint8_t *m, uint32_t mask, uint32_t a) {
    uint32_t p = a & mask;
    if (DYN_LIKELY(p <= mask - 3u)) {
        return ((uint32_t)m[p] << 24) |
               ((uint32_t)m[p + 1u] << 16) |
               ((uint32_t)m[p + 2u] << 8) |
               (uint32_t)m[p + 3u];
    }
    return ((uint32_t)m[p] << 24) |
           ((uint32_t)m[(a + 1u) & mask] << 16) |
           ((uint32_t)m[(a + 2u) & mask] << 8) |
           (uint32_t)m[(a + 3u) & mask];
}

static inline void store8(uint8_t *m, uint32_t mask, uint32_t a, uint32_t v) {
    m[a & mask] = (uint8_t)v;
}

static inline void store16be(uint8_t *m, uint32_t mask, uint32_t a, uint32_t v) {
    uint32_t p = a & mask;
    if (DYN_LIKELY(p <= mask - 1u)) {
        m[p] = (uint8_t)(v >> 8);
        m[p + 1u] = (uint8_t)v;
    } else {
        m[p] = (uint8_t)(v >> 8);
        m[(a + 1u) & mask] = (uint8_t)v;
    }
}

static inline void store32be(uint8_t *m, uint32_t mask, uint32_t a, uint32_t v) {
    uint32_t p = a & mask;
    if (DYN_LIKELY(p <= mask - 3u)) {
        m[p] = (uint8_t)(v >> 24);
        m[p + 1u] = (uint8_t)(v >> 16);
        m[p + 2u] = (uint8_t)(v >> 8);
        m[p + 3u] = (uint8_t)v;
    } else {
        m[p] = (uint8_t)(v >> 24);
        m[(a + 1u) & mask] = (uint8_t)(v >> 16);
        m[(a + 2u) & mask] = (uint8_t)(v >> 8);
        m[(a + 3u) & mask] = (uint8_t)v;
    }
}

static PyObject *get_attr(PyObject *object, const char *name) {
    return PyObject_GetAttrString(object, name);
}

static int load_state(State *s, PyObject *machine) {
    PyObject *obj = NULL;
    Py_ssize_t i;
    Py_ssize_t memory_size;
    memset(s, 0, sizeof(*s));
    s->machine = machine;

    obj = get_attr(machine, "memory");
    if (!obj || !PyByteArray_Check(obj)) {
        Py_XDECREF(obj);
        PyErr_SetString(PyExc_TypeError, "machine.memory must be a bytearray");
        return -1;
    }
    memory_size = PyByteArray_GET_SIZE(obj);
    if (memory_size <= 0) {
        Py_DECREF(obj);
        PyErr_SetString(PyExc_ValueError, "machine.memory must not be empty");
        return -1;
    }
    if (PyObject_GetBuffer(obj, &s->memory_view, PyBUF_WRITABLE) < 0) {
        Py_DECREF(obj);
        return -1;
    }
    s->memory = (uint8_t *)s->memory_view.buf;
    Py_DECREF(obj);

#define LOAD_OWNED(field, name) do { \
    s->field = get_attr(machine, name); \
    if (!s->field) return -1; \
} while (0)
    LOAD_OWNED(regs_obj, "regs");
    LOAD_OWNED(inputs, "inputs");
    LOAD_OWNED(keyboard_inputs, "keyboard_inputs");
    LOAD_OWNED(outputs, "outputs");
    LOAD_OWNED(screen_updates, "screen_updates");
    LOAD_OWNED(screen_update_callback, "screen_update_callback");
    LOAD_OWNED(persistent_obj, "persistent");
#undef LOAD_OWNED

    if (!PyList_Check(s->regs_obj) || PyList_GET_SIZE(s->regs_obj) != 16) {
        PyErr_SetString(PyExc_TypeError, "machine.regs must be a 16-item list");
        return -1;
    }
    for (i = 0; i < 16; ++i) {
        s->regs[i] = (uint32_t)PyLong_AsUnsignedLongMask(PyList_GET_ITEM(s->regs_obj, i));
        if (PyErr_Occurred()) return -1;
    }

#define LOAD_U32(name, target) do { \
    obj = get_attr(machine, name); \
    if (!obj) return -1; \
    target = (uint32_t)PyLong_AsUnsignedLongMask(obj); \
    Py_DECREF(obj); \
    if (PyErr_Occurred()) return -1; \
} while (0)
    LOAD_U32("mask", s->mask);
    LOAD_U32("pc", s->pc);
#undef LOAD_U32

    /* The implementation relies on the original emulator's mask semantics. */
    if ((uint64_t)s->mask + 1u > (uint64_t)memory_size) {
        PyErr_SetString(PyExc_ValueError, "machine.mask addresses beyond machine.memory");
        return -1;
    }

    obj = get_attr(machine, "symphony");
    if (!obj) return -1;
    s->is_symphony = PyObject_IsTrue(obj);
    Py_DECREF(obj);
    if (s->is_symphony < 0) return -1;

    obj = get_attr(machine, "live_time");
    if (!obj) return -1;
    s->live_time = PyObject_IsTrue(obj);
    Py_DECREF(obj);
    if (s->live_time < 0) return -1;

    obj = get_attr(machine, "steps");
    if (!obj) return -1;
    s->steps = PyLong_AsUnsignedLongLong(obj);
    Py_DECREF(obj);
    if (PyErr_Occurred()) return -1;

    obj = get_attr(machine, "time_value");
    if (!obj) return -1;
    s->time_value = PyLong_AsUnsignedLongLongMask(obj);
    Py_DECREF(obj);
    if (PyErr_Occurred()) return -1;

    obj = get_attr(machine, "time_per_step_ns");
    if (!obj) return -1;
    s->time_per_step_ns = PyLong_AsUnsignedLongLongMask(obj);
    Py_DECREF(obj);
    if (PyErr_Occurred()) return -1;

    obj = get_attr(machine, "time_frequency_hz");
    if (!obj) return -1;
    s->time_frequency_hz = PyLong_AsUnsignedLongLongMask(obj);
    Py_DECREF(obj);
    if (PyErr_Occurred()) return -1;

    if (!PyByteArray_Check(s->persistent_obj)) {
        PyErr_SetString(PyExc_TypeError, "machine.persistent must be a bytearray");
        return -1;
    }
    if (PyByteArray_GET_SIZE(s->persistent_obj) != 0) {
        if (PyObject_GetBuffer(
                s->persistent_obj, &s->persistent_view, PyBUF_WRITABLE) < 0)
            return -1;
        s->has_persistent = 1;
        s->persistent = (uint8_t *)s->persistent_view.buf;
        obj = get_attr(machine, "persistent_mask");
        if (!obj) return -1;
        s->persistent_mask = (uint32_t)PyLong_AsUnsignedLongMask(obj);
        Py_DECREF(obj);
        if (PyErr_Occurred()) return -1;
        if ((uint64_t)s->persistent_mask + 1u > (uint64_t)PyByteArray_GET_SIZE(s->persistent_obj)) {
            PyErr_SetString(PyExc_ValueError, "machine.persistent_mask addresses beyond persistent memory");
            return -1;
        }
    }

    s->decode_count = (size_t)s->mask + 1u;
    if (s->decode_count > SIZE_MAX / sizeof(Decoded)) {
        PyErr_NoMemory();
        return -1;
    }
    s->decode = (Decoded *)PyMem_Calloc(s->decode_count, sizeof(Decoded));
    if (!s->decode) {
        PyErr_NoMemory();
        return -1;
    }
    return 0;
}

static void release_state(State *s) {
    PyMem_Free(s->decode);
    s->decode = NULL;
    if (s->memory_view.obj) PyBuffer_Release(&s->memory_view);
    if (s->persistent_view.obj) PyBuffer_Release(&s->persistent_view);
    Py_XDECREF(s->regs_obj);
    Py_XDECREF(s->inputs);
    Py_XDECREF(s->keyboard_inputs);
    Py_XDECREF(s->outputs);
    Py_XDECREF(s->screen_updates);
    Py_XDECREF(s->screen_update_callback);
    Py_XDECREF(s->persistent_obj);
}

static int set_attr_u64(PyObject *object, const char *name, uint64_t value) {
    PyObject *number = PyLong_FromUnsignedLongLong(value);
    int result;
    if (!number) return -1;
    result = PyObject_SetAttrString(object, name, number);
    Py_DECREF(number);
    return result;
}

static int sync_state(State *s) {
    PyObject *value = NULL;
    int i;
    for (i = 0; i < 16; ++i) {
        value = PyLong_FromUnsignedLong(s->regs[i]);
        if (!value) return -1;
        if (PyList_SetItem(s->regs_obj, i, value) < 0) return -1; /* steals value */
    }
    if (set_attr_u64(s->machine, "pc", s->pc) < 0 ||
        set_attr_u64(s->machine, "steps", s->steps) < 0)
        return -1;
    return 0;
}

static int append_u32(PyObject *list, uint32_t value) {
    PyObject *number = PyLong_FromUnsignedLong(value);
    int result;
    if (!number) return -1;
    result = PyList_Append(list, number);
    Py_DECREF(number);
    return result;
}

static int queue_pop(PyObject *queue, uint32_t *result) {
    PyObject *value;
    int truth = PyObject_IsTrue(queue);
    if (truth < 0) return -1;
    if (!truth) {
        *result = 0;
        return 0;
    }
    value = PyObject_CallMethod(queue, "popleft", NULL);
    if (!value) return -1;
    *result = (uint32_t)PyLong_AsUnsignedLongMask(value);
    Py_DECREF(value);
    return PyErr_Occurred() ? -1 : 0;
}

static int append_screen(PyObject *list, uint32_t setting, uint32_t value) {
    PyObject *pair = Py_BuildValue("(II)", setting, value);
    int result;
    if (!pair) return -1;
    result = PyList_Append(list, pair);
    Py_DECREF(pair);
    return result;
}

static int call_screen_update_callback(State *s, uint32_t setting, uint32_t value) {
    PyObject *result = PyObject_CallFunction(
        s->screen_update_callback, "IIK", setting, value,
        (unsigned long long)(s->steps + 1u));
    int stop;
    if (!result) return -1;
    stop = PyObject_IsTrue(result);
    Py_DECREF(result);
    return stop;
}

static uint64_t current_time_ns(void) {
    struct timespec now;
#if defined(_WIN32)
    if (timespec_get(&now, TIME_UTC) != TIME_UTC) return 0;
#else
    if (clock_gettime(CLOCK_REALTIME, &now) != 0) return 0;
#endif
    return (uint64_t)now.tv_sec * UINT64_C(1000000000) + (uint64_t)now.tv_nsec;
}

static inline uint64_t frequency_time_ns(const State *s) {
    uint64_t seconds = s->steps / s->time_frequency_hz;
    uint64_t cycles = s->steps % s->time_frequency_hz;
    return s->time_value + seconds * UINT64_C(1000000000)
        + cycles * UINT64_C(1000000000) / s->time_frequency_hz;
}

static inline void invalidate_decode(State *s, uint32_t address, unsigned size) {
    /* Any <=4-byte instruction beginning up to three bytes before the write can overlap it. */
    int delta;
    int end = (int)size - 1;
    for (delta = -3; delta <= end; ++delta) {
        uint32_t p = (address + (uint32_t)delta) & s->mask;
        s->decode[p].valid = 0;
    }
}

/* Writes to zr go to the sink slot, so zr always reads zero. */
static inline uint8_t dst(uint8_t r) {
    return r ? r : 16u;
}

static int decode_instruction(State *s, uint32_t pc, Decoded *d) {
    const uint8_t *m = s->memory;
    const uint32_t mask = s->mask;
    uint8_t op = mem8(m, mask, pc);
    uint8_t x, code;
    int immediate;

    memset(d, 0, sizeof(*d));
    d->opcode = op;
    d->uop = U_INVALID;
    d->pc_tag = pc;

    if (op == 0x00) {
        d->uop = U_NOP;
        d->next_pc = DYN_NEXT_PC(pc, 1u);
    } else if (op == 0x01) {
        d->uop = U_IN;
        d->a = dst(mem8(m, mask, pc + 1u) >> 4);
        d->next_pc = DYN_NEXT_PC(pc, 2u);
    } else if (op == 0x02) {
        d->uop = U_OUT_R;
        d->a = mem8(m, mask, pc + 2u) & 15u;
        d->next_pc = DYN_NEXT_PC(pc, 3u);
    } else if (op == 0x12) {
        d->uop = U_OUT_I;
        d->imm = mem16be(m, mask, pc + 2u);
        d->next_pc = DYN_NEXT_PC(pc, 4u);
    } else if (op == 0x03) {
        d->uop = U_KEY;
        d->a = dst(mem8(m, mask, pc + 1u) >> 4);
        d->next_pc = DYN_NEXT_PC(pc, 2u);
    } else if (op == 0x04) {
        d->uop = s->screen_update_callback == Py_None
            ? U_SCREEN_R : U_SCREEN_CALLBACK_R;
        d->a = mem8(m, mask, pc + 1u) & 15u;
        d->c = mem8(m, mask, pc + 2u) & 15u;
        d->next_pc = DYN_NEXT_PC(pc, 3u);
    } else if (op == 0x14) {
        d->uop = s->screen_update_callback == Py_None
            ? U_SCREEN_I : U_SCREEN_CALLBACK_I;
        d->a = mem8(m, mask, pc + 1u) & 15u;
        d->imm = mem16be(m, mask, pc + 2u);
        d->next_pc = DYN_NEXT_PC(pc, 4u);
    } else if (op == 0x05) {
        d->uop = s->live_time ? U_TIME_LIVE_LO
            : s->time_frequency_hz ? U_TIME_FREQUENCY_LO
            : s->time_per_step_ns ? U_TIME_STEPPED_LO : U_TIME_LO;
        d->a = dst(mem8(m, mask, pc + 1u) >> 4);
        d->next_pc = DYN_NEXT_PC(pc, 2u);
    } else if (op == 0x06) {
        d->uop = s->live_time ? U_TIME_LIVE_HI
            : s->time_frequency_hz ? U_TIME_FREQUENCY_HI
            : s->time_per_step_ns ? U_TIME_STEPPED_HI : U_TIME_HI;
        d->a = dst(mem8(m, mask, pc + 1u) >> 4);
        d->next_pc = DYN_NEXT_PC(pc, 2u);
    } else if (op == 0x07) {
        d->uop = U_GETPC;
        d->a = dst(mem8(m, mask, pc + 1u) >> 4);
        d->next_pc = DYN_NEXT_PC(pc, 2u);
    } else if (op >= 0x20 && op <= 0x3a && (op & 15u) <= 10u) {
        x = mem8(m, mask, pc + 1u);
        d->a = dst(x >> 4);  /* dst */
        d->b = x & 15u;      /* lhs */
        code = op & 15u;
        immediate = (op & 16u) != 0;
        if (immediate) {
            d->imm = mem16be(m, mask, pc + 2u);
            d->uop = (uint8_t)(U_NAND_RI + code);
            d->next_pc = DYN_NEXT_PC(pc, 4u);
        } else {
            d->c = mem8(m, mask, pc + 2u) & 15u;
            d->uop = (uint8_t)(U_NAND_RR + code);
            d->next_pc = DYN_NEXT_PC(pc, 3u);
        }
    } else if (op >= 0x40 && op <= 0x5f) {
        immediate = (op & 16u) != 0;
        /* Condition bits and the flag-source register, both read at execute time. */
        d->b = (uint8_t)(op & 15u);
        d->a = mem8(m, mask, pc + 1u) & 15u;
        if (immediate) {
            d->imm = mem16be(m, mask, pc + 2u);
            d->uop = U_BRANCH_I;
            d->next_pc = DYN_NEXT_PC(pc, 4u);
        } else {
            d->c = mem8(m, mask, pc + 2u) & 15u;
            d->uop = U_BRANCH_R;
            d->next_pc = DYN_NEXT_PC(pc, 3u);
        }
    } else if (op >= 0x60 && op <= 0x77) {
        immediate = (op & 16u) != 0;
        code = op & 7u;
        x = mem8(m, mask, pc + 1u);
        /* Loads write their destination; stores only read their source. */
        d->a = (code < 4u) ? dst(x >> 4) : (x & 15u);
        if (immediate) {
            d->imm = mem16be(m, mask, pc + 2u);
            d->uop = (uint8_t)(U_LOAD8_I + code);
            d->next_pc = DYN_NEXT_PC(pc, 4u);
        } else {
            d->c = mem8(m, mask, pc + 2u) & 15u;
            d->uop = (uint8_t)(U_LOAD8_R + code);
            d->next_pc = DYN_NEXT_PC(pc, 3u);
        }
    }

    d->valid = 1;
    return 0;
}

static inline Decoded *decoded_at(State *s, uint32_t pc) {
    Decoded *d = &s->decode[pc & s->mask];
    if (DYN_UNLIKELY(!d->valid || d->pc_tag != pc))
        decode_instruction(s, pc, d);
    return d;
}

/* cmp's result word: bit0 equals, bit1 lower (unsigned), bit2 less (signed). */
static inline uint32_t flags32(uint32_t a, uint32_t b) {
    return (uint32_t)(a == b) |
           ((uint32_t)(a < b) << 1) |
           ((uint32_t)((int32_t)a < (int32_t)b) << 2);
}

static inline uint32_t sar32(uint32_t a, uint32_t b) {
    if (b >= 32u)
        return (a & 0x80000000u) ? 0xffffffffu : 0u;
    return (uint32_t)((int32_t)a >> b);
}

static PyObject *run_chunk(PyObject *self, PyObject *args) {
    PyObject *machine, *halt_obj, *ret;
    State s;
    Decoded *d = NULL;
    uint64_t step_limit;
    uint32_t halt = 0;
    uint32_t previous = 0;
    uint32_t rhs, addr, next;
    int has_halt, stopped = 0;
    int error = 0;
    (void)self;

    if (!PyArg_ParseTuple(args, "OOK:run_chunk", &machine, &halt_obj, &step_limit))
        return NULL;
    has_halt = halt_obj != Py_None;
    if (has_halt) {
        halt = (uint32_t)PyLong_AsUnsignedLongMask(halt_obj);
        if (PyErr_Occurred()) return NULL;
    }
    if (load_state(&s, machine) < 0) {
        release_state(&s);
        return NULL;
    }

#define FINISH_INSN(newpc) do { \
    uint32_t _np = (uint32_t)(newpc); \
    s.pc = _np; \
    ++s.steps; \
    if (DYN_UNLIKELY(s.pc == previous)) { stopped = 1; goto done; } \
    if (DYN_UNLIKELY(s.steps >= step_limit)) goto done; \
    if (DYN_UNLIKELY(has_halt && s.pc == halt)) { stopped = 1; goto done; } \
    previous = s.pc; \
    d = decoded_at(&s, s.pc); \
    goto dispatch; \
} while (0)

#define FINISH_SCREEN(newpc, screen_result) do { \
    int _screen_result = (screen_result); \
    if (_screen_result < 0) { error = 1; goto done; } \
    if (_screen_result > 0) { \
        s.pc = (uint32_t)(newpc); \
        ++s.steps; \
        stopped = 1; \
        goto done; \
    } \
    FINISH_INSN(newpc); \
} while (0)

/* Mask the condition bits against the named flag register, OR-reduce, then
 * invert when condition bit 3 is set. */
#define BRANCH_TAKEN(d) \
    ((((d)->b & s.regs[(d)->a] & 7u) != 0u) ^ (((d)->b & 8u) != 0u))

#define BAD_PERSISTENT() do { \
    if (DYN_UNLIKELY(!s.has_persistent)) { \
        PyErr_SetString(PyExc_RuntimeError, "persistent memory is not configured"); \
        error = 1; goto done; \
    } \
} while (0)

    if (s.steps >= step_limit) goto done;
    if (has_halt && s.pc == halt) { stopped = 1; goto done; }
    previous = s.pc;
    d = decoded_at(&s, s.pc);

#if defined(__GNUC__) || defined(__clang__)
    {
        static void *const labels[U_COUNT] = {
            &&L_INVALID, &&L_NOP, &&L_IN, &&L_OUT_R, &&L_OUT_I, &&L_KEY,
            &&L_SCREEN_R, &&L_SCREEN_I, &&L_TIME_LO, &&L_TIME_HI, &&L_GETPC,
            &&L_NAND_RR, &&L_OR_RR, &&L_AND_RR, &&L_NOR_RR, &&L_ADD_RR, &&L_SUB_RR,
            &&L_XOR_RR, &&L_SHL_RR, &&L_SHR_RR, &&L_SAR_RR, &&L_CMP_RR,
            &&L_NAND_RI, &&L_OR_RI, &&L_AND_RI, &&L_NOR_RI, &&L_ADD_RI, &&L_SUB_RI,
            &&L_XOR_RI, &&L_SHL_RI, &&L_SHR_RI, &&L_SAR_RI, &&L_CMP_RI,
            &&L_BRANCH_R, &&L_BRANCH_I,
            &&L_LOAD8_R, &&L_LOAD16_R, &&L_LOAD32_R, &&L_PLOAD32_R,
            &&L_STORE8_R, &&L_STORE16_R, &&L_STORE32_R, &&L_PSTORE32_R,
            &&L_LOAD8_I, &&L_LOAD16_I, &&L_LOAD32_I, &&L_PLOAD32_I,
            &&L_STORE8_I, &&L_STORE16_I, &&L_STORE32_I, &&L_PSTORE32_I,
            &&L_SCREEN_CALLBACK_R, &&L_SCREEN_CALLBACK_I,
            &&L_TIME_STEPPED_LO, &&L_TIME_STEPPED_HI,
            &&L_TIME_FREQUENCY_LO, &&L_TIME_FREQUENCY_HI,
            &&L_TIME_LIVE_LO, &&L_TIME_LIVE_HI
        };

dispatch:
        goto *labels[d->uop];

L_INVALID:
        PyErr_Format(PyExc_RuntimeError, "unsupported opcode %#x at %#x", d->opcode, previous);
        error = 1; goto done;
L_NOP:      FINISH_INSN(d->next_pc);
L_IN:       if (queue_pop(s.inputs, &s.regs[d->a]) < 0) { error = 1; goto done; } FINISH_INSN(d->next_pc);
L_OUT_R:    if (append_u32(s.outputs, s.regs[d->a]) < 0) { error = 1; goto done; } FINISH_INSN(d->next_pc);
L_OUT_I:    if (append_u32(s.outputs, d->imm) < 0) { error = 1; goto done; } FINISH_INSN(d->next_pc);
L_KEY:      if (queue_pop(s.keyboard_inputs, &s.regs[d->a]) < 0) { error = 1; goto done; } FINISH_INSN(d->next_pc);
L_SCREEN_R: if (append_screen(s.screen_updates, s.regs[d->a], s.regs[d->c]) < 0) { error = 1; goto done; } FINISH_INSN(d->next_pc);
L_SCREEN_I: if (append_screen(s.screen_updates, s.regs[d->a], d->imm) < 0) { error = 1; goto done; } FINISH_INSN(d->next_pc);
L_TIME_LO:  s.regs[d->a] = (uint32_t)s.time_value; FINISH_INSN(d->next_pc);
L_TIME_HI:  s.regs[d->a] = (uint32_t)(s.time_value >> 32); FINISH_INSN(d->next_pc);
L_GETPC:    s.regs[d->a] = previous; FINISH_INSN(d->next_pc);

#define RR_BIN(label, expr) label: rhs = s.regs[d->c]; s.regs[d->a] = (expr); FINISH_INSN(d->next_pc)
#define RI_BIN(label, expr) label: rhs = d->imm;       s.regs[d->a] = (expr); FINISH_INSN(d->next_pc)
        RR_BIN(L_NAND_RR, ~(s.regs[d->b] & rhs));
        RR_BIN(L_OR_RR,    s.regs[d->b] | rhs);
        RR_BIN(L_AND_RR,   s.regs[d->b] & rhs);
        RR_BIN(L_NOR_RR,  ~(s.regs[d->b] | rhs));
        RR_BIN(L_ADD_RR,   s.regs[d->b] + rhs);
        RR_BIN(L_SUB_RR,   s.regs[d->b] - rhs);
        RR_BIN(L_XOR_RR,   s.regs[d->b] ^ rhs);
        RR_BIN(L_SHL_RR,   rhs < 32u ? s.regs[d->b] << rhs : 0u);
        RR_BIN(L_SHR_RR,   rhs < 32u ? s.regs[d->b] >> rhs : 0u);
        RR_BIN(L_SAR_RR,   sar32(s.regs[d->b], rhs));
L_CMP_RR:  s.regs[d->a] = flags32(s.regs[d->b], s.regs[d->c]); FINISH_INSN(d->next_pc);

        RI_BIN(L_NAND_RI, ~(s.regs[d->b] & rhs));
        RI_BIN(L_OR_RI,    s.regs[d->b] | rhs);
        RI_BIN(L_AND_RI,   s.regs[d->b] & rhs);
        RI_BIN(L_NOR_RI,  ~(s.regs[d->b] | rhs));
        RI_BIN(L_ADD_RI,   s.regs[d->b] + rhs);
        RI_BIN(L_SUB_RI,   s.regs[d->b] - rhs);
        RI_BIN(L_XOR_RI,   s.regs[d->b] ^ rhs);
        RI_BIN(L_SHL_RI,   rhs < 32u ? s.regs[d->b] << rhs : 0u);
        RI_BIN(L_SHR_RI,   rhs < 32u ? s.regs[d->b] >> rhs : 0u);
        RI_BIN(L_SAR_RI,   sar32(s.regs[d->b], rhs));
L_CMP_RI:  s.regs[d->a] = flags32(s.regs[d->b], d->imm); FINISH_INSN(d->next_pc);
#undef RR_BIN
#undef RI_BIN

L_BRANCH_R: next = BRANCH_TAKEN(d) ? s.regs[d->c] : d->next_pc; FINISH_INSN(next);
L_BRANCH_I: next = BRANCH_TAKEN(d) ? d->imm       : d->next_pc; FINISH_INSN(next);

L_LOAD8_R:  addr=s.regs[d->c]; s.regs[d->a]=mem8(s.memory,s.mask,addr);  FINISH_INSN(d->next_pc);
L_LOAD16_R: addr=s.regs[d->c]; s.regs[d->a]=mem16be(s.memory,s.mask,addr);  FINISH_INSN(d->next_pc);
L_LOAD32_R: addr=s.regs[d->c]; s.regs[d->a]=mem32be(s.memory,s.mask,addr);  FINISH_INSN(d->next_pc);
L_PLOAD32_R: BAD_PERSISTENT(); addr=s.regs[d->c]; s.regs[d->a]=mem32be(s.persistent,s.persistent_mask,addr); FINISH_INSN(d->next_pc);
L_STORE8_R: addr=s.regs[d->c]; store8(s.memory,s.mask,addr,s.regs[d->a]); invalidate_decode(&s,addr,1); FINISH_INSN(d->next_pc);
L_STORE16_R: addr=s.regs[d->c]; store16be(s.memory,s.mask,addr,s.regs[d->a]); invalidate_decode(&s,addr,2); FINISH_INSN(d->next_pc);
L_STORE32_R: addr=s.regs[d->c]; store32be(s.memory,s.mask,addr,s.regs[d->a]); invalidate_decode(&s,addr,4); FINISH_INSN(d->next_pc);
L_PSTORE32_R: BAD_PERSISTENT(); addr=s.regs[d->c]; store32be(s.persistent,s.persistent_mask,addr,s.regs[d->a]); FINISH_INSN(d->next_pc);

L_LOAD8_I:  addr=d->imm; s.regs[d->a]=mem8(s.memory,s.mask,addr);  FINISH_INSN(d->next_pc);
L_LOAD16_I: addr=d->imm; s.regs[d->a]=mem16be(s.memory,s.mask,addr);  FINISH_INSN(d->next_pc);
L_LOAD32_I: addr=d->imm; s.regs[d->a]=mem32be(s.memory,s.mask,addr);  FINISH_INSN(d->next_pc);
L_PLOAD32_I: BAD_PERSISTENT(); addr=d->imm; s.regs[d->a]=mem32be(s.persistent,s.persistent_mask,addr); FINISH_INSN(d->next_pc);
L_STORE8_I: addr=d->imm; store8(s.memory,s.mask,addr,s.regs[d->a]); invalidate_decode(&s,addr,1); FINISH_INSN(d->next_pc);
L_STORE16_I: addr=d->imm; store16be(s.memory,s.mask,addr,s.regs[d->a]); invalidate_decode(&s,addr,2); FINISH_INSN(d->next_pc);
L_STORE32_I: addr=d->imm; store32be(s.memory,s.mask,addr,s.regs[d->a]); invalidate_decode(&s,addr,4); FINISH_INSN(d->next_pc);
L_PSTORE32_I: BAD_PERSISTENT(); addr=d->imm; store32be(s.persistent,s.persistent_mask,addr,s.regs[d->a]); FINISH_INSN(d->next_pc);

L_SCREEN_CALLBACK_R:
    if (append_screen(s.screen_updates, s.regs[d->a], s.regs[d->c]) < 0) { error = 1; goto done; }
    FINISH_SCREEN(d->next_pc, call_screen_update_callback(&s, s.regs[d->a], s.regs[d->c]));
L_SCREEN_CALLBACK_I:
    if (append_screen(s.screen_updates, s.regs[d->a], d->imm) < 0) { error = 1; goto done; }
    FINISH_SCREEN(d->next_pc, call_screen_update_callback(&s, s.regs[d->a], d->imm));
L_TIME_STEPPED_LO: s.regs[d->a]=(uint32_t)(s.time_value+s.steps*s.time_per_step_ns); FINISH_INSN(d->next_pc);
L_TIME_STEPPED_HI: s.regs[d->a]=(uint32_t)((s.time_value+s.steps*s.time_per_step_ns)>>32); FINISH_INSN(d->next_pc);
L_TIME_FREQUENCY_LO: s.regs[d->a]=(uint32_t)frequency_time_ns(&s); FINISH_INSN(d->next_pc);
L_TIME_FREQUENCY_HI: s.regs[d->a]=(uint32_t)(frequency_time_ns(&s)>>32); FINISH_INSN(d->next_pc);
L_TIME_LIVE_LO: s.regs[d->a]=(uint32_t)current_time_ns(); FINISH_INSN(d->next_pc);
L_TIME_LIVE_HI: s.regs[d->a]=(uint32_t)(current_time_ns()>>32); FINISH_INSN(d->next_pc);
    }
#else
    /* MSVC/portable fallback. Still benefits from predecode and specialized uops. */
    for (;;) {
dispatch:
        switch ((UOp)d->uop) {
            case U_INVALID: PyErr_Format(PyExc_RuntimeError, "unsupported opcode %#x at %#x", d->opcode, previous); error=1; goto done;
            case U_NOP: FINISH_INSN(d->next_pc);
            case U_IN: if(queue_pop(s.inputs,&s.regs[d->a])<0){error=1;goto done;} FINISH_INSN(d->next_pc);
            case U_OUT_R: if(append_u32(s.outputs,s.regs[d->a])<0){error=1;goto done;} FINISH_INSN(d->next_pc);
            case U_OUT_I: if(append_u32(s.outputs,d->imm)<0){error=1;goto done;} FINISH_INSN(d->next_pc);
            case U_KEY: if(queue_pop(s.keyboard_inputs,&s.regs[d->a])<0){error=1;goto done;} FINISH_INSN(d->next_pc);
            case U_SCREEN_R: if(append_screen(s.screen_updates,s.regs[d->a],s.regs[d->c])<0){error=1;goto done;} FINISH_INSN(d->next_pc);
            case U_SCREEN_I: if(append_screen(s.screen_updates,s.regs[d->a],d->imm)<0){error=1;goto done;} FINISH_INSN(d->next_pc);
            case U_TIME_LO: s.regs[d->a]=(uint32_t)s.time_value; FINISH_INSN(d->next_pc);
            case U_TIME_HI: s.regs[d->a]=(uint32_t)(s.time_value>>32); FINISH_INSN(d->next_pc);
            case U_GETPC: s.regs[d->a]=previous; FINISH_INSN(d->next_pc);

#define SW_RR(u, expr) case u: rhs=s.regs[d->c]; s.regs[d->a]=(expr); FINISH_INSN(d->next_pc)
#define SW_RI(u, expr) case u: rhs=d->imm; s.regs[d->a]=(expr); FINISH_INSN(d->next_pc)
            SW_RR(U_NAND_RR, ~(s.regs[d->b]&rhs)); SW_RR(U_OR_RR,s.regs[d->b]|rhs); SW_RR(U_AND_RR,s.regs[d->b]&rhs); SW_RR(U_NOR_RR,~(s.regs[d->b]|rhs));
            SW_RR(U_ADD_RR,s.regs[d->b]+rhs); SW_RR(U_SUB_RR,s.regs[d->b]-rhs); SW_RR(U_XOR_RR,s.regs[d->b]^rhs); SW_RR(U_SHL_RR,rhs<32u?s.regs[d->b]<<rhs:0u); SW_RR(U_SHR_RR,rhs<32u?s.regs[d->b]>>rhs:0u); SW_RR(U_SAR_RR,sar32(s.regs[d->b],rhs));
            case U_CMP_RR: s.regs[d->a]=flags32(s.regs[d->b],s.regs[d->c]);FINISH_INSN(d->next_pc);
            SW_RI(U_NAND_RI, ~(s.regs[d->b]&rhs)); SW_RI(U_OR_RI,s.regs[d->b]|rhs); SW_RI(U_AND_RI,s.regs[d->b]&rhs); SW_RI(U_NOR_RI,~(s.regs[d->b]|rhs));
            SW_RI(U_ADD_RI,s.regs[d->b]+rhs); SW_RI(U_SUB_RI,s.regs[d->b]-rhs); SW_RI(U_XOR_RI,s.regs[d->b]^rhs); SW_RI(U_SHL_RI,rhs<32u?s.regs[d->b]<<rhs:0u); SW_RI(U_SHR_RI,rhs<32u?s.regs[d->b]>>rhs:0u); SW_RI(U_SAR_RI,sar32(s.regs[d->b],rhs));
            case U_CMP_RI: s.regs[d->a]=flags32(s.regs[d->b],d->imm);FINISH_INSN(d->next_pc);
#undef SW_RR
#undef SW_RI

            case U_BRANCH_R: next=BRANCH_TAKEN(d)?s.regs[d->c]:d->next_pc; FINISH_INSN(next);
            case U_BRANCH_I: next=BRANCH_TAKEN(d)?d->imm:d->next_pc; FINISH_INSN(next);

            case U_LOAD8_R: addr=s.regs[d->c];s.regs[d->a]=mem8(s.memory,s.mask,addr);FINISH_INSN(d->next_pc);
            case U_LOAD16_R:addr=s.regs[d->c];s.regs[d->a]=mem16be(s.memory,s.mask,addr);FINISH_INSN(d->next_pc);
            case U_LOAD32_R:addr=s.regs[d->c];s.regs[d->a]=mem32be(s.memory,s.mask,addr);FINISH_INSN(d->next_pc);
            case U_PLOAD32_R:BAD_PERSISTENT();addr=s.regs[d->c];s.regs[d->a]=mem32be(s.persistent,s.persistent_mask,addr);FINISH_INSN(d->next_pc);
            case U_STORE8_R:addr=s.regs[d->c];store8(s.memory,s.mask,addr,s.regs[d->a]);invalidate_decode(&s,addr,1);FINISH_INSN(d->next_pc);
            case U_STORE16_R:addr=s.regs[d->c];store16be(s.memory,s.mask,addr,s.regs[d->a]);invalidate_decode(&s,addr,2);FINISH_INSN(d->next_pc);
            case U_STORE32_R:addr=s.regs[d->c];store32be(s.memory,s.mask,addr,s.regs[d->a]);invalidate_decode(&s,addr,4);FINISH_INSN(d->next_pc);
            case U_PSTORE32_R:BAD_PERSISTENT();addr=s.regs[d->c];store32be(s.persistent,s.persistent_mask,addr,s.regs[d->a]);FINISH_INSN(d->next_pc);
            case U_LOAD8_I:addr=d->imm;s.regs[d->a]=mem8(s.memory,s.mask,addr);FINISH_INSN(d->next_pc);
            case U_LOAD16_I:addr=d->imm;s.regs[d->a]=mem16be(s.memory,s.mask,addr);FINISH_INSN(d->next_pc);
            case U_LOAD32_I:addr=d->imm;s.regs[d->a]=mem32be(s.memory,s.mask,addr);FINISH_INSN(d->next_pc);
            case U_PLOAD32_I:BAD_PERSISTENT();addr=d->imm;s.regs[d->a]=mem32be(s.persistent,s.persistent_mask,addr);FINISH_INSN(d->next_pc);
            case U_STORE8_I:addr=d->imm;store8(s.memory,s.mask,addr,s.regs[d->a]);invalidate_decode(&s,addr,1);FINISH_INSN(d->next_pc);
            case U_STORE16_I:addr=d->imm;store16be(s.memory,s.mask,addr,s.regs[d->a]);invalidate_decode(&s,addr,2);FINISH_INSN(d->next_pc);
            case U_STORE32_I:addr=d->imm;store32be(s.memory,s.mask,addr,s.regs[d->a]);invalidate_decode(&s,addr,4);FINISH_INSN(d->next_pc);
            case U_PSTORE32_I:BAD_PERSISTENT();addr=d->imm;store32be(s.persistent,s.persistent_mask,addr,s.regs[d->a]);FINISH_INSN(d->next_pc);
            case U_SCREEN_CALLBACK_R: if(append_screen(s.screen_updates,s.regs[d->a],s.regs[d->c])<0){error=1;goto done;} FINISH_SCREEN(d->next_pc,call_screen_update_callback(&s,s.regs[d->a],s.regs[d->c]));
            case U_SCREEN_CALLBACK_I: if(append_screen(s.screen_updates,s.regs[d->a],d->imm)<0){error=1;goto done;} FINISH_SCREEN(d->next_pc,call_screen_update_callback(&s,s.regs[d->a],d->imm));
            case U_TIME_STEPPED_LO:s.regs[d->a]=(uint32_t)(s.time_value+s.steps*s.time_per_step_ns);FINISH_INSN(d->next_pc);
            case U_TIME_STEPPED_HI:s.regs[d->a]=(uint32_t)((s.time_value+s.steps*s.time_per_step_ns)>>32);FINISH_INSN(d->next_pc);
            case U_TIME_FREQUENCY_LO:s.regs[d->a]=(uint32_t)frequency_time_ns(&s);FINISH_INSN(d->next_pc);
            case U_TIME_FREQUENCY_HI:s.regs[d->a]=(uint32_t)(frequency_time_ns(&s)>>32);FINISH_INSN(d->next_pc);
            case U_TIME_LIVE_LO:s.regs[d->a]=(uint32_t)current_time_ns();FINISH_INSN(d->next_pc);
            case U_TIME_LIVE_HI:s.regs[d->a]=(uint32_t)(current_time_ns()>>32);FINISH_INSN(d->next_pc);
            default: PyErr_SetString(PyExc_RuntimeError,"internal decoder error");error=1;goto done;
        }
    }
#endif

#undef FINISH_SCREEN

done:
    if (sync_state(&s) < 0) error = 1;
    if (error) {
        release_state(&s);
        return NULL;
    }
    ret = Py_BuildValue("(iI)", stopped, s.regs[1]);
    release_state(&s);
    return ret;

#undef FINISH_INSN
#undef REQUIRE_CMP
#undef BAD_PERSISTENT
}

static PyMethodDef methods[] = {
    {"run_chunk", run_chunk, METH_VARARGS, "Execute a bounded optimized native instruction batch."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT,
    "_native",
    "Optimized native Dynphony/Symphony emulator core.",
    -1,
    methods
};

PyMODINIT_FUNC PyInit__native(void) {
    return PyModule_Create(&module);
}
