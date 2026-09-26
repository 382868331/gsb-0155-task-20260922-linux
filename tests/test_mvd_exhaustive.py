"""Exhaustive small-domain cross-check for MVD chase, 4NF, and counterexamples.

Independent reference: over {A,B,C} with binary domain {0,1}, enumerate ALL
relations (as bitmasks) and decide losslessness, MVD implication, superkeys,
and the 4NF condition directly by definition.  Non-implied MVDs always have
a two-row chase witness, hence a counterexample over a 2-element domain, so
the finite check is exact.  The reference never calls the package under test.
"""

import unittest
from itertools import combinations

from fdlossless import analyze, implies_mvd

ATTRS = ("A", "B", "C")
N = 3
FULL = 0b111
ALL_TUPLES = list(range(1 << N))
ALL_RELATIONS = [
    frozenset(t for i, t in enumerate(ALL_TUPLES) if (m >> i) & 1)
    for m in range(1 << len(ALL_TUPLES))
]


def names(mask):
    return {ATTRS[i] for i in range(N) if (mask >> i) & 1}


# All normalized nontrivial MVDs (lhs_mask, rhs_mask): disjoint, rhs nonempty,
# lhs | rhs != full set.
MVD_UNIVERSE = []
for lhs in range(1 << N):
    for rhs in range(1, 1 << N):
        if lhs & rhs or (lhs | rhs) == FULL:
            continue
        MVD_UNIVERSE.append((lhs, rhs))

MVD_SETS = [()]
MVD_SETS.extend((m,) for m in MVD_UNIVERSE)
MVD_PAIRS = list(combinations(MVD_UNIVERSE, 2))

# A few FDs (lhs_mask, rhs_bit) to mix with MVDs.
FD_SAMPLES = [
    (0b001, 0b010),  # A -> B
    (0b010, 0b100),  # B -> C
    (0b011, 0b100),  # AB -> C
    (0b000, 0b001),  # {} -> A
]

DECOMPS = [
    [0b011, 0b101],  # {A,B}, {A,C}
    [0b011, 0b110],  # {A,B}, {B,C}
    [0b101, 0b110],  # {A,C}, {B,C}
    [0b001, 0b010, 0b100],
    [0b111],
    [0b011, 0b100],  # {A,B}, {C}
    [0b001, 0b110],  # {A}, {B,C}
]


def satisfies(relation, fds, mvds):
    for lhs, rhs in fds:
        for t1 in relation:
            for t2 in relation:
                if t1 & lhs == t2 & lhs and t1 & rhs != t2 & rhs:
                    return False
    for lhs, rhs in mvds:
        xy = lhs | rhs
        for t1 in relation:
            for t2 in relation:
                if t1 & lhs != t2 & lhs:
                    continue
                swapped = (t1 & xy) | (t2 & (FULL ^ xy))
                if swapped not in relation:
                    return False
    return True


def join_of_projections(relation, decomp):
    projections = [frozenset(t & s for t in relation) for s in decomp]
    return frozenset(
        t for t in ALL_TUPLES
        if all((t & s) in proj for s, proj in zip(decomp, projections))
    )


def brute_lossless(relations, decomp):
    return all(join_of_projections(r, decomp) == r for r in relations)


def brute_implied_mvd(relations, lhs, rhs):
    """True iff X ->> Y holds in every satisfying relation (exact here)."""
    return all(satisfies(r, (), ((lhs, rhs),)) for r in relations)


def brute_superkey(relations, x, sub):
    """X functionally determines the subrelation in every satisfying relation."""
    for r in relations:
        for t1 in r:
            for t2 in r:
                if t1 & x == t2 & x and t1 & sub != t2 & sub:
                    return False
    return True


def brute_four_nf(relations, decomp):
    """The 4NF condition by definition on every subrelation."""
    for sub in decomp:
        attrs = [i for i in range(N) if (sub >> i) & 1]
        for xsize in range(len(attrs) + 1):
            for xcombo in combinations(attrs, xsize):
                x = sum(1 << i for i in xcombo)
                if brute_superkey(relations, x, sub):
                    continue
                rest = [i for i in attrs if not (x >> i) & 1]
                for ysize in range(1, len(rest) + 1):
                    for ycombo in combinations(rest, ysize):
                        y = sum(1 << i for i in ycombo)
                        if x | y == sub:
                            continue  # trivial on the subrelation
                        if brute_implied_mvd(relations, x, y):
                            return False
    return True


def satisfying(fds, mvds):
    return [r for r in ALL_RELATIONS if satisfies(r, fds, mvds)]


def mvd_input(mvds):
    return [(names(l), names(r)) for l, r in mvds]


def fd_input(fds):
    return [(names(l), names(r)) for l, r in fds]


