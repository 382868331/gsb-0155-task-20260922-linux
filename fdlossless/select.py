"""Cost-optimal selection of a lossless, dependency-preserving decomposition.

Public entry point:
``select_decomposition(attributes, FDs, candidates, must_keep, costs, ...)``.

Given a relation schema, its functional dependencies, a small pool of
candidate subrelations (at most 8), attribute groups that must be kept
together inside one selected subrelation, and a non-negative integer cost
per candidate, enumerate every subset of the candidates and pick the
cheapest selection that

  1. covers every attribute of the original relation,
  2. keeps every required group inside some selected subrelation,
  3. is a lossless-join decomposition (decided by the symbolic chase), and
  4. preserves the dependencies (the union of the full projected FDs
     implies every original FD).

Ties are broken deterministically: first by total cost, then by the number
of selected subrelations, then lexicographically by the canonical
attribute tuple (each subrelation's attributes name-sorted, the
subrelations themselves sorted).  With no feasible selection a
``NoFeasibleDecomposition`` error explains which constraint class failed.

Feasibility reuses the same from-scratch core as ``analyze`` (closure,
projection, chase); the winning selection is additionally re-checked by a
full ``analyze`` call whose result is returned as ``SelectionResult.analysis``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .core import (
    DEFAULT_STEP_BUDGET,
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
from .errors import InvalidInputError, NoFeasibleDecomposition

MAX_SELECT_ATTRIBUTES = 6
MAX_CANDIDATES = 8


# ---------------------------------------------------------------------------
# Public result structure
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SelectionResult:
    """The winning decomposition and the inputs it was chosen from.

    ``chosen`` is the canonical form used by the tie-break: each
    subrelation's attributes name-sorted, subrelations sorted
    lexicographically.  ``chosen_indices`` refers to the input candidate
    order (ascending).  ``analysis`` is the full ``analyze`` re-check of
    the chosen decomposition (lossless and dependency-preserving by
    construction).
    """

    attributes: tuple[str, ...]
    candidates: tuple[tuple[str, ...], ...]  # noqa: E501  (attrs in input order)
    costs: tuple[int, ...]
    must_keep: tuple[tuple[str, ...], ...]
    chosen: tuple[tuple[str, ...], ...]
    chosen_indices: tuple[int, ...]
    total_cost: int
    analysis: AnalysisResult


# ---------------------------------------------------------------------------
# Input validation (reject, never silently correct)
# ---------------------------------------------------------------------------

def _validate_select_attributes(attributes) -> tuple[str, ...]:
    attrs = _validate_attributes(attributes)
    if len(attrs) > MAX_SELECT_ATTRIBUTES:
        raise InvalidInputError(
            f"attributes: {len(attrs)} attributes given, at most "
            f"{MAX_SELECT_ATTRIBUTES} allowed for decomposition selection"
        )
    return attrs


def _validate_candidates(candidates, attrs: tuple[str, ...]) -> tuple[frozenset, ...]:
    if isinstance(candidates, (str, bytes)):
        raise InvalidInputError(
            "candidates: expected an iterable of attribute sets, not a single string"
        )
    try:
        items = list(candidates)
    except TypeError:
        raise InvalidInputError(
            f"candidates: expected an iterable of attribute sets, "
            f"got {type(candidates).__name__}"
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
    parts = []
    seen = set()
    for i, part in enumerate(items):
        s = _validate_name_set(part, f"candidates[{i}]", known)
        if not s:
            raise InvalidInputError(
                f"candidates[{i}]: candidate subrelation must be non-empty"
            )
        if s in seen:
            raise InvalidInputError(
                f"candidates[{i}]: duplicate candidate {sorted(s)}"
            )
        seen.add(s)
        parts.append(s)
    return tuple(parts)


def _validate_must_keep(must_keep, attrs: tuple[str, ...]) -> tuple[frozenset, ...]:
    if isinstance(must_keep, (str, bytes)):
        raise InvalidInputError(
            "must_keep: expected an iterable of attribute groups, "
            "not a single string"
        )
    try:
        items = list(must_keep)
    except TypeError:
        raise InvalidInputError(
            f"must_keep: expected an iterable of attribute groups, "
            f"got {type(must_keep).__name__}"
        ) from None
    known = frozenset(attrs)
    groups = []
    seen = set()
    for i, group in enumerate(items):
        g = _validate_name_set(group, f"must_keep[{i}]", known)
        if not g:
            raise InvalidInputError(
                f"must_keep[{i}]: required attribute group must be non-empty"
            )
        if g in seen:
            raise InvalidInputError(
                f"must_keep[{i}]: duplicate required group {sorted(g)}"
            )
        seen.add(g)
        groups.append(g)
    return tuple(groups)


def _validate_costs(costs, n: int) -> tuple[int, ...]:
    if isinstance(costs, (str, bytes)):
        raise InvalidInputError(
            "costs: expected an iterable of non-negative integers, "
            "not a single string"
        )
    try:
        items = list(costs)
    except TypeError:
        raise InvalidInputError(
            f"costs: expected an iterable of non-negative integers, "
            f"got {type(costs).__name__}"
        ) from None
    if len(items) != n:
        raise InvalidInputError(
            f"costs: expected exactly {n} entr{'y' if n == 1 else 'ies'} "
            f"(one per candidate), got {len(items)}"
        )
    out = []
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
                f"costs[{i}]: expected int, got {type(c).__name__}"
            )
        if c < 0:
            raise InvalidInputError(
                f"costs[{i}]: cost must be >= 0, got {c}"
            )
        out.append(c)
    return tuple(out)


# ---------------------------------------------------------------------------
# Canonical form for the lexicographic tie-break
# ---------------------------------------------------------------------------

def _canonical(parts) -> tuple[tuple[str, ...], ...]:
    """Name-sort each subrelation, then sort the subrelations."""
    return tuple(sorted(tuple(sorted(p)) for p in parts))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def select_decomposition(
    attributes,
    FDs,
    candidates,
    must_keep,
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
        Functional dependencies, same contract as ``analyze``.
    candidates : iterable of iterable of str
        Candidate subrelations (1..8, non-empty, distinct, known
        attributes only).  The decomposition is chosen from these.
    must_keep : iterable of iterable of str
        Attribute groups that must each be contained in one selected
        subrelation.  May be empty.
    costs : iterable of int
        One non-negative integer cost per candidate, in candidate order.
    step_budget : int, keyword-only
        Chase merge budget per candidate selection, same semantics as in
        ``analyze`` (exhaustion raises ``SearchBudgetExhausted``).

    Returns
    -------
    SelectionResult
        The selection minimizing (total cost, number of subrelations,
        canonical attribute tuple), plus a full ``analyze`` re-check.

    Raises
    ------
    NoFeasibleDecomposition
        No subset of the candidates satisfies coverage, the required
        groups, losslessness, and dependency preservation; the message
        says which constraint class failed.
    """
    attrs = _validate_select_attributes(attributes)
    fds = _validate_fds(FDs, attrs)
    cands = _validate_candidates(candidates, attrs)
    groups = _validate_must_keep(must_keep, attrs)
    cost_t = _validate_costs(costs, len(cands))
    budget = _validate_budget(step_budget)

    universe = frozenset(attrs)
    m = len(cands)
    best_key = None   # (total_cost, relation_count, canonical tuple)
    best_idx = None
    n_cover = 0           # selections covering every attribute
    n_kept = 0            # ... and keeping every required group
    n_lossy = 0           # ... but lossy
    n_not_preserving = 0  # ... lossless but not dependency-preserving

    for mask in range(1, 1 << m):
        idx = tuple(i for i in range(m) if (mask >> i) & 1)
        sel = [cands[i] for i in idx]
        if frozenset().union(*sel) != universe:
            continue
        n_cover += 1
        if not all(any(g <= s for s in sel) for g in groups):
            continue
        n_kept += 1
        lossless, _, _ = _chase(attrs, fds, sel, budget)
        if not lossless:
            n_lossy += 1
            continue
        projections = tuple(_project(attrs, fds, s) for s in sel)
        if not _is_dependency_preserving(fds, projections):
            n_not_preserving += 1
            continue
        key = (
            sum(cost_t[i] for i in idx),
            len(idx),
            _canonical(sel),
        )
        if best_key is None or key < best_key:
            best_key = key
            best_idx = idx

    if best_idx is None:
        _raise_infeasible(attrs, cands, groups, n_cover, n_kept,
                          n_lossy, n_not_preserving)

    chosen_sets = [cands[i] for i in best_idx]
    analysis = analyze(
        attrs,
        [(set(lhs), set(rhs)) for lhs, rhs in fds],
        [set(s) for s in chosen_sets],
        step_budget=budget,
    )
    return SelectionResult(
        attributes=attrs,
        candidates=tuple(tuple(a for a in attrs if a in c) for c in cands),
        costs=cost_t,
        must_keep=tuple(tuple(sorted(g)) for g in groups),
        chosen=best_key[2],
        chosen_indices=best_idx,
        total_cost=best_key[0],
        analysis=analysis,
    )


