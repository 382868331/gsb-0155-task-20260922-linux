"""Unit tests for MVD support, 4NF checking, and MVD-aware chase."""

import unittest

from fdlossless import (
    MVD,
    InvalidInputError,
    SearchBudgetExhausted,
    analyze,
    attribute_closure,
    implies_mvd,
)

PSL = ["person", "skill", "language"]
INDEPENDENT = [({"person"}, {"skill"}), ({"person"}, {"language"})]


class MvdValidationTests(unittest.TestCase):
    def test_rhs_attributes_in_lhs_are_normalized_away(self):
        r = analyze(
            PSL, [], [{"person", "skill"}, {"person", "language"}],
            MVDs=[({"person", "skill"}, {"person", "skill"})],
        )
        self.assertEqual(r.mvds, (MVD(lhs=("person", "skill"), rhs=()),))

    def test_partial_overlap_normalized(self):
        r = analyze(PSL, [], [PSL], MVDs=[({"person"}, {"person", "skill"})])
        self.assertEqual(r.mvds, (MVD(lhs=("person",), rhs=("skill",)),))

    def test_duplicate_after_normalization_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, r"MVDs\[1\]"):
            analyze(
                PSL, [], [PSL],
                MVDs=[({"person"}, {"skill"}), ({"person"}, {"person", "skill"})],
            )

    def test_empty_rhs_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, r"MVDs\[0\]\.rhs"):
            analyze(PSL, [], [PSL], MVDs=[({"person"}, set())])

    def test_unknown_attribute_located(self):
        with self.assertRaisesRegex(InvalidInputError, r"MVDs\[0\]\.lhs\[0\]"):
            analyze(PSL, [], [PSL], MVDs=[({"zzz"}, {"skill"})])

    def test_nine_attributes_with_mvds_rejected(self):
        attrs = [f"A{i}" for i in range(9)]
        with self.assertRaisesRegex(InvalidInputError, "at most 8"):
            analyze(attrs, [], [attrs], MVDs=[({"A0"}, {"A1"})])

    def test_nine_attributes_without_mvds_still_allowed(self):
        attrs = [f"A{i}" for i in range(9)]
        r = analyze(attrs, [], [attrs])
        self.assertTrue(r.lossless)

    def test_old_interface_defaults(self):
        r = analyze(["A", "B"], [({"A"}, {"B"})], [{"A"}, {"B"}])
        self.assertEqual(r.mvds, ())
        self.assertTrue(r.four_nf)
        self.assertEqual(r.four_nf_violations, ())


class MvdImplicationTests(unittest.TestCase):
    def test_complementation(self):
        self.assertTrue(
            implies_mvd(PSL, [], [({"person"}, {"skill"})], {"person"}, {"language"})
        )

    def test_fd_implies_mvd(self):
        self.assertTrue(
            implies_mvd(["A", "B", "C"], [({"A"}, {"B"})], [], {"A"}, {"B"})
        )

    def test_non_implied_mvd(self):
        self.assertFalse(
            implies_mvd(PSL, [], [({"person"}, {"skill"})], {"skill"}, {"language"})
        )

    def test_trivial_mvds_always_hold(self):
        self.assertTrue(implies_mvd(PSL, [], [], {"person"}, {"person"}))
        self.assertTrue(implies_mvd(["A", "B"], [], [], {"A"}, {"B"}))

    def test_independence_does_not_imply_cross_mvd(self):
        self.assertFalse(implies_mvd(PSL, [], INDEPENDENT, {"skill"}, {"language"}))

    def test_budget_exhaustion_is_unfinished_not_refutation(self):
        with self.assertRaises(SearchBudgetExhausted):
            implies_mvd(
                PSL, [], INDEPENDENT, {"person"}, {"language"}, step_budget=0
            )
        self.assertTrue(implies_mvd(PSL, [], INDEPENDENT, {"person"}, {"language"}))

    def test_attribute_closure_public(self):
        self.assertEqual(
            attribute_closure(
                ["A", "B", "C"], [({"A"}, {"B"}), ({"B"}, {"C"})], {"A"}
            ),
            frozenset({"A", "B", "C"}),
        )
        with self.assertRaisesRegex(InvalidInputError, r"X\[0\]"):
            attribute_closure(["A"], [], {"Z"})


