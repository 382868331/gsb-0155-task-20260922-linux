"""Tests for the in-memory versioned trial layer.

Covers:
* versioning: only commit replaces the official analysis; rollback and
  stale commits publish nothing;
* declaration identity: removal deletes one declared id and never an
  identical dependency contributed by another declaration;
* recomputation: every trial extension recomputes the minimal cover,
  keys, projections and chase from the candidate set, and an undone add
  never leaves a stale closure behind;
* budget: exhaustion yields only ``TrialIncomplete`` — no registered
  trial, no committed version, no half analysis;
* concurrency: two trials racing to commit, exactly one wins;
* an independent closure/key enumeration (which never imports the
  package internals) cross-checks the candidate keys of every committed
  state and every modified trial on all 512 FD subsets of a 9-FD universe.
"""

import threading
import unittest
from itertools import combinations

from fdlossless import (
    AnalysisVersionStore,
    FD,
    InvalidInputError,
    SearchBudgetExhausted,
    StaleVersionError,
    TrialConflictError,
    TrialIncomplete,
    analyze,
)

ATTRS = ("A", "B", "C")
DECOMP = [{"A", "B"}, {"A", "C"}]


def base_store():
    # F = {A -> B, B -> C}; decomposition {A,B}, {A,C}: lossless (A is a
    # key) but not dependency preserving (B -> C is lost by the split).
    return AnalysisVersionStore(
        list(ATTRS),
        [("ab", {"A"}, {"B"}), ("bc", {"B"}, {"C"})],
        DECOMP,
    )


class VersioningTests(unittest.TestCase):
    def test_initial_version_is_one_and_matches_stateless_analysis(self):
        store = base_store()
        self.assertEqual(store.version, 1)
        expected = analyze(
            ATTRS, [({"A"}, {"B"}), ({"B"}, {"C"})], DECOMP
        )
        self.assertEqual(store.current(), expected)
        self.assertEqual(
            [d.fd_id for d in store.declarations()], ["ab", "bc"]
        )

    def test_open_trial_does_not_change_official_version(self):
        store = base_store()
        before = store.current()
        out = store.open_trial(add_fds=[("ac", {"A"}, {"C"})])
        self.assertEqual(out.base_version, 1)
        self.assertEqual(store.version, 1)
        self.assertEqual(store.current(), before)  # trial is isolated

    def test_commit_replaces_official_version_and_closes_trial(self):
        store = base_store()
        out = store.open_trial(add_fds=[("ca", {"C"}, {"A"})])
        # C becomes an additional key once C -> A (and A -> B -> C) holds.
        self.assertIn(("C",), out.result.candidate_keys)
        receipt = store.commit(out.trial_id)
        self.assertEqual((receipt.version, receipt.previous_version), (2, 1))
        self.assertEqual(store.version, 2)
        self.assertEqual(store.current(), out.result)
        self.assertIn(("C",), store.current().candidate_keys)
        # The committed trial is closed for every further operation.
        with self.assertRaises(TrialConflictError):
            store.commit(out.trial_id)
        with self.assertRaises(TrialConflictError):
            store.extend_trial(out.trial_id)
        with self.assertRaises(TrialConflictError):
            store.rollback(out.trial_id)

    def test_rollback_publishes_nothing_and_closes_trial(self):
        store = base_store()
        before = store.current()
        out = store.open_trial(
            add_fds=[("ca", {"C"}, {"A"})], remove_fds=["ab"]
        )
        store.rollback(out.trial_id)
        self.assertEqual(store.version, 1)
        self.assertEqual(store.current(), before)
        with self.assertRaises(TrialConflictError):
            store.trial(out.trial_id)
        with self.assertRaises(TrialConflictError):
            store.commit(out.trial_id)
        with self.assertRaises(TrialConflictError):
            store.extend_trial(out.trial_id, add_fds=[("x", {"A"}, {"B"})])
        # A fresh trial after rollback behaves exactly like the base.
        again = store.open_trial()
        self.assertTrue(again.diff.empty)

    def test_unknown_trial_id(self):
        store = base_store()
        with self.assertRaises(TrialConflictError):
            store.trial(999)
        with self.assertRaises(TrialConflictError):
            store.commit(999)
        with self.assertRaises(InvalidInputError):
            store.trial("1")
        with self.assertRaises(InvalidInputError):
            store.trial(True)


