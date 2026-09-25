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

    Inside a trial the same rule applies: when the recomputation under a
    candidate FD set exhausts the budget, the trial mutation is rejected
    wholesale and the trial keeps its last complete analysis.  No
    half-finished analysis is ever stored or committable.
    """


class StaleVersionError(FDToolError):
    """A commit referenced an out-of-date committed version.

    Commits carry the version number the trial was based on.  When
    another trial has committed in the meantime, the optimistic version
    check fails rather than silently overwriting the newer version.  The
    losing trial stays open and can be inspected or discarded, but this
    library never auto-merges it — start a new trial from the current
    version instead.
    """


class TrialClosedError(FDToolError):
    """An operation targeted a trial that has already committed or closed.

    Trial ids are never reused; begin a new trial instead.
    """
