"""fdlossless — lossless-decomposition and 4NF witness engine for FDs and MVDs.

Pure Python 3.14.7 standard library; no third-party code.
"""

from .core import (
    DEFAULT_STEP_BUDGET,
    MAX_ATTRIBUTES,
    MAX_ATTRIBUTES_MVD,
    AnalysisResult,
    ChaseStep,
    CounterExample,
    FD,
    MVD,
    FourNFViolation,
    analyze,
    attribute_closure,
    format_symbol,
    implies_mvd,
)
from .errors import FDToolError, InvalidInputError, SearchBudgetExhausted

__all__ = [
    "analyze",
    "attribute_closure",
    "implies_mvd",
    "format_symbol",
    "AnalysisResult",
    "ChaseStep",
    "CounterExample",
    "FD",
    "MVD",
    "FourNFViolation",
    "FDToolError",
    "InvalidInputError",
    "SearchBudgetExhausted",
    "MAX_ATTRIBUTES",
    "MAX_ATTRIBUTES_MVD",
    "DEFAULT_STEP_BUDGET",
]
