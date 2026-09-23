"""Independent brute-force cross-check for select_decomposition.

The reference implementation here imports NOTHING from the package under
test except the public ``select_decomposition`` entry point.  Dimensions:

  * 8 hand-picked FD sets over R(A,B,C) (no FDs, a chain, an empty
    determinant, a cycle, ...);
  * 4 candidate pools of different shapes (the full 7 non-empty subsets,
    the pool without the whole relation, a sparse pool that forces
    infeasibility, and a pool that cannot even cover all attributes);
  * 3 required-group configurations;
  * 4 deterministic cost plans per pool.

For each (FD set, pool) pair the reference independently classifies every
candidate subset once:

  * coverage and required-group satisfaction by direct set arithmetic;
  * losslessness by enumerating EVERY relation over the binary domain
    {0,1} (256 of them) and testing
    r == pi_P1(r) |x| ... |x| pi_Pk(r) for every r satisfying the FDs;
  * dependency preservation with an independently written closure over
    independently enumerated projected FDs.

Then, per (groups, costs), it derives the optimum with the documented
ordering (total cost, relation count, lexicographic scheme key) and
compares it against ``select_decomposition``; an infeasible verdict must
also carry the independently computed category counts in its reason.
"""

import unittest
from itertools import combinations, product

from fdlossless import select_decomposition

ATTRS = ("A", "B", "C")
N = 3
ALL_TUPLES = list(product((0, 1), repeat=N))  # all N-tuples over {0,1}
UNIVERSE = frozenset(range(N))


def pool_of(letters_sets):
    return tuple(
        frozenset(ATTRS.index(a) for a in letters)
        for letters in letters_sets
    )


POOL_SCENARIOS = {
    "full": pool_of(["A", "B", "C", "AB", "AC", "BC", "ABC"]),
    "without universe": pool_of(["A", "B", "C", "AB", "AC", "BC"]),
    "sparse": pool_of(["A", "B", "C", "BC"]),
    "cannot cover C": pool_of(["A", "B", "AB"]),
}


# ---------------------------------------------------------------------------
# Independent reference: closure, preservation, brute-force losslessness
# ---------------------------------------------------------------------------

def ref_closure(fds, seed):
    """fds: iterable of (frozenset[lhs cols], frozenset[rhs cols])."""
    result = set(seed)
    changed = True
    while changed:
        changed = False
        for lhs, rhs in fds:
            if lhs <= result and not rhs <= result:
                result |= rhs
                changed = True
    return frozenset(result)


def ref_projected_fds(fds, part):
    """All implied singleton FDs X -> {a} with X, a inside ``part``.

    Enumerated independently of the package (non-minimal on purpose:
    redundancy is harmless for a closure-based implication test).
    """
    out = []
    cols = sorted(part)
    for size in range(len(cols) + 1):
        for combo in combinations(cols, size):
            x = frozenset(combo)
            implied = ref_closure(fds, x)
            for a in cols:
                if a in implied:
                    out.append((x, frozenset({a})))
    return out


def ref_preserving(fds, parts):
    projected = [fd for part in parts for fd in ref_projected_fds(fds, part)]
    return all(rhs <= ref_closure(projected, lhs) for lhs, rhs in fds)


def ref_satisfies(relation, fds):
    for lhs, rhs in fds:
        lhs_cols = sorted(lhs)
        rhs_col = next(iter(rhs))
        for t1 in relation:
            for t2 in relation:
                if (all(t1[c] == t2[c] for c in lhs_cols)
                        and t1[rhs_col] != t2[rhs_col]):
                    return False
    return True


def ref_restrict(t, part):
    return tuple(t[c] for c in sorted(part))


def ref_join_of_projections(relation, parts):
    projections = [
        frozenset(ref_restrict(t, s) for t in relation) for s in parts
    ]
    return frozenset(
        t for t in ALL_TUPLES
        if all(ref_restrict(t, s) in proj
               for s, proj in zip(parts, projections))
    )


def ref_lossless(fds, parts):
    """Lossless iff no FD-satisfying relation gains a spurious tuple."""
    for r_mask in range(1 << len(ALL_TUPLES)):
        relation = frozenset(
            t for i, t in enumerate(ALL_TUPLES) if (r_mask >> i) & 1
        )
        if ref_satisfies(relation, fds):
            if ref_join_of_projections(relation, parts) != relation:
                return False
    return True


def ref_lex_key(parts):
    return tuple(
        sorted(tuple(sorted(ATTRS[c] for c in p)) for p in parts)
    )