class DeclarationIdentityTests(unittest.TestCase):
    def test_duplicate_dependencies_are_distinct_declarations(self):
        # Two declarations state exactly A -> B; the store accepts them
        # even though the stateless analyze() rejects duplicate FDs.
        store = AnalysisVersionStore(
            ["A", "B"],
            [("x", {"A"}, {"B"}), ("y", {"A"}, {"B"})],
            [{"A", "B"}],
        )
        self.assertEqual(len(store.declarations()), 2)
        out = store.open_trial(remove_fds=["x"])
        # Only declaration x is gone; y still contributes A -> B, so the
        # analysis and all its properties are unchanged.
        self.assertEqual([d.fd_id for d in out.candidate_fds], ["y"])
        self.assertTrue(out.diff.empty)
        self.assertEqual(out.result.candidate_keys, (("A",),))

    def test_removing_both_identical_declarations_drops_the_dependency(self):
        # R(A,B,C), decomposition {AB}, {AC}: intersection {A}, so the
        # binary criterion makes the join lossless exactly when A -> B or
        # A -> C.  Two declarations both state A -> B; removing one keeps
        # it lossless, removing both flips to lossy.
        store = AnalysisVersionStore(
            ["A", "B", "C"],
            [("x", {"A"}, {"B"}), ("y", {"A"}, {"B"})],
            [{"A", "B"}, {"A", "C"}],
        )
        first = store.open_trial(remove_fds=["x"])
        self.assertTrue(first.result.lossless)
        store.commit(first.trial_id)
        second = store.open_trial(remove_fds=["y"])
        self.assertFalse(second.result.lossless)
        self.assertIsNotNone(second.result.counterexample)

    def test_multi_rhs_declaration_removal_is_scoped_to_its_id(self):
        # x: A -> {B, C} and y: A -> B share the singleton FD A -> B after
        # normalization.  Removing x must drop A -> C (x's own fact) but
        # must not delete A -> B contributed by y.
        store = AnalysisVersionStore(
            ["A", "B", "C"],
            [("x", {"A"}, {"B", "C"}), ("y", {"A"}, {"B"})],
            [{"A", "B", "C"}],
        )
        out = store.open_trial(remove_fds=["x"])
        self.assertEqual(
            set(out.result.fds), {FD(("A",), "B")}
        )
        # Only A -> B survives: A+ = {A,B}, so the key is {A, C}.
        self.assertEqual(out.result.candidate_keys, (("A", "C"),))


