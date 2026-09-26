"""Lossless-decomposition and 4NF witness engine for FDs and MVDs.

Public entry point: ``analyze(attributes, FDs, decomposition, *, step_budget=...)``.
Passing the keyword ``MVDs=[(lhs, rhs), ...]`` additionally accepts multivalued
dependencies X ->> Y, checks the decomposition against the 4NF condition, and
limits the schema to 8 attributes (``MAX_ATTRIBUTES_MVD``).

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
MAX_ATTRIBUTES_MVD = 8


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
    """One chase action: an FD equality merge or an MVD row addition.

    ``rows`` are 0-based tableau row indices; ``column`` is the attribute
    whose symbols were equated; ``kept`` / ``replaced`` are the formatted
    symbols (distinguished symbols print as the attribute name,
    nondistinguished ones as ``b<row>_<col>`` with 1-based indices).
    For MVD steps (``rule == "MVD"``) no symbols are equated; instead a new
    row is appended and ``added_row`` holds its 0-based index.
    """

    step: int
    fd: FD | "MVD"
    rows: tuple[int, int]
    column: str
    kept: str
    replaced: str
    detail: str
    rule: str = "FD"
    added_row: int | None = None


@dataclass(frozen=True)
class MVD:
    """A multivalued dependency X ->> Y.

    ``lhs`` and ``rhs`` are tuples of attribute names in canonical (input)
    attribute order.  Input MVDs are normalized on validation: attributes
    already in ``lhs`` are removed from ``rhs``, so the two are always
    disjoint.  An empty ``rhs`` is a trivial MVD (always satisfied).
    """

    lhs: tuple[str, ...]
    rhs: tuple[str, ...]

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        left = ",".join(self.lhs) if self.lhs else "∅"
        right = ",".join(self.rhs) if self.rhs else "∅"
        return f"{left} ->> {right}"


@dataclass(frozen=True)
class FourNFViolation:
    """One nontrivial implied MVD whose determinant is not a superkey.

    ``relation`` is the subrelation (of the decomposition) on which the
    violation occurs; ``mvd`` is an inclusion-minimal nontrivial MVD implied
    by the input FDs+MVDs over that subrelation; ``determinant_is_superkey``
    records whether the determinant functionally determines the whole
    subrelation (always False for a reported violation, kept explicit so
    callers can present the check outcome directly).
    """

    relation: tuple[str, ...]
    mvd: MVD
    determinant_is_superkey: bool
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
    mvds: tuple[MVD, ...] = ()               # input MVDs, normalized (rhs disjoint from lhs)
    four_nf: bool = True                     # every subrelation satisfies the 4NF condition
    four_nf_violations: tuple[FourNFViolation, ...] = ()


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


def _validate_mvds(MVDs, attrs: tuple[str, ...]) -> tuple[tuple[frozenset, frozenset], ...]:
    """Validate MVDs and normalize: rhs attributes already in lhs are removed.

    Returns (lhs, rhs) frozenset pairs with disjoint sides; the rhs may be
    empty (a trivial MVD, kept explicitly and ignored by the chase).
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
        if not rhs:
            raise InvalidInputError(
                f"MVDs[{i}].rhs: right-hand side must be non-empty"
            )
        rhs = rhs - lhs  # normalization: drop attributes already on the left
        key = (lhs, rhs)
        if key in seen:
            raise InvalidInputError(
                f"MVDs[{i}]: duplicate MVD {sorted(lhs)} ->> {sorted(rhs)} "
                "after normalization (rhs attributes in lhs are removed)"
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


def _chase_rows(attrs, fds, mvds, table, budget: int):
    """Chase a symbolic tableau under FDs (equality merges) and MVDs (row adds).

    ``table`` is a list of rows of symbols; it is mutated in place and also
    returned together with the step log and the number of steps used.
    FD steps equate two symbols table-wide; MVD steps append a new row
    built from two rows that agree on the determinant (symbols are copied,
    never invented, so the row space is finite and the chase terminates).
    Every merge and every row addition counts against ``budget``.
    """
    n = len(attrs)
    index = {a: j for j, a in enumerate(attrs)}
    log: list[ChaseStep] = []
    steps = 0

    def spend(what: str):
        if steps >= budget:
            raise SearchBudgetExhausted(
                f"chase budget exhausted after {steps} step(s) "
                f"(step_budget={budget}) while applying {what}; no verdict "
                "reached — this is not a proof that the decomposition is "
                "lossy, nor that any dependency is or is not implied"
            )

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
                        spend("an FD equality merge")
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
        for lhs, rhs in mvds:
            if not rhs:  # trivial MVD (normalized away): always satisfied
                continue
            x_cols = [index[x] for x in lhs]
            xy = {index[x] for x in lhs} | {index[y] for y in rhs}
            mvd_obj = MVD(
                lhs=tuple(x for x in attrs if x in lhs),
                rhs=tuple(y for y in attrs if y in rhs),
            )
            lhs_txt = ",".join(mvd_obj.lhs) if mvd_obj.lhs else "∅"
            rhs_txt = ",".join(mvd_obj.rhs)
            known = {tuple(row) for row in table}
            for i in range(len(table)):
                for j in range(len(table)):
                    if i == j:
                        continue
                    if not all(table[i][c] == table[j][c] for c in x_cols):
                        continue
                    new_row = [
                        table[i][c] if c in xy else table[j][c]
                        for c in range(n)
                    ]
                    key = tuple(new_row)
                    if key in known:
                        continue
                    spend("an MVD row addition")
                    steps += 1
                    table.append(new_row)
                    known.add(key)
                    added = len(table) - 1
                    log.append(
                        ChaseStep(
                            step=steps,
                            fd=mvd_obj,
                            rows=(i, j),
                            column="",
                            kept="",
                            replaced="",
                            detail=(
                                f"rows {i + 1} and {j + 1} agree on "
                                f"{{{lhs_txt}}}; MVD {lhs_txt} ->> {rhs_txt} "
                                f"adds row {added + 1}: take columns "
                                f"{{{lhs_txt},{rhs_txt}}} from row {i + 1} "
                                f"and the rest from row {j + 1}"
                            ),
                            rule="MVD",
                            added_row=added,
                        )
                    )
                    changed = True
    return table, tuple(log), steps


def _chase(attrs, fds, mvds, decomp, budget: int):
    n = len(attrs)
    table = [
        [("d", j) if attrs[j] in part else ("n", i, j) for j in range(n)]
        for i, part in enumerate(decomp)
    ]
    table, log, _ = _chase_rows(attrs, fds, mvds, table, budget)
    lossless = any(all(s[0] == "d" for s in row) for row in table)
    return lossless, table, log


# ---------------------------------------------------------------------------
# Counterexample construction from the final (unmerged) tableau
# ---------------------------------------------------------------------------

def _satisfies(instance, fds, mvds, index, n) -> bool:
    for lhs, rhs in fds:
        lhs_cols = [index[x] for x in lhs]
        (a_name,) = rhs
        a = index[a_name]
        for t1 in instance:
            for t2 in instance:
                if all(t1[c] == t2[c] for c in lhs_cols) and t1[a] != t2[a]:
                    return False
    for lhs, rhs in mvds:
        x_cols = [index[x] for x in lhs]
        xy = {index[x] for x in lhs} | {index[y] for y in rhs}
        rest = [c for c in range(n) if c not in xy]
        tuples = set(instance)
        for t1 in instance:
            for t2 in instance:
                if not all(t1[c] == t2[c] for c in x_cols):
                    continue
                swapped = tuple(
                    t1[c] if c in xy else t2[c] for c in range(n)
                )
                if swapped not in tuples:
                    return False
    return True


def _build_counterexample(attrs, fds, mvds, decomp, table) -> CounterExample:
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
    if not _satisfies(instance_t, fds, mvds, index, n):
        raise AssertionError("internal error: counterexample violates a dependency")
    for part, proj in zip(decomp, projections):
        cols = [index[a] for a in attrs if a in part]
        if tuple(spurious[c] for c in cols) not in proj:
            raise AssertionError("internal error: spurious tuple not in join")
    if spurious in seen:
        raise AssertionError("internal error: spurious tuple already in instance")

    explanation = (
        f"instance r = {list(instance_t)} satisfies every input FD and MVD, and "
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
# MVD implication and the 4NF condition
# ---------------------------------------------------------------------------

def _implies_mvd_pairs(attrs, fds, mvds, lhs, budget: int):
    """Chase the 2-row implication tableau for determinant ``lhs``.

    Row 1 is all-distinguished; row 2 is distinguished exactly on ``lhs``.
    Returns the chased rows.  Chase characterization: X ->> Y is implied
    iff some final row equals row 1 on X ∪ Y and row 2 on the rest (the
    swapped tuple must be present in the chased model).
    """
    n = len(attrs)
    index = {a: j for j, a in enumerate(attrs)}
    x_cols = {index[a] for a in lhs}
    table = [
        [("d", j) for j in range(n)],
        [("d", j) if j in x_cols else ("n", 1, j) for j in range(n)],
    ]
    table, _, _ = _chase_rows(attrs, fds, mvds, table, budget)
    return table


def _row_witnesses(table, index, lhs, rhs) -> bool:
    """True iff some chased row is the swapped tuple for X ->> Y."""
    n = len(table[0])
    xy = {index[a] for a in lhs | rhs}
    top, partner = table[0], table[1]
    for row in table:
        if all(row[c] == top[c] for c in xy) and all(
            row[c] == partner[c] for c in range(n) if c not in xy
        ):
            return True
    return False


def _implies_mvd(attrs, fds, mvds, lhs, rhs, budget: int) -> bool:
    """True iff the FDs+MVDs imply X ->> Y (chase decision procedure)."""
    lhs = frozenset(lhs)
    rhs = frozenset(rhs)
    if rhs <= lhs or lhs | rhs >= frozenset(attrs):
        return True  # trivial MVD
    index = {a: j for j, a in enumerate(attrs)}
    table = _implies_mvd_pairs(attrs, fds, mvds, lhs, budget)
    return _row_witnesses(table, index, lhs, rhs)


def _four_nf_violations(attrs, fds, mvds, decomp, budget: int):
    """Check each subrelation against the 4NF condition.

    For every subrelation S and every determinant X ⊆ S that is not a
    superkey of S (under the FDs alone — MVDs never determine attributes),
    one implication chase yields every implied X ->> Y: each final row w
    witnesses exactly the Y with M_w ⊆ Y ⊆ A_w, where M_w (resp. A_w) is
    the set of attributes outside X on which w differs from row 2 (resp.
    agrees with row 1).  Each inclusion-minimal nontrivial implied MVD
    (Y nonempty, X ∪ Y ≠ S) is reported as a violation and independently
    re-verified with its own implication chase before being returned.
    """
    violations: list[FourNFViolation] = []
    for part in decomp:
        sub = tuple(a for a in attrs if a in part)
        sub_set = frozenset(sub)
        for size in range(len(sub) + 1):
            for combo in combinations(sub, size):
                x = frozenset(combo)
                if _closure(fds, x) >= sub_set:
                    continue  # determinant is a superkey: 4NF satisfied here
                table = _implies_mvd_pairs(attrs, fds, mvds, x, budget)
                index = {a: j for j, a in enumerate(attrs)}
                x_cols = {index[a] for a in x}
                top, partner = table[0], table[1]
                # Intervals [M_w, A_w ∩ S] of implied right-hand sides.
                intervals: list[tuple[frozenset, frozenset]] = []
                for row in table:
                    must = frozenset(
                        attrs[j] for j in range(len(attrs))
                        if j not in x_cols and row[j] != partner[j]
                    )
                    may = frozenset(
                        attrs[j] for j in range(len(attrs))
                        if j not in x_cols and row[j] == top[j]
                    ) & sub_set
                    if must <= may:
                        intervals.append((must, may))
                # Nontrivial implied Y: nonempty, inside S - X, X ∪ Y ≠ S,
                # inside some interval.  Keep inclusion-minimal ones.
                implied: list[frozenset] = []
                rest = [a for a in sub if a not in x]
                for ysize in range(1, len(rest) + 1):
                    for ycombo in combinations(rest, ysize):
                        y = frozenset(ycombo)
                        if x | y >= sub_set:
                            continue  # trivial on S
                        if not any(lo <= y <= hi for lo, hi in intervals):
                            continue
                        if any(m < y for m in implied):
                            continue  # not inclusion-minimal
                        implied.append(y)
                for y in implied:
                    # Independent re-verification of the witness MVD.
                    if not _implies_mvd(attrs, fds, mvds, x, y, budget):
                        raise AssertionError(
                            "internal error: reported 4NF violation is not "
                            "an implied MVD"
                        )
                    mvd = MVD(
                        lhs=tuple(a for a in attrs if a in x),
                        rhs=tuple(a for a in attrs if a in y),
                    )
                    lhs_txt = ",".join(mvd.lhs) if mvd.lhs else "∅"
                    rhs_txt = ",".join(mvd.rhs)
                    violations.append(
                        FourNFViolation(
                            relation=sub,
                            mvd=mvd,
                            determinant_is_superkey=False,
                            detail=(
                                f"on subrelation {{{','.join(sub)}}}: "
                                f"nontrivial MVD {lhs_txt} ->> {rhs_txt} "
                                "is implied by the given dependencies, but "
                                f"{{{lhs_txt}}} is not a superkey of this "
                                "subrelation — the 4NF condition fails"
                            ),
                        )
                    )
    return tuple(violations)



# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def analyze(attributes, FDs, decomposition, *, MVDs=None,
            step_budget=DEFAULT_STEP_BUDGET):
    """Analyze a decomposition of a relation schema under FDs and MVDs.

    Parameters
    ----------
    attributes : iterable of str
        Attribute names of the original relation (unique, non-empty).
        At most 12 without MVDs; at most 8 (``MAX_ATTRIBUTES_MVD``) when
        ``MVDs`` is given.
    FDs : iterable of (iterable of str, iterable of str)
        Functional dependencies; each side is a set of attribute names.
        Empty determinants are allowed; right-hand sides must be non-empty.
    decomposition : iterable of iterable of str
        Subrelations; non-empty, known attributes only, and their union must
        equal ``attributes``.
    MVDs : iterable of (iterable of str, iterable of str), keyword-only
        Multivalued dependencies X ->> Y.  Attributes of Y that already
        occur in X are normalized away.  Passing ``MVDs`` (even empty)
        enables the 4NF check of the decomposition and the 8-attribute
        limit.  Omitting it preserves the original FD-only interface.
    step_budget : int, keyword-only
        Maximum number of chase steps (FD merges plus MVD row additions).
        Exhaustion raises ``SearchBudgetExhausted`` — an unfinished
        computation, never a lossy verdict.

    Returns
    -------
    AnalysisResult
        Minimal cover, candidate keys (sorted by size, then
        lexicographically), full projected FDs per subrelation, the
        dependency-preservation verdict, the chase log (FD merges and MVD
        row additions), the losslessness verdict, a verified
        counterexample when lossy, the normalized MVDs, and — when
        ``MVDs`` was given — the 4NF verdict with every violating
        nontrivial implied MVD and whether its determinant is a superkey.
    """
    attrs = _validate_attributes(attributes)
    fds = _validate_fds(FDs, attrs)
    decomp = _validate_decomposition(decomposition, attrs)
    budget = _validate_budget(step_budget)
    if MVDs is None:
        mvds = ()
        check_4nf = False
    else:
        if len(attrs) > MAX_ATTRIBUTES_MVD:
            raise InvalidInputError(
                f"attributes: {len(attrs)} attributes given, at most "
                f"{MAX_ATTRIBUTES_MVD} allowed when MVDs are provided"
            )
        mvds = _validate_mvds(MVDs, attrs)
        check_4nf = True

    cover_raw = _minimal_cover(fds)
    keys = _candidate_keys(attrs, fds)
    projections_raw = tuple(_project(attrs, fds, part) for part in decomp)
    preserving = _is_dependency_preserving(fds, projections_raw)
    lossless, table, log = _chase(attrs, fds, mvds, decomp, budget)
    counterexample = None if lossless else _build_counterexample(
        attrs, fds, mvds, decomp, table
    )
    if check_4nf:
        violations = _four_nf_violations(attrs, fds, mvds, decomp, budget)
    else:
        violations = ()

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
        mvds=tuple(
            MVD(
                lhs=tuple(a for a in attrs if a in lhs),
                rhs=tuple(a for a in attrs if a in rhs),
            )
            for lhs, rhs in mvds
        ),
        four_nf=not violations,
        four_nf_violations=violations,
    )


