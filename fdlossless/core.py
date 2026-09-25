"""Lossless-decomposition witness engine for functional dependencies.

Stateless entry point: ``analyze(attributes, FDs, decomposition, *,
step_budget=...)``.

Stateful what-if workflow: ``AnalysisSession`` holds the committed analysis
version; ``begin_trial()`` opens a trial in which several FD declarations can
be added and removed.  Every mutation recomputes the minimal cover, keys,
projections, preservation verdict and chase *from the candidate declaration
set* — no closure is ever reused across a mutation — and returns a
``TrialView`` with the diff against the trial's base version.  Only
``commit()`` (with an optimistic version check) replaces the committed
analysis; budget exhaustion rejects the whole mutation instead of publishing
a half-finished analysis.

Everything here is computed from scratch with the Python standard library:
attribute closure, minimal cover, candidate keys, full FD projection onto
each subrelation, dependency-preservation test, and a symbolic-tableau chase
that decides losslessness and emits a step-by-step equality log.  When the
join is lossy, a small counterexample instance is built from the final
(unmerged) tableau and verified before it is returned.

Dependency preservation and losslessness are computed independently; one is
never used as a proxy for the other.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from itertools import combinations

from .errors import (
    InvalidInputError,
    SearchBudgetExhausted,
    StaleVersionError,
    TrialClosedError,
)

MAX_ATTRIBUTES = 12
DEFAULT_STEP_BUDGET = 100_000


# ---------------------------------------------------------------------------
# Public result structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FD:
    """A functional dependency with a singleton right-hand side.

    ``lhs`` is a tuple of attribute names in the canonical (input) attribute
    order; ``rhs`` is a single attribute name.
    """

    lhs: tuple[str, ...]
    rhs: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        left = ",".join(self.lhs) if self.lhs else "∅"
        return f"{left} -> {self.rhs}"


@dataclass(frozen=True)
class ChaseStep:
    """One equality merge applied by the chase.

    ``rows`` are 0-based tableau row indices; ``column`` is the attribute
    whose symbols were equated; ``kept`` / ``replaced`` are the formatted
    symbols (distinguished symbols print as the attribute name,
    nondistinguished ones as ``b<row>_<col>`` with 1-based indices).
    """

    step: int
    fd: FD
    rows: tuple[int, int]
    column: str
    kept: str
    replaced: str
    detail: str


@dataclass(frozen=True)
class CounterExample:
    """Witness that the join is lossy.

    ``instance`` is a small relation (tuple of tuples of integer constants,
    in attribute order) that satisfies every input FD.  ``projections`` holds
    one projected relation per subrelation.  ``spurious_tuple`` belongs to
    the natural join of the projections but not to ``instance`` — the join
    produces a tuple the original relation does not have.
    """

    instance: tuple[tuple[int, ...], ...]
    projections: tuple[tuple[tuple[int, ...], ...], ...]
    spurious_tuple: tuple[int, ...]
    explanation: str


@dataclass(frozen=True)
class AnalysisResult:
    attributes: tuple[str, ...]
    fds: tuple[FD, ...]                       # input FDs, normalized to singleton RHS
    decomposition: tuple[tuple[str, ...], ...]
    minimal_cover: tuple[FD, ...]
    candidate_keys: tuple[tuple[str, ...], ...]  # sorted by size, then lexicographically
    projections: tuple[tuple[FD, ...], ...]      # full projected FDs per subrelation
    dependency_preserving: bool
    lossless: bool
    chase_log: tuple[ChaseStep, ...]
    counterexample: CounterExample | None


@dataclass(frozen=True)
class DeclaredFD:
    """One live FD declaration with its original declaration id.

    A declaration is stored exactly as given (``rhs`` may contain several
    attributes); ``expanded`` yields the singleton-RHS FDs the engine
    works with.  Removal is by declaration ``id``: deleting declaration
    #k removes exactly that declaration — all of its right-hand sides —
    and never a different declaration that merely implies one of the same
    normalized FDs.
    """

    id: int
    lhs: tuple[str, ...]
    rhs: tuple[str, ...]

    @property
    def expanded(self) -> tuple[FD, ...]:
        return tuple(FD(lhs=self.lhs, rhs=a) for a in self.rhs)

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        left = ",".join(self.lhs) if self.lhs else "∅"
        return f"#{self.id}: {left} -> {','.join(self.rhs)}"


@dataclass(frozen=True)
class AnalysisDelta:
    """Set-wise difference of a candidate analysis against its base version.

    Every ``*_added`` / ``*_removed`` pair lists elements present in only
    one side; unchanged elements appear in neither.  Projection diffs are
    aligned with ``AnalysisResult.decomposition`` (one tuple per
    subrelation).
    """

    base_version: int
    declared_added: tuple[DeclaredFD, ...]
    declared_removed: tuple[DeclaredFD, ...]
    minimal_cover_added: tuple[FD, ...]
    minimal_cover_removed: tuple[FD, ...]
    candidate_keys_added: tuple[tuple[str, ...], ...]
    candidate_keys_removed: tuple[tuple[str, ...], ...]
    projections_added: tuple[tuple[FD, ...], ...]
    projections_removed: tuple[tuple[FD, ...], ...]
    dependency_preserving_old: bool
    dependency_preserving_new: bool
    lossless_old: bool
    lossless_new: bool

    @property
    def changed(self) -> bool:
        return bool(
            self.declared_added
            or self.declared_removed
            or self.minimal_cover_added
            or self.minimal_cover_removed
            or self.candidate_keys_added
            or self.candidate_keys_removed
            or any(self.projections_added)
            or any(self.projections_removed)
            or self.dependency_preserving_old != self.dependency_preserving_new
            or self.lossless_old != self.lossless_new
        )

    @property
    def dependency_preserving_changed(self) -> bool:
        return self.dependency_preserving_old != self.dependency_preserving_new

    @property
    def lossless_changed(self) -> bool:
        return self.lossless_old != self.lossless_new


@dataclass(frozen=True)
class TrialView:
    """Immutable snapshot of one trial after a mutation (or at begin time).

    ``result`` is a complete analysis recomputed from the candidate
    declaration set; ``delta`` compares it against ``base_version`` (the
    committed version the trial started from).  After ``commit`` or
    ``close_trial`` the id is dead and every further use raises
    ``TrialClosedError``.
    """

    trial_id: int
    base_version: int
    open: bool
    declared_fds: tuple[DeclaredFD, ...]
    result: AnalysisResult
    delta: AnalysisDelta


@dataclass(frozen=True)
class CommitOutcome:
    """Result of a successful commit: the new committed version number."""

    version: int
    result: AnalysisResult


# ---------------------------------------------------------------------------
# Input validation (reject, never silently correct)
# ---------------------------------------------------------------------------

def _validate_attributes(attributes) -> tuple[str, ...]:
    if isinstance(attributes, (str, bytes)):
        raise InvalidInputError(
            "attributes: expected an iterable of names, not a single string"
        )
    try:
        items = list(attributes)
    except TypeError:
        raise InvalidInputError(
            f"attributes: expected an iterable of names, got {type(attributes).__name__}"
        ) from None
    if not items:
        raise InvalidInputError("attributes: at least one attribute is required")
    seen = set()
    for i, a in enumerate(items):
        if isinstance(a, float) and (math.isnan(a) or math.isinf(a)):
            raise InvalidInputError(
                f"attributes[{i}]: NaN/Infinity is not a valid attribute name"
            )
        if not isinstance(a, str):
            raise InvalidInputError(
                f"attributes[{i}]: attribute names must be str, "
                f"got {type(a).__name__} ({a!r})"
            )
        if a == "":
            raise InvalidInputError(f"attributes[{i}]: empty attribute name")
        if a in seen:
            raise InvalidInputError(f"attributes[{i}]: duplicate attribute {a!r}")
        seen.add(a)
    if len(items) > MAX_ATTRIBUTES:
        raise InvalidInputError(
            f"attributes: {len(items)} attributes given, at most "
            f"{MAX_ATTRIBUTES} allowed"
        )
    return tuple(items)


def _validate_name_set(value, where: str, known: frozenset) -> frozenset:
    if isinstance(value, (str, bytes)):
        raise InvalidInputError(
            f"{where}: expected an iterable of attribute names, not a single string"
        )
    try:
        items = list(value)
    except TypeError:
        raise InvalidInputError(
            f"{where}: expected an iterable of attribute names, "
            f"got {type(value).__name__}"
        ) from None
    seen = set()
    for j, a in enumerate(items):
        if isinstance(a, float) and (math.isnan(a) or math.isinf(a)):
            raise InvalidInputError(
                f"{where}[{j}]: NaN/Infinity is not a valid attribute name"
            )
        if not isinstance(a, str):
            raise InvalidInputError(
                f"{where}[{j}]: attribute names must be str, "
                f"got {type(a).__name__} ({a!r})"
            )
        if a in seen:
            raise InvalidInputError(f"{where}[{j}]: duplicate attribute {a!r}")
        seen.add(a)
        if a not in known:
            raise InvalidInputError(f"{where}[{j}]: unknown attribute {a!r}")
    return frozenset(items)


def _validate_fds(FDs, attrs: tuple[str, ...]) -> tuple[tuple[frozenset, frozenset], ...]:
    """Validate and normalize to singleton-RHS (frozenset, frozenset) pairs."""
    normalized: list[tuple[frozenset, frozenset]] = []
    for lhs, rhs in _validate_fd_declarations(FDs, attrs):
        # Split in canonical attribute order for deterministic output.
        for a in sorted(rhs, key=attrs.index):
            normalized.append((lhs, frozenset({a})))
    return tuple(normalized)


def _validate_fd_declarations(FDs, attrs: tuple[str, ...]
                              ) -> tuple[tuple[frozenset, frozenset], ...]:
    """Like ``_validate_fds`` but keeps one entry per *declared* pair.

    A declaration ``(X, {a, b})`` stays one entry ``(frozenset(X),
    frozenset({a, b}))`` — removal is by declaration, so declaration
    boundaries must survive validation.  Normalized duplicates (same
    singleton X -> a produced by two declarations, including by the RHS
    split) are still rejected.
    """
    if isinstance(FDs, (str, bytes)):
        raise InvalidInputError("FDs: expected an iterable of (lhs, rhs) pairs")
    try:
        items = list(FDs)
    except TypeError:
        raise InvalidInputError(
            f"FDs: expected an iterable of (lhs, rhs) pairs, "
            f"got {type(FDs).__name__}"
        ) from None
    known = frozenset(attrs)
    declarations: list[tuple[frozenset, frozenset]] = []
    seen = set()
    for i, fd in enumerate(items):
        if isinstance(fd, (str, bytes, set, frozenset)):
            raise InvalidInputError(
                f"FDs[{i}]: expected an ordered (lhs, rhs) pair, got {fd!r}"
            )
        try:
            lhs_raw, rhs_raw = fd
        except (TypeError, ValueError):
            raise InvalidInputError(
                f"FDs[{i}]: expected a (lhs, rhs) pair, got {fd!r}"
            ) from None
        lhs = _validate_name_set(lhs_raw, f"FDs[{i}].lhs", known)
        rhs = _validate_name_set(rhs_raw, f"FDs[{i}].rhs", known)
        if not rhs:
            raise InvalidInputError(f"FDs[{i}].rhs: right-hand side must be non-empty")
        for a in sorted(rhs, key=attrs.index):
            key = (lhs, frozenset({a}))
            if key in seen:
                raise InvalidInputError(
                    f"FDs[{i}]: duplicate FD {sorted(lhs)} -> {a!r} after "
                    "normalization to singleton right-hand sides"
                )
            seen.add(key)
        declarations.append((lhs, rhs))
    return tuple(declarations)


def _validate_decomposition(decomposition, attrs: tuple[str, ...]) -> tuple[frozenset, ...]:
    if isinstance(decomposition, (str, bytes)):
        raise InvalidInputError(
            "decomposition: expected an iterable of attribute sets"
        )
    try:
        items = list(decomposition)
    except TypeError:
        raise InvalidInputError(
            f"decomposition: expected an iterable of attribute sets, "
            f"got {type(decomposition).__name__}"
        ) from None
    if not items:
        raise InvalidInputError("decomposition: at least one subrelation is required")
    known = frozenset(attrs)
    parts = []
    seen = set()
    for i, part in enumerate(items):
        s = _validate_name_set(part, f"decomposition[{i}]", known)
        if not s:
            raise InvalidInputError(f"decomposition[{i}]: subrelation must be non-empty")
        if s in seen:
            raise InvalidInputError(
                f"decomposition[{i}]: duplicate subrelation {sorted(s)}"
            )
        seen.add(s)
        parts.append(s)
    union = frozenset().union(*parts)
    missing = [a for a in attrs if a not in union]
    if missing:
        raise InvalidInputError(
            f"decomposition: attributes not covered by any subrelation: {missing}"
        )
    return tuple(parts)


def _validate_budget(step_budget) -> int:
    if isinstance(step_budget, bool):
        raise InvalidInputError(
            f"step_budget: expected int, got bool ({step_budget!r})"
        )
    if isinstance(step_budget, float):
        if math.isnan(step_budget) or math.isinf(step_budget):
            raise InvalidInputError(
                "step_budget: NaN/Infinity is not a valid merge budget"
            )
        raise InvalidInputError(
            f"step_budget: expected int, got float ({step_budget!r})"
        )
    if not isinstance(step_budget, int):
        raise InvalidInputError(
            f"step_budget: expected int, got {type(step_budget).__name__}"
        )
    if step_budget < 0:
        raise InvalidInputError(
            f"step_budget: budget must be >= 0, got {step_budget}"
        )
    return step_budget


# ---------------------------------------------------------------------------
# Attribute closure
# ---------------------------------------------------------------------------

def _closure(fds, seed) -> frozenset:
    """Closure of the attribute set ``seed`` under singleton-RHS ``fds``."""
    result = set(seed)
    changed = True
    while changed:
        changed = False
        for lhs, rhs in fds:
            if lhs <= result and not rhs <= result:
                result |= rhs
                changed = True
    return frozenset(result)


# ---------------------------------------------------------------------------
# Minimal cover
# ---------------------------------------------------------------------------

def _minimal_cover(fds) -> tuple[tuple[frozenset, frozenset], ...]:
    # Drop trivial FDs and exact duplicates (order-preserving).
    work: list[list[set]] = []
    seen = set()
    for lhs, rhs in fds:
        if rhs <= lhs:
            continue
        key = (lhs, rhs)
        if key in seen:
            continue
        seen.add(key)
        work.append([set(lhs), set(rhs)])
    # Left-reduction: remove extraneous determinant attributes.  One pass
    # is complete: every reduction keeps the FD set equivalent, and the
    # extraneous test is a closure under the full current set, whose
    # answer is invariant under those equivalent reductions (verified by
    # exhaustive comparison against a fixpoint version over every <=2-FD
    # set on {A,B,C}).  Within one FD the already-shortened left side is
    # used for the next attribute test.
    for i in range(len(work)):
        lhs, rhs = work[i]
        for a in sorted(lhs):
            if len(lhs) <= 1:
                break
            trial = lhs - {a}
            current = [(set(l), set(r)) for l, r in work]
            if set(rhs) <= _closure(current, trial):
                lhs -= {a}
        work[i][0] = lhs
    # Remove redundant FDs.
    result = [(set(l), set(r)) for l, r in work]
    i = 0
    while i < len(result):
        lhs, rhs = result[i]
        rest = result[:i] + result[i + 1:]
        if set(rhs) <= _closure(rest, lhs):
            result = rest
        else:
            i += 1
    return tuple((frozenset(l), frozenset(r)) for l, r in result)


# ---------------------------------------------------------------------------
# Candidate keys
# ---------------------------------------------------------------------------

def _candidate_keys(attrs: tuple[str, ...], fds) -> tuple[tuple[str, ...], ...]:
    universe = frozenset(attrs)
    keys: list[tuple[str, ...]] = []
    found: list[frozenset] = []
    for size in range(len(attrs) + 1):
        for combo in combinations(attrs, size):
            s = frozenset(combo)
            if any(k <= s for k in found):
                continue
            if _closure(fds, s) == universe:
                found.append(s)
                keys.append(tuple(sorted(combo)))
    # Sort by size, then lexicographically by the (name-sorted) key tuple.
    keys.sort(key=lambda k: (len(k), k))
    return tuple(keys)


# ---------------------------------------------------------------------------
# Full projection of the FDs onto each subrelation
# ---------------------------------------------------------------------------

def _project(attrs: tuple[str, ...], fds, sub: frozenset) -> tuple[tuple[frozenset, frozenset], ...]:
    """All FDs X -> {a} implied by ``fds`` with X, a inside ``sub`` and X minimal.

    This is a complete generating set for the projection of F onto ``sub``:
    every implied FD over ``sub`` has some listed X -> a with X a subset of
    its left-hand side.
    """
    ordered = [a for a in attrs if a in sub]
    out: list[tuple[frozenset, frozenset]] = []
    for a in ordered:
        rest = [x for x in ordered if x != a]
        minimal_lhs: list[frozenset] = []
        for size in range(len(rest) + 1):
            for combo in combinations(rest, size):
                x = frozenset(combo)
                if any(m <= x for m in minimal_lhs):
                    continue
                if a in _closure(fds, x):
                    minimal_lhs.append(x)
                    out.append((x, frozenset({a})))
    return tuple(out)


def _is_dependency_preserving(fds, projections) -> bool:
    """True iff the union of the projected FDs implies every original FD."""
    g = [fd for proj in projections for fd in proj]
    return all(set(rhs) <= _closure(g, lhs) for lhs, rhs in fds)


# ---------------------------------------------------------------------------
# Symbolic-tableau chase
# ---------------------------------------------------------------------------

def format_symbol(symbol, attrs: tuple[str, ...]) -> str:
    """Human-readable form of a tableau symbol."""
    if symbol[0] == "d":
        return attrs[symbol[1]]
    return f"b{symbol[1] + 1}_{symbol[2] + 1}"


def _chase(attrs, fds, decomp, budget: int):
    n = len(attrs)
    index = {a: j for j, a in enumerate(attrs)}
    table = [
        [("d", j) if attrs[j] in part else ("n", i, j) for j in range(n)]
        for i, part in enumerate(decomp)
    ]
    log: list[ChaseStep] = []
    steps = 0
    changed = True
    while changed:
        changed = False
        for lhs, rhs in fds:
            (a_name,) = rhs
            a = index[a_name]
            cols = [index[x] for x in lhs]
            for i in range(len(table)):
                for j in range(i + 1, len(table)):
                    if all(table[i][c] == table[j][c] for c in cols):
                        si, sj = table[i][a], table[j][a]
                        if si == sj:
                            continue
                        kept = min(si, sj)       # 'd' < 'n'; lower row wins
                        replaced = max(si, sj)
                        if steps >= budget:
                            raise SearchBudgetExhausted(
                                f"chase merge budget exhausted after {steps} "
                                f"merge(s) (step_budget={budget}); no verdict "
                                "reached — this is not a proof that the "
                                "decomposition is lossy"
                            )
                        steps += 1
                        for row in table:
                            for c in range(n):
                                if row[c] == replaced:
                                    row[c] = kept
                        fd_obj = FD(
                            lhs=tuple(x for x in attrs if x in lhs), rhs=a_name
                        )
                        lhs_txt = ",".join(fd_obj.lhs) if fd_obj.lhs else "∅"
                        log.append(
                            ChaseStep(
                                step=steps,
                                fd=fd_obj,
                                rows=(i, j),
                                column=a_name,
                                kept=format_symbol(kept, attrs),
                                replaced=format_symbol(replaced, attrs),
                                detail=(
                                    f"rows {i + 1} and {j + 1} agree on "
                                    f"{{{lhs_txt}}}; FD {lhs_txt} -> {a_name} "
                                    f"equates column {a_name}: "
                                    f"{format_symbol(replaced, attrs)} := "
                                    f"{format_symbol(kept, attrs)}"
                                ),
                            )
                        )
                        changed = True
    lossless = any(all(s[0] == "d" for s in row) for row in table)
    return lossless, table, tuple(log)


# ---------------------------------------------------------------------------
# Counterexample construction from the final (unmerged) tableau
# ---------------------------------------------------------------------------

def _satisfies(instance, fds, index) -> bool:
    for lhs, rhs in fds:
        lhs_cols = [index[x] for x in lhs]
        (a_name,) = rhs
        a = index[a_name]
        for t1 in instance:
            for t2 in instance:
                if all(t1[c] == t2[c] for c in lhs_cols) and t1[a] != t2[a]:
                    return False
    return True


def _build_counterexample(attrs, fds, decomp, table) -> CounterExample:
    n = len(attrs)
    index = {a: j for j, a in enumerate(attrs)}
    mapping: dict = {}
    next_const = n  # distinguished symbol for column j maps to j (< n)

    def const(sym) -> int:
        nonlocal next_const
        if sym[0] == "d":
            return sym[1]
        if sym not in mapping:
            mapping[sym] = next_const
            next_const += 1
        return mapping[sym]

    instance: list[tuple[int, ...]] = []
    seen = set()
    for row in table:
        t = tuple(const(s) for s in row)
        if t not in seen:
            seen.add(t)
            instance.append(t)
    instance_t = tuple(instance)
    spurious = tuple(range(n))  # the all-distinguished tuple

    projections = []
    for part in decomp:
        cols = [index[a] for a in attrs if a in part]
        projections.append(
            tuple(sorted({tuple(t[c] for c in cols) for t in instance_t}))
        )

    # Self-check the witness before returning it.
    if not _satisfies(instance_t, fds, index):
        raise AssertionError("internal error: counterexample violates an FD")
    for part, proj in zip(decomp, projections):
        cols = [index[a] for a in attrs if a in part]
        if tuple(spurious[c] for c in cols) not in proj:
            raise AssertionError("internal error: spurious tuple not in join")
    if spurious in seen:
        raise AssertionError("internal error: spurious tuple already in instance")

    explanation = (
        f"instance r = {list(instance_t)} satisfies every input FD, and "
        f"t = {spurious} lies in the natural join of the projections "
        f"(its restriction to each subrelation occurs in that projection), "
        f"but t is not a tuple of r; the join produces a tuple the original "
        "relation does not have."
    )
    return CounterExample(
        instance=instance_t,
        projections=tuple(projections),
        spurious_tuple=spurious,
        explanation=explanation,
    )


# ---------------------------------------------------------------------------
# Analysis assembly (inputs already validated/normalized)
# ---------------------------------------------------------------------------

def _canon_fd(attrs, fd_pair) -> FD:
    lhs, rhs = fd_pair
    (a,) = rhs
    return FD(lhs=tuple(x for x in attrs if x in lhs), rhs=a)


def _sort_fds(attrs, fd_pairs) -> tuple[FD, ...]:
    return tuple(sorted((_canon_fd(attrs, p) for p in fd_pairs),
                        key=lambda f: (f.lhs, f.rhs)))


def _compute_analysis(attrs, fds, decomp, budget: int) -> AnalysisResult:
    """Run every analysis stage over validated inputs.

    ``fds`` are validated singleton-RHS ``(frozenset, frozenset)`` pairs,
    ``decomp`` validated frozensets.  Raises ``SearchBudgetExhausted``
    before producing anything when the chase cannot finish within
    ``budget`` — callers never see a partially computed analysis.
    """
    cover_raw = _minimal_cover(fds)
    keys = _candidate_keys(attrs, fds)
    projections_raw = tuple(_project(attrs, fds, part) for part in decomp)
    preserving = _is_dependency_preserving(fds, projections_raw)
    lossless, table, log = _chase(attrs, fds, decomp, budget)
    counterexample = None if lossless else _build_counterexample(
        attrs, fds, decomp, table
    )
    return AnalysisResult(
        attributes=attrs,
        fds=_sort_fds(attrs, fds),
        decomposition=tuple(
            tuple(a for a in attrs if a in part) for part in decomp
        ),
        minimal_cover=_sort_fds(attrs, cover_raw),
        candidate_keys=keys,
        projections=tuple(_sort_fds(attrs, p) for p in projections_raw),
        dependency_preserving=preserving,
        lossless=lossless,
        chase_log=log,
        counterexample=counterexample,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def analyze(attributes, FDs, decomposition, *, step_budget=DEFAULT_STEP_BUDGET):
    """Analyze a decomposition of a relation schema under functional dependencies.

    Parameters
    ----------
    attributes : iterable of str
        Attribute names of the original relation (1..12, unique, non-empty).
    FDs : iterable of (iterable of str, iterable of str)
        Functional dependencies; each side is a set of attribute names.
        Empty determinants are allowed; right-hand sides must be non-empty.
    decomposition : iterable of iterable of str
        Subrelations; non-empty, known attributes only, and their union must
        equal ``attributes``.
    step_budget : int, keyword-only
        Maximum number of chase equality merges.  Exhaustion raises
        ``SearchBudgetExhausted`` (distinct from a lossy verdict).

    Returns
    -------
    AnalysisResult
        Minimal cover, candidate keys (sorted by size, then
        lexicographically), full projected FDs per subrelation, the
        dependency-preservation verdict, the chase log, the losslessness
        verdict, and — when lossy — a verified counterexample.
    """
    attrs = _validate_attributes(attributes)
    fds = _validate_fds(FDs, attrs)
    decomp = _validate_decomposition(decomposition, attrs)
    budget = _validate_budget(step_budget)
    return _compute_analysis(attrs, fds, decomp, budget)


# ---------------------------------------------------------------------------
# What-if sessions: trial FD edits with version-gated commits
# ---------------------------------------------------------------------------

class _Trial:
    __slots__ = ("id", "base_version", "declarations", "result", "budget",
                 "open")

    def __init__(self, trial_id, base_version, declarations, result, budget):
        self.id = trial_id
        self.base_version = base_version
        # id -> DeclaredFD; insertion order is the declaration order.
        self.declarations: dict[int, DeclaredFD] = dict(declarations)
        self.result = result
        self.budget = budget
        self.open = True


class AnalysisSession:
    """In-memory, versioned holder of one committed analysis plus what-if trials.

    Construction validates the schema, decomposition and initial FD set and
    runs the full initial analysis (so construction itself can raise
    ``InvalidInputError`` / ``SearchBudgetExhausted``).  Afterwards:

    - ``begin_trial()`` opens an isolated scratch space based on the
      currently committed version;
    - ``add_fds`` / ``remove_fds`` mutate one trial and immediately return a
      fresh ``TrialView`` whose analysis has been recomputed from the whole
      candidate declaration set — minimal cover, candidate keys and
      projections are never carried over from before the mutation;
    - budget exhaustion rejects the whole mutation (the trial keeps its
      last complete analysis, nothing half-finished is published);
    - ``commit`` advances the committed version only if the version the
      trial was based on is still current (``StaleVersionError`` otherwise);
    - ``discard_trial`` / ``close_trial`` drop the scratch space.

    Everything lives in memory; there is no persistence, server or UI.
    A session uses an internal lock so its version counter and trial table
    stay consistent across threads, but a ``_Trial`` object is only ever
    touched by the thread mutating it.
    """

    def __init__(self, attributes, FDs, decomposition, *,
                 step_budget=DEFAULT_STEP_BUDGET):
        attrs = _validate_attributes(attributes)
        declarations = _validate_fd_declarations(FDs, attrs)
        decomp = _validate_decomposition(decomposition, attrs)
        budget = _validate_budget(step_budget)
        self._attrs = attrs
        self._decomp = decomp
        self._budget = budget
        self._lock = threading.Lock()
        self._version = 1
        self._committed_decl: dict[int, DeclaredFD] = {}
        for i, (lhs, rhs) in enumerate(declarations):
            self._committed_decl[i] = DeclaredFD(
                id=i,
                lhs=tuple(a for a in attrs if a in lhs),
                rhs=tuple(sorted(rhs, key=attrs.index)),
            )
        initial_fds = self._decl_fds(self._committed_decl)
        self._committed: AnalysisResult = _compute_analysis(
            attrs, initial_fds, decomp, budget
        )
        # Committed declaration ids come from the global monotonic counter
        # below, shared across trials and commits (ids are never reused).
        self._next_decl_id = len(declarations)
        self._next_trial_id = 0
        self._trials: dict[int, _Trial] = {}
        # In-memory snapshots of past committed versions, retained only
        # while some open trial is based on them, so that trial's delta can
        # still compare against its real base after a competing commit.
        # The current version is always retained.
        self._base_snapshots: dict[int, AnalysisResult] = {
            1: self._committed
        }
        self._base_decl_snapshots: dict[int, dict[int, DeclaredFD]] = {
            1: dict(self._committed_decl)
        }
        self._snapshot_refs: dict[int, int] = {}

    # -- introspection ----------------------------------------------------

    @property
    def version(self) -> int:
        """Currently committed version number (starts at 1)."""
        with self._lock:
            return self._version

    @property
    def committed(self) -> AnalysisResult:
        """Immutable snapshot of the currently committed analysis."""
        with self._lock:
            return self._committed

    @property
    def committed_declarations(self) -> tuple[DeclaredFD, ...]:
        with self._lock:
            return tuple(self._committed_decl.values())

    # -- trial lifecycle --------------------------------------------------

    def begin_trial(self) -> TrialView:
        """Open a trial seeded with the committed declarations and analysis."""
        with self._lock:
            trial_id = self._next_trial_id
            self._next_trial_id += 1
            trial = _Trial(
                trial_id,
                self._version,
                self._committed_decl,
                self._committed,
                self._budget,
            )
            self._trials[trial_id] = trial
            self._retain(trial.base_version)
            view = self._view(trial)
        return view

    def _get_open_trial(self, trial_id) -> _Trial:
        trial = self._trials.get(trial_id)
        if trial is None or not trial.open:
            raise TrialClosedError(
                f"trial {trial_id!r} is not open (committed or closed; "
                "trial ids are never reused — begin a new trial)"
            )
        return trial

    def add_fds(self, trial_id, FDs) -> TrialView:
        """Append FD declarations to a trial and return the recomputed view.

        ``FDs`` follows the same contract as in ``analyze``: an iterable of
        ``(lhs, rhs)`` attribute-set pairs.  Each item becomes one
        declaration with a fresh id (a multi-attribute RHS stays one
        declaration and is removed as a unit).  Declarations identical to
        ones already live in the trial are rejected (the trial is left
        untouched).  If the recomputation exhausts the step budget, the new
        declarations are rejected wholesale and the previous view remains
        valid — callers must not treat that as an analysis result.
        """
        with self._lock:
            trial = self._get_open_trial(trial_id)
            parsed = _validate_fd_declarations(FDs, self._attrs)
            if not parsed:
                raise InvalidInputError(
                    "FDs: add_fds requires at least one declaration"
                )
            live: set[tuple[frozenset, str]] = set()
            for decl in trial.declarations.values():
                for e in decl.expanded:
                    live.add((frozenset(e.lhs), e.rhs))
            staged: list[tuple[frozenset, frozenset]] = []
            staged_norm: list[tuple[frozenset, str]] = []
            # Phase 1: validate the whole batch (including duplicates
            # against live declarations and within the batch) before any
            # id is allocated or state is touched, so a rejected batch
            # neither applies partially nor burns declaration ids.
            for lhs, rhs in parsed:
                for a in sorted(rhs, key=self._attrs.index):
                    key = (lhs, a)
                    if key in live:
                        raise InvalidInputError(
                            f"FD {sorted(lhs)} -> {a!r} duplicates a "
                            "declaration already present in this trial "
                            "(normalize or drop the duplicate)"
                        )
                    if any(l == lhs and aa == a for l, aa in staged_norm):
                        raise InvalidInputError(
                            f"FD {sorted(lhs)} -> {a!r} listed twice in "
                            "the same add_fds batch"
                        )
                    staged_norm.append(key)
                staged.append((lhs, rhs))
            # Phase 2: recompute from the whole candidate set first; a
            # budget failure must not advance the id counter or touch the
            # trial at all.
            candidate = list(self._trial_fds(trial))
            for lhs, rhs in staged:
                for a in sorted(rhs, key=self._attrs.index):
                    candidate.append((lhs, frozenset({a})))
            new_result = _compute_analysis(
                self._attrs, tuple(candidate), self._decomp, trial.budget
            )
            # Phase 3: analysis complete — allocate session-monotonic ids
            # (two concurrent trials still get distinct ids; ids are never
            # reused after commit or discard) and publish.
            for lhs, rhs in staged:
                decl_id = self._next_decl_id
                self._next_decl_id += 1
                trial.declarations[decl_id] = DeclaredFD(
                    id=decl_id,
                    lhs=tuple(a for a in self._attrs if a in lhs),
                    rhs=tuple(sorted(rhs, key=self._attrs.index)),
                )
            trial.result = new_result
            view = self._view(trial)
        return view

    def remove_fds(self, trial_id, ids) -> TrialView:
        """Remove declarations by their **original declaration ids**.

        ``ids`` is an iterable of ints.  Removal targets declarations, not
        normalized FDs: removing id k deletes exactly declaration k even
        when another live declaration implies the same X -> a, and an id
        that is absent from the trial is an error (nothing is removed).
        The analysis is then recomputed from the remaining declarations.
        """
        with self._lock:
            trial = self._get_open_trial(trial_id)
            id_list = self._validate_id_list(ids, "ids")
            unknown = [i for i in id_list if i not in trial.declarations]
            if unknown:
                raise InvalidInputError(
                    f"ids: declaration id(s) {unknown} not present in "
                    f"trial {trial.id} (removal is by original declaration "
                    "id; ids are not reused after commit)"
                )
            kept = {
                k: v for k, v in trial.declarations.items() if k not in set(id_list)
            }
            candidate_fds = self._decl_fds(kept)
            # Recompute before publishing; budget failure leaves the trial
            # on its previous complete analysis.
            new_result = _compute_analysis(
                self._attrs, candidate_fds, self._decomp, trial.budget
            )
            trial.declarations = kept
            trial.result = new_result
            view = self._view(trial)
        return view

    def trial_view(self, trial_id) -> TrialView:
        """Return the trial's current view (its last complete analysis)."""
        with self._lock:
            trial = self._get_open_trial(trial_id)
            return self._view(trial)

    def commit(self, trial_id, expected_version=None) -> CommitOutcome:
        """Commit a trial, replacing the committed analysis.

        Optimistic concurrency: the commit succeeds only when the version
        the trial was based on is still the committed version.  Pass
        ``expected_version`` to additionally pin the commit to a specific
        version.  A losing commit raises ``StaleVersionError`` and leaves
        both the committed analysis and the trial untouched (the trial can
        still be inspected or discarded).  On success the trial is closed
        and its declarations/analysis become version ``old + 1``.
        """
        with self._lock:
            trial = self._get_open_trial(trial_id)
            if expected_version is not None:
                ev = self._validate_version(expected_version, "expected_version")
                if ev != trial.base_version:
                    raise StaleVersionError(
                        f"trial {trial.id} is based on version "
                        f"{trial.base_version}, but commit pins "
                        f"expected_version={ev}"
                    )
            if trial.base_version != self._version:
                raise StaleVersionError(
                    f"trial {trial.id} is based on stale version "
                    f"{trial.base_version}; committed version is now "
                    f"{self._version}. Commit rejected — begin a new trial "
                    "and re-apply the edits against the current version."
                )
            old_version = self._version
            new_version = old_version + 1
            new_decl = dict(trial.declarations)
            # Publish atomically: result, declarations and version move
            # together under the lock.
            self._base_snapshots[new_version] = trial.result
            self._base_decl_snapshots[new_version] = new_decl
            self._committed = trial.result
            self._committed_decl = new_decl
            self._version = new_version
            trial.open = False
            self._release(old_version)
            outcome = CommitOutcome(new_version, trial.result)
        return outcome

    def discard_trial(self, trial_id) -> None:
        """Drop an open trial without touching the committed version."""
        with self._lock:
            trial = self._get_open_trial(trial_id)
            trial.open = False
            self._release(trial.base_version)

    # alias matching the task wording ("撤销" of a trial as well as of FDs)
    close_trial = discard_trial

    # -- internals --------------------------------------------------------

    @staticmethod
    def _validate_id_list(ids, where) -> list[int]:
        if isinstance(ids, (str, bytes, int)):
            raise InvalidInputError(
                f"{where}: expected an iterable of declaration ids, "
                f"got {type(ids).__name__}"
            )
        try:
            items = list(ids)
        except TypeError:
            raise InvalidInputError(
                f"{where}: expected an iterable of declaration ids, "
                f"got {type(ids).__name__}"
            ) from None
        out: list[int] = []
        seen = set()
        for j, x in enumerate(items):
            if isinstance(x, bool) or not isinstance(x, int):
                raise InvalidInputError(
                    f"{where}[{j}]: declaration id must be int, "
                    f"got {type(x).__name__} ({x!r})"
                )
            if x < 0:
                raise InvalidInputError(
                    f"{where}[{j}]: declaration id must be >= 0, got {x}"
                )
            if x in seen:
                raise InvalidInputError(
                    f"{where}[{j}]: declaration id {x} listed more than once"
                )
            seen.add(x)
            out.append(x)
        if not out:
            raise InvalidInputError(f"{where}: at least one id is required")
        return out

    @staticmethod
    def _validate_version(value, where) -> int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise InvalidInputError(
                f"{where}: expected int version, got {type(value).__name__}"
            )
        if value < 1:
            raise InvalidInputError(
                f"{where}: versions start at 1, got {value}"
            )
        return value

    def _trial_fds(self, trial: _Trial):
        return self._decl_fds(trial.declarations)

    def _retain(self, version: int) -> None:
        self._snapshot_refs[version] = (
            self._snapshot_refs.get(version, 0) + 1
        )

    def _release(self, version: int) -> None:
        n = self._snapshot_refs.get(version, 0) - 1
        if n <= 0:
            self._snapshot_refs.pop(version, None)
            # The currently committed version is always retained, even with
            # no open trials based on it.
            if version != self._version:
                self._base_snapshots.pop(version, None)
                self._base_decl_snapshots.pop(version, None)
        else:
            self._snapshot_refs[version] = n

    @staticmethod
    def _decl_fds(declarations):
        return tuple(
            (frozenset(decl.lhs), frozenset({a}))
            for decl in declarations.values()
            for a in decl.rhs
        )

    def _view(self, trial: _Trial) -> TrialView:
        # An open trial's base snapshot is retained by refcount (or is the
        # current committed version), so the lookup always succeeds.
        base_result = self._base_snapshots[trial.base_version]
        base_decls = self._base_decl_snapshots[trial.base_version]
        return TrialView(
            trial_id=trial.id,
            base_version=trial.base_version,
            open=trial.open,
            declared_fds=tuple(trial.declarations.values()),
            result=trial.result,
            delta=self._diff(base_result, base_decls, trial),
        )

    def _diff(self, base_result, base_decls, trial: _Trial) -> AnalysisDelta:
        def diff(old, new, *, key):
            old_s, new_s = set(old), set(new)
            return (tuple(sorted(new_s - old_s, key=key)),
                    tuple(sorted(old_s - new_s, key=key)))

        fd_key = lambda f: (f.lhs, f.rhs)
        decl_key = lambda d: (d.lhs, d.rhs, d.id)
        decl_added, decl_removed = diff(
            tuple(base_decls.values()), tuple(trial.declarations.values()),
            key=decl_key,
        )
        cover_added, cover_removed = diff(
            base_result.minimal_cover, trial.result.minimal_cover, key=fd_key
        )
        keys_added, keys_removed = diff(
            base_result.candidate_keys, trial.result.candidate_keys,
            key=lambda k: (len(k), k),
        )
        proj_added, proj_removed = [], []
        for old_p, new_p in zip(base_result.projections,
                                trial.result.projections):
            a, r = diff(old_p, new_p, key=fd_key)
            proj_added.append(a)
            proj_removed.append(r)
        return AnalysisDelta(
            base_version=trial.base_version,
            declared_added=decl_added,
            declared_removed=decl_removed,
            minimal_cover_added=cover_added,
            minimal_cover_removed=cover_removed,
            candidate_keys_added=keys_added,
            candidate_keys_removed=keys_removed,
            projections_added=tuple(proj_added),
            projections_removed=tuple(proj_removed),
            dependency_preserving_old=base_result.dependency_preserving,
            dependency_preserving_new=trial.result.dependency_preserving,
            lossless_old=base_result.lossless,
            lossless_new=trial.result.lossless,
        )
