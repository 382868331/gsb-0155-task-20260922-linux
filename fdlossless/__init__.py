"""fdlossless — lossless-decomposition witness engine for functional dependencies.

Pure Python 3.14.7 standard library; no third-party code.
"""

from .core import (
    DEFAULT_STEP_BUDGET,
    MAX_ATTRIBUTES,
    AnalysisDiff,
    AnalysisResult,
    AnalysisVersionStore,
    ChaseStep,
    CommitReceipt,
    CounterExample,
    DeclaredFD,
    FD,
    TrialOutcome,
    analyze,
    format_symbol,
)
from .errors import (
    FDToolError,
    InvalidInputError,
    SearchBudgetExhausted,
    StaleVersionError,
    TrialConflictError,
    TrialIncomplete,
)

__all__ = [
    "analyze",
    "format_symbol",
    "AnalysisVersionStore",
    "AnalysisResult",
    "AnalysisDiff",
    "ChaseStep",
    "CommitReceipt",
    "CounterExample",
    "DeclaredFD",
    "FD",
    "TrialOutcome",
    "FDToolError",
    "InvalidInputError",
    "SearchBudgetExhausted",
    "StaleVersionError",
    "TrialConflictError",
    "TrialIncomplete",
    "MAX_ATTRIBUTES",
    "DEFAULT_STEP_BUDGET",
]
