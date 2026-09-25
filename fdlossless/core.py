"""Lossless-decomposition witness engine for functional dependencies.

Public entry points:

* ``analyze(attributes, FDs, decomposition, *, step_budget=...)`` — the
  stateless one-shot analysis;
* ``AnalysisVersionStore(...)`` — an in-memory versioned container around
  the same analysis.  A caller opens *trials* that add and remove several
  declarations at once, each time recomputing the minimal cover,
  candidate keys, projections, dependency preservation and the chase from
  the whole candidate set, with a diff versus the committed version.
  Only ``commit`` replaces the official analysis (with optimistic version
  checking); ``rollback`` discards a trial.  Budget exhaustion reports
  ``TrialIncomplete`` and publishes nothing.

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
    TrialConflictError,
    TrialIncomplete,
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


# ---------------------------------------------------------------------------
# Declared FDs and trial/version structures (in-memory analysis versions)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DeclaredFD:
    """One user-declared FD carrying its identity.

    ``fd_id`` is the caller-chosen name of the *declaration*.  Removal in a
    trial always references a declaration id and deletes that declaration
    only; the same normalized dependency implied or duplicated by other
    declarations is never removed by it.  ``lhs`` / ``rhs`` are the
    declared attribute sets; ``singletons`` is the same declaration split
    into singleton-RHS ``FD`` objects after validation.
    """

    fd_id: str
    lhs: tuple[str, ...]
    rhs: tuple[str, ...]
    singletons: tuple[FD, ...]


@dataclass(frozen=True)
class AnalysisDiff:
    """What changes between the base version's analysis and the candidate one.

    FD collections are compared as normalized singleton-RHS dependencies
    (declaration identity is irrelevant to the properties).  Keys,
    projections and the two booleans are compared structurally.
    """

    minimal_cover_added: tuple[FD, ...]
    minimal_cover_removed: tuple[FD, ...]
    candidate_keys_added: tuple[tuple[str, ...], ...]
    candidate_keys_removed: tuple[tuple[str, ...], ...]
    projections_added: tuple[tuple[FD, ...], ...]    # aligned per subrelation
    projections_removed: tuple[tuple[FD, ...], ...]
    dependency_preserving_changed: bool
    lossless_changed: bool

    @property
    def empty(self) -> bool:
        """True when nothing differs, including the minimal cover.

        Removing a declaration that was never in the minimal cover (an FD
        already implied by the others) leaves every field empty.  Removing
        a redundant member *of the minimal cover* can change which FDs the
        cover lists while keys, projections and verdicts survive; such a
        case is not ``empty`` even though the decomposition properties are
        preserved — inspect the individual fields for that distinction.
        """
        return (
            not self.minimal_cover_added
            and not self.minimal_cover_removed
            and not self.candidate_keys_added
            and not self.candidate_keys_removed
            and not any(self.projections_added)
            and not any(self.projections_removed)
            and not self.dependency_preserving_changed
            and not self.lossless_changed
        )


@dataclass(frozen=True)
class TrialOutcome:
    """Result of opening or extending a trial.

    ``base_version`` is the committed version the trial is pinned to;
    ``candidate_fds`` is the trial's current candidate declaration set;
    ``added_ids`` / ``removed_ids`` are the *net* changes versus the
    committed declarations (an id added and removed again within the
    trial appears on neither).  ``result`` is the full analysis freshly
    recomputed from the candidate set; ``diff`` reports every change in
    the decomposition properties versus the committed version.
    """

    trial_id: int
    base_version: int
    candidate_fds: tuple[DeclaredFD, ...]
    added_ids: tuple[str, ...]
    removed_ids: tuple[str, ...]
    result: AnalysisResult
    diff: AnalysisDiff


@dataclass(frozen=True)
class CommitReceipt:
    """Published after a successful trial commit.

    ``version`` is the new current version (strictly increasing; the first
    committed version is 2 when the store starts at version 1).  Only a
    complete trial committed against the current version produces one.
    """

    version: int
    previous_version: int
    trial_id: int
    added_ids: tuple[str, ...]
    removed_ids: tuple[str, ...]
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
    normalized: list[tuple[frozenset, frozenset]] = []
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
            normalized.append(key)
    return tuple(normalized)


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
    # Left-reduction: remove extraneous determinant attributes.
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
# Public entry point
# ---------------------------------------------------------------------------

def _compute_analysis(attrs, fds, decomp, budget) -> AnalysisResult:
    """Run every analysis algorithm on already-validated inputs.

    ``fds`` are normalized singleton-RHS (frozenset, frozenset) pairs and
    ``decomp`` a tuple of frozensets.  Shared by the stateless
    :func:`analyze` entry point and the versioned trial layer, so a trial
    always recomputes from its candidate set through exactly the same
    algorithms — never from a cached closure of an earlier set.
    """
    cover_raw = _minimal_cover(fds)
    keys = _candidate_keys(attrs, fds)
    projections_raw = tuple(_project(attrs, fds, part) for part in decomp)
    preserving = _is_dependency_preserving(fds, projections_raw)
    lossless, table, log = _chase(attrs, fds, decomp, budget)
    counterexample = None if lossless else _build_counterexample(
        attrs, fds, decomp, table
    )

    def canon(fd_pair):
        lhs, rhs = fd_pair
        (a,) = rhs
        return FD(lhs=tuple(x for x in attrs if x in lhs), rhs=a)

    def sort_fds(fd_pairs):
        return tuple(sorted((canon(p) for p in fd_pairs),
                            key=lambda f: (f.lhs, f.rhs)))

    return AnalysisResult(
        attributes=attrs,
        fds=sort_fds(fds),
        decomposition=tuple(
            tuple(a for a in attrs if a in part) for part in decomp
        ),
        minimal_cover=sort_fds(cover_raw),
        candidate_keys=keys,
        projections=tuple(sort_fds(p) for p in projections_raw),
        dependency_preserving=preserving,
        lossless=lossless,
        chase_log=log,
        counterexample=counterexample,
    )


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
# Versioned trial layer — in-memory analysis versions
# ---------------------------------------------------------------------------
#
# A store holds exactly one *committed* state: (version, declared FDs,
# full analysis).  That is the only "official" analysis.  A trial is an
# isolated scratch space pinned to the version it was opened against; it
# can add and remove several declarations at once, and every extension
# recomputes the minimal cover, candidate keys, projections, preservation
# and chase from the whole candidate set through ``_compute_analysis`` —
# closures from a prior candidate set are never reused.  Only ``commit``
# replaces the official state; it is atomic, optimistic on the version,
# and refuses to publish anything when the trial's base version is stale
# or the trial was rolled back.  Budget exhaustion registers no trial and
# changes no state: the caller only gets ``TrialIncomplete``.

class _Trial:
    """Mutable internal trial record (never handed out directly)."""

    __slots__ = (
        "trial_id", "base_version", "added_ids", "removed_ids",
        "declarations", "outcome", "active",
    )

    def __init__(self, trial_id, base_version, added_ids, removed_ids,
                 declarations, outcome):
        self.trial_id = trial_id
        self.base_version = base_version
        self.added_ids = added_ids          # cumulative, input order
        self.removed_ids = removed_ids      # cumulative, input order
        self.declarations = declarations    # tuple of DeclaredFD (candidate set)
        self.outcome = outcome              # latest TrialOutcome
        self.active = True


def _declarations_to_pairs(declarations):
    """Flatten declared FDs to the validated singleton (lhs, rhs) pairs."""
    pairs = []
    for d in declarations:
        for s in d.singletons:
            pairs.append((frozenset(s.lhs), frozenset({s.rhs})))
    return tuple(pairs)


def _diff_analyses(base: AnalysisResult, cand: AnalysisResult) -> AnalysisDiff:
    """Structural comparison of two analyses over the same schema/decomp."""
    def parts(old, new):
        old_s, new_s = set(old), set(new)
        added = tuple(sorted(new_s - old_s, key=lambda f: (f.lhs, f.rhs)))
        removed = tuple(sorted(old_s - new_s, key=lambda f: (f.lhs, f.rhs)))
        return added, removed

    cover_added, cover_removed = parts(base.minimal_cover, cand.minimal_cover)

    base_keys, cand_keys = set(base.candidate_keys), set(cand.candidate_keys)
    keys_added = tuple(sorted(cand_keys - base_keys))
    keys_removed = tuple(sorted(base_keys - cand_keys))

    proj_added: list[tuple[FD, ...]] = []
    proj_removed: list[tuple[FD, ...]] = []
    for p_old, p_new in zip(base.projections, cand.projections):
        a, r = parts(p_old, p_new)
        proj_added.append(a)
        proj_removed.append(r)

    return AnalysisDiff(
        minimal_cover_added=cover_added,
        minimal_cover_removed=cover_removed,
        candidate_keys_added=keys_added,
        candidate_keys_removed=keys_removed,
        projections_added=tuple(proj_added),
        projections_removed=tuple(proj_removed),
        dependency_preserving_changed=(
            base.dependency_preserving != cand.dependency_preserving
        ),
        lossless_changed=base.lossless != cand.lossless,
    )


class AnalysisVersionStore:
    """In-memory versioned container for one decomposition analysis.

    The schema (attributes) and the decomposition are fixed for the
    store's lifetime; only the FD set changes across versions.  All state
    lives in memory; there is no persistence, no database and no network.
    Methods are safe to call from multiple threads: a lock makes each
    open/extend/rollback/commit atomic, so two concurrent commits race
    cleanly — exactly one wins, the loser gets :class:`StaleVersionError`.
    """

    def __init__(self, attributes, FDs, decomposition, *,
                 step_budget=DEFAULT_STEP_BUDGET):
        self._attrs = _validate_attributes(attributes)
        decomp = _validate_decomposition(decomposition, self._attrs)
        self._budget = _validate_budget(step_budget)
        self._decomp = decomp
        self._lock = threading.RLock()
        self._version = 1
        self._next_trial_id = 1
        self._trials: dict[int, _Trial] = {}
        initial_decls = self._build_declarations(FDs)
        self._declarations = initial_decls
        self._result = self._recompute(initial_decls)

    # -- introspection ----------------------------------------------------

    @property
    def version(self) -> int:
        """Current committed version (starts at 1)."""
        return self._version

    @property
    def attributes(self) -> tuple[str, ...]:
        return self._attrs

    @property
    def decomposition(self) -> tuple[tuple[str, ...], ...]:
        return self._result.decomposition

    def current(self) -> AnalysisResult:
        """The official analysis of the current committed version."""
        return self._result

    def declarations(self) -> tuple[DeclaredFD, ...]:
        """The committed declarations, in declaration order."""
        return self._declarations

    def trial(self, trial_id) -> TrialOutcome:
        """Latest outcome of an active trial; raises once it is closed."""
        with self._lock:
            t = self._require_active_trial(trial_id)
            return t.outcome

    # -- declaration validation specific to the trial layer ---------------

    def _build_declarations(self, FDs) -> tuple[DeclaredFD, ...]:
        """Validate the initial ``{fd_id: (lhs, rhs)}`` mapping/iterable.

        Unlike the stateless :func:`analyze`, declarations carry ids, so
        duplicate *dependencies* are allowed here (two declarations may
        state the same FD; removing one leaves the other intact); only
        duplicate ids are rejected.
        """
        decls: list[DeclaredFD] = []
        ids = set()
        if isinstance(FDs, (str, bytes)):
            raise InvalidInputError(
                "FDs: expected an iterable/mapping of (id, lhs, rhs) entries"
            )
        items = self._normalize_fd_entries(FDs)
        known = frozenset(self._attrs)
        for pos, (fd_id, lhs_raw, rhs_raw) in enumerate(items):
            if isinstance(fd_id, float) and (math.isnan(fd_id) or math.isinf(fd_id)):
                raise InvalidInputError(
                    f"FDs[{pos}].fd_id: NaN/Infinity is not a valid id"
                )
            if not isinstance(fd_id, str) or fd_id == "":
                raise InvalidInputError(
                    f"FDs[{pos}].fd_id: declaration id must be a non-empty "
                    f"str, got {type(fd_id).__name__} ({fd_id!r})"
                )
            if fd_id in ids:
                raise InvalidInputError(
                    f"FDs[{pos}].fd_id: duplicate declaration id {fd_id!r}"
                )
            where = f"FDs[{pos}] ({fd_id!r})"
            lhs = _validate_name_set(lhs_raw, f"{where}.lhs", known)
            rhs = _validate_name_set(rhs_raw, f"{where}.rhs", known)
            if not rhs:
                raise InvalidInputError(f"{where}.rhs: right-hand side must be non-empty")
            lhs_t = tuple(a for a in self._attrs if a in lhs)
            rhs_t = tuple(a for a in self._attrs if a in rhs)
            singletons = tuple(FD(lhs=lhs_t, rhs=a) for a in rhs_t)
            ids.add(fd_id)
            decls.append(DeclaredFD(fd_id, lhs_t, rhs_t, singletons))
        return tuple(decls)

    @staticmethod
    def _normalize_fd_entries(FDs):
        """Accept a mapping {id: (lhs, rhs)} or an iterable of triples.

        Each accepted item becomes ``(fd_id, lhs, rhs)``.
        """
        if hasattr(FDs, "items"):
            try:
                return [(k, v[0], v[1]) for k, v in FDs.items()]
            except (TypeError, IndexError, KeyError):
                raise InvalidInputError(
                    "FDs: mapping values must be (lhs, rhs) pairs"
                ) from None
        try:
            items = list(FDs)
        except TypeError:
            raise InvalidInputError(
                f"FDs: expected an iterable/mapping of declarations, "
                f"got {type(FDs).__name__}"
            ) from None
        normalized = []
        for i, item in enumerate(items):
            if isinstance(item, (str, bytes)):
                raise InvalidInputError(
                    f"FDs[{i}]: expected (fd_id, lhs, rhs), got {item!r}"
                )
            try:
                fd_id, lhs, rhs = item
            except (TypeError, ValueError):
                raise InvalidInputError(
                    f"FDs[{i}]: expected a (fd_id, lhs, rhs) triple, got {item!r}"
                ) from None
            normalized.append((fd_id, lhs, rhs))
        return normalized

    def _validate_add_specs(self, specs):
        """Validate ``add_fds`` specs for one trial operation.

        Returns a list of ``(fd_id, DeclaredFD)`` in the given order.
        Duplicate normalized dependencies are allowed (they are separate
        declarations and must be removable independently); only duplicate
        ids within the operation are rejected.  Trivial FDs are accepted
        here and, as in :func:`analyze`, simply never enter a minimal
        cover.
        """
        items = self._normalize_fd_entries(specs)
        known = frozenset(self._attrs)
        out = []
        local_ids = set()
        for pos, (fd_id, lhs_raw, rhs_raw) in enumerate(items):
            if not isinstance(fd_id, str) or fd_id == "":
                raise InvalidInputError(
                    f"add_fds[{pos}].fd_id: declaration id must be a non-empty "
                    f"str, got {type(fd_id).__name__} ({fd_id!r})"
                )
            if fd_id in local_ids:
                raise InvalidInputError(
                    f"add_fds[{pos}].fd_id: duplicate id {fd_id!r} in one "
                    "trial operation"
                )
            where = f"add_fds[{pos}] ({fd_id!r})"
            lhs = _validate_name_set(lhs_raw, f"{where}.lhs", known)
            rhs = _validate_name_set(rhs_raw, f"{where}.rhs", known)
            if not rhs:
                raise InvalidInputError(f"{where}.rhs: right-hand side must be non-empty")
            local_ids.add(fd_id)
            lhs_t = tuple(a for a in self._attrs if a in lhs)
            rhs_t = tuple(a for a in self._attrs if a in rhs)
            singletons = tuple(FD(lhs=lhs_t, rhs=a) for a in rhs_t)
            out.append((fd_id, DeclaredFD(fd_id, lhs_t, rhs_t, singletons)))
        return out

    @staticmethod
    def _validate_id_list(value, where):
        if isinstance(value, (str, bytes)):
            raise InvalidInputError(
                f"{where}: expected an iterable of declaration ids, not a "
                "single string"
            )
        try:
            ids = list(value)
        except TypeError:
            raise InvalidInputError(
                f"{where}: expected an iterable of declaration ids, got "
                f"{type(value).__name__}"
            ) from None
        out = []
        seen = set()
        for i, fd_id in enumerate(ids):
            if not isinstance(fd_id, str) or fd_id == "":
                raise InvalidInputError(
                    f"{where}[{i}]: declaration id must be a non-empty str, "
                    f"got {fd_id!r}"
                )
            if fd_id in seen:
                raise InvalidInputError(
                    f"{where}[{i}]: duplicate declaration id {fd_id!r} in "
                    "one trial operation"
                )
            seen.add(fd_id)
            out.append(fd_id)
        return out

    # -- core recomputation ----------------------------------------------

    def _recompute(self, declarations) -> AnalysisResult:
        """Recompute the complete analysis from the candidate set.

        Called for the initial state and for every trial open/extend.  It
        builds a fresh singleton-RHS pair list and runs
        :func:`_compute_analysis`, so minimal cover, keys, projections,
        preservation and the chase are all derived anew; no closure from
        an earlier candidate set survives an undo.
        """
        pairs = _declarations_to_pairs(declarations)
        return _compute_analysis(self._attrs, pairs, self._decomp, self._budget)

    # -- trial lifecycle --------------------------------------------------

    def _require_active_trial(self, trial_id) -> _Trial:
        if not isinstance(trial_id, int) or isinstance(trial_id, bool) or trial_id <= 0:
            raise InvalidInputError(
                f"trial_id: expected a positive int, got {trial_id!r}"
            )
        trial = self._trials.get(trial_id)
        if trial is None:
            raise TrialConflictError(
                f"unknown trial id {trial_id}; open a trial first"
            )
        if not trial.active:
            raise TrialConflictError(
                f"trial {trial_id} is closed (committed or rolled back)"
            )
        return trial

    def _apply_candidate(self, base_decls, add_specs, remove_ids):
        """Validate adds/removes against a candidate declaration tuple.

        Returns the new candidate declaration tuple.  Raises
        :class:`TrialConflictError` for unknown/duplicated ids without
        touching any state.  Removal is by declaration id only: the
        target declaration is dropped, and any identical dependency
        contributed by another declaration is retained.
        """
        by_id = {d.fd_id: d for d in base_decls}

        # Removals are checked against what is currently present (the
        # candidate set after earlier operations in this trial).
        surviving = list(base_decls)
        surviving_ids = set(by_id)
        for fd_id in remove_ids:
            if fd_id not in surviving_ids:
                raise TrialConflictError(
                    f"remove_fds: declaration id {fd_id!r} is not present in "
                    "the trial's candidate set"
                )
            surviving_ids.discard(fd_id)
            surviving = [d for d in surviving if d.fd_id != fd_id]

        for fd_id, decl in add_specs:
            if fd_id in surviving_ids:
                raise TrialConflictError(
                    f"add_fds: declaration id {fd_id!r} already exists in the "
                    "trial's candidate set; remove it before re-adding"
                )
            surviving_ids.add(fd_id)
            surviving.append(decl)
        return tuple(surviving)

    @staticmethod
    def _net_id_lists(committed, candidate):
        """Net added/removed declaration ids versus the committed set.

        Membership only: added ids follow candidate declaration order,
        removed ids follow committed declaration order.  An id added and
        removed again within the trial, or a committed id removed and
        re-added (even with different sides), appears on neither list —
        the replacement is visible directly in ``candidate_fds`` and in
        the property ``diff``.
        """
        committed_ids = {d.fd_id for d in committed}
        candidate_ids = {d.fd_id for d in candidate}
        added_ids = tuple(d.fd_id for d in candidate
                          if d.fd_id not in committed_ids)
        removed_ids = tuple(d.fd_id for d in committed
                            if d.fd_id not in candidate_ids)
        return added_ids, removed_ids

    def _recompute_or_incomplete(self, declarations):
        """Recompute, translating budget exhaustion into TrialIncomplete.

        On exhaustion nothing is returned and the caller must not register
        or keep any partial candidate analysis.  Any other error
        (invalid input etc.) propagates unchanged.
        """
        try:
            return self._recompute(declarations)
        except SearchBudgetExhausted as exc:
            raise TrialIncomplete(
                f"trial computation incomplete: {exc}; no trial/analysis was "
                "published — retry with a larger step_budget",
                step_budget=self._budget,
                reason="chase_budget_exhausted",
            ) from None

    def open_trial(self, *, add_fds=(), remove_fds=()) -> TrialOutcome:
        """Open a fresh trial pinned to the current committed version.

        ``add_fds`` / ``remove_fds`` are applied together to the committed
        declaration set, and the whole candidate analysis is recomputed
        from scratch.  Either or both may be empty (a no-op trial is
        legal).  Budget exhaustion raises :class:`TrialIncomplete` and
        registers nothing.
        """
        add_specs = self._validate_add_specs(add_fds)
        remove_ids = self._validate_id_list(remove_fds, "remove_fds")
        with self._lock:
            base_decls = self._declarations
            candidate = self._apply_candidate(base_decls, add_specs, remove_ids)
            result = self._recompute_or_incomplete(candidate)
            trial_id = self._next_trial_id
            self._next_trial_id += 1
            added_cum, removed_cum = self._net_id_lists(
                self._declarations, candidate
            )
            outcome = TrialOutcome(
                trial_id=trial_id,
                base_version=self._version,
                candidate_fds=candidate,
                added_ids=added_cum,
                removed_ids=removed_cum,
                result=result,
                diff=_diff_analyses(self._result, result),
            )
            self._trials[trial_id] = _Trial(
                trial_id, self._version, list(added_cum), list(removed_cum),
                candidate, outcome,
            )
            return outcome

    def extend_trial(self, trial_id, *, add_fds=(), remove_fds=()) -> TrialOutcome:
        """Apply more adds/removes to an open trial and recompute.

        The new operation is applied on top of the trial's current
        candidate set (so a declaration added earlier in the trial can be
        removed, and a removed id can be re-added with new sides).  The
        returned outcome supersedes the previous one for that trial.
        """
        add_specs = self._validate_add_specs(add_fds)
        remove_ids = self._validate_id_list(remove_fds, "remove_fds")
        with self._lock:
            trial = self._require_active_trial(trial_id)
            candidate = self._apply_candidate(
                trial.declarations, add_specs, remove_ids
            )
            result = self._recompute_or_incomplete(candidate)
            # Rebuild the cumulative id lists as the net membership change
            # versus the committed declaration set: an id added in the
            # trial and later removed again appears on neither list; a
            # committed id removed and later re-added appears on neither
            # list either (its new content is visible in candidate_fds).
            added_cum, removed_cum = self._net_id_lists(
                self._declarations, candidate
            )
            trial.added_ids = list(added_cum)
            trial.removed_ids = list(removed_cum)
            trial.declarations = candidate
            outcome = TrialOutcome(
                trial_id=trial.trial_id,
                base_version=trial.base_version,
                candidate_fds=candidate,
                added_ids=added_cum,
                removed_ids=removed_cum,
                result=result,
                diff=_diff_analyses(self._result, result),
            )
            trial.outcome = outcome
            return outcome

    def rollback(self, trial_id) -> None:
        """Discard a trial.  Its candidate analysis is never usable again."""
        with self._lock:
            trial = self._require_active_trial(trial_id)
            trial.active = False
            trial.declarations = ()
            trial.outcome = None

    def commit(self, trial_id, *, expected_version=None) -> CommitReceipt:
        """Publish a trial's candidate analysis as the new official version.

        Optimistic concurrency: the trial is pinned to its base version.
        ``expected_version`` may additionally assert the version the
        caller believes is current; if it is given and differs from the
        trial's base, :class:`InvalidInputError` is raised.  If another
        trial committed first, the current version differs from the
        trial's base and the commit is refused with
        :class:`StaleVersionError` — the trial stays open and untouched so
        the caller can roll it back and recompute against the new
        candidate set.  On success the official state is replaced
        atomically, the version is incremented, and the trial is closed.
        """
        if expected_version is not None:
            if (isinstance(expected_version, bool)
                    or not isinstance(expected_version, int)):
                raise InvalidInputError(
                    "expected_version: expected int, got "
                    f"{type(expected_version).__name__} ({expected_version!r})"
                )
        with self._lock:
            trial = self._require_active_trial(trial_id)
            if expected_version is not None and expected_version != trial.base_version:
                raise InvalidInputError(
                    f"expected_version={expected_version} does not match the "
                    f"trial's base version {trial.base_version}"
                )
            current = self._version
            if trial.base_version != current:
                raise StaleVersionError(trial.base_version, current)
            # Publish only a complete, freshly computed analysis.
            result = trial.outcome.result
            receipt = CommitReceipt(
                version=current + 1,
                previous_version=current,
                trial_id=trial.trial_id,
                added_ids=tuple(trial.added_ids),
                removed_ids=tuple(trial.removed_ids),
                result=result,
            )
            self._declarations = trial.declarations
            self._result = result
            self._version = current + 1
            trial.active = False
            trial.outcome = None
            return receipt
