"""Unit tests for select_decomposition (hand-computed expected values).

Every expected decomposition here is derived on paper, never produced by
the implementation under test.  Independent brute-force cross-checks live
in test_selection_exhaustive.py.
"""

import math
import unittest

from fdlossless import (
    InvalidInputError,
    SearchBudgetExhausted,
    select_decomposition,
)


def names(parts):
    return [set(p) for p in parts]


class FeasibleSelectionTests(unittest.TestCase):
    def test_cheapest_subset_hand_computed(self):
        # R(A,B,C,D), F = {A->B, B->C}.
        # Candidates: AB(1), BC(1), ACD(10), AD(1).
        # Hand analysis of covering subsets:
        #   {AB,BC,AD} cost 3: A->B on AB, B->C on BC; chase yields an
        #     all-distinguished row (A agrees AB/AD -> B; B agrees all -> C).
        #   {AB,ACD} cost 11: lossless (A common) but B->C is lost.
        #   {BC,ACD} cost 11: lossy (C common, no FD out of C), A->B lost.
        # => optimum {AB, AD, BC} at cost 3.
        r = select_decomposition(
            ["A", "B", "C", "D"],
            [({"A"}, {"B"}), ({"B"}, {"C"})],
            names([("A", "B"), ("B", "C"), ("A", "C", "D"), ("A", "D")]),
            [],
            [1, 1, 10, 1],
        )
        self.assertTrue(r.feasible)
        self.assertEqual(
            r.chosen, (("A", "B"), ("A", "D"), ("B", "C"))
        )
        self.assertEqual(r.total_cost, 3)
        self.assertEqual(r.relation_count, 3)
        self.assertEqual(r.subsets_considered, 15)
        self.assertIsNone(r.reason)
        # The witness analysis is a real analyze() result for the chosen set.
        self.assertTrue(r.analysis.lossless)
        self.assertTrue(r.analysis.dependency_preserving)
        self.assertEqual(
            r.analysis.decomposition,
            (("A", "B"), ("A", "D"), ("B", "C")),
        )
        # Echoed inputs are normalized but unchanged in content.
        self.assertEqual(r.costs, (1, 1, 10, 1))
        self.assertEqual(len(r.candidates), 4)

    def test_cost_beats_relation_count(self):
        # R(A,B,C), F = {A->B, B->C}.
        # ABC alone costs 10; {AB, BC} costs 2 and is lossless (B common,
        # B->C) and preserving.  Cheaper wins even with more relations.
        r = select_decomposition(
            ["A", "B", "C"],
            [({"A"}, {"B"}), ({"B"}, {"C"})],
            names([("A", "B"), ("B", "C"), ("A", "B", "C")]),
            [],
            [1, 1, 10],
        )
        self.assertTrue(r.feasible)
        self.assertEqual(r.chosen, (("A", "B"), ("B", "C")))
        self.assertEqual(r.total_cost, 2)
        self.assertEqual(r.relation_count, 2)

    def test_tie_cost_then_relation_count_single_relation_wins(self):
        # All costs 0: the whole schema as one candidate is feasible by
        # itself, so one relation beats two.
        r = select_decomposition(
            ["A", "B", "C"],
            [({"A"}, {"B"}), ({"B"}, {"C"})],
            names([("A", "B"), ("B", "C"), ("A", "B", "C")]),
            [],
            [0, 0, 0],
        )
        self.assertTrue(r.feasible)
        self.assertEqual(r.chosen, (("A", "B", "C"),))
        self.assertEqual(r.total_cost, 0)
        self.assertEqual(r.relation_count, 1)

    def test_tie_cost_and_count_lexicographic_schemes(self):
        # R(A,B,C,D), F = {A->B, A->C, A->D} (A is a key).
        # Two feasible 2-relation schemes at cost 0:
        #   {AB, ACD}: key parts (A,B),(A,C,D)
        #   {ABC, AD}: key parts (A,B,C),(A,D)
        # (A,B) is a strict prefix of (A,B,C), so {AB, ACD} is lexicographically
        # smaller.  3-relation schemes lose on count regardless.
        r = select_decomposition(
            ["A", "B", "C", "D"],
            [({"A"}, {"B"}), ({"A"}, {"C"}), ({"A"}, {"D"})],
            names([
                ("A", "B"),       # X
                ("A", "C", "D"),  # Y
                ("A", "B", "C"),  # U
                ("A", "D"),       # V
            ]),
            [],
            [0, 0, 0, 0],
        )
        self.assertTrue(r.feasible)
        self.assertEqual(r.chosen, (("A", "B"), ("A", "C", "D")))
        self.assertEqual(r.total_cost, 0)
        self.assertEqual(r.relation_count, 2)

    def test_required_group_forces_expensive_scheme(self):
        # R(A,B,C,D), F = {A->B, B->C}.
        # Without the constraint, cheap {ABC, AD} cost 2 is lossless and
        # preserving.  Required group {C,D} fits in no candidate but CD,
        # so CD (cost 100) is mandatory; {ABC, AD, CD} is the feasible
        # optimum at cost 102 (chase: ABC/AD already force an
        # all-distinguished row; CD adds a row).
        r = select_decomposition(
            ["A", "B", "C", "D"],
            [({"A"}, {"B"}), ({"B"}, {"C"})],
            names([
                ("A", "B", "C"),  # X cost 1
                ("A", "D"),       # Y cost 1
                ("C", "D"),       # Z cost 100
            ]),
            [{"C", "D"}],
            [1, 1, 100],
        )
        self.assertTrue(r.feasible)
        self.assertEqual(
            r.chosen, (("A", "B", "C"), ("A", "D"), ("C", "D"))
        )
        self.assertEqual(r.total_cost, 102)
        self.assertEqual(r.relation_count, 3)
        self.assertEqual(r.required_groups, (("C", "D"),))

    def test_zero_cost_candidates_and_empty_fds(self):
        # No FDs: a decomposition is lossless iff some attribute set chain
        # ... in fact with no FDs a 2-part split is lossless iff the parts
        # intersect with no FDs? Intersection alone does not help without
        # FDs: with zero FDs the only lossless decomposition is a single
        # relation (or redundant parts).  ABCD alone at cost 0 wins.
        r = select_decomposition(
            ["A", "B", "C", "D"],
            [],
            names([
                ("A", "B"),
                ("C", "D"),
                ("A", "B", "C", "D"),
            ]),
            [],
            [0, 0, 5],
        )
        self.assertTrue(r.feasible)
        self.assertEqual(r.chosen, (("A", "B", "C", "D"),))
        self.assertEqual(r.total_cost, 5)

    def test_chosen_members_are_input_candidates(self):
        r = select_decomposition(
            ["A", "B", "C"],
            [({"A"}, {"B"})],
            names([("A", "B"), ("A", "C"), ("B", "C")]),
            [],
            [2, 3, 4],
        )
        self.assertTrue(r.feasible)
        candidate_sets = {frozenset(c) for c in r.candidates}
        for part in r.chosen:
            self.assertIn(frozenset(part), candidate_sets)


