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
from .errors import (
    FDToolError,
    InvalidInputError,
    NoFeasibleDecomposition,
    SearchBudgetExhausted,
)
from .select import (
    MAX_CANDIDATES,
    MAX_SELECT_ATTRIBUTES,
    SelectionResult,
    select_decomposition,
)

__all__ = [
    "analyze",
    "select_decomposition",
    "format_symbol",
    "AnalysisResult",
    "SelectionResult",
    "ChaseStep",
    "CounterExample",
    "FD",
    "FDToolError",
    "InvalidInputError",
    "NoFeasibleDecomposition",
    "SearchBudgetExhausted",
    "MAX_ATTRIBUTES",
    "MAX_SELECT_ATTRIBUTES",
    "MAX_CANDIDATES",
    "DEFAULT_STEP_BUDGET",
]
