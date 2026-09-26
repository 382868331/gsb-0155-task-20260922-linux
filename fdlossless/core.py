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
MAX_ATTRIBUTES_MVD = 8
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
class MVD:
    """A multivalued dependency X ->> Y with normalized sides.

    Attributes of Y already present in X have been removed, so
    ``lhs`` and ``rhs`` are disjoint and ``rhs`` is non-empty.
    Both tuples follow the canonical (input) attribute order.
    """

    lhs: tuple[str, ...]
    rhs: tuple[str, ...]

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        left = ",".join(self.lhs) if self.lhs else "∅"
        right = ",".join(self.rhs)
        return f"{left} ->> {right}"


@dataclass(frozen=True)
class ChaseEvent:
    """One chase step under functional AND multivalued dependencies.

    ``kind`` is ``"fd-merge"`` (two symbols are equated) or
    ``"mvd-row"`` (a new tableau row is generated).
    For ``"mvd-row"``, ``rows`` are the (0-based) parent rows and
    ``added_row`` carries the formatted symbols of the new row.
    """

    step: int
    kind: str
    dependency: str
    rows: tuple[int, ...]
    added_row: tuple[str, ...] | None
    detail: str


@dataclass(frozen=True)
class MVDImplication:
    """Result of testing whether FDs + MVDs imply a candidate MVD.

    When the MVD is not implied, ``counterexample`` is a finite relation
    (tuple of integer-constant tuples, in attribute order) that satisfies
    every input FD and MVD but violates the queried MVD; it has been
    checked by brute force before being returned.
    """

    mvd: MVD
    implied: bool
    chase_log: tuple[ChaseEvent, ...]
    counterexample: tuple[tuple[int, ...], ...] | None
    detail: str


@dataclass(frozen=True)
class FourNFViolation:
    """A non-trivial MVD that violates 4NF on one subrelation.

    ``determinant_superkey`` is always ``False`` for a genuine violation
    (4NF requires every non-trivial MVD X ->> Y to have X a superkey);
    the flag is kept explicitly so the superkey check is part of the
    output rather than an implicit reason.
    """

    subrelation: tuple[str, ...]
    mvd: MVD
    determinant_superkey: bool
    detail: str


@dataclass(frozen=True)
class FourNFResult:
    attributes: tuple[str, ...]
    fds: tuple[FD, ...]
    mvds: tuple[MVD, ...]
    decomposition: tuple[tuple[str, ...], ...]
    candidate_keys: tuple[tuple[str, ...], ...]
    lossless: bool
    chase_log: tuple[ChaseEvent, ...]
    counterexample: CounterExample | None
    violations: tuple[FourNFViolation, ...]
    is_4nf: bool


# ---------------------------------------------------------------------------
# Input validation (reject, never silently correct)
# ---------------------------------------------------------------------------

