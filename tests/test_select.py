"""Unit tests for select_decomposition (cost-optimal decomposition choice).

Expected values in the hand-computed cases are derived on paper from the
definitions (coverage, chase losslessness, projection-based dependency
preservation), not from the implementation under test.  The cross-check
test re-enumerates every candidate subset in the test itself and uses the
already-independently-validated ``analyze`` as the feasibility oracle.
"""

import unittest

from fdlossless import (
    InvalidInputError,
    NoFeasibleDecomposition,
    analyze,
    select_decomposition,
)


def reference_select(attrs, fds, candidates, must_keep, costs):
    """Independent in-test enumeration; ``analyze`` is the feasibility oracle.

    Returns the winning (cost, count, canonical) key or None.
    """
    best = None
    m = len(candidates)
    for mask in range(1, 1 << m):
        idx = [i for i in range(m) if (mask >> i) & 1]
        sel = [candidates[i] for i in idx]
        if frozenset().union(*map(frozenset, sel)) != frozenset(attrs):
            continue
        if not all(any(set(g) <= set(s) for s in sel) for g in must_keep):
            continue
        r = analyze(attrs, fds, sel)
        if not (r.lossless and r.dependency_preserving):
            continue
        canon = tuple(sorted(tuple(sorted(s)) for s in sel))
        key = (sum(costs[i] for i in idx), len(idx), canon)
        if best is None or key < best:
            best = key
    return best


class HandComputedSelectionTests(unittest.TestCase):
    def test_textbook_chain_picks_two_relations(self):
        # R(A,B,C), F = {A->B, B->C}; candidates AB, BC, AC (cost 1), ABC (5).
        # {AB,BC}: lossless (B -> BC) and preserves both FDs, cost 2.
        # {AB,AC}: lossless but loses B->C.  {AC,BC}: lossy.  {ABC}: cost 5.
        r = select_decomposition(
            ["A", "B", "C"],
            [({"A"}, {"B"}), ({"B"}, {"C"})],
            [{"A", "B"}, {"B", "C"}, {"A", "C"}, {"A", "B", "C"}],
            [],
            [1, 1, 1, 5],
        )
        self.assertEqual(r.chosen, (("A", "B"), ("B", "C")))
        self.assertEqual(r.chosen_indices, (0, 1))
        self.assertEqual(r.total_cost, 2)
        self.assertTrue(r.analysis.lossless)
        self.assertTrue(r.analysis.dependency_preserving)

    def test_tie_on_cost_broken_by_relation_count(self):
        # {ABC} (cost 4, 1 relation) beats {AB,AC} (cost 4, 2 relations).
        r = select_decomposition(
            ["A", "B", "C"],
            [({"A"}, {"B"})],
            [{"A", "B"}, {"A", "C"}, {"A", "B", "C"}],
            [],
            [2, 2, 4],
        )
        self.assertEqual(r.chosen, (("A", "B", "C"),))
        self.assertEqual(r.total_cost, 4)

    def test_tie_on_cost_and_count_broken_lexicographically(self):
        # F = {A->B, B->A}: both {AB,AC} and {AB,BC} are lossless and
        # preserving with cost 2 and 2 relations; {AC,BC} is lossy.
        # Canonical order: ((A,B),(A,C)) < ((A,B),(B,C)).
        r = select_decomposition(
            ["A", "B", "C"],
            [({"A"}, {"B"}), ({"B"}, {"A"})],
            [{"A", "B"}, {"A", "C"}, {"B", "C"}],
            [],
            [1, 1, 1],
        )
        self.assertEqual(r.chosen, (("A", "B"), ("A", "C")))
        self.assertEqual(r.total_cost, 2)

    def test_must_keep_forces_extra_relation(self):
        # F = {A->B, A->C}.  Without must_keep the optimum is {AB,AC}
        # (cost 2).  Requiring {B,C} to stay together forces BC in, and
        # {AB,BC} / {AC,BC} are both lossy, so all three are needed.
        base = (["A", "B", "C"], [({"A"}, {"B"}), ({"A"}, {"C"})],
                [{"A", "B"}, {"A", "C"}, {"B", "C"}], [1, 1, 1])
        attrs, fds, candidates, costs = base
        r_plain = select_decomposition(attrs, fds, candidates, [], costs)
        self.assertEqual(r_plain.chosen, (("A", "B"), ("A", "C")))
        r_kept = select_decomposition(
            attrs, fds, candidates, [{"B", "C"}], costs
        )
        self.assertEqual(
            r_kept.chosen, (("A", "B"), ("A", "C"), ("B", "C"))
        )
        self.assertEqual(r_kept.total_cost, 3)

    def test_zero_costs_are_valid(self):
        r = select_decomposition(
            ["A", "B"], [], [{"A", "B"}], [], [0]
        )
        self.assertEqual(r.total_cost, 0)
        self.assertEqual(r.chosen, (("A", "B"),))

    def test_result_is_deterministic_value_object(self):
        args = (
            ["A", "B", "C"],
            [({"A"}, {"B"}), ({"B"}, {"C"})],
            [{"A", "B"}, {"B", "C"}, {"A", "C"}],
            [],
            [1, 1, 1],
        )
        self.assertEqual(select_decomposition(*args), select_decomposition(*args))


