"""Unit tests for fdlossless core algorithms and error semantics."""

import math
import unittest

from fdlossless import (
    FD,
    InvalidInputError,
    SearchBudgetExhausted,
    analyze,
)


def fd(lhs, rhs):
    return FD(lhs=tuple(lhs), rhs=rhs)


class MinimalCoverTests(unittest.TestCase):
    def test_extraneous_attribute_in_lhs(self):
        # AB -> C with A -> C: B is extraneous.
        r = analyze(["A", "B", "C"], [({"A", "B"}, {"C"}), ({"A"}, {"C"})],
                    [{"A", "B", "C"}])
        self.assertEqual(r.minimal_cover, (fd("A", "C"),))

    def test_redundant_fd_removed(self):
        # A -> C is implied by A -> B, B -> C.
        r = analyze(
            ["A", "B", "C"],
            [({"A"}, {"B"}), ({"B"}, {"C"}), ({"A"}, {"C"})],
            [{"A", "B", "C"}],
        )
        self.assertEqual(r.minimal_cover, (fd("A", "B"), fd("B", "C")))

    def test_rhs_split(self):
        r = analyze(["A", "B", "C"], [({"A"}, {"B", "C"})], [{"A", "B", "C"}])
        self.assertEqual(r.minimal_cover, (fd("A", "B"), fd("A", "C")))

    def test_cyclic_fds(self):
        # A -> B -> C -> A: cover keeps a cycle; each single attribute is a key.
        r = analyze(
            ["A", "B", "C"],
            [({"A"}, {"B"}), ({"B"}, {"C"}), ({"C"}, {"A"})],
            [{"A", "B", "C"}],
        )
        self.assertEqual(len(r.minimal_cover), 3)
        self.assertEqual(r.candidate_keys, (("A",), ("B",), ("C",)))

    def test_empty_determinant(self):
        # ∅ -> A means A is constant; A never needs to appear in a key.
        r = analyze(["A", "B"], [(set(), {"A"})], [{"A", "B"}])
        self.assertEqual(r.minimal_cover, (fd((), "A"),))
        self.assertEqual(r.candidate_keys, (("B",),))


class CandidateKeyTests(unittest.TestCase):
    def test_keys_sorted_by_size_then_lexicographically(self):
        # Keys: {D} alone is not a key; construct a schema with known keys.
        # F: AB -> C, C -> D, D -> A  over (A, B, C, D)
        # Keys: {A,B}, {B,C}, {B,D} — all size 2, lexicographic order.
        r = analyze(
            ["A", "B", "C", "D"],
            [({"A", "B"}, {"C"}), ({"C"}, {"D"}), ({"D"}, {"A"})],
            [{"A", "B", "C", "D"}],
        )
        self.assertEqual(
            r.candidate_keys, (("A", "B"), ("B", "C"), ("B", "D"))
        )

    def test_full_set_is_key_when_no_fds(self):
        r = analyze(["A", "B"], [], [{"A", "B"}])
        self.assertEqual(r.candidate_keys, (("A", "B"),))


class ProjectionTests(unittest.TestCase):
    def test_projection_is_complete_transitively(self):
        # F = {A -> B, B -> C}; projection onto {A, C} must contain A -> C.
        r = analyze(
            ["A", "B", "C"],
            [({"A"}, {"B"}), ({"B"}, {"C"})],
            [{"A", "B"}, {"A", "C"}],
        )
        proj_ac = r.projections[1]
        self.assertIn(fd("A", "C"), proj_ac)

    def test_projection_only_minimal_lhs(self):
        # A -> C already minimal; AB -> C must NOT be listed on {A,B,C}-free
        # subrelation {A, B, C} is the whole set here, but projection lists
        # only minimal left-hand sides.
        r = analyze(
            ["A", "B", "C"],
            [({"A"}, {"C"})],
            [{"A", "B", "C"}],
        )
        self.assertIn(fd("A", "C"), r.projections[0])
        self.assertNotIn(fd(("A", "B"), "C"), r.projections[0])


class DependencyPreservationTests(unittest.TestCase):
    def test_preserving(self):
        r = analyze(
            ["A", "B", "C"],
            [({"A"}, {"B"}), ({"B"}, {"C"})],
            [{"A", "B"}, {"B", "C"}],
        )
        self.assertTrue(r.dependency_preserving)

    def test_not_preserving(self):
        # B -> C is lost by {A,B}, {A,C}.
        r = analyze(
            ["A", "B", "C"],
            [({"A"}, {"B"}), ({"B"}, {"C"})],
            [{"A", "B"}, {"A", "C"}],
        )
        self.assertFalse(r.dependency_preserving)