class MvdChaseTests(unittest.TestCase):
    def test_classic_4nf_decomposition_is_lossless(self):
        r = analyze(
            PSL, [], [{"person", "skill"}, {"person", "language"}],
            MVDs=INDEPENDENT,
        )
        self.assertTrue(r.lossless)
        self.assertIsNone(r.counterexample)
        mvd_steps = [s for s in r.chase_log if s.rule == "MVD"]
        self.assertTrue(mvd_steps)
        self.assertIsNotNone(mvd_steps[0].added_row)
        self.assertIn("->>", mvd_steps[0].detail)

    def test_mvds_are_not_treated_as_fds(self):
        r = analyze(
            PSL, [], [{"person", "language"}, {"skill", "language"}],
            MVDs=[({"person"}, {"skill"})],
        )
        self.assertFalse(r.lossless)
        self.assertIsNotNone(r.counterexample)

    def test_lossy_counterexample_satisfies_mvds(self):
        r = analyze(
            PSL, [], [{"person", "skill"}, {"skill", "language"}],
            MVDs=[({"person"}, {"skill"})],
        )
        self.assertFalse(r.lossless)
        ce = r.counterexample
        idx = {a: i for i, a in enumerate(PSL)}
        for t1 in ce.instance:
            for t2 in ce.instance:
                if t1[idx["person"]] != t2[idx["person"]]:
                    continue
                swapped = list(t2)
                swapped[idx["skill"]] = t1[idx["skill"]]
                self.assertIn(tuple(swapped), ce.instance)
        self.assertNotIn(ce.spurious_tuple, ce.instance)

    def test_chase_budget_exhaustion_with_mvds(self):
        kwargs = dict(
            FDs=[],
            decomposition=[{"person", "skill"}, {"person", "language"}],
            MVDs=INDEPENDENT,
        )
        with self.assertRaises(SearchBudgetExhausted):
            analyze(PSL, step_budget=0, **kwargs)
        r = analyze(PSL, step_budget=100, **kwargs)
        self.assertTrue(r.lossless)


class FourNFTests(unittest.TestCase):
    def test_undecomposed_relation_violates_4nf(self):
        r = analyze(PSL, [], [PSL], MVDs=INDEPENDENT)
        self.assertFalse(r.four_nf)
        mvds = {str(v.mvd) for v in r.four_nf_violations}
        self.assertIn("person ->> skill", mvds)
        self.assertIn("person ->> language", mvds)
        for v in r.four_nf_violations:
            self.assertFalse(v.determinant_is_superkey)
            self.assertEqual(v.relation, tuple(PSL))
            self.assertIn("not a superkey", v.detail)

    def test_decomposed_relation_satisfies_4nf(self):
        r = analyze(
            PSL, [], [{"person", "skill"}, {"person", "language"}],
            MVDs=INDEPENDENT,
        )
        self.assertTrue(r.four_nf)
        self.assertEqual(r.four_nf_violations, ())

    def test_superkey_determinant_is_not_a_violation(self):
        r = analyze(
            PSL,
            [({"person"}, {"skill"}), ({"person"}, {"language"})],
            [PSL],
            MVDs=[],
        )
        self.assertTrue(r.four_nf)

    def test_violation_only_on_the_guilty_subrelation(self):
        bad = analyze(["A", "B", "C"], [], [{"A", "B", "C"}], MVDs=[({"A"}, {"B"})])
        self.assertFalse(bad.four_nf)
        good = analyze(
            ["A", "B", "C"], [], [{"A", "B"}, {"A", "C"}], MVDs=[({"A"}, {"B"})]
        )
        self.assertTrue(good.four_nf)

    def test_fd_induced_mvd_violation_detected(self):
        r = analyze(["A", "B", "C"], [({"A"}, {"B"})], [{"A", "B", "C"}], MVDs=[])
        self.assertFalse(r.four_nf)
        self.assertIn("A ->> B", {str(v.mvd) for v in r.four_nf_violations})


if __name__ == "__main__":
    unittest.main()