class NoFeasibleTests(unittest.TestCase):
    def test_attribute_in_no_candidate(self):
        with self.assertRaisesRegex(NoFeasibleDecomposition, "'B'"):
            select_decomposition(["A", "B"], [], [{"A"}], [], [0])

    def test_required_group_in_no_candidate(self):
        with self.assertRaisesRegex(NoFeasibleDecomposition, r"\{B, C\}"):
            select_decomposition(
                ["A", "B", "C"],
                [],
                [{"A", "B"}, {"A", "C"}],
                [{"B", "C"}],
                [1, 1],
            )

    def test_covering_selection_all_lossy(self):
        # F = {B->C}; the only covering selection {AB,AC} is lossy.
        with self.assertRaisesRegex(
            NoFeasibleDecomposition, r"1 lossy, 0 lossless"
        ):
            select_decomposition(
                ["A", "B", "C"],
                [({"B"}, {"C"})],
                [{"A", "B"}, {"A", "C"}],
                [],
                [1, 1],
            )

    def test_covering_selection_not_dependency_preserving(self):
        # F = {A->B, B->C}; {AB,AC} is lossless but loses B->C.
        with self.assertRaisesRegex(
            NoFeasibleDecomposition, r"0 lossy, 1 lossless"
        ):
            select_decomposition(
                ["A", "B", "C"],
                [({"A"}, {"B"}), ({"B"}, {"C"})],
                [{"A", "B"}, {"A", "C"}],
                [],
                [1, 1],
            )

    def test_no_feasible_is_not_invalid_input(self):
        # A no-solution verdict must not be confused with a contract breach.
        try:
            select_decomposition(["A", "B"], [], [{"A"}], [], [0])
        except NoFeasibleDecomposition as exc:
            self.assertNotIsInstance(exc, InvalidInputError)
        else:  # pragma: no cover - defensive
            self.fail("expected NoFeasibleDecomposition")


class SelectionValidationTests(unittest.TestCase):
    def test_seven_attributes_rejected(self):
        attrs = [f"A{i}" for i in range(7)]
        with self.assertRaisesRegex(InvalidInputError, "at most 6"):
            select_decomposition(attrs, [], [attrs], [], [0])

    def test_nine_candidates_rejected(self):
        cands = [{f"A{i}"} for i in range(6)] + [{"A0", "A1"}, {"A1", "A2"},
                                                 {"A2", "A3"}]
        with self.assertRaisesRegex(InvalidInputError, "at most 8"):
            select_decomposition(
                [f"A{i}" for i in range(6)], [], cands, [], [0] * 9
            )

    def test_empty_candidates_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, "candidates"):
            select_decomposition(["A"], [], [], [], [])

    def test_empty_candidate_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, r"candidates\[0\]"):
            select_decomposition(["A"], [], [set()], [], [0])

    def test_duplicate_candidates_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, "duplicate"):
            select_decomposition(
                ["A"], [], [{"A"}, {"A"}], [], [0, 0]
            )

    def test_unknown_attribute_in_candidate_located(self):
        with self.assertRaisesRegex(InvalidInputError, r"candidates\[1\]\[0\]"):
            select_decomposition(["A"], [], [{"A"}, {"Z"}], [], [0, 0])

    def test_costs_length_mismatch(self):
        with self.assertRaisesRegex(InvalidInputError, "costs"):
            select_decomposition(["A"], [], [{"A"}], [], [0, 0])

    def test_negative_cost_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, r"costs\[0\]"):
            select_decomposition(["A"], [], [{"A"}], [], [-1])

    def test_bool_cost_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, r"costs\[0\].*bool"):
            select_decomposition(["A"], [], [{"A"}], [], [True])

    def test_float_cost_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, r"costs\[0\]"):
            select_decomposition(["A"], [], [{"A"}], [], [1.5])

    def test_nan_cost_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, r"costs\[0\]"):
            select_decomposition(["A"], [], [{"A"}], [], [float("nan")])

    def test_empty_must_keep_group_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, r"must_keep\[0\]"):
            select_decomposition(["A"], [], [{"A"}], [set()], [0])

    def test_unknown_attribute_in_must_keep_located(self):
        with self.assertRaisesRegex(InvalidInputError, r"must_keep\[0\]\[0\]"):
            select_decomposition(["A"], [], [{"A"}], [{"Z"}], [0])

    def test_duplicate_must_keep_group_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, "duplicate"):
            select_decomposition(
                ["A"], [], [{"A"}], [{"A"}, {"A"}], [0]
            )

    def test_bool_budget_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, "step_budget"):
            select_decomposition(
                ["A"], [], [{"A"}], [], [0], step_budget=True
            )


