"""Bounded evaluation of side-effect-free SSA calls with constant arguments."""

from ..analysis.cfg import build_cfg
from ..ir import BasicBlock, Instruction
from .sccp import _fold_binary, _fold_unary, _normalize


def evaluate_constant_calls(module, instruction_limit=1024):
    """Replace provably evaluable direct calls with typed constants.

    This deliberately starts with scalar SSA only: memory, nested calls, and
    target/runtime operations reject evaluation.  The instruction budget makes
    loops safe to attempt without requiring a separate trip-count proof.
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
                            instruction_limit,
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


def _evaluate(function, arguments, instruction_limit):
    if len(arguments) != len(function.params):
        return None
    cfg = build_cfg(function)
    if not cfg.blocks:
        return None
    parameters = {
        parameter.key: value for parameter, value in zip(function.params, arguments)
    }
    values = {}
    label = cfg.blocks[0].label
    predecessor = None
    steps = 0

    while steps < instruction_limit:
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
            steps += 1
            if steps > instruction_limit:
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


__all__ = ["evaluate_constant_calls"]
