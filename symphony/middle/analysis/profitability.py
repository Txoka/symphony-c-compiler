"""Shared static profitability primitives for loop transformations.

Legality stays in each transformation.  This module only answers cost questions
from explicit setup/steady-state/pressure estimates and, when possible, a
bounded exact simulation of a canonical integer induction recurrence.
"""

from dataclasses import dataclass

from .cfg import build_cfg


@dataclass(frozen=True)
class LoopTransformationCost:
    """Target-instruction estimate for one proposed loop transformation."""

    trip_count: int | None
    loop_depth: int
    setup: int
    before_each: int
    after_each: int
    pressure: int = 0
    pressure_each: int = 0

    def estimated_saving(self, unknown_trip_count, depth_weight):
        trips = self.trip_count if self.trip_count is not None else unknown_trip_count
        frequency = trips * max(1, depth_weight) ** max(0, self.loop_depth - 1)
        return (
            frequency * (self.before_each - self.after_each - self.pressure_each)
            - self.setup
            - self.pressure
        )


def profitable(cost, *, unknown_trip_count=8, depth_weight=4, minimum_saving=1):
    """Whether a speed transformation clears the configured static threshold."""
    return cost.estimated_saving(unknown_trip_count, depth_weight) >= minimum_saving


def exact_trip_count(
    start,
    step,
    operator,
    bound,
    type_,
    *,
    simulation_limit=65536,
):
    """Return trips for a canonical header condition, or ``None`` if uncertain.

    Simulation deliberately follows target-width wraparound.  Repetition,
    overflow-driven nontermination, or a count beyond the analysis budget is
    reported as unknown rather than guessed.
    """
    if not type_.integer or simulation_limit < 0:
        return None
    bits = type_.size * 8
    mask = (1 << bits) - 1

    def normalize(value):
        value &= mask
        if type_.signed and value & (1 << (bits - 1)):
            value -= 1 << bits
        return value

    compare = {
        "==": lambda a, b: a == b,
        "!=": lambda a, b: a != b,
        "<": lambda a, b: a < b,
        "<=": lambda a, b: a <= b,
        ">": lambda a, b: a > b,
        ">=": lambda a, b: a >= b,
    }.get(operator)
    if compare is None:
        return None
    value, bound, step = normalize(start), normalize(bound), normalize(step)
    seen = set()
    for trips in range(simulation_limit + 1):
        if not compare(value, bound):
            return trips
        if value in seen:
            return None
        seen.add(value)
        value = normalize(value + step)
    return None


def _liveness(function):
    """Compute phi-edge-aware liveness, excluding rematerialized addresses."""
    cfg = build_cfg(function)
    rematerialized = {
        item.dst
        for block in cfg.blocks
        for item in block.instructions
        if item.dst is not None and item.op in ("const", "global_addr", "local_addr")
    }
    uses = {block.label: set() for block in cfg.blocks}
    definitions = {block.label: set() for block in cfg.blocks}
    phi_definitions = {block.label: set() for block in cfg.blocks}
    edge_uses = {}
    for block in cfg.blocks:
        seen = set()
        for item in block.instructions:
            if item.op == "phi":
                if item.dst is not None and item.dst not in rematerialized:
                    seen.add(item.dst)
                    definitions[block.label].add(item.dst)
                    phi_definitions[block.label].add(item.dst)
                for predecessor, value in item.extra:
                    if isinstance(value, int) and value not in rematerialized:
                        edge_uses.setdefault((predecessor, block.label), set()).add(value)
                continue
            uses[block.label].update(
                value for value in item.args
                if isinstance(value, int)
                and value not in seen
                and value not in rematerialized
            )
            if item.dst is not None and item.dst not in rematerialized:
                seen.add(item.dst)
                definitions[block.label].add(item.dst)
    live_in = {block.label: set() for block in cfg.blocks}
    live_out = {block.label: set() for block in cfg.blocks}
    changed = True
    while changed:
        changed = False
        for block in reversed(cfg.blocks):
            outgoing = set().union(*(
                (live_in[label] - phi_definitions[label])
                | edge_uses.get((block.label, label), set())
                for label in block.successors
            ))
            incoming = uses[block.label] | (outgoing - definitions[block.label])
            if outgoing != live_out[block.label] or incoming != live_in[block.label]:
                live_out[block.label], live_in[block.label] = outgoing, incoming
                changed = True
    return cfg, rematerialized, live_out


def _live_before(item, live_after, rematerialized):
    live = set(live_after)
    if item.dst is not None:
        live.discard(item.dst)
    if item.op != "phi":
        live.update(
            value for value in item.args
            if isinstance(value, int) and value not in rematerialized
        )
    return live


def peak_live_values(function):
    """Estimate peak simultaneously live non-rematerialized SSA values."""
    cfg, rematerialized, live_out = _liveness(function)
    peak = 0
    for block in cfg.blocks:
        live = set(live_out[block.label])
        peak = max(peak, len(live))
        for item in reversed(block.instructions):
            live = _live_before(item, live, rematerialized)
            peak = max(peak, len(live))
    return peak


def live_values_before(function, block_label, target):
    """Return non-rematerialized SSA values live immediately before ``target``."""
    cfg, rematerialized, live_out = _liveness(function)
    live = set(live_out[block_label])
    for item in reversed(cfg.by_label[block_label].instructions):
        live = _live_before(item, live, rematerialized)
        if item is target:
            return live
    raise ValueError("target instruction is not in the requested block")


__all__ = [
    "LoopTransformationCost", "exact_trip_count", "live_values_before",
    "peak_live_values", "profitable"
]
