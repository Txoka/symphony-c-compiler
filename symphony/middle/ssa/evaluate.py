"""Bounded evaluation of side-effect-free SSA calls with constant arguments."""

from ..analysis.cfg import build_cfg
from ..analysis.dominance import build_dominator_tree, find_natural_loops
from ..ir import BasicBlock, Instruction
from .module import analyze_immutable_globals
from .sccp import _fold_binary, _fold_unary, _normalize


def evaluate_constant_calls(module, instruction_limit=1024):
    """Replace provably evaluable direct calls with typed constants.

    This deliberately starts with scalar SSA only: memory and target/runtime
    operations reject evaluation. Direct call towers share one instruction
    budget, and recursive cycles are rejected. The budget makes loops safe to
    attempt without requiring a separate trip-count proof.
    """
    functions = {function.name: function for function in module.functions}
    changed = False
    for caller in module.functions:
        definitions = {
            item.dst: item
            for block in caller.blocks
            for item in block.instructions
            if item.dst is not None
        }
        rewritten = []
        for block in caller.blocks:
            items = []
            for item in block.instructions:
                replacement = None
                if (
                    item.op == "direct_call"
                    and item.dst is not None
                    and item.type.kind != "void"
                    and item.extra in functions
                ):
                    arguments = [definitions.get(value) for value in item.args]
                    if all(value is not None and value.op == "const" for value in arguments):
                        result = _evaluate(
                            functions[item.extra],
                            tuple(value.extra for value in arguments),
                            functions,
                            [instruction_limit],
                            set(),
                        )
                        if result is not None:
                            replacement = Instruction(
                                "const", item.dst, (), item.type,
                                _normalize(result, item.type),
                            )
                items.append(replacement or item)
                changed |= replacement is not None
            rewritten.append(BasicBlock(block.label, items))
        caller.blocks = rewritten
    return changed


def evaluate_constant_loops(module, iteration_limit=8, instruction_limit=1024):
    """Collapse fully evaluable natural-loop regions inside larger functions."""
    globals_, unsafe, _ = analyze_immutable_globals(module)
    immutable = {name: value for name, value in globals_.items() if name not in unsafe}
    changed = False
    for function in module.functions:
        while _evaluate_one_loop(function, immutable, iteration_limit, instruction_limit):
            changed = True
    return changed


def _evaluate_one_loop(function, immutable, iteration_limit, instruction_limit):
    cfg = build_cfg(function)
    loops = sorted(
        find_natural_loops(build_dominator_tree(cfg)), key=lambda loop: len(loop.blocks)
    )
    definitions = {
        item.dst: item
        for block in cfg.blocks
        for item in block.instructions
        if item.dst is not None
    }
    def_block = {
        item.dst: block.label
        for block in cfg.blocks
        for item in block.instructions
        if item.dst is not None
    }

    for loop in loops:
        header = cfg.by_label[loop.header]
        outside = [value for value in header.predecessors if value not in loop.blocks]
        exits = {
            successor
            for label in loop.blocks
            for successor in cfg.by_label[label].successors
            if successor not in loop.blocks
        }
        if len(outside) != 1 or not exits:
            continue
        preheader = outside[0]
        if header.label not in cfg.by_label[preheader].successors:
            continue
        if len(cfg.by_label[preheader].successors) != 1:
            continue
        phis = [item for item in header.instructions if item.op == "phi"]
        if not phis or any(
            set(source for source, _ in phi.extra) != set(header.predecessors)
            for phi in phis
        ):
            continue
        loop_definitions = {
            item.dst
            for label in loop.blocks
            for item in cfg.by_label[label].instructions
            if item.dst is not None
        }
        static_cache = {}

        def static_value(value, visiting=None):
            if value in static_cache:
                return static_cache[value]
            visiting = set() if visiting is None else visiting
            if value in visiting or def_block.get(value) in loop.blocks:
                return None
            visiting.add(value)
            item = definitions.get(value)
            result = None
            if item is not None and item.op == "const":
                result = _normalize(item.extra, item.type)
            elif item is not None and item.op == "global_addr" and item.extra in immutable:
                result = ("global", item.extra, 0)
            elif item is not None and item.op in ("copy", "cast"):
                result = static_value(item.args[0], visiting)
                if result is not None and item.type.integer and isinstance(result, int):
                    result = _normalize(result, item.type)
            elif item is not None and item.op == "binary":
                left = static_value(item.args[0], set(visiting))
                right = static_value(item.args[1], set(visiting))
                result = _region_binary(item.extra, left, right, item.type)
            static_cache[value] = result
            return result

        values = {
            value: result
            for value in definitions
            if def_block.get(value) not in loop.blocks
            if (result := static_value(value)) is not None
        }
        for phi in phis:
            incoming = dict(phi.extra).get(preheader)
            value = static_value(incoming) if incoming is not None else None
            if value is None:
                break
            values[incoming] = value
            values[phi.dst] = value
        else:
            result = _run_loop_region(
                cfg, loop, values, immutable, iteration_limit, instruction_limit
            )
            if result is None:
                continue
            exit_source, exit_label, final_values = result
            live_out = {phi.dst for phi in phis}
            live_out.update(
                value
                for block in cfg.blocks
                if block.label not in loop.blocks
                for item in block.instructions
                for value in item.args
                if value in loop_definitions
            )
            live_out.update(
                value
                for block in cfg.blocks
                if block.label not in loop.blocks
                for item in block.instructions
                if item.op == "phi"
                for source, value in item.extra
                if value in loop_definitions
            )
            if any(not isinstance(final_values.get(value), int) for value in live_out):
                continue

            preheader_block = cfg.by_label[preheader]
            if preheader_block.terminator() is not None:
                terminator = preheader_block.instructions[-1]
                if terminator.op != "jump" or cfg.label_blocks.get(
                    terminator.extra, terminator.extra
                ) != header.label:
                    continue
                preheader_block.instructions.pop()
            preheader_block.instructions.extend(
                Instruction(
                    "const", value, (), definitions[value].type, final_values[value]
                )
                for value in sorted(live_out)
            )
            preheader_block.instructions.append(Instruction("jump", extra=exit_label))

            surviving = []
            for block in function.blocks:
                if block.label in loop.blocks:
                    continue
                items = []
                for item in block.instructions:
                    if item.op == "phi":
                        extra = tuple(
                            (source, value)
                            for source, value in item.extra
                            if source not in loop.blocks
                        ) + tuple(
                            (preheader, value)
                            for source, value in item.extra
                            if source == exit_source
                        )
                        item = Instruction("phi", item.dst, (), item.type, extra)
                    items.append(item)
                surviving.append(BasicBlock(block.label, items))
            function.blocks = surviving
            return True
    return False


