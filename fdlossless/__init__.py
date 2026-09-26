"""fdlossless — lossless-decomposition witness engine for functional dependencies.

Pure Python 3.14.7 standard library; no third-party code.
"""

from .core import (
    DEFAULT_STEP_BUDGET,
    MAX_ATTRIBUTES,
    MAX_ATTRIBUTES_MVD,
    AnalysisResult,
    ChaseEvent,
    ChaseStep,
    CounterExample,
    FD,
    FourNFResult,
    FourNFViolation,
    MVD,
    MVDImplication,
    analyze,
    analyze4nf,
    attribute_closure,
    check_fd,
    check_mvd,
    format_symbol,
)
from .errors import FDToolError, InvalidInputError, SearchBudgetExhausted

__all__ = [
    "analyze",
    "analyze4nf",
    "attribute_closure",
    "check_fd",
    "check_mvd",
    "format_symbol",
    "AnalysisResult",
    "ChaseEvent",
    "ChaseStep",
    "CounterExample",
    "FD",
    "FourNFResult",
    "FourNFViolation",
    "MVD",
    "MVDImplication",
    "FDToolError",
    "InvalidInputError",
    "SearchBudgetExhausted",
    "MAX_ATTRIBUTES",
    "MAX_ATTRIBUTES_MVD",
    "DEFAULT_STEP_BUDGET",
]