class MvdExhaustiveCrossCheck(unittest.TestCase):
    def test_chase_verdict_matches_brute_force(self):
        cases = []
        for mvds in MVD_SETS:
            for decomp in DECOMPS:
                cases.append(((), mvds, decomp))
        for mvds in MVD_PAIRS:
            for decomp in DECOMPS[:4]:
                cases.append(((), mvds, decomp))
        for fd in FD_SAMPLES:
            for mvd in MVD_UNIVERSE:
                for decomp in DECOMPS[:3]:
                    cases.append(((fd,), (mvd,), decomp))
        checked = lossy = 0
        for fds, mvds, decomp in cases:
            relations = satisfying(fds, mvds)
            expected = brute_lossless(relations, decomp)
            result = analyze(
                ATTRS, fd_input(fds), [names(s) for s in decomp],
                MVDs=mvd_input(mvds),
            )
            self.assertEqual(
                result.lossless, expected,
                f"verdict mismatch: fds={fds} mvds={mvds} decomp={decomp}",
            )
            checked += 1
            if not expected:
                lossy += 1
                self._check_counterexample(result, fds, mvds, decomp)
        self.assertGreater(lossy, 50)
        self.assertGreater(checked - lossy, 50)

    def _check_counterexample(self, result, fds, mvds, decomp):
        ce = result.counterexample
        self.assertIsNotNone(ce)
        idx = {a: i for i, a in enumerate(ATTRS)}
        # 1. The instance satisfies every FD and every MVD.
        for lhs, rhs in fds:
            lcols = [i for i in range(N) if (lhs >> i) & 1]
            (rcol,) = [i for i in range(N) if (rhs >> i) & 1]
            for t1 in ce.instance:
                for t2 in ce.instance:
                    if all(t1[c] == t2[c] for c in lcols):
                        self.assertEqual(t1[rcol], t2[rcol])
        for lhs, rhs in mvds:
            xcols = [i for i in range(N) if (lhs >> i) & 1]
            ycols = {i for i in range(N) if (rhs >> i) & 1}
            for t1 in ce.instance:
                for t2 in ce.instance:
                    if not all(t1[c] == t2[c] for c in xcols):
                        continue
                    swapped = tuple(
                        t1[c] if c in xcols or c in ycols else t2[c]
                        for c in range(N)
                    )
                    self.assertIn(swapped, ce.instance)
        # 2. The spurious tuple is in the join of the projections ...
        for s, proj in zip(decomp, ce.projections):
            cols = [i for i in range(N) if (s >> i) & 1]
            self.assertIn(tuple(ce.spurious_tuple[c] for c in cols), proj)
        # 3. ... but not in the instance.
        self.assertNotIn(ce.spurious_tuple, ce.instance)

    def test_implies_mvd_matches_finite_check(self):
        for mvds in MVD_SETS:
            relations = satisfying((), mvds)
            for lhs, rhs in MVD_UNIVERSE:
                expected = brute_implied_mvd(relations, lhs, rhs)
                got = implies_mvd(
                    ATTRS, [], mvd_input(mvds), names(lhs), names(rhs),
                )
                self.assertEqual(
                    got, expected,
                    f"implication mismatch: mvds={mvds} {lhs:b} ->> {rhs:b}",
                )

    def test_four_nf_matches_brute_force(self):
        cases = []
        for mvds in MVD_SETS:
            for decomp in DECOMPS:
                cases.append(((), mvds, decomp))
        for fd in FD_SAMPLES:
            for mvd in MVD_UNIVERSE[:6]:
                for decomp in DECOMPS[:3]:
                    cases.append(((fd,), (mvd,), decomp))
        checked = violations_seen = 0
        for fds, mvds, cases_decomp in cases:
            relations = satisfying(fds, mvds)
            expected = brute_four_nf(relations, cases_decomp)
            result = analyze(
                ATTRS, fd_input(fds), [names(s) for s in cases_decomp],
                MVDs=mvd_input(mvds),
            )
            self.assertEqual(
                result.four_nf, expected,
                f"4NF mismatch: fds={fds} mvds={mvds} decomp={cases_decomp}",
            )
            checked += 1
            if not expected:
                violations_seen += 1
                # Every reported violation is genuinely implied and its
                # determinant genuinely fails to be a superkey.
                for v in result.four_nf_violations:
                    sub_mask = sum(1 << ATTRS.index(a) for a in v.relation)
                    x_mask = sum(1 << ATTRS.index(a) for a in v.mvd.lhs)
                    y_mask = sum(1 << ATTRS.index(a) for a in v.mvd.rhs)
                    self.assertTrue(
                        brute_implied_mvd(relations, x_mask, y_mask),
                        f"reported violation {v.mvd} is not implied",
                    )
                    self.assertFalse(
                        brute_superkey(relations, x_mask, sub_mask),
                        f"determinant of {v.mvd} is actually a superkey",
                    )
                    self.assertFalse(v.determinant_is_superkey)
        self.assertGreater(violations_seen, 20)
        self.assertGreater(checked - violations_seen, 20)


if __name__ == "__main__":
    unittest.main()
