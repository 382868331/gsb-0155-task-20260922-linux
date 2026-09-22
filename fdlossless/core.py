"""Lossless-decomposition witness engine for functional dependencies.

Public entry point: ``analyze(attributes, FDs, decomposition, *, step_budget=...)``.

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
from dataclasses import dataclass
from itertools import combinations

from .errors import InvalidInputError, SearchBudgetExhausted

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