class LosslessnessTests(unittest.TestCase):
    def test_lossless_but_not_dependency_preserving(self):
        # Classic: lossless (A -> AB) yet B -> C is not preserved.
        r = analyze(
            ["A", "B", "C"],
            [({"A"}, {"B"}), ({"B"}, {"C"})],
            [{"A", "B"}, {"A", "C"}],
        )
        self.assertTrue(r.lossless)
        self.assertFalse(r.dependency_preserving)
        self.assertIsNone(r.counterexample)
        self.assertTrue(len(r.chase_log) >= 1)

    def test_preserving_but_lossy_not_confused(self):
        # Dependency preservation must never be used as the lossless verdict:
        # this decomposition preserves F = {A -> B} yet the join is lossy.
        r = analyze(["A", "B", "C"], [({"A"}, {"B"})], [{"A", "B"}, {"B", "C"}])
        self.assertTrue(r.dependency_preserving)
        self.assertFalse(r.lossless)
        self.assertIsNotNone(r.counterexample)

    def test_lossy_counterexample_is_valid(self):
        r = analyze(["A", "B", "C"], [({"A"}, {"C"})], [{"A", "B"}, {"B", "C"}])
        self.assertFalse(r.lossless)
        ce = r.counterexample
        self.assertIsNotNone(ce)
        # Instance satisfies A -> C (checked independently here).
        for t1 in ce.instance:
            for t2 in ce.instance:
                if t1[0] == t2[0]:
                    self.assertEqual(t1[2], t2[2])
        # Spurious tuple in the join of the projections ...
        self.assertIn((ce.spurious_tuple[0], ce.spurious_tuple[1]),
                      ce.projections[0])
        self.assertIn((ce.spurious_tuple[1], ce.spurious_tuple[2]),
                      ce.projections[1])
        # ... but not in the instance.
        self.assertNotIn(ce.spurious_tuple, ce.instance)
        self.assertIn("not a tuple of r", ce.explanation)

    def test_chase_log_records_each_merge(self):
        r = analyze(
            ["A", "B", "C"],
            [({"A"}, {"B"}), ({"B"}, {"C"})],
            [{"A", "B"}, {"B", "C"}],
        )
        self.assertTrue(r.lossless)
        self.assertEqual(len(r.chase_log), 1)
        step = r.chase_log[0]
        self.assertEqual(step.step, 1)
        self.assertEqual(step.column, "C")
        self.assertEqual(step.rows, (0, 1))
        self.assertIn("B -> C", step.detail)

    def test_empty_determinant_chase(self):
        # ∅ -> A forces column A to agree across all rows.
        r = analyze(["A", "B"], [(set(), {"A"})], [{"A"}, {"B"}])
        self.assertTrue(r.lossless)
        self.assertEqual(len(r.chase_log), 1)
        self.assertEqual(r.chase_log[0].fd.lhs, ())


class BudgetTests(unittest.TestCase):
    def test_budget_exhaustion_is_not_a_lossy_verdict(self):
        kwargs = dict(
            FDs=[({"A"}, {"B"}), ({"B"}, {"C"})],
            decomposition=[{"A", "B"}, {"B", "C"}],
        )
        with self.assertRaises(SearchBudgetExhausted):
            analyze(["A", "B", "C"], step_budget=0, **kwargs)
        # Same input with enough budget gives the real verdict: lossless.
        r = analyze(["A", "B", "C"], step_budget=10, **kwargs)
        self.assertTrue(r.lossless)


class ValidationTests(unittest.TestCase):
    def test_too_many_attributes(self):
        attrs = [f"A{i}" for i in range(13)]
        with self.assertRaisesRegex(InvalidInputError, "attributes: 13"):
            analyze(attrs, [], [attrs])

    def test_nan_attribute_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, r"attributes\[1\]"):
            analyze(["A", math.nan], [], [{"A"}])

    def test_inf_attribute_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, r"attributes\[0\]"):
            analyze([math.inf], [], [{"A"}])

    def test_bool_budget_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, "step_budget"):
            analyze(["A"], [], [{"A"}], step_budget=True)

    def test_nan_budget_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, "step_budget"):
            analyze(["A"], [], [{"A"}], step_budget=math.nan)

    def test_negative_budget_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, "step_budget"):
            analyze(["A"], [], [{"A"}], step_budget=-1)

    def test_unknown_attribute_in_fd_located(self):
        with self.assertRaisesRegex(InvalidInputError, r"FDs\[0\]\.lhs\[0\]"):
            analyze(["A"], [({"Z"}, {"A"})], [{"A"}])

    def test_empty_rhs_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, r"FDs\[0\]\.rhs"):
            analyze(["A"], [({"A"}, set())], [{"A"}])

    def test_decomposition_must_cover_attributes(self):
        with self.assertRaisesRegex(InvalidInputError, "not covered"):
            analyze(["A", "B"], [], [{"A"}])

    def test_empty_subrelation_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, r"decomposition\[2\]"):
            analyze(["A", "B"], [], [{"A"}, {"B"}, set()])

    def test_duplicate_attributes_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, "duplicate"):
            analyze(["A", "A"], [], [{"A"}])

    def test_malformed_fd_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, r"FDs\[0\]"):
            analyze(["A"], [("A",)], [{"A"}])

    def test_result_is_pure_value_object(self):
        r1 = analyze(["A", "B"], [({"A"}, {"B"})], [{"A"}, {"B"}])
        r2 = analyze(["A", "B"], [({"A"}, {"B"})], [{"A"}, {"B"}])
        self.assertEqual(r1, r2)  # deterministic, no hidden state


if __name__ == "__main__":
    unittest.main()