class IndependentEnumerationCrossCheck(unittest.TestCase):
    """select_decomposition must agree with an in-test brute-force pass."""

    SCENARIOS = [
        # The six-attribute chain demo scenario (8 candidates).
        (
            ["A", "B", "C", "D", "E", "F"],
            [({"A"}, {"B"}), ({"B"}, {"C"}), ({"C"}, {"D"}),
             ({"D"}, {"E"}), ({"E"}, {"F"})],
            [{"A", "B"}, {"B", "C"}, {"C", "D"}, {"D", "E"},
             {"E", "F"}, {"A", "B", "C"}, {"D", "E", "F"}, {"A", "F"}],
            [{"E", "F"}],
            [1, 1, 1, 1, 1, 3, 3, 2],
        ),
        # Four attributes, mixed costs, one required group.
        (
            ["A", "B", "C", "D"],
            [({"A", "B"}, {"C"}), ({"C"}, {"D"}), ({"D"}, {"A"})],
            [{"A", "B"}, {"B", "C"}, {"C", "D"}, {"A", "D"},
             {"A", "B", "C"}, {"B", "C", "D"}],
            [{"A", "D"}],
            [2, 2, 2, 1, 4, 4],
        ),
        # No feasible selection (nothing keeps {B,D} and stays lossless).
        (
            ["A", "B", "C", "D"],
            [({"A"}, {"B"}), ({"B"}, {"C"})],
            [{"A", "B"}, {"B", "C"}, {"A", "D"}, {"C", "D"}],
            [{"B", "D"}],
            [1, 1, 1, 1],
        ),
    ]

    def test_matches_independent_enumeration(self):
        for attrs, fds, candidates, must_keep, costs in self.SCENARIOS:
            with self.subTest(attrs=attrs, candidates=candidates):
                expected = reference_select(
                    attrs, fds, candidates, must_keep, costs
                )
                if expected is None:
                    with self.assertRaises(NoFeasibleDecomposition):
                        select_decomposition(
                            attrs, fds, candidates, must_keep, costs
                        )
                else:
                    r = select_decomposition(
                        attrs, fds, candidates, must_keep, costs
                    )
                    self.assertEqual(r.total_cost, expected[0])
                    self.assertEqual(len(r.chosen), expected[1])
                    self.assertEqual(r.chosen, expected[2])

    def test_analysis_recheck_confirms_verdicts(self):
        for attrs, fds, candidates, must_keep, costs in self.SCENARIOS[:2]:
            r = select_decomposition(attrs, fds, candidates, must_keep, costs)
            self.assertTrue(r.analysis.lossless)
            self.assertTrue(r.analysis.dependency_preserving)
            self.assertEqual(
                {frozenset(s) for s in r.analysis.decomposition},
                {frozenset(s) for s in r.chosen},
            )
            # Every required group sits inside one chosen subrelation.
            for g in must_keep:
                self.assertTrue(
                    any(set(g) <= set(s) for s in r.chosen),
                    f"group {g} not kept in {r.chosen}",
                )


if __name__ == "__main__":
    unittest.main()