def attribute_closure(attributes, FDs, X) -> frozenset:
    """Closure of the attribute set ``X`` under the given FDs.

    MVDs deliberately play no role here: multivalued dependencies never
    determine attribute values.  Input is validated with the same contract
    as ``analyze``; returns a frozenset of attribute names.
    """
    attrs = _validate_attributes(attributes)
    fds = _validate_fds(FDs, attrs)
    seed = _validate_name_set(X, "X", frozenset(attrs))
    return _closure(fds, seed)


def implies_mvd(attributes, FDs, MVDs, X, Y, *,
                step_budget=DEFAULT_STEP_BUDGET) -> bool:
    """True iff the given FDs+MVDs imply the multivalued dependency X ->> Y.

    Decided by chasing the two-row implication tableau; ``SearchBudgetExhausted``
    means the question was not settled, not that the MVD fails to hold.
    """
    attrs = _validate_attributes(attributes)
    if len(attrs) > MAX_ATTRIBUTES_MVD:
        raise InvalidInputError(
            f"attributes: {len(attrs)} attributes given, at most "
            f"{MAX_ATTRIBUTES_MVD} allowed for MVD reasoning"
        )
    fds = _validate_fds(FDs, attrs)
    mvds = _validate_mvds(MVDs, attrs)
    known = frozenset(attrs)
    x = _validate_name_set(X, "X", known)
    y = _validate_name_set(Y, "Y", known)
    if not y:
        raise InvalidInputError("Y: right-hand side must be non-empty")
    budget = _validate_budget(step_budget)
    return _implies_mvd(attrs, fds, mvds, x, y - x, budget)