class InfeasibleTests(unittest.TestCase):
    def test_no_subset_covers_attributes(self):
        # Candidate C is missing entirely; nothing covers {A,B,C}.
        r = select_decomposition(
            ["A", "B", "C"],
            [({"A"}, {"B"})],
            names([("A",), ("B",)]),
            [],
            [1, 1],
        )
        self.assertFalse(r.feasible)
        self.assertEqual(r.chosen, ())
        self.assertIsNone(r.total_cost)
        self.assertIsNone(r.relation_count)
        self.assertIsNone(r.analysis)
        self.assertEqual(r.subsets_considered, 3)
        self.assertIn("covers every attribute", r.reason)

    def test_covering_but_all_lossy_and_nonpreserving(self):
        # R(A,B,C), F = {A->C}; candidates AB, BC, {C}.
        # Covering subsets: {AB,BC}, {AB,{C}}, {AB,BC,{C}}.
        # All three are lossy (no common determinant: AB/BC share B with no
        # FD out of B; AB/{C} are disjoint) and lose A->C (A and C never
        # share a relation).
        r = select_decomposition(
            ["A", "B", "C"],
            [({"A"}, {"C"})],
            names([("A", "B"), ("B", "C"), ("C",)]),
            [],
            [1, 1, 1],
        )
        self.assertFalse(r.feasible)
        self.assertIn("both lossy and not dependency-preserving: 3", r.reason)
        self.assertIn("3 attribute-covering", r.reason)

    def test_required_group_contained_in_no_candidate(self):
        # {A,B} must stay together, but only singleton candidates exist.
        # The covering subset {A},{B} exists, so coverage alone is fine;
        # the required-group requirement is the classified failure.
        r = select_decomposition(
            ["A", "B"],
            [],
            names([("A",), ("B",)]),
            [{"A", "B"}],
            [1, 1],
        )
        self.assertFalse(r.feasible)
        self.assertIn("required attribute group", r.reason)
        self.assertIn("('A', 'B')", r.reason)

    def test_lossless_but_not_preserving_classified_separately(self):
        # R(A,B,C), F = {A->B, B->C}; candidates AB and AC.
        # {AB, AC} covers and is lossless (A common, A->B) but loses B->C
        # (B and C share no relation).  The reason must distinguish this
        # category from a plain lossy split.
        r = select_decomposition(
            ["A", "B", "C"],
            [({"A"}, {"B"}), ({"B"}, {"C"})],
            names([("A", "B"), ("A", "C")]),
            [],
            [1, 1],
        )
        self.assertFalse(r.feasible)
        self.assertIn(
            "lossless but not dependency-preserving: 1", r.reason
        )


