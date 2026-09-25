"""fdlossless — lossless-decomposition witness engine for functional dependencies.

Pure Python 3.14.7 standard library; no third-party code.
"""

from .core import (
    DEFAULT_STEP_BUDGET,
    MAX_ATTRIBUTES,
    AnalysisDelta,
    AnalysisResult,
    AnalysisSession,
    ChaseStep,
    CommitOutcome,
    CounterExample,
    DeclaredFD,
    FD,
    TrialView,
    analyze,
    format_symbol,
)
from .errors import (
    FDToolError,
    InvalidInputError,
    SearchBudgetExhausted,
    StaleVersionError,
    TrialClosedError,
)

__all__ = [
    "analyze",
    "format_symbol",
    "AnalysisResult",
    "AnalysisDelta",
    "TrialView",
    "CommitOutcome",
    "DeclaredFD",
    "AnalysisSession",
    "ChaseStep",
    "CounterExample",
    "FD",
    "FDToolError",
    "InvalidInputError",
    "SearchBudgetExhausted",
    "StaleVersionError",
    "TrialClosedError",
    "MAX_ATTRIBUTES",
    "DEFAULT_STEP_BUDGET",
]