def _run_loop_region(cfg, loop, initial, immutable, iteration_limit, instruction_limit):
    values = dict(initial)
    label = loop.header
    predecessor = next(
        value for value in cfg.by_label[loop.header].predecessors if value not in loop.blocks
    )
    header_visits = 0
    steps = 0
    while steps < instruction_limit:
        block = cfg.by_label[label]
        if label == loop.header:
            header_visits += 1
            if header_visits > iteration_limit + 1:
                return None
        incoming_values = {}
        for item in block.instructions:
            if item.op != "phi":
                continue
            incoming = dict(item.extra).get(predecessor)
            if incoming is None or incoming not in values:
                return None
            incoming_values[item.dst] = values[incoming]
        values.update(incoming_values)
        transferred = False
        for item in block.instructions:
            if item.op in ("label", "phi"):
                continue
            steps += 1
            if steps > instruction_limit:
                return None
            if item.op == "const":
                values[item.dst] = _normalize(item.extra, item.type)
            elif item.op == "global_addr" and item.extra in immutable:
                values[item.dst] = ("global", item.extra, 0)
            elif item.op in ("copy", "cast"):
                if item.args[0] not in values:
                    return None
                value = values[item.args[0]]
                values[item.dst] = (
                    _normalize(value, item.type)
                    if isinstance(value, int) and item.type.integer
                    else value
                )
            elif item.op == "unary":
                value = values.get(item.args[0])
                if not isinstance(value, int):
                    return None
                result = _fold_unary(item.extra, value, item.type)
                if result is None:
                    return None
                values[item.dst] = result
            elif item.op == "binary":
                if any(value not in values for value in item.args):
                    return None
                result = _region_binary(
                    item.extra, values[item.args[0]], values[item.args[1]], item.type
                )
                if result is None:
                    return None
                values[item.dst] = result
            elif item.op == "load":
                pointer = values.get(item.args[0])
                if not (
                    isinstance(pointer, tuple)
                    and len(pointer) == 3
                    and pointer[0] == "global"
                    and pointer[1] in immutable
                ):
                    return None
                global_ = immutable[pointer[1]]
                offset = pointer[2]
                if not 0 <= offset <= len(global_.data) - item.type.size:
                    return None
                raw = int.from_bytes(
                    global_.data[offset:offset + item.type.size], "big", signed=False
                )
                values[item.dst] = _normalize(raw, item.type)
            elif item.op == "jump":
                next_label = cfg.label_blocks.get(item.extra, item.extra)
                predecessor, label = label, next_label
                transferred = True
                break
            elif item.op in ("branch_if", "cbranch_if"):
                if item.op == "branch_if":
                    condition = values.get(item.args[0])
                    if not isinstance(condition, int):
                        return None
                    take_target = bool(condition) == item.extra[0]
                else:
                    left, right = (values.get(value) for value in item.args)
                    result = _region_binary(item.extra[0], left, right, item.type)
                    if result is None:
                        return None
                    take_target = bool(result)
                target = cfg.label_blocks.get(item.extra[1], item.extra[1])
                alternatives = [value for value in block.successors if value != target]
                if not take_target and len(alternatives) != 1:
                    return None
                next_label = target if take_target else alternatives[0]
                if next_label not in loop.blocks:
                    return label, next_label, values
                predecessor, label = label, next_label
                transferred = True
                break
            else:
                return None
        if transferred:
            continue
        if len(block.successors) != 1:
            return None
        next_label = block.successors[0]
        if next_label not in loop.blocks:
            return label, next_label, values
        predecessor, label = label, next_label
    return None


