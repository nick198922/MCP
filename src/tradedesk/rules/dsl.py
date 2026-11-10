"""Minimal, safe boolean-expression DSL.

Allowed grammar: literals (int, float, str, bool, None), identifier lookups,
comparisons, boolean operators (``and`` / ``or`` / ``not``), unary ``-``,
simple arithmetic (``+ - * / // % **``; ``**`` takes an exponent of at most
+-1000 and no integer result may exceed 4096 bits; no str / list result may exceed
100 000 items and ``%`` does not format strings), and calls to a *whitelisted* set of
functions. No attribute access, no subscripts, no comprehensions, no
assignments; at most 10 000 characters and 100 levels of nesting. Suitable for
user-authored alert rules.

Call ``compile_expr(src)`` once to validate and get a reusable AST, then
``evaluate(ast, env)`` with an environment ``{names: values, calls: {name: fn}}``.
Callables may be *async* — ``evaluate`` is an ``async`` function and awaits
coroutines returned by them.

Everything the evaluator refuses raises ``DslError``.
"""

from __future__ import annotations

import ast
import inspect
import operator
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class DslError(Exception):
    """Raised for anything the DSL does not allow or cannot execute."""


# `**` bounds: `9 ** 9 ** 9` computed an integer of hundreds of millions of digits and blocked the event loop. 10 ** 1000
# (3322 bits) still evaluates; an integer beyond MAX_INT_BITS is far outside the float range any metric is compared with.
MAX_POW_EXPONENT = 1000
MAX_INT_BITS = 4096
# Sequence size bound: `"a" * 10**9` allocated a gigabyte (rules_test, the alerts job on a stored rule) before item 299.
# A repetition is checked BEFORE it is computed, a concatenation after; `%` formatting of a str / bytes is refused
# outright (`"%999999999d" % 1` is as big, and a rule has no use for it).
MAX_SEQ_LEN = 100_000
# Expression size bounds (item 301): `1 + 1 + ... + 1` with 5000 terms compiled, was stored by rules_add and then raised
# RecursionError on every evaluation (the alerts job skipped it silently each tick); a 50k-term comparison chain held the
# event loop ~0.5 s per evaluation. The evaluator recurses once per nesting level, so the depth bound keeps it far below
# the Python stack limit wherever it is called from; real rules are under 200 characters and a few levels deep.
MAX_EXPR_LEN = 10_000
MAX_DEPTH = 100
_SEQ = (str, bytes, bytearray, list, tuple)
_NOT_CONST = object()


def _pow(base: Any, exp: Any) -> Any:
    """``base ** exp`` with the size checked BEFORE computing: |exp| > MAX_POW_EXPONENT, or an integer result that is
    sure to be longer than MAX_INT_BITS, raises DslError; so does a complex result (a negative base to a fractional
    power)."""
    if isinstance(exp, int | float) and abs(exp) > MAX_POW_EXPONENT:
        raise DslError(f"exponent {exp!r} is beyond the allowed +-{MAX_POW_EXPONENT}")
    # |base| >= 2 ** (bit_length - 1), so the result has at least (bit_length - 1) * exp + 1 bits.
    if isinstance(base, int) and isinstance(exp, int) and exp > 0 and (abs(base).bit_length() - 1) * exp >= MAX_INT_BITS:
        raise DslError(f"integer result beyond {MAX_INT_BITS} bits")
    result = operator.pow(base, exp)
    if isinstance(result, complex):
        raise DslError("** gave a complex number (negative base to a fractional power)")
    return result


_CMP = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}

_BIN = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: _pow,
}


def _binop(op: ast.operator, left: Any, right: Any) -> Any:
    """One arithmetic step; an integer result longer than MAX_INT_BITS raises DslError, so every operand of the next
    step stays small (a product of two such integers is at most twice as long). A str / bytes / list / tuple result
    longer than MAX_SEQ_LEN raises DslError too, a repetition before it is computed; `%` on a str / bytes is refused."""
    if isinstance(op, ast.Mod) and isinstance(left, str | bytes | bytearray):
        raise DslError("string formatting (%) is not allowed")
    if isinstance(op, ast.Mult):
        for seq, n in ((left, right), (right, left)):
            if isinstance(seq, _SEQ) and isinstance(n, int) and len(seq) * n > MAX_SEQ_LEN:
                raise DslError(f"sequence result beyond {MAX_SEQ_LEN} items")
    result = _BIN[type(op)](left, right)
    if isinstance(result, int) and result.bit_length() > MAX_INT_BITS:
        raise DslError(f"integer result beyond {MAX_INT_BITS} bits")
    if isinstance(result, _SEQ) and len(result) > MAX_SEQ_LEN:
        raise DslError(f"sequence result beyond {MAX_SEQ_LEN} items")
    return result


def _fold(node: ast.AST) -> Any:
    """Value of a subtree of numeric / string literals, unary +/- and arithmetic, else _NOT_CONST; one pass over every
    node. Raises DslError where the evaluator would (an oversized `**`, integer or sequence, `%` on a string); any other
    error of a literal subtree (1 / 0, a float overflow, "a" + 1) is left to evaluation, as before."""
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, int | float | str | bytes) else _NOT_CONST
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        v = _fold(node.operand)
        if not isinstance(v, int | float):  # _NOT_CONST, or a string (-"a" is left to evaluation)
            return _NOT_CONST
        return v if isinstance(node.op, ast.UAdd) else -v
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        left, right = _fold(node.left), _fold(node.right)
        if left is _NOT_CONST or right is _NOT_CONST:
            return _NOT_CONST
        try:
            return _binop(node.op, left, right)
        except DslError:
            raise
        except Exception:  # noqa: BLE001 - ZeroDivisionError, OverflowError: reported by the evaluation
            return _NOT_CONST
    for child in ast.iter_child_nodes(node):
        _fold(child)
    return _NOT_CONST


