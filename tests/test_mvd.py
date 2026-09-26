"""Unit tests and exhaustive cross-checks for MVD / 4NF support."""

import unittest
from itertools import combinations

from fdlossless import (
    InvalidInputError,
    SearchBudgetExhausted,
    analyze,
    analyze4nf,
    attribute_closure,
    check_fd,
    check_mvd,
)


def mvd_str(v):
    return str(v.mvd)


class MVDValidationTests(unittest.TestCase):
    def test_rhs_attributes_in_lhs_are_removed(self):
        r = analyze4nf(["A", "B", "C"], [], [({"A"}, {"A", "B"})],
                       [{"A", "B"}, {"A", "C"}])
        self.assertEqual([str(m) for m in r.mvds], ["A ->> B"])

    def test_rhs_empty_after_normalization_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, r"MVDs\[0\]\.rhs"):
            analyze4nf(["A", "B"], [], [({"A"}, {"A"})], [{"A", "B"}])

    def test_unknown_attribute_located(self):
        with self.assertRaisesRegex(InvalidInputError, r"MVDs\[0\]\.lhs\[0\]"):
            analyze4nf(["A"], [], [({"Z"}, {"A"})], [{"A"}])

    def test_duplicate_mvd_rejected(self):
        with self.assertRaisesRegex(InvalidInputError, "duplicate MVD"):
            analyze4nf(["A", "B"], [], [({"A"}, {"B"}), ({"A"}, {"B"})],
                       [{"A", "B"}])

    def test_nine_attributes_rejected_for_mvd_analysis(self):
        attrs = [f"A{i}" for i in range(9)]
        with self.assertRaisesRegex(InvalidInputError, "at most 8"):
            analyze4nf(attrs, [], [], [attrs])
        with self.assertRaisesRegex(InvalidInputError, "at most 8"):
            check_mvd(attrs, [], [], {"A0"}, {"A1"})

    def test_old_fd_interface_still_allows_12_attributes(self):
        attrs = [f"A{i}" for i in range(12)]
        r = analyze(attrs, [], [attrs])
        self.assertTrue(r.lossless)


class ClosureAndImplicationTests(unittest.TestCase):
    def test_attribute_closure(self):
        self.assertEqual(
            attribute_closure(["A", "B", "C"],
                              [({"A"}, {"B"}), ({"B"}, {"C"})], {"A"}),
            ("A", "B", "C"),
        )

    def test_check_fd(self):
        self.assertTrue(check_fd(["A", "B", "C"],
                                 [({"A"}, {"B"}), ({"B"}, {"C"})],
                                 {"A"}, {"C"}))
        self.assertFalse(check_fd(["A", "B"], [({"A"}, {"B"})], {"B"}, {"A"}))

    def test_complementation(self):
        # Over {A,B,C}, A ->> B implies A ->> C (complementation).
        r = check_mvd(["A", "B", "C"], [], [({"A"}, {"B"})], {"A"}, {"C"})
        self.assertTrue(r.implied)

    def test_fd_implies_mvd(self):
        # An FD X -> Y is also an MVD X ->> Y.
        r = check_mvd(["A", "B", "C"], [({"A"}, {"B"})], [], {"A"}, {"B"})
        self.assertTrue(r.implied)

    def test_mvd_not_treated_as_fd(self):
        # A ->> B does NOT imply the FD A -> B.
        self.assertFalse(check_fd(["A", "B"], [], {"A"}, {"B"}))
        r = check_mvd(["A", "B", "C"], [], [({"A"}, {"B"})], {"A"}, {"B"})
        self.assertTrue(r.implied)  # trivially, it is an input MVD
        # ... but the FD A -> B still does not follow.
        self.assertFalse(check_fd(["A", "B", "C"], [], {"A"}, {"B"}))

    def test_negative_witness_is_verified(self):
        # Skills tied to languages (S -> L): P ->> S is not implied.
        r = check_mvd(["P", "S", "L"], [({"S"}, {"L"})], [], {"P"}, {"S"})
        self.assertFalse(r.implied)
        inst = r.counterexample
        self.assertIsNotNone(inst)
        # Independently verify the witness: satisfies S -> L ...
        for t1 in inst:
            for t2 in inst:
                if t1[1] == t2[1]:
                    self.assertEqual(t1[2], t2[2])
        # ... and violates P ->> S: two tuples agree on P, but the
        # swapped tuple is missing.
        tset = set(inst)
        found = False
        for t1 in inst:
            for t2 in inst:
                if t1[0] == t2[0] and t1 != t2:
                    swapped = (t1[0], t1[1], t2[2])
                    if swapped not in tset:
                        found = True
        self.assertTrue(found)