class PropertyChangeTests(unittest.TestCase):
    def test_redundant_fd_removal_leaves_all_properties_intact(self):
        # {A -> B, B -> C, A -> C} on {AB, BC}: preserving and lossless,
        # key {A}.  A -> C is redundant (implied by the other two) and is
        # not even a member of the minimal cover.
        store = AnalysisVersionStore(
            ATTRS,
            [("ab", {"A"}, {"B"}), ("bc", {"B"}, {"C"}),
             ("ac", {"A"}, {"C"})],
            [{"A", "B"}, {"B", "C"}],
        )
        before = store.current()
        self.assertTrue(before.dependency_preserving)
        self.assertTrue(before.lossless)
        out = store.open_trial(remove_fds=["ac"])
        self.assertTrue(out.diff.empty)
        self.assertEqual(out.result.candidate_keys, before.candidate_keys)
        self.assertEqual(out.result.projections, before.projections)
        self.assertEqual(
            out.result.dependency_preserving, before.dependency_preserving
        )
        self.assertEqual(out.result.lossless, before.lossless)
        receipt = store.commit(out.trial_id)
        self.assertEqual(receipt.version, 2)
        self.assertTrue(store.current().lossless)

    def test_critical_fd_removal_flips_to_lossy_with_verified_witness(self):
        # Base: {A -> B, B -> C} on {AB, AC} is lossless with key {A}.
        # Removing A -> B destroys the only thing making A determine B:
        # the key grows to {A, B} and the join becomes lossy.
        store = base_store()
        self.assertTrue(store.current().lossless)
        out = store.open_trial(remove_fds=["ab"])
        self.assertTrue(out.diff.lossless_changed)
        self.assertFalse(out.result.lossless)
        self.assertEqual(out.result.candidate_keys, (("A", "B"),))
        self.assertEqual(out.diff.candidate_keys_removed, (("A",),))
        self.assertEqual(out.diff.candidate_keys_added, (("A", "B"),))
        ce = out.result.counterexample
        self.assertIsNotNone(ce)
        # The counterexample is independently re-checked against the
        # candidate FD set {B -> C}.
        index = {a: j for j, a in enumerate(ATTRS)}
        b_col, c_col = index["B"], index["C"]
        for t1 in ce.instance:
            for t2 in ce.instance:
                if t1[b_col] == t2[b_col]:
                    self.assertEqual(t1[c_col], t2[c_col])
        for cols, proj in (([0, 1], ce.projections[0]),
                           ([0, 2], ce.projections[1])):
            self.assertIn(
                tuple(ce.spurious_tuple[c] for c in cols), proj
            )
        self.assertNotIn(ce.spurious_tuple, ce.instance)
        # The official version stays lossless until an actual commit.
        self.assertTrue(store.current().lossless)
        store.rollback(out.trial_id)
        self.assertTrue(store.current().lossless)

    def test_extend_trial_recomputes_and_undo_invalidates_old_closure(self):
        store = base_store()
        out = store.open_trial(add_fds=[("ca", {"C"}, {"A"})])
        self.assertIn(("C",), out.result.candidate_keys)
        # Undo the add within the same trial: the candidate set is
        # recomputed and the key C must disappear — no stale closure.
        out = store.extend_trial(out.trial_id, remove_fds=["ca"])
        self.assertEqual(out.added_ids, ())
        self.assertEqual(out.removed_ids, ())
        self.assertTrue(out.diff.empty)
        self.assertEqual(out.result.candidate_keys, (("A",),))
        # Remove a committed FD, then re-add the same id with new sides;
        # every step is computed from the full candidate set.
        out = store.extend_trial(out.trial_id, remove_fds=["ab"])
        self.assertFalse(out.result.lossless)
        out = store.extend_trial(
            out.trial_id, add_fds=[("ab", {"A"}, {"B", "C"})]
        )
        # Net effect versus base: ab still present, so neither list shows.
        self.assertEqual(out.added_ids, ())
        self.assertEqual(out.removed_ids, ())
        # A -> B, A -> C, B -> C: key {A}, lossless on {AB, AC}.
        self.assertEqual(out.result.candidate_keys, (("A",),))
        self.assertTrue(out.result.lossless)

    def test_remove_unknown_id_rejected_without_state_change(self):
        store = base_store()
        before = store.current()
        with self.assertRaises(TrialConflictError):
            store.open_trial(remove_fds=["ghost"])
        with self.assertRaises(TrialConflictError):
            store.open_trial(add_fds=[("ab", {"A"}, {"B"})])
        self.assertEqual(store.current(), before)
        out = store.open_trial(remove_fds=["ab"])
        with self.assertRaises(TrialConflictError):
            store.extend_trial(out.trial_id, remove_fds=["ab"])
        store.rollback(out.trial_id)

    def test_trial_result_equals_stateless_analysis_of_candidate_set(self):
        store = base_store()
        out = store.open_trial(
            add_fds=[("ac", {"A"}, {"C"}), ("cb", {"C"}, {"B"})],
            remove_fds=["bc"],
        )
        candidate_pairs = [
            (set(d.lhs), set(d.rhs)) for d in out.candidate_fds
        ]
        direct = analyze(ATTRS, candidate_pairs, DECOMP)
        self.assertEqual(out.result, direct)


