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


class StaleVersionError(FDToolError):
    """A commit targeted a version that is no longer current.

    Carries ``expected_version`` and ``current_version``.  The trial and
    its candidate analysis are left untouched; the caller must roll it
    back and open a fresh trial against the current version.  Nothing is
    published by the rejected commit.
    """

    def __init__(self, expected_version, current_version):
        self.expected_version = expected_version
        self.current_version = current_version
        super().__init__(
            f"stale version: trial is based on version {expected_version}, "
            f"but the current version is {current_version}; roll back the "
            "trial and recompute from the current candidate set"
        )


class TrialConflictError(FDToolError):
    """A trial specification is inconsistent with the candidate set.

    Examples: removing an unknown declaration id, adding an id that
    already exists, reusing a removed id inside the same trial, or
    committing a trial that was rolled back.  No trial is created (or
    published) when this is raised.
    """


class TrialIncomplete(FDToolError):
    """A trial could not finish within its search budget.

    Only an 'incomplete' indication is returned: no trial object is
    registered, no partial analysis is published, and the committed
    version is untouched.  This is never a lossy verdict — retry the
    trial with a larger ``step_budget``.
    """

    def __init__(self, message, *, step_budget, reason):
        self.step_budget = step_budget
        self.reason = reason
        super().__init__(message)