def _validate_attributes(attributes, limit: int = MAX_ATTRIBUTES) -> tuple[str, ...]:
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
    if len(items) > limit:
        raise InvalidInputError(
            f"attributes: {len(items)} attributes given, at most "
            f"{limit} allowed"
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


def _validate_mvds(MVDs, attrs: tuple[str, ...]) -> tuple[tuple[frozenset, frozenset], ...]:
    """Validate MVDs and normalize: attributes of Y already in X are removed.

    Returns (lhs, rhs) frozenset pairs with disjoint sides and non-empty rhs.
    """
    if isinstance(MVDs, (str, bytes)):
        raise InvalidInputError("MVDs: expected an iterable of (lhs, rhs) pairs")
    try:
        items = list(MVDs)
    except TypeError:
        raise InvalidInputError(
            f"MVDs: expected an iterable of (lhs, rhs) pairs, "
            f"got {type(MVDs).__name__}"
        ) from None
    known = frozenset(attrs)
    normalized: list[tuple[frozenset, frozenset]] = []
    seen = set()
    for i, mvd in enumerate(items):
        if isinstance(mvd, (str, bytes, set, frozenset)):
            raise InvalidInputError(
                f"MVDs[{i}]: expected an ordered (lhs, rhs) pair, got {mvd!r}"
            )
        try:
            lhs_raw, rhs_raw = mvd
        except (TypeError, ValueError):
            raise InvalidInputError(
                f"MVDs[{i}]: expected a (lhs, rhs) pair, got {mvd!r}"
            ) from None
        lhs = _validate_name_set(lhs_raw, f"MVDs[{i}].lhs", known)
        rhs = _validate_name_set(rhs_raw, f"MVDs[{i}].rhs", known)
        rhs = rhs - lhs
        if not rhs:
            raise InvalidInputError(
                f"MVDs[{i}].rhs: right-hand side is empty after removing "
                "attributes already in the determinant"
            )
        key = (lhs, rhs)
        if key in seen:
            raise InvalidInputError(
                f"MVDs[{i}]: duplicate MVD {sorted(lhs)} ->> {sorted(rhs)} "
                "after normalization"
            )
        seen.add(key)
        normalized.append(key)
    return tuple(normalized)


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
# Chase under functional AND multivalued dependencies
# ---------------------------------------------------------------------------

def _canon_mvd(attrs, pair) -> MVD:
    lhs, rhs = pair
    return MVD(
        lhs=tuple(a for a in attrs if a in lhs),
        rhs=tuple(a for a in attrs if a in rhs),
    )


def _chase_general(attrs, fds, mvds, decomp, budget: int):
    """Chase with FD equality merges and MVD row generation.

    The budget counts every step: each symbol merge and each added row.
    Returns (lossless, final_table, log).
    """
    n = len(attrs)
    index = {a: j for j, a in enumerate(attrs)}
    table = [
        [("d", j) if attrs[j] in part else ("n", i, j) for j in range(n)]
        for i, part in enumerate(decomp)
    ]
    rowset = {tuple(row) for row in table}
    log: list[ChaseEvent] = []
    steps = 0

    def spend():
        nonlocal steps
        if steps >= budget:
            raise SearchBudgetExhausted(
                f"chase budget exhausted after {steps} step(s) "
                f"(merges + generated rows, step_budget={budget}); no "
                "verdict reached — this is not a proof that the "
                "decomposition is lossy"
            )
        steps += 1

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
                        kept = min(si, sj)
                        replaced = max(si, sj)
                        spend()
                        for row in table:
                            for c in range(n):
                                if row[c] == replaced:
                                    row[c] = kept
                        rowset.clear()
                        rowset.update(tuple(r) for r in table)
                        fd_obj = FD(
                            lhs=tuple(x for x in attrs if x in lhs), rhs=a_name
                        )
                        lhs_txt = ",".join(fd_obj.lhs) if fd_obj.lhs else "∅"
                        log.append(
                            ChaseEvent(
                                step=steps,
                                kind="fd-merge",
                                dependency=str(fd_obj),
                                rows=(i, j),
                                added_row=None,
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
        for lhs, rhs in mvds:
            xcols = [index[x] for x in lhs]
            xset = set(xcols)
            ycols = {index[y] for y in rhs}
            mvd_txt = str(_canon_mvd(attrs, (lhs, rhs)))
            m = len(table)
            for i in range(m):
                for j in range(m):
                    if i == j:
                        continue
                    if not all(table[i][c] == table[j][c] for c in xcols):
                        continue
                    new = tuple(
                        table[i][c] if (c in xset or c in ycols) else table[j][c]
                        for c in range(n)
                    )
                    if new in rowset:
                        continue
                    spend()
                    table.append(list(new))
                    rowset.add(new)
                    formatted = tuple(format_symbol(s, attrs) for s in new)
                    log.append(
                        ChaseEvent(
                            step=steps,
                            kind="mvd-row",
                            dependency=mvd_txt,
                            rows=(i, j),
                            added_row=formatted,
                            detail=(
                                f"rows {i + 1} and {j + 1} agree on the "
                                f"determinant of {mvd_txt}; generated row "
                                f"{len(table)} = {formatted} (row {i + 1} "
                                f"on X∪Y, row {j + 1} on the rest)"
                            ),
                        )
                    )
                    changed = True
    lossless = any(all(s[0] == "d" for s in row) for row in table)
    return lossless, table, tuple(log)


# ---------------------------------------------------------------------------
# Finite instances: satisfaction checks and witnesses
# ---------------------------------------------------------------------------

def _instance_from_table(attrs, table) -> tuple[tuple[int, ...], ...]:
    """Map a final tableau to a finite relation of integer constants."""
    n = len(attrs)
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
    return tuple(instance)


def _mvd_holds(instance, xcols, ycols, n: int) -> bool:
    """Brute-force check of X ->> Y on a finite relation."""
    xy = set(xcols) | set(ycols)
    tset = set(instance)
    for t1 in instance:
        for t2 in instance:
            if all(t1[c] == t2[c] for c in xcols):
                t3 = tuple(t1[c] if c in xy else t2[c] for c in range(n))
                if t3 not in tset:
                    return False
    return True


def _satisfies_all(instance, fds, mvds, index, n: int) -> bool:
    if not _satisfies(instance, fds, index):
        return False
    for lhs, rhs in mvds:
        xcols = [index[x] for x in lhs]
        ycols = [index[y] for y in rhs]
        if not _mvd_holds(instance, xcols, ycols, n):
            return False
    return True


def _mvd_implication_chase(attrs, fds, mvds, x, y, budget: int):
    """Test D ⊨ X ->> Y via a two-row tableau chase.

    Row 1 is distinguished on X∪Y, row 2 on X∪Z (Z = R − X − Y).
    The MVD is implied iff the chase derives an all-distinguished row.
    """
    z = frozenset(a for a in attrs if a not in x and a not in y)
    decomp = (frozenset(x) | frozenset(y), frozenset(x) | z)
    return _chase_general(attrs, fds, mvds, decomp, budget)


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


# ---------------------------------------------------------------------------
# Public helpers: closure and implication
# ---------------------------------------------------------------------------

def attribute_closure(attributes, FDs, seed) -> tuple[str, ...]:
    """Closure of ``seed`` under ``FDs``, in canonical attribute order."""
    attrs = _validate_attributes(attributes)
    fds = _validate_fds(FDs, attrs)
    seed_set = _validate_name_set(seed, "seed", frozenset(attrs))
    closed = _closure(fds, seed_set)
    return tuple(a for a in attrs if a in closed)


def check_fd(attributes, FDs, lhs, rhs) -> bool:
    """True iff ``FDs`` logically imply ``lhs -> rhs`` (rhs non-empty)."""
    attrs = _validate_attributes(attributes)
    fds = _validate_fds(FDs, attrs)
    known = frozenset(attrs)
    left = _validate_name_set(lhs, "lhs", known)
    right = _validate_name_set(rhs, "rhs", known)
    if not right:
        raise InvalidInputError("rhs: right-hand side must be non-empty")
    return right <= _closure(fds, left)


def check_mvd(attributes, FDs, MVDs, lhs, rhs, *,
              step_budget=DEFAULT_STEP_BUDGET) -> MVDImplication:
    """Test whether FDs + MVDs imply ``lhs ->> rhs``.

    Attributes of ``rhs`` already in ``lhs`` are removed first.  When the
    MVD is not implied, the result carries a finite counterexample relation
    that satisfies every input dependency but violates the queried MVD;
    the witness is verified by brute force before being returned.
    """
    attrs = _validate_attributes(attributes, limit=MAX_ATTRIBUTES_MVD)
    fds = _validate_fds(FDs, attrs)
    mvds = _validate_mvds(MVDs, attrs)
    budget = _validate_budget(step_budget)
    known = frozenset(attrs)
    left = _validate_name_set(lhs, "lhs", known)
    right = _validate_name_set(rhs, "rhs", known)
    right = right - left
    if not right:
        raise InvalidInputError(
            "rhs: right-hand side is empty after removing attributes "
            "already in the determinant"
        )
    query = _canon_mvd(attrs, (left, right))
    implied, table, log = _mvd_implication_chase(
        attrs, fds, mvds, left, right, budget
    )
    counterexample = None
    if implied:
        detail = f"{query} is implied by the given FDs and MVDs (chase derived an all-distinguished row)."
    else:
        n = len(attrs)
        index = {a: j for j, a in enumerate(attrs)}
        instance = _instance_from_table(attrs, table)
        if not _satisfies_all(instance, fds, mvds, index, n):
            raise AssertionError(
                "internal error: counterexample violates an input dependency"
            )
        xcols = [index[x] for x in left]
        ycols = [index[y] for y in right]
        if _mvd_holds(instance, xcols, ycols, n):
            raise AssertionError(
                "internal error: counterexample satisfies the queried MVD"
            )
        counterexample = instance
        detail = (
            f"{query} is NOT implied; counterexample relation "
            f"{list(instance)} satisfies every input FD and MVD but "
            "violates the queried MVD."
        )
    return MVDImplication(
        mvd=query,
        implied=implied,
        chase_log=log,
        counterexample=counterexample,
        detail=detail,
    )


# ---------------------------------------------------------------------------
# 4NF decomposition analysis under FDs + MVDs
# ---------------------------------------------------------------------------

def analyze4nf(attributes, FDs, MVDs, decomposition, *,
               step_budget=DEFAULT_STEP_BUDGET) -> FourNFResult:
    """Verify a decomposition against 4NF under functional + multivalued deps.

    Reports the lossless-join verdict (chase with FD merges and MVD row
    generation, with a verified counterexample when lossy) and every
    4NF violation: a non-trivial MVD X ->> Y implied by the input
    dependencies with X, Y inside one subrelation, X ∪ Y ≠ subrelation,
    and X not a superkey of that subrelation (superkeys are decided by
    FD closure only — MVDs never make a determinant a key).

    Only minimal left-hand sides and inclusion-minimal right-hand sides
    are reported.  No cheapest decomposition is searched for; the given
    decomposition is verified as-is.
    """
    attrs = _validate_attributes(attributes, limit=MAX_ATTRIBUTES_MVD)
    fds = _validate_fds(FDs, attrs)
    mvds = _validate_mvds(MVDs, attrs)
    decomp = _validate_decomposition(decomposition, attrs)
    budget = _validate_budget(step_budget)

    keys = _candidate_keys(attrs, fds)
    lossless, table, log = _chase_general(attrs, fds, mvds, decomp, budget)

    counterexample = None
    if not lossless:
        n = len(attrs)
        index = {a: j for j, a in enumerate(attrs)}
        instance = _instance_from_table(attrs, table)
        if not _satisfies_all(instance, fds, mvds, index, n):
            raise AssertionError(
                "internal error: counterexample violates an input dependency"
            )
        spurious = tuple(range(n))
        projections = []
        for part in decomp:
            cols = [index[a] for a in attrs if a in part]
            projections.append(
                tuple(sorted({tuple(t[c] for c in cols) for t in instance}))
            )
        for part, proj in zip(decomp, projections):
            cols = [index[a] for a in attrs if a in part]
            if tuple(spurious[c] for c in cols) not in proj:
                raise AssertionError(
                    "internal error: spurious tuple not in join"
                )
        if spurious in instance:
            raise AssertionError(
                "internal error: spurious tuple already in instance"
            )
        counterexample = CounterExample(
            instance=instance,
            projections=tuple(projections),
            spurious_tuple=spurious,
            explanation=(
                f"instance r = {list(instance)} satisfies every input FD and "
                f"MVD, and t = {spurious} lies in the natural join of the "
                "projections (its restriction to each subrelation occurs in "
                "that projection), but t is not a tuple of r; the join "
                "produces a tuple the original relation does not have."
            ),
        )

    # 4NF check: implied non-trivial MVDs inside each subrelation.
    memo: dict = {}

    def implied(x, y) -> bool:
        key = (x, y)
        if key not in memo:
            memo[key] = _mvd_implication_chase(
                attrs, fds, mvds, x, y, budget
            )[0]
        return memo[key]

    violations: list[FourNFViolation] = []
    for part in decomp:
        s_attrs = tuple(a for a in attrs if a in part)
        s_set = frozenset(part)
        for xsize in range(len(s_attrs) + 1):
            for xcombo in combinations(s_attrs, xsize):
                x = frozenset(xcombo)
                superkey = _closure(fds, x) >= s_set
                if superkey:
                    continue  # X a superkey: no 4NF violation via X
                rest = [a for a in s_attrs if a not in x]
                found: list[frozenset] = []  # minimal violating rhs sets
                for ysize in range(1, len(rest) + 1):
                    for ycombo in combinations(rest, ysize):
                        y = frozenset(ycombo)
                        if y == frozenset(rest):
                            continue  # trivial: X ∪ Y = subrelation
                        if any(f <= y for f in found):
                            continue  # non-minimal rhs
                        if not implied(x, y):
                            continue
                        found.append(y)
                        mvd = _canon_mvd(attrs, (x, y))
                        sub_txt = ",".join(s_attrs)
                        violations.append(
                            FourNFViolation(
                                subrelation=s_attrs,
                                mvd=mvd,
                                determinant_superkey=False,
                                detail=(
                                    f"subrelation {{{sub_txt}}}: {mvd} is "
                                    "non-trivial (Y ⊄ X and X ∪ Y ≠ "
                                    "subrelation) and implied by the input "
                                    f"dependencies, but {{{','.join(mvd.lhs)}}}"
                                    " is not a superkey of the subrelation "
                                    "(its FD closure does not cover it) — "
                                    "4NF violated."
                                ),
                            )
                        )

    def sort_mvds(pairs):
        return tuple(sorted((_canon_mvd(attrs, p) for p in pairs),
                            key=lambda m: (m.lhs, m.rhs)))

    return FourNFResult(
        attributes=attrs,
        fds=tuple(sorted(
            (FD(lhs=tuple(a for a in attrs if a in l), rhs=next(iter(r)))
             for l, r in fds),
            key=lambda f: (f.lhs, f.rhs),
        )),
        mvds=sort_mvds(mvds),
        decomposition=tuple(
            tuple(a for a in attrs if a in part) for part in decomp
        ),
        candidate_keys=keys,
        lossless=lossless,
        chase_log=log,
        counterexample=counterexample,
        violations=tuple(violations),
        is_4nf=not violations,
    )
