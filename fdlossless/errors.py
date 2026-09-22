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


class SearchBudgetExhausted(FDToolError):
    """The chase used up its merge budget before reaching a verdict.

    This is NOT a proof that the decomposition is lossy.  Re-run with a
    larger ``step_budget``.  A lossy verdict is only ever reported when
    the chase terminates within budget without an all-distinguished row.
    """
