"""Rules DSL: safe expression evaluator over a small library of metric
lookups. See :mod:`.dsl` for the parser/evaluator and :mod:`.metrics` for the
functions exposed to rule authors."""

from .dsl import DslError, compile_expr, evaluate
from .metrics import METRICS, MetricContext

__all__ = ["DslError", "compile_expr", "evaluate", "METRICS", "MetricContext"]