class FourNFTests(unittest.TestCase):
    M = [({"P"}, {"S"}), ({"P"}, {"L"})]

    def test_decomposed_schema_is_4nf_and_lossless(self):
        r = analyze4nf(["P", "S", "L"], [], self.M, [{"P", "S"}, {"P", "L"}])
        self.assertTrue(r.lossless)
        self.assertTrue(r.is_4nf)
        self.assertEqual(r.violations, ())
        self.assertTrue(any(e.kind == "mvd-row" for e in r.chase_log))

    def test_unsplit_relation_violates_4nf(self):
        r = analyze4nf(["P", "S", "L"], [], self.M, [{"P", "S", "L"}])
        self.assertFalse(r.is_4nf)
        reported = {mvd_str(v) for v in r.violations}
        self.assertIn("P ->> S", reported)
        self.assertIn("P ->> L", reported)
        for v in r.violations:
            self.assertFalse(v.determinant_superkey)
            self.assertEqual(v.subrelation, ("P", "S", "L"))

    def test_superkey_determinant_is_not_a_violation(self):
        # K is a key of {K, A, B}; K ->> A is then 4NF-legal.
        r = analyze4nf(["K", "A", "B"], [({"K"}, {"A", "B"})],
                       [({"K"}, {"A"})], [{"K", "A", "B"}])
        self.assertTrue(r.is_4nf)

    def test_fd_violation_shows_up_as_4nf_violation(self):
        # BCNF-style: B -> C inside {A,B,C} with B not a superkey.
        r = analyze4nf(["A", "B", "C"], [({"B"}, {"C"})], [],
                       [{"A", "B", "C"}])
        self.assertFalse(r.is_4nf)
        self.assertIn("B ->> C", {mvd_str(v) for v in r.violations})

    def test_lossy_decomposition_reports_counterexample(self):
        r = analyze4nf(["A", "B", "C"], [], [({"A"}, {"B"})],
                       [{"A", "C"}, {"B", "C"}])
        self.assertFalse(r.lossless)
        ce = r.counterexample
        self.assertIsNotNone(ce)
        # Instance satisfies A ->> B (checked independently).
        tset = set(ce.instance)
        for t1 in ce.instance:
            for t2 in ce.instance:
                if t1[0] == t2[0]:
                    self.assertIn((t1[0], t1[1], t2[2]), tset)
        self.assertNotIn(ce.spurious_tuple, ce.instance)

    def test_budget_exhaustion_is_not_a_verdict(self):
        kwargs = dict(FDs=[], MVDs=[({"P"}, {"S"})],
                      decomposition=[{"P", "S"}, {"P", "L"}])
        with self.assertRaises(SearchBudgetExhausted):
            analyze4nf(["P", "S", "L"], step_budget=0, **kwargs)
        r = analyze4nf(["P", "S", "L"], step_budget=100, **kwargs)
        self.assertTrue(r.lossless)


# ---------------------------------------------------------------------------
# Exhaustive cross-check against brute force over the binary domain
# ---------------------------------------------------------------------------

ATTRS = ("A", "B", "C")
N = 3
ALL_TUPLES = list(range(1 << N))


def _mask(names):
    m = 0
    for a in names:
        m |= 1 << ATTRS.index(a)
    return m


# All MVDs (X, Y) over {A,B,C} with X, Y disjoint and Y non-empty,
# represented as (x_mask, y_mask).
MVD_UNIVERSE = []
for x in range(1 << N):
    for y in range(1, 1 << N):
        if x & y == 0:
            MVD_UNIVERSE.append((x, y))

MVD_SETS = [()]
MVD_SETS.extend((m,) for m in MVD_UNIVERSE)
MVD_SETS.extend(combinations(MVD_UNIVERSE, 2))


def mvd_holds_rel(relation, x, y):
    tset = set(relation)
    for t1 in relation:
        for t2 in relation:
            if t1 & x == t2 & x:
                if (t1 & (x | y)) | (t2 & ~x & ~y) not in tset:
                    return False
    return True


def names(mask):
    return {ATTRS[i] for i in range(N) if (mask >> i) & 1}


