"""Exhaustive cross-check of the chase verdict against brute force.

The reference here is fully independent of the package under test: for
every FD set (size <= 2, singleton RHS, over {A,B,C}) and every two-part
covering decomposition (plus the three-singleton decomposition), it
enumerates ALL relations over the binary domain {0,1} and decides
losslessness by definition: r == pi_R1(r) |x| ... |x| pi_Rk(r) for every r
satisfying F.  The package's chase verdict must agree on every case, and
every counterexample it emits must check out against the same definitions.
"""

import unittest
from itertools import combinations

from fdlossless import analyze

ATTRS = ("A", "B", "C")
N = 3
ALL_TUPLES = list(range(1 << N))  # tuples as bitmasks over the domain {0,1}


def mask_of(names):
    m = 0
    for a in names:
        m |= 1 << ATTRS.index(a)
    return m


# All non-trivial singleton-RHS FDs over {A,B,C} as (lhs_mask, rhs_bit).
FD_UNIVERSE = []
for lhs_size in range(0, N):
    for lhs in combinations(range(N), lhs_size):
        lhs_mask = sum(1 << i for i in lhs)
        for a in range(N):
            if not (lhs_mask >> a) & 1:
                FD_UNIVERSE.append((lhs_mask, 1 << a))

FD_SETS = [()]
for size in (1, 2):
    FD_SETS.extend(combinations(FD_UNIVERSE, size))

# All unordered pairs {S1, S2} of non-empty subsets covering {A,B,C},
# plus the decomposition into three singletons.
DECOMPS = []
seen = set()
for assign in range(1, 3 ** N):
    parts = [0, 0]
    x = assign
    for i in range(N):
        choice = x % 3
        x //= 3
        if choice in (0, 1):
            parts[choice] |= 1 << i
        else:
            parts[0] |= 1 << i
            parts[1] |= 1 << i
    if parts[0] and parts[1] and parts[0] != parts[1]:
        key = tuple(sorted(parts))
        if key not in seen:
            seen.add(key)
            DECOMPS.append(list(key))
DECOMPS.append([0b001, 0b010, 0b100])


def satisfies(relation, fds):
    """relation: frozenset of tuple bitmasks; fds: (lhs_mask, rhs_bit)."""
    for lhs, rhs in fds:
        for t1 in relation:
            for t2 in relation:
                if t1 & lhs == t2 & lhs and t1 & rhs != t2 & rhs:
                    return False
    return True


def join_of_projections(relation, decomp):
    """Natural join of the projections, by the membership definition."""
    projections = [frozenset(t & s for t in relation) for s in decomp]
    return frozenset(
        t for t in ALL_TUPLES
        if all((t & s) in proj for s, proj in zip(decomp, projections))
    )


def brute_force_lossless(fds, decomp):
    for r_mask in range(1 << len(ALL_TUPLES)):
        relation = frozenset(
            t for i, t in enumerate(ALL_TUPLES) if (r_mask >> i) & 1
        )
        if satisfies(relation, fds):
            if join_of_projections(relation, decomp) != relation:
                return False
    return True


def names(mask):
    return {ATTRS[i] for i in range(N) if (mask >> i) & 1}


class ExhaustiveCrossCheck(unittest.TestCase):
    def test_chase_verdict_matches_brute_force(self):
        checked = 0
        lossy = 0
        for fds in FD_SETS:
            fd_input = [(names(lhs), names(rhs)) for lhs, rhs in fds]
            for decomp in DECOMPS:
                decomp_input = [names(s) for s in decomp]
                expected = brute_force_lossless(fds, decomp)
                result = analyze(ATTRS, fd_input, decomp_input)
                self.assertEqual(
                    result.lossless, expected,
                    f"verdict mismatch: FDs={fd_input} decomp={decomp_input}",
                )
                checked += 1
                if not expected:
                    lossy += 1
                    self._check_counterexample(result, fds, decomp)
        # Sanity: the sample must contain both outcomes in good number.
        self.assertGreater(lossy, 100)
        self.assertGreater(checked - lossy, 100)

    def _check_counterexample(self, result, fds, decomp):
        ce = result.counterexample
        self.assertIsNotNone(ce)
        # Convert integer-constant tuples back to bit patterns is impossible
        # (constants are arbitrary), so verify structurally instead:
        # 1. The instance satisfies every FD (checked pair by pair).
        for lhs, rhs in fds:
            lhs_cols = [i for i in range(N) if (lhs >> i) & 1]
            (rhs_col,) = [i for i in range(N) if (rhs >> i) & 1]
            for t1 in ce.instance:
                for t2 in ce.instance:
                    if all(t1[c] == t2[c] for c in lhs_cols):
                        self.assertEqual(t1[rhs_col], t2[rhs_col])
        # 2. The spurious tuple is in the natural join of the projections:
        #    its restriction to each subrelation occurs in that projection.
        for s, proj in zip(decomp, ce.projections):
            cols = [i for i in range(N) if (s >> i) & 1]
            restricted = tuple(ce.spurious_tuple[c] for c in cols)
            self.assertIn(restricted, proj)
        # 3. ... but is not a tuple of the instance.
        self.assertNotIn(ce.spurious_tuple, ce.instance)
        # 4. Projections stored in the witness match the instance.
        for s, proj in zip(decomp, ce.projections):
            cols = [ATTRS.index(a) for a in ATTRS if a in names(s)]
            recomputed = sorted({tuple(t[c] for c in cols) for t in ce.instance})
            self.assertEqual(list(proj), recomputed)


if __name__ == "__main__":
    unittest.main()