def _too_deep(root: ast.AST) -> bool:
    """True when the tree nests more than MAX_DEPTH nodes deep (operator / context leaves count as one more level).
    Iterative, so the check itself cannot overflow the stack."""
    stack = [(root, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > MAX_DEPTH:
            return True
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return False


@dataclass(frozen=True)
class CompiledExpr:
    source: str
    tree: ast.Expression


def compile_expr(src: str) -> CompiledExpr:
    """Parse and validate the expression. No execution yet."""
    if not isinstance(src, str) or not src.strip():
        raise DslError("empty expression")
    if len(src) > MAX_EXPR_LEN:
        raise DslError(f"expression longer than {MAX_EXPR_LEN} characters")
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as e:
        raise DslError(f"syntax error: {e.msg}") from None
    except ValueError as e:  # Python 3.11: a null byte in the source
        raise DslError(f"syntax error: {e}") from None
    except (RecursionError, MemoryError):  # a long flat chain such as 1 + 1 + ... (100k terms) overflows the parser's stack
        raise DslError("expression too long or too deeply nested") from None
    if _too_deep(tree.body):
        raise DslError(f"expression nested deeper than {MAX_DEPTH} levels")

    for node in ast.walk(tree):
        if isinstance(
            node,
            (
                ast.Attribute,
                ast.Subscript,
                ast.Lambda,
                ast.IfExp,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
                ast.Starred,
                ast.NamedExpr,
                ast.Yield,
                ast.YieldFrom,
                ast.Await,
            ),
        ):
            raise DslError(f"disallowed syntax: {type(node).__name__}")
        if isinstance(node, ast.BinOp) and type(node.op) not in _BIN:
            raise DslError(f"disallowed operator: {type(node.op).__name__}")
        # Reject dunder names so `__import__('os')` and friends never reach the
        # evaluator — they would be caught there anyway, but failing at compile
        # time keeps rules_test honest about validation vs runtime data.
        if isinstance(node, ast.Name) and node.id.startswith("_"):
            raise DslError(f"disallowed name: {node.id!r}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id.startswith("_"):
            raise DslError(f"disallowed call: {node.func.id!r}")
    # A literal `9 ** 9 ** 9` is refused here, so rules_add answers with an expression error instead of storing a rule
    # that can never evaluate (the evaluator refuses it too, for operands that come from metrics).
    try:
        _fold(tree.body)
    except RecursionError:  # deeper than the Python stack: the evaluator reports it, as before
        pass
    return CompiledExpr(source=src, tree=tree)


async def evaluate(expr: CompiledExpr | str, env: dict[str, Any]) -> Any:
    """Execute a compiled expression (or source string) against an env.

    ``env`` is ``{"names": {name: value}, "calls": {name: callable}}``. Calls
    may be ``async def`` — this function awaits them.
    """
    compiled = expr if isinstance(expr, CompiledExpr) else compile_expr(expr)
    names: dict[str, Any] = env.get("names", {}) or {}
    calls: dict[str, Callable[..., Any]] = env.get("calls", {}) or {}
    return await _eval(compiled.tree.body, names, calls)


async def _eval(node: ast.AST, names: dict[str, Any], calls: dict[str, Callable[..., Any]]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in names:
            return names[node.id]
        raise DslError(f"unknown name: {node.id!r}")
    if isinstance(node, ast.UnaryOp):
        v = await _eval(node.operand, names, calls)
        if isinstance(node.op, ast.USub):
            return -v
        if isinstance(node.op, ast.UAdd):
            return +v
        if isinstance(node.op, ast.Not):
            return not v
        raise DslError(f"disallowed unary op: {type(node.op).__name__}")
    if isinstance(node, ast.BinOp):
        left = await _eval(node.left, names, calls)
        right = await _eval(node.right, names, calls)
        try:
            return _binop(node.op, left, right)
        except OverflowError as e:  # a big int mixed with a float (2**4000 + 0.5), or a float ** beyond the float range
            raise DslError(f"arithmetic overflow: {e}") from None
    if isinstance(node, ast.Compare):
        left = await _eval(node.left, names, calls)
        for op_node, comparator in zip(node.ops, node.comparators, strict=False):
            right = await _eval(comparator, names, calls)
            if type(op_node) not in _CMP:
                raise DslError(f"disallowed comparator: {type(op_node).__name__}")
            if not _CMP[type(op_node)](left, right):
                return False
            left = right
        return True
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            for v in node.values:
                r = await _eval(v, names, calls)
                if not r:
                    return r
            return r  # last truthy
        if isinstance(node.op, ast.Or):
            for v in node.values:
                r = await _eval(v, names, calls)
                if r:
                    return r
            return r
        raise DslError(f"disallowed boolop: {type(node.op).__name__}")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise DslError("calls must be bare function names")
        fn = calls.get(node.func.id)
        if fn is None:
            raise DslError(f"unknown function: {node.func.id!r}")
        if node.keywords:
            raise DslError("keyword arguments are not supported")
        args = [await _eval(a, names, calls) for a in node.args]
        result = fn(*args)
        if inspect.isawaitable(result):
            result = await result
        return result
    raise DslError(f"unsupported expression: {type(node).__name__}")
