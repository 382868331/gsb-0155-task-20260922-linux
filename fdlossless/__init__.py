"""fdlossless — lossless-decomposition witness engine for functional dependencies.

Pure Python 3.14.7 standard library; no third-party code.
"""

from .core import (
    DEFAULT_STEP_BUDGET,
    MAX_ATTRIBUTES,
    AnalysisResult,
    ChaseStep,
    CounterExample,
    FD,
    analyze,
    format_symbol,
)
from .errors import FDToolError, InvalidInputError, SearchBudgetExhausted
from .selection import (
    MAX_CANDIDATES,
    SELECT_MAX_ATTRIBUTES,
    SelectionResult,
    select_decomposition,
)

__all__ = [
    "analyze",
    "format_symbol",
    "AnalysisResult",
    "ChaseStep",
    "CounterExample",
    "FD",
    "FDToolError",
    "InvalidInputError",
    "SearchBudgetExhausted",
    "MAX_ATTRIBUTES",
    "DEFAULT_STEP_BUDGET",
    "select_decomposition",
    "SelectionResult",
    "MAX_CANDIDATES",
    "SELECT_MAX_ATTRIBUTES",
]
