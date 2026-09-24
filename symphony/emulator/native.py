"""Optional native execution engine for the reference Machine state model."""

try:
    from . import _native
except ImportError:
    _native = None


def available(symphony=False):
    del symphony  # both ISAs are served by the same native extension
    return _native is not None


def run(machine, halt_address=None, max_steps=5_000_000, progress=None,
        progress_interval=250_000):
    engine = _native
    if engine is None:
        raise RuntimeError("native emulator extension is not installed")
    # A progress meter needs bounded calls so Python can redraw it.  Keep the
    # native predecode table for that whole run, though: otherwise every redraw
    # starts quick, branch-heavy programs with a cold decoder again.
    cache_session = progress is not None
    if cache_session:
        machine._native_keep_decode_cache = True
    try:
        while machine.steps < max_steps:
            limit = max_steps
            if progress is not None:
                limit = min(limit, machine.steps + progress_interval)
            stopped, value = engine.run_chunk(machine, halt_address, limit)
            if stopped:
                return value
            if progress is not None:
                progress(machine)
    finally:
        if cache_session:
            engine.clear_decode_cache(machine)
            del machine._native_keep_decode_cache
    raise RuntimeError(
        f"execution limit exceeded ({max_steps} instructions), PC={machine.pc:#x}"
    )


__all__ = ["available", "run"]