# ---------------------------------------------------------------------------
# Scenario inputs
# ---------------------------------------------------------------------------

FD_SCENARIOS = {
    "no fds": (),
    "single A->B": (("A", "B"),),
    "chain A->B,B->C": (("A", "B"), ("B", "C")),
    "A->C only": (("A", "C"),),
    "empty determinant": (("", "A"),),
    "AB->C,C->A": (("AB", "C"), ("C", "A")),
    "cycle": (("A", "B"), ("B", "C"), ("C", "A")),
    "A key for rest": (("A", "B"), ("A", "C")),
}

GROUP_SCENARIOS = {
    "none": (),
    "AB together": ("AB",),
    "ABC together": ("ABC",),
}

COST_PLANS = {
    "all zero": lambda m: [0] * m,
    "deterministic mix": lambda m: [(3 * i + 1) % 7 for i in range(m)],
    "first cheap": lambda m: [0] + [9] * (m - 1),
    "last cheap": lambda m: [9] * (m - 1) + [0],
}


def fd_inputs(fd_spec):
    out_cols = []
    out_names = []
    for lhs_letters, rhs_letter in fd_spec:
        lhs_cols = frozenset(ATTRS.index(a) for a in lhs_letters)
        rhs_col = frozenset({ATTRS.index(rhs_letter)})
        out_cols.append((lhs_cols, rhs_col))
        out_names.append((set(lhs_letters), {rhs_letter}))
    return tuple(out_cols), out_names


def group_inputs(group_spec):
    return tuple(frozenset(ATTRS.index(a) for a in letters)
                 for letters in group_spec)


# ---------------------------------------------------------------------------
# Per (pool, FD set): classify every subset once.
# ---------------------------------------------------------------------------

def classify_subsets(pool, fds_cols):
    """mask -> ('uncovered' | 'group-independent' record).

    Returns {mask: dict(covering, lossless, preserving)} for cheap reuse
    across group configurations and cost plans.
    """
    info = {}
    for mask in range(1, 1 << len(pool)):
        idxs = tuple(i for i in range(len(pool)) if (mask >> i) & 1)
        parts = tuple(pool[i] for i in idxs)
        covering = frozenset().union(*parts) == UNIVERSE
        if covering:
            info[mask] = dict(
                covering=True,
                lossless=ref_lossless(fds_cols, parts),
                preserving=ref_preserving(fds_cols, parts),
            )
        else:
            info[mask] = dict(
                covering=False, lossless=False, preserving=False
            )
    return info


def reference_optimum(info, pool, groups, costs):
    n_covering = n_group_ok = 0
    n_lossless_only = n_preserving_only = n_neither = 0
    best = None

    for mask, rec in info.items():
        if not rec["covering"]:
            continue
        n_covering += 1
        idxs = tuple(i for i in range(len(pool)) if (mask >> i) & 1)
        parts = tuple(pool[i] for i in idxs)
        if any(all(not (g <= p) for p in parts) for g in groups):
            continue
        n_group_ok += 1
        lossless, preserving = rec["lossless"], rec["preserving"]
        if not (lossless and preserving):
            if lossless:
                n_lossless_only += 1
            elif preserving:
                n_preserving_only += 1
            else:
                n_neither += 1
            continue
        total = sum(costs[i] for i in idxs)
        key = (total, len(parts), ref_lex_key(parts))
        if best is None or key < best[0]:
            best = (key, parts, total)

    counters = dict(
        covering=n_covering,
        group_ok=n_group_ok,
        lossless_only=n_lossless_only,
        preserving_only=n_preserving_only,
        neither=n_neither,
    )
    if best is None:
        return False, (), None, counters
    _, parts, total = best
    chosen = tuple(
        tuple(sorted(ATTRS[c] for c in p))
        for p in sorted(parts, key=lambda p: tuple(sorted(p)))
    )
    return True, chosen, total, counters