def _format_group(g) -> str:
    return "{" + ", ".join(sorted(g)) + "}"


def _raise_infeasible(attrs, cands, groups, n_cover, n_kept,
                      n_lossy, n_not_preserving):
    if n_cover == 0:
        union_all = frozenset().union(*cands) if cands else frozenset()
        missing = [a for a in attrs if a not in union_all]
        raise NoFeasibleDecomposition(
            "no feasible decomposition: attribute(s) "
            + ", ".join(repr(a) for a in missing)
            + " do not appear in any candidate subrelation, so no "
            "selection can cover the original relation"
        )
    if n_kept == 0:
        unkeepable = [g for g in groups
                      if not any(g <= c for c in cands)]
        if unkeepable:
            detail = (
                "required group(s) "
                + ", ".join(_format_group(g) for g in unkeepable)
                + " are not contained in any single candidate subrelation"
            )
        else:
            detail = (
                "no covering selection keeps every required group "
                "inside one of its subrelations"
            )
        raise NoFeasibleDecomposition(
            f"no feasible decomposition: {detail}"
        )
    raise NoFeasibleDecomposition(
        f"no feasible decomposition: {n_kept} selection(s) cover the "
        f"attributes and keep the required groups, but none is both "
        f"lossless and dependency-preserving ({n_lossy} lossy, "
        f"{n_not_preserving} lossless but not dependency-preserving)"
    )