def _region_binary(operator, left, right, type_):
    if isinstance(left, tuple) and isinstance(right, int) and operator in ("+", "-"):
        return left[0], left[1], left[2] + (right if operator == "+" else -right)
    if isinstance(right, tuple) and isinstance(left, int) and operator == "+":
        return right[0], right[1], right[2] + left
    if not isinstance(left, int) or not isinstance(right, int):
        return None
    return _safe_binary(operator, left, right, type_)


def _evaluate(function, arguments, functions, budget, active):
    if len(arguments) != len(function.params):
        return None
    if function.name in active:
        return None
    cfg = build_cfg(function)
    if not cfg.blocks:
        return None
    active = active | {function.name}
    parameters = {
        parameter.key: value for parameter, value in zip(function.params, arguments)
    }
    values = {}
    label = cfg.blocks[0].label
    predecessor = None
    while budget[0] > 0:
        block = cfg.by_label[label]
        phi_values = {}
        for item in block.instructions:
            if item.op != "phi":
                continue
            incoming = next(
                (value for source, value in item.extra if source == predecessor),
                None,
            )
            if incoming is None or incoming not in values:
                return None
            phi_values[item.dst] = values[incoming]
        values.update(phi_values)

        transferred = False
        for item in block.instructions:
            if item.op in ("label", "phi"):
                continue
            budget[0] -= 1
            if budget[0] < 0:
                return None
            if item.op == "param":
                if item.extra[1] not in parameters:
                    return None
                values[item.dst] = _normalize(parameters[item.extra[1]], item.type)
            elif item.op == "const":
                values[item.dst] = _normalize(item.extra, item.type)
            elif item.op in ("local_addr", "global_addr"):
                # Address declarations left behind by scalar promotion are
                # harmless until an operation actually consumes them.
                values[item.dst] = None
            elif item.op == "copy":
                if item.args[0] not in values or values[item.args[0]] is None:
                    return None
                values[item.dst] = values[item.args[0]]
            elif item.op == "cast":
                if item.args[0] not in values or values[item.args[0]] is None:
                    return None
                values[item.dst] = _normalize(values[item.args[0]], item.type)
            elif item.op == "unary":
                if item.args[0] not in values or values[item.args[0]] is None:
                    return None
                result = _fold_unary(item.extra, values[item.args[0]], item.type)
                if result is None:
                    return None
                values[item.dst] = result
            elif item.op == "binary":
                if any(value not in values or values[value] is None for value in item.args):
                    return None
                result = _safe_binary(
                    item.extra, values[item.args[0]], values[item.args[1]], item.type
                )
                if result is None:
                    return None
                values[item.dst] = result
            elif item.op == "direct_call":
                if (
                    item.dst is None
                    or item.type.kind == "void"
                    or item.extra not in functions
                    or any(
                        value not in values or not isinstance(values[value], int)
                        for value in item.args
                    )
                ):
                    return None
                result = _evaluate(
                    functions[item.extra],
                    tuple(values[value] for value in item.args),
                    functions,
                    budget,
                    active,
                )
                if result is None:
                    return None
                values[item.dst] = _normalize(result, item.type)
            elif item.op == "return":
                if len(item.args) != 1 or item.args[0] not in values:
                    return None
                return values[item.args[0]]
            elif item.op == "jump":
                predecessor, label = label, cfg.label_blocks[item.extra]
                transferred = True
                break
            elif item.op in ("branch_if", "cbranch_if"):
                if any(value not in values or values[value] is None for value in item.args):
                    return None
                if item.op == "branch_if":
                    outcome = bool(values[item.args[0]])
                    take_target = outcome == item.extra[0]
                else:
                    result = _safe_binary(
                        item.extra[0],
                        values[item.args[0]],
                        values[item.args[1]],
                        item.type,
                    )
                    if result is None:
                        return None
                    take_target = bool(result)
                target = cfg.label_blocks[item.extra[1]]
                if take_target:
                    next_label = target
                else:
                    alternatives = [value for value in block.successors if value != target]
                    if len(alternatives) != 1:
                        return None
                    next_label = alternatives[0]
                predecessor, label = label, next_label
                transferred = True
                break
            else:
                return None
        if transferred:
            continue
        if len(block.successors) != 1:
            return None
        predecessor, label = label, block.successors[0]
    return None


def _safe_binary(operator, left, right, type_):
    if not type_.integer:
        return None
    bits = type_.size * 8
    if type_.signed:
        low, high = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
        if operator in ("+", "-", "*"):
            mathematical = {
                "+": left + right,
                "-": left - right,
                "*": left * right,
            }[operator]
            if not low <= mathematical <= high:
                return None
        if operator in ("/", "%") and left == low and right == -1:
            return None
    return _fold_binary(operator, left, right, type_)


__all__ = ["evaluate_constant_calls", "evaluate_constant_loops"]
