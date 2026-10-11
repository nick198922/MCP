"""Minimal, safe boolean-expression DSL.

Allowed grammar: literals (int, float, str, bool, None), identifier lookups,
comparisons, boolean operators (``and`` / ``or`` / ``not``), unary ``-``,
simple arithmetic (``+ - * / // % **``), and calls to a *whitelisted* set of
functions. No attribute access, no subscripts, no comprehensions, no
assignments. Suitable for user-authored alert rules.

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
    ast.Pow: operator.pow,
}


@dataclass(frozen=True)
class CompiledExpr:
    source: str
    tree: ast.Expression


def compile_expr(src: str) -> CompiledExpr:
    """Parse and validate the expression. No execution yet."""
    if not isinstance(src, str) or not src.strip():
        raise DslError("empty expression")
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as e:
        raise DslError(f"syntax error: {e.msg}") from None

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
        return _BIN[type(node.op)](left, right)
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
