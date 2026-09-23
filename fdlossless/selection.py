"""Optimal lossless, dependency-preserving decomposition selector.

Public entry point:
``select_decomposition(attributes, FDs, candidates, required_groups, costs)``.

Given the original relation, its functional dependencies, a small list of
candidate subrelations, attribute groups that must be kept together, and a
non-negative integer cost per candidate, this module enumerates every subset
of the candidates (at most 2**8 - 1 = 255 subsets), keeps exactly the
subsets that

  1. cover every attribute of the original relation,
  2. keep every required attribute group inside one chosen subrelation,
  3. are lossless under the FDs (symbolic-tableau chase), and
  4. preserve the FDs (the union of the projections implies every FD),

and returns the optimum by the ordering

      total cost  <  number of relations  <  lexicographic attribute order

where the lexicographic order compares the chosen subrelations as sorted
tuples of attribute names, themselves sorted in ascending order.

When no subset is feasible, the result reports ``feasible=False`` together
with a classified reason (coverage failure / required-group failure / no
subset that is both lossless and dependency-preserving, with counts).

The selector is a pure function: all input validation happens before any
computation, and failure leaves no partial state.  It reuses the closure,
projection, dependency-preservation and chase machinery from :mod:`core`,
and runs the public :func:`analyze` on the chosen subset so the returned
:class:`SelectionResult` carries the full witness (chase log, projections,
candidate keys, ...).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .core import (
    DEFAULT_STEP_BUDGET,
    FD,
    AnalysisResult,
    _chase,
    _is_dependency_preserving,
    _project,
    _validate_attributes,
    _validate_budget,
    _validate_fds,
    _validate_name_set,
    analyze,
)
from .errors import InvalidInputError

#: At most six attributes for the selector (explicit enumeration domain).
SELECT_MAX_ATTRIBUTES = 6

#: At most eight distinct candidate subrelations (2**8 subsets at most).
MAX_CANDIDATES = 8


@dataclass(frozen=True)
class SelectionResult:
    """Outcome of :func:`select_decomposition`.

    When ``feasible`` is true, ``chosen`` lists the chosen subrelations (in
    input-attribute order, matching ``analysis.decomposition``),
    ``total_cost`` / ``relation_count`` are set, and ``analysis`` is the full
    :class:`AnalysisResult` for that decomposition.

    When no feasible subset exists, ``chosen`` is empty, ``total_cost`` /
    ``relation_count`` / ``analysis`` are ``None``, and ``reason`` gives a
    human-readable, classified explanation of why no solution exists.
    """

    attributes: tuple[str, ...]
    fds: tuple[FD, ...]
    candidates: tuple[tuple[str, ...], ...]
    required_groups: tuple[tuple[str, ...], ...]
    costs: tuple[int, ...]
    feasible: bool
    chosen: tuple[tuple[str, ...], ...]
    total_cost: int | None
    relation_count: int | None
    reason: str | None
    analysis: AnalysisResult | None
    subsets_considered: int


# ---------------------------------------------------------------------------
# Input validation (reject, never silently correct; all before computation)
# ---------------------------------------------------------------------------

def _validate_candidates(value, attrs: tuple[str, ...]) -> tuple[frozenset, ...]:
    if isinstance(value, (str, bytes)):
        raise InvalidInputError(
            "candidates: expected an iterable of attribute sets, "
            "not a single string"
        )
    try:
        items = list(value)
    except TypeError:
        raise InvalidInputError(
            f"candidates: expected an iterable of attribute sets, "
            f"got {type(value).__name__}"
        ) from None
    if not items:
        raise InvalidInputError(
            "candidates: at least one candidate subrelation is required"
        )
    if len(items) > MAX_CANDIDATES:
        raise InvalidInputError(
            f"candidates: {len(items)} candidates given, at most "
            f"{MAX_CANDIDATES} allowed"
        )
    known = frozenset(attrs)
    parts: list[frozenset] = []
    seen: set[frozenset] = set()
    for i, part in enumerate(items):
        s = _validate_name_set(part, f"candidates[{i}]", known)
        if not s:
            raise InvalidInputError(
                f"candidates[{i}]: candidate subrelation must be non-empty"
            )
        if s in seen:
            raise InvalidInputError(
                f"candidates[{i}]: duplicate candidate "
                f"{sorted((a for a in attrs if a in s))}"
            )
        seen.add(s)
        parts.append(s)
    return tuple(parts)


def _validate_required_groups(value, attrs: tuple[str, ...]) -> tuple[frozenset, ...]:
    if isinstance(value, (str, bytes)):
        raise InvalidInputError(
            "required_groups: expected an iterable of attribute sets, "
            "not a single string"
        )
    try:
        items = list(value)
    except TypeError:
        raise InvalidInputError(
            "required_groups: expected an iterable of attribute sets, "
            f"got {type(value).__name__}"
        ) from None
    known = frozenset(attrs)
    groups: list[frozenset] = []
    seen: set[frozenset] = set()
    for i, group in enumerate(items):
        s = _validate_name_set(group, f"required_groups[{i}]", known)
        if not s:
            raise InvalidInputError(
                f"required_groups[{i}]: required group must be non-empty"
            )
        if s in seen:
            raise InvalidInputError(
                f"required_groups[{i}]: duplicate required group "
                f"{sorted((a for a in attrs if a in s))}"
            )
        seen.add(s)
        groups.append(s)
    return tuple(groups)


def _validate_costs(value, n: int) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)):
        raise InvalidInputError(
            "costs: expected an iterable of non-negative ints, "
            "not a single string"
        )
    try:
        items = list(value)
    except TypeError:
        raise InvalidInputError(
            f"costs: expected an iterable of non-negative ints, "
            f"got {type(value).__name__}"
        ) from None
    if len(items) != n:
        raise InvalidInputError(
            f"costs: expected {n} entr{'y' if n == 1 else 'ies'} matching "
            f"the {n} candidate{'s' if n != 1 else ''}, got {len(items)}"
        )
    costs: list[int] = []
    for i, c in enumerate(items):
        if isinstance(c, bool):
            raise InvalidInputError(
                f"costs[{i}]: expected int, got bool ({c!r})"
            )
        if isinstance(c, float):
            if math.isnan(c) or math.isinf(c):
                raise InvalidInputError(
                    f"costs[{i}]: NaN/Infinity is not a valid cost"
                )
            raise InvalidInputError(
                f"costs[{i}]: expected int, got float ({c!r})"
            )
        if not isinstance(c, int):
            raise InvalidInputError(
                f"costs[{i}]: costs must be non-negative ints, "
                f"got {type(c).__name__} ({c!r})"
            )
        if c < 0:
            raise InvalidInputError(
                f"costs[{i}]: costs must be non-negative, got {c}"
            )
        costs.append(c)
    return tuple(costs)


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------

def _scheme_lex_key(parts) -> tuple[tuple[str, ...], ...]:
    """Canonical lexicographic key of a set of subrelations.

    Each subrelation becomes a tuple of attribute names in ascending name
    order; the tuples are sorted so the key is independent of input order.
    """
    return tuple(sorted(tuple(sorted(part)) for part in parts))


def _infeasibility_reason(
    attrs: tuple[str, ...],
    groups: tuple[frozenset, ...],
    cand_sets: tuple[frozenset, ...],
    n_covering: int,
    n_group_ok: int,
    n_lossless_only: int,
    n_preserving_only: int,
    n_neither: int,
) -> str:
    if n_covering == 0:
        return (
            "no subset of the candidate subrelations covers every attribute "
            "of the original relation"
        )
    if n_group_ok == 0:
        unsat = [
            tuple(a for a in attrs if a in g)
            for g in groups
            if not any(g <= p for p in cand_sets)
        ]
        detail = ""
        if unsat:
            detail = f"; no candidate contains the group(s) {unsat}"
        return (
            "no attribute-covering subset keeps every required attribute "
            "group inside one chosen subrelation" + detail
        )
    segments = []
    if n_lossless_only:
        segments.append(
            f"lossless but not dependency-preserving: {n_lossless_only}"
        )
    if n_preserving_only:
        segments.append(
            f"dependency-preserving but lossy: {n_preserving_only}"
        )
    if n_neither:
        segments.append(f"both lossy and not dependency-preserving: {n_neither}")
    return (
        f"all {n_group_ok} attribute-covering subset(s) that satisfy the "
        "required groups fail the losslessness/dependency-preservation "
        f"requirement ({'; '.join(segments)})"
    )


def select_decomposition(
    attributes,
    FDs,
    candidates,
    required_groups,
    costs,
    *,
    step_budget=DEFAULT_STEP_BUDGET,
):
    """Select the cheapest lossless, dependency-preserving decomposition.

    Parameters
    ----------
    attributes : iterable of str
        Attribute names of the original relation (1..6, unique, non-empty).
    FDs : iterable of (iterable of str, iterable of str)
        Functional dependencies, as accepted by :func:`analyze`.
    candidates : iterable of iterable of str
        Candidate subrelations: 1..8 non-empty, distinct sets of known
        attribute names.  Their union does not need to cover every attribute
        upfront (failure to cover is reported as an infeasible instance, not
        an invalid-input error).
    required_groups : iterable of iterable of str
        Attribute groups that must each be contained in one chosen
        subrelation; may be empty.
    costs : iterable of int
        Non-negative integer costs, one per candidate in input order.
    step_budget : int, keyword-only
        Per-subset chase merge budget (see :class:`SearchBudgetExhausted`).

    Returns
    -------
    SelectionResult
        Feasible optimum plus the full :class:`AnalysisResult` witness, or a
        classified ``feasible=False`` outcome.
    """
    # --- validate everything first; on error no state is produced ----------
    attrs = _validate_attributes(attributes)
    if len(attrs) > SELECT_MAX_ATTRIBUTES:
        raise InvalidInputError(
            f"attributes: {len(attrs)} attributes given, at most "
            f"{SELECT_MAX_ATTRIBUTES} allowed for select_decomposition"
        )
    fds = _validate_fds(FDs, attrs)
    cand_sets = _validate_candidates(candidates, attrs)
    groups = _validate_required_groups(required_groups, attrs)
    cost_list = _validate_costs(costs, len(cand_sets))
    budget = _validate_budget(step_budget)

    m = len(cand_sets)
    universe = frozenset(attrs)

    # Projections depend only on the candidate, not on the enclosing subset.
    proj_cache = [_project(attrs, fds, part) for part in cand_sets]

    n_covering = 0
    n_group_ok = 0
    n_lossless_only = 0
    n_preserving_only = 0
    n_neither = 0

    best_key = None       # (cost, relation count, lexicographic scheme key)
    best_mask = 0

    # Enumerate every non-empty subset of the candidates.
    for mask in range(1, 1 << m):
        idxs = tuple(i for i in range(m) if (mask >> i) & 1)
        union = frozenset().union(*(cand_sets[i] for i in idxs))
        if union != universe:
            continue
        n_covering += 1

        parts = tuple(cand_sets[i] for i in idxs)
        if any(all(not (g <= p) for p in parts) for g in groups):
            continue
        n_group_ok += 1

        preserving = _is_dependency_preserving(
            fds, [proj_cache[i] for i in idxs]
        )
        lossless, _table, _log = _chase(attrs, fds, parts, budget)
        if not (lossless and preserving):
            if lossless:
                n_lossless_only += 1
            elif preserving:
                n_preserving_only += 1
            else:
                n_neither += 1
            continue

        total = sum(cost_list[i] for i in idxs)
        key = (total, len(idxs), _scheme_lex_key(parts))
        if best_key is None or key < best_key:
            best_key = key
            best_mask = mask

    candidate_out = tuple(
        tuple(a for a in attrs if a in part) for part in cand_sets
    )
    group_out = tuple(
        sorted(tuple(a for a in attrs if a in g) for g in groups)
    )
    fd_out = tuple(
        sorted(
            (_fd_canon(attrs, p) for p in fds),
            key=lambda f: (f.lhs, f.rhs),
        )
    )

    if best_key is None:
        reason = _infeasibility_reason(
            attrs,
            groups,
            cand_sets,
            n_covering,
            n_group_ok,
            n_lossless_only,
            n_preserving_only,
            n_neither,
        )
        return SelectionResult(
            attributes=attrs,
            fds=fd_out,
            candidates=candidate_out,
            required_groups=group_out,
            costs=cost_list,
            feasible=False,
            chosen=(),
            total_cost=None,
            relation_count=None,
            reason=reason,
            analysis=None,
            subsets_considered=(1 << m) - 1,
        )

    idxs = tuple(i for i in range(m) if (best_mask >> i) & 1)
    # Canonicalize the chosen decomposition into ascending attribute order
    # (the same order used by the tie-break key) before running analyze.
    chosen_sets = sorted(
        (cand_sets[i] for i in idxs),
        key=lambda p: tuple(sorted(p)),
    )
    total = sum(cost_list[i] for i in idxs)
    analysis = analyze(
        attrs,
        [(set(lhs), set(rhs)) for lhs, rhs in fds],
        [set(p) for p in chosen_sets],
        step_budget=budget,
    )

    return SelectionResult(
        attributes=attrs,
        fds=fd_out,
        candidates=candidate_out,
        required_groups=group_out,
        costs=cost_list,
        feasible=True,
        chosen=analysis.decomposition,
        total_cost=total,
        relation_count=len(idxs),
        reason=None,
        analysis=analysis,
        subsets_considered=(1 << m) - 1,
    )


def _fd_canon(attrs: tuple[str, ...], fd_pair) -> FD:
    lhs, rhs = fd_pair
    (a,) = rhs
    return FD(lhs=tuple(x for x in attrs if x in lhs), rhs=a)