class ValidationTests(unittest.TestCase):
    def _kwargs(self, **over):
        base = dict(
            attributes=["A", "B"],
            FDs=[({"A"}, {"B"})],
            candidates=[{"A", "B"}],
            required_groups=[],
            costs=[1],
        )
        base.update(over)
        return base

    def test_seven_attributes_rejected(self):
        attrs = ["A", "B", "C", "D", "E", "F", "G"]
        with self.assertRaisesRegex(InvalidInputError, "at most 6"):
            select_decomposition(
                attrs, [], [set(attrs)], [], [1]
            )

    def test_nine_candidates_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, "at most 8"):
            select_decomposition(
                ["A", "B", "C", "D"],
                [],
                [{"A"}, {"B"}, {"C"}, {"D"},
                 {"A", "B"}, {"B", "C"}, {"C", "D"},
                 {"A", "D"}, {"A", "C"}],
                [],
                [1] * 9,
            )

    def test_empty_candidates_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, "at least one"):
            select_decomposition(["A"], [], [], [], [])

    def test_duplicate_candidate_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, "duplicate candidate"):
            select_decomposition(
                ["A", "B"], [], [{"A"}, {"A"}], [], [1, 1]
            )

    def test_empty_candidate_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, r"candidates\[1\]"):
            select_decomposition(
                ["A", "B"], [], [{"A"}, set()], [], [1, 1]
            )

    def test_unknown_candidate_attribute_located(self):
        with self.assertRaisesRegex(InvalidInputError, r"candidates\[0\]\[0\]"):
            select_decomposition(["A"], [], [{"Z"}], [], [1])

    def test_cost_count_mismatch(self):
        with self.assertRaisesRegex(InvalidInputError, "costs: expected 2"):
            select_decomposition(
                ["A", "B"], [], [{"A"}, {"B"}], [], [1]
            )

    def test_negative_cost_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, r"costs\[1\]"):
            select_decomposition(
                ["A", "B"], [], [{"A"}, {"B"}], [], [1, -2]
            )

    def test_float_cost_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, r"costs\[0\]"):
            select_decomposition(
                ["A", "B"], [], [{"A"}, {"B"}], [], [1.5, 1]
            )

    def test_nan_cost_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, "costs"):
            select_decomposition(
                ["A", "B"], [], [{"A"}, {"B"}], [], [math.nan, 1]
            )

    def test_bool_cost_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, r"costs\[0\]"):
            select_decomposition(
                ["A", "B"], [], [{"A"}, {"B"}], [], [True, 1]
            )

    def test_unknown_required_group_attribute(self):
        with self.assertRaisesRegex(
            InvalidInputError, r"required_groups\[0\]\[0\]"
        ):
            select_decomposition(
                ["A"], [], [{"A"}], [{"Z"}], [1]
            )

    def test_duplicate_required_group_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, "duplicate required"):
            select_decomposition(
                ["A", "B"],
                [],
                [{"A", "B"}],
                [{"A", "B"}, {"B", "A"}],
                [1],
            )

    def test_empty_required_group_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, r"required_groups\[0\]"):
            select_decomposition(
                ["A"], [], [{"A"}], [set()], [1]
            )

    def test_invalid_fd_still_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, r"FDs\[0\]\.rhs"):
            select_decomposition(
                ["A"], [({"A"}, set())], [{"A"}], [], [1]
            )

    def test_no_partial_state_on_validation_failure(self):
        # A failing call must not affect a subsequent valid call: the
        # selector is pure; assert by calling a bad then a good case with
        # the same candidate/cost shapes and checking the good result.
        with self.assertRaises(InvalidInputError):
            select_decomposition(
                ["A", "B"],
                [({"A"}, {"B"})],
                [{"A"}, {"B"}],
                [],
                [-1, 1],
            )
        r = select_decomposition(
            ["A", "B"],
            [({"A"}, {"B"})],
            [{"A"}, {"B"}, {"A", "B"}],
            [],
            [1, 1, 5],
        )
        self.assertTrue(r.feasible)
        self.assertEqual(r.chosen, (("A", "B"),))
        self.assertEqual(r.total_cost, 5)

    def test_result_is_pure_value_object(self):
        args = (
            ["A", "B", "C"],
            [({"A"}, {"B"}), ({"B"}, {"C"})],
            names([("A", "B"), ("B", "C")]),
            [],
            [1, 1],
        )
        self.assertEqual(
            select_decomposition(*args), select_decomposition(*args)
        )


class BudgetTests(unittest.TestCase):
    def test_budget_exhaustion_propagates_and_is_not_infeasible(self):
        # R(A,B,C), F = {A->B, B->C}, candidates {AB},{BC}: the only
        # covering subset needs one chase merge (rows agree on B, B->C).
        # budget 0 must raise before a verdict, never silently call it
        # infeasible.
        args = (
            ["A", "B", "C"],
            [({"A"}, {"B"}), ({"B"}, {"C"})],
            [{"A", "B"}, {"B", "C"}],
            [],
            [1, 1],
        )
        with self.assertRaises(SearchBudgetExhausted):
            select_decomposition(*args, step_budget=0)
        # Sufficient budget gives the real, feasible verdict.
        r = select_decomposition(*args, step_budget=10)
        self.assertTrue(r.feasible)
        self.assertEqual(r.chosen, (("A", "B"), ("B", "C")))


if __name__ == "__main__":
    unittest.main()
