"""Error types for the fdlossless package."""


class FDToolError(Exception):
    """Base class for every error raised by fdlossless."""


class InvalidInputError(FDToolError, ValueError):
    """Input contract violation.

    The message always locates the offending parameter, e.g.
    ``FDs[2].lhs: unknown attribute 'X'`` or
    ``attributes: 13 attributes given, at most 12 allowed``.
    Invalid input is rejected, never silently corrected.
    """


class NoFeasibleDecomposition(FDToolError):
    """No selection of the candidate subrelations meets every constraint.

    Raised by ``select_decomposition`` when no subset of the candidates
    simultaneously covers all attributes, keeps every required attribute
    group inside one selected subrelation, is lossless, and preserves the
    dependencies.  The message states which constraint class failed (and,
    for the lossless/preservation conflict, how many otherwise-eligible
    selections failed on each count).  This is a proven no-solution
    verdict within the given candidate set, not a budget exhaustion.
    """


class SearchBudgetExhausted(FDToolError):
    """The chase used up its merge budget before reaching a verdict.

    This is NOT a proof that the decomposition is lossy.  Re-run with a
    larger ``step_budget``.  A lossy verdict is only ever reported when
    the chase terminates within budget without an all-distinguished row.
    """