class SelectionExhaustiveCrossCheck(unittest.TestCase):
    def test_selector_matches_independent_enumeration(self):
        calls = feasible_hits = infeasible_hits = 0
        category_hits = {"covering": 0, "group": 0,
                         "lossless_only": 0, "preserving_only": 0,
                         "neither": 0}
        for fd_name, fd_spec in FD_SCENARIOS.items():
            fds_cols, fd_name_input = fd_inputs(fd_spec)
            for pool_name, pool in POOL_SCENARIOS.items():
                info = classify_subsets(pool, fds_cols)
                pool_name_input = [
                    {ATTRS[c] for c in part} for part in pool
                ]
                pool_sets = {frozenset(part) for part in pool}
                for g_name, g_spec in GROUP_SCENARIOS.items():
                    groups = group_inputs(g_spec)
                    group_name_input = [set(s) for s in g_spec]
                    for plan_name, plan in COST_PLANS.items():
                        costs = plan(len(pool))
                        with self.subTest(
                            fd=fd_name, pool=pool_name,
                            groups=g_name, costs=plan_name
                        ):
                            feasible, chosen, total, counters = (
                                reference_optimum(
                                    info, pool, groups, costs
                                )
                            )
                            r = select_decomposition(
                                ATTRS,
                                fd_name_input,
                                pool_name_input,
                                group_name_input,
                                costs,
                            )
                            calls += 1
                            self.assertEqual(r.feasible, feasible)
                            self.assertEqual(
                                r.subsets_considered,
                                (1 << len(pool)) - 1,
                            )
                            if feasible:
                                feasible_hits += 1
                                self.assertEqual(r.chosen, chosen)
                                self.assertEqual(r.total_cost, total)
                                self.assertEqual(
                                    r.relation_count, len(chosen)
                                )
                                self.assertIsNone(r.reason)
                                self.assertTrue(r.analysis.lossless)
                                self.assertTrue(
                                    r.analysis.dependency_preserving
                                )
                                for part in r.chosen:
                                    self.assertIn(
                                        frozenset(
                                            ATTRS.index(a) for a in part
                                        ),
                                        pool_sets,
                                    )
                                col_index = {
                                    frozenset(p): i
                                    for i, p in enumerate(pool)
                                }
                                recomputed = sum(
                                    costs[col_index[frozenset(
                                        ATTRS.index(a) for a in part
                                    )]]
                                    for part in r.chosen
                                )
                                self.assertEqual(recomputed, total)
                            else:
                                infeasible_hits += 1
                                self.assertEqual(r.chosen, ())
                                self.assertIsNone(r.total_cost)
                                self.assertIsNone(r.analysis)
                                self._check_reason(
                                    r.reason, counters, category_hits
                                )
        self.assertEqual(calls, 8 * 4 * 3 * 4)
        self.assertGreater(feasible_hits, 50)
        self.assertGreater(infeasible_hits, 50)
        # Every infeasibility category must really occur in the sample.
        for key, count in category_hits.items():
            self.assertGreater(
                count, 0, f"category {key} never exercised"
            )

    def _check_reason(self, reason, counters, category_hits):
        if counters["covering"] == 0:
            category_hits["covering"] += 1
            self.assertIn("covers every attribute", reason)
            return
        if counters["group_ok"] == 0:
            category_hits["group"] += 1
            self.assertIn("required attribute group", reason)
            return
        self.assertIn(
            f"all {counters['group_ok']} attribute-covering", reason
        )
        seen_any = False
        if counters["lossless_only"]:
            seen_any = True
            category_hits["lossless_only"] += 1
            self.assertIn(
                f"lossless but not dependency-preserving: "
                f"{counters['lossless_only']}",
                reason,
            )
        if counters["preserving_only"]:
            seen_any = True
            category_hits["preserving_only"] += 1
            self.assertIn(
                f"dependency-preserving but lossy: "
                f"{counters['preserving_only']}",
                reason,
            )
        if counters["neither"]:
            seen_any = True
            category_hits["neither"] += 1
            self.assertIn(
                f"both lossy and not dependency-preserving: "
                f"{counters['neither']}",
                reason,
            )
        self.assertTrue(seen_any)

    def test_reference_sanity_both_outcomes_occur(self):
        # Guard against a vacuously-passing reference: with the
        # "without universe" pool under the chain FD set, feasible and
        # infeasible covering subsets must both exist in good number.
        fds_cols, _ = fd_inputs((("A", "B"), ("B", "C")))
        pool = POOL_SCENARIOS["without universe"]
        feasible_subsets = infeasible_subsets = 0
        for mask in range(1, 1 << len(pool)):
            idxs = tuple(i for i in range(len(pool)) if (mask >> i) & 1)
            parts = tuple(pool[i] for i in idxs)
            if frozenset().union(*parts) != UNIVERSE:
                continue
            if ref_lossless(fds_cols, parts) and ref_preserving(
                fds_cols, parts
            ):
                feasible_subsets += 1
            else:
                infeasible_subsets += 1
        self.assertGreater(feasible_subsets, 3)
        self.assertGreater(infeasible_subsets, 3)


if __name__ == "__main__":
    unittest.main()