class MVDImplicationCrossCheck(unittest.TestCase):
    def test_chase_implication_matches_brute_force(self):
        # Semantic implication over ALL relations on the domain {0,1}.
        # For each MVD set, precompute the relations satisfying it.
        sat_cache = {}
        for mset in MVD_SETS:
            sat = []
            for r_mask in range(1 << len(ALL_TUPLES)):
                rel = [t for i, t in enumerate(ALL_TUPLES)
                       if (r_mask >> i) & 1]
                if all(mvd_holds_rel(rel, x, y) for x, y in mset):
                    sat.append(rel)
            sat_cache[mset] = sat
        checked = 0
        implied_count = 0
        for mset in MVD_SETS:
            mvd_input = [(names(x), names(y)) for x, y in mset]
            for x, y in MVD_UNIVERSE:
                expected = all(
                    mvd_holds_rel(rel, x, y) for rel in sat_cache[mset]
                )
                got = check_mvd(ATTRS, [], mvd_input, names(x), names(y))
                self.assertEqual(
                    got.implied, expected,
                    f"implication mismatch: {mvd_input} |= {names(x)} ->> {names(y)}",
                )
                if not expected:
                    self._check_witness(got, mset, x, y)
                checked += 1
                implied_count += expected
        self.assertGreater(implied_count, 100)
        self.assertGreater(checked - implied_count, 100)

    def _check_witness(self, result, mset, x, y):
        inst = result.counterexample
        self.assertIsNotNone(inst)
        # Map integer constants to bit patterns is impossible; verify
        # structurally on the returned instance instead.
        for mx, my in mset:
            xc = [ATTRS.index(a) for a in ATTRS if (mx >> ATTRS.index(a)) & 1]
            yc = [ATTRS.index(a) for a in ATTRS if (my >> ATTRS.index(a)) & 1]
            self._assert_mvd_holds(inst, xc, yc)
        xc = [i for i in range(N) if (x >> i) & 1]
        yc = [i for i in range(N) if (y >> i) & 1]
        with self.assertRaises(AssertionError):
            self._assert_mvd_holds(inst, xc, yc)

    def _assert_mvd_holds(self, inst, xc, yc):
        tset = set(inst)
        xy = set(xc) | set(yc)
        for t1 in inst:
            for t2 in inst:
                if all(t1[c] == t2[c] for c in xc):
                    t3 = tuple(t1[c] if c in xy else t2[c]
                               for c in range(N))
                    self.assertIn(t3, tset)


class MVDLosslessCrossCheck(unittest.TestCase):
    DECOMPS = [
        [0b011, 0b101],
        [0b011, 0b110],
        [0b101, 0b110],
        [0b011, 0b100],
        [0b001, 0b010, 0b100],
        [0b111],
    ]

    def test_chase_verdict_matches_brute_force_with_mvds(self):
        checked = 0
        lossy = 0
        for mset in [()] + [(m,) for m in MVD_UNIVERSE]:
            mvd_input = [(names(x), names(y)) for x, y in mset]
            for decomp_masks in self.DECOMPS:
                expected = self._brute_force_lossless(mset, decomp_masks)
                decomp_input = [names(d) for d in decomp_masks]
                result = analyze4nf(ATTRS, [], mvd_input, decomp_input)
                self.assertEqual(
                    result.lossless, expected,
                    f"lossless mismatch: MVDs={mvd_input} decomp={decomp_input}",
                )
                checked += 1
                if not expected:
                    lossy += 1
                    self._check_counterexample(result, mset, decomp_masks)
        self.assertGreater(lossy, 20)
        self.assertGreater(checked - lossy, 20)

    def _brute_force_lossless(self, mset, decomp):
        for r_mask in range(1 << len(ALL_TUPLES)):
            rel = frozenset(t for i, t in enumerate(ALL_TUPLES)
                            if (r_mask >> i) & 1)
            if not all(mvd_holds_rel(rel, x, y) for x, y in mset):
                continue
            projections = [frozenset(t & s for t in rel) for s in decomp]
            join = frozenset(
                t for t in ALL_TUPLES
                if all((t & s) in proj for s, proj in zip(decomp, projections))
            )
            if join != rel:
                return False
        return True

    def _check_counterexample(self, result, mset, decomp):
        ce = result.counterexample
        self.assertIsNotNone(ce)
        tset = set(ce.instance)
        for mx, my in mset:
            xc = [i for i in range(N) if (mx >> i) & 1]
            yc = [i for i in range(N) if (my >> i) & 1]
            xy = set(xc) | set(yc)
            for t1 in ce.instance:
                for t2 in ce.instance:
                    if all(t1[c] == t2[c] for c in xc):
                        t3 = tuple(t1[c] if c in xy else t2[c]
                                   for c in range(N))
                        self.assertIn(t3, tset)
        for s, proj in zip(decomp, ce.projections):
            cols = [i for i in range(N) if (s >> i) & 1]
            restricted = tuple(ce.spurious_tuple[c] for c in cols)
            self.assertIn(restricted, proj)
        self.assertNotIn(ce.spurious_tuple, tset)


if __name__ == "__main__":
    unittest.main()