class CompetingCommitTests(unittest.TestCase):
    def test_two_trials_compete_exactly_one_wins(self):
        store = base_store()
        t1 = store.open_trial(remove_fds=["ab"])
        t2 = store.open_trial(add_fds=[("ca", {"C"}, {"A"})])
        r1 = store.commit(t1.trial_id)
        self.assertEqual(r1.version, 2)
        with self.assertRaises(StaleVersionError) as ctx:
            store.commit(t2.trial_id)
        self.assertEqual(ctx.exception.expected_version, 1)
        self.assertEqual(ctx.exception.current_version, 2)
        # Refusal publishes nothing; the loser trial is still open and its
        # candidate analysis can still be inspected before rollback.
        self.assertEqual(store.version, 2)
        self.assertIn(("C",), store.trial(t2.trial_id).result.candidate_keys)
        store.rollback(t2.trial_id)
        # A retry based on the new version commits as version 3.
        retry = store.open_trial(add_fds=[("ca", {"C"}, {"A"})])
        self.assertEqual(retry.base_version, 2)
        r3 = store.commit(retry.trial_id)
        self.assertEqual((r3.version, r3.previous_version), (3, 2))

    def test_expected_version_assertion(self):
        store = base_store()
        t = store.open_trial(add_fds=[("ca", {"C"}, {"A"})])
        with self.assertRaises(InvalidInputError):
            store.commit(t.trial_id, expected_version=2)
        with self.assertRaises(InvalidInputError):
            store.commit(t.trial_id, expected_version="1")
        self.assertEqual(store.version, 1)
        receipt = store.commit(t.trial_id, expected_version=1)
        self.assertEqual(receipt.version, 2)

    def test_concurrent_commits_only_one_accepted(self):
        outcomes = []

        def worker(add_id, dep, barrier):
            store = self_store
            try:
                out = store.open_trial(add_fds=[(add_id, dep[0], dep[1])])
                barrier.wait(timeout=5)
                store.commit(out.trial_id)
                outcomes.append(("committed", add_id))
            except StaleVersionError:
                outcomes.append(("stale", add_id))

        store = base_store()
        self_store = store
        barrier = threading.Barrier(2)
        threads = [
            threading.Thread(target=worker,
                             args=("ca", ({"C"}, {"A"}), barrier)),
            threading.Thread(target=worker,
                             args=("cb", ({"C"}, {"B"}), barrier)),
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(sorted(o[0] for o in outcomes),
                         ["committed", "stale"])
        self.assertEqual(store.version, 2)


class BudgetTests(unittest.TestCase):
    def make_zero_budget_store(self):
        # No FDs initially: the chase needs zero merges (it is lossy), so
        # construction succeeds under step_budget=0.
        return AnalysisVersionStore(
            ATTRS, [], [{"A", "B"}, {"B", "C"}], step_budget=0
        )

    def test_incomplete_trial_registers_nothing(self):
        store = self.make_zero_budget_store()
        with self.assertRaises(TrialIncomplete) as ctx:
            store.open_trial(
                add_fds=[("ab", {"A"}, {"B"}), ("bc", {"B"}, {"C"})]
            )
        self.assertEqual(ctx.exception.reason, "chase_budget_exhausted")
        self.assertEqual(store.version, 1)
        self.assertEqual(store.declarations(), ())
        # No trial was registered: ids start again at 1, and nothing commits.
        with self.assertRaises(TrialConflictError):
            store.commit(1)

    def test_failed_extension_keeps_last_complete_analysis(self):
        store = self.make_zero_budget_store()
        out = store.open_trial(add_fds=[("ab", {"A"}, {"B"})])
        self.assertEqual(out.trial_id, 1)
        self.assertFalse(out.result.lossless)  # A -> B needs no merge here
        # This extension would require a chase merge: incomplete, not lossy.
        with self.assertRaises(TrialIncomplete):
            store.extend_trial(out.trial_id, add_fds=[("bc", {"B"}, {"C"})])
        still = store.trial(out.trial_id)
        self.assertEqual(still.result, out.result)  # no half analysis
        self.assertEqual([d.fd_id for d in still.candidate_fds], ["ab"])
        receipt = store.commit(out.trial_id)  # complete state commits
        self.assertEqual(receipt.version, 2)
        self.assertEqual(store.version, 2)

    def test_construction_budget_exhaustion_is_distinct_error(self):
        with self.assertRaises(SearchBudgetExhausted):
            AnalysisVersionStore(
                ATTRS,
                [("ab", {"A"}, {"B"}), ("bc", {"B"}, {"C"})],
                [{"A", "B"}, {"B", "C"}],
                step_budget=0,
            )


class TrialValidationTests(unittest.TestCase):
    def test_constructor_accepts_mapping_and_iterable(self):
        s1 = AnalysisVersionStore(
            ATTRS, {"ab": ({"A"}, {"B"})}, [{"A", "B", "C"}]
        )
        s2 = AnalysisVersionStore(
            ATTRS, [("ab", {"A"}, {"B"})], [{"A", "B", "C"}]
        )
        self.assertEqual(s1.current(), s2.current())

    def test_bad_inputs_are_rejected(self):
        store = base_store()
        bad_adds = [
            [("", {"A"}, {"B"})],
            [(1, {"A"}, {"B"})],
            [("x", {"A"}, set())],
            [("x", {"Z"}, {"B"})],
            [("x", {"A"}, {"Z"})],
        ]
        for spec in bad_adds:
            with self.assertRaises(InvalidInputError):
                store.open_trial(add_fds=spec)
        with self.assertRaises(InvalidInputError):
            store.open_trial(add_fds=[("x", {"A"}, {"B"}),
                                      ("x", {"B"}, {"A"})])
        with self.assertRaises(InvalidInputError):
            store.open_trial(remove_fds=["ab", "ab"])
        with self.assertRaises(InvalidInputError):
            store.open_trial(remove_fds="ab")
        with self.assertRaises(InvalidInputError):
            AnalysisVersionStore(
                ATTRS, [("x", {"A"}, {"B"}), ("x", {"B"}, {"A"})],
                [{"A", "B", "C"}],
            )
        with self.assertRaises(InvalidInputError):
            AnalysisVersionStore(
                ATTRS, [(1, {"A"}, {"B"})], [{"A", "B", "C"}]
            )


# ---------------------------------------------------------------------------
# Independent cross-check: closure and candidate keys, never using package
# internals.  Universe = the 9 singleton-RHS FDs over {A,B,C} whose LHS has
# size 0 or 1; every one of the 512 FD subsets is loaded into a store and
# the reported keys are compared against a brute-force enumeration.  Each
# subset is also modified in a trial and the candidate keys re-checked.
# ---------------------------------------------------------------------------

UNIVERSE = []
for lhs in (frozenset(), frozenset({"A"}), frozenset({"B"}),
            frozenset({"C"})):
    for a in ATTRS:
        if a not in lhs:
            UNIVERSE.append((lhs, frozenset({a})))


def independent_closure(fds, seed):
    result = set(seed)
    changed = True
    while changed:
        changed = False
        for lhs, rhs in fds:
            if lhs <= result and not rhs <= result:
                result |= rhs
                changed = True
    return frozenset(result)


def independent_keys(fds):
    universe = frozenset(ATTRS)
    keys = set()
    for size in range(4):
        for combo in combinations(ATTRS, size):
            s = frozenset(combo)
            if independent_closure(fds, s) == universe:
                keys.add(tuple(sorted(s)))
    minimal = {
        k for k in keys
        if not any(o != k and set(o) <= set(k) for o in keys)
    }
    return tuple(sorted(minimal, key=lambda k: (len(k), k)))


class IndependentKeyCrossCheck(unittest.TestCase):
    def test_all_512_subsets_committed_and_trial_keys(self):
        checked = 0
        for mask in range(1 << len(UNIVERSE)):
            chosen = [UNIVERSE[i] for i in range(len(UNIVERSE))
                      if (mask >> i) & 1]
            ids = [f"fd{i}" for i in range(len(chosen))]
            store = AnalysisVersionStore(
                ATTRS,
                [(ids[i], set(lhs), set(rhs))
                 for i, (lhs, rhs) in enumerate(chosen)],
                [{"A", "B", "C"}],
            )
            self.assertEqual(
                store.current().candidate_keys, independent_keys(chosen),
                f"committed key mismatch for {chosen}",
            )
            # Trial: remove every second declaration and add one unused FD;
            # candidate keys must equal the brute-force keys of the exact
            # candidate set (proving full recomputation, no stale closure).
            remove = [ids[i] for i in range(len(ids)) if i % 2 == 1]
            extra_id = "extra"
            extra = (frozenset({"A", "B"}), frozenset({"C"}))
            out = store.open_trial(
                add_fds=[(extra_id, set(extra[0]), set(extra[1]))],
                remove_fds=remove,
            )
            expected_set = [
                chosen[i] for i in range(len(chosen)) if i % 2 == 0
            ] + [extra]
            self.assertEqual(
                out.result.candidate_keys, independent_keys(expected_set),
                f"trial key mismatch for {chosen}",
            )
            store.rollback(out.trial_id)
            checked += 1
        self.assertEqual(checked, 512)


if __name__ == "__main__":
    unittest.main()
