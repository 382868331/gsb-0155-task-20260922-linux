"""Tests for the what-if trial / versioned-commit layer.

Covers, independently of the package internals:

* adding and removing several declarations in one trial, with every view
  recomputed from the whole candidate declaration set (cross-checked
  against a fresh stateless ``analyze`` call on the same candidate set);
* removal by *declaration id* — a derivable copy of an FD that comes from
  other declarations must not be silently dropped, and a multi-attribute
  RHS is removed as one declaration;
* redundant-FD revocation leaving every decomposition property unchanged;
* critical-FD revocation turning a lossless join lossy, with the
  counterexample independently verified and the candidate keys checked by
  enumerating closures over small attribute sets;
* optimistic two-trial commits (winner commits, loser gets
  ``StaleVersionError`` and nothing is overwritten);
* budget exhaustion rejecting the whole mutation (no half analysis);
* closed-trial and input-validation error semantics.
"""

import itertools
import random
import unittest

from fdlossless import (
    FD,
    AnalysisSession,
    InvalidInputError,
    SearchBudgetExhausted,
    StaleVersionError,
    TrialClosedError,
    analyze,
)

ATTRS = ("A", "B", "C")


def names(names_tuple, mask):
    return {names_tuple[i] for i in range(len(names_tuple)) if (mask >> i) & 1}


def independent_closure(attrs, fd_masks, seed_mask):
    """Reference closure: fd_masks is a set of (lhs_mask, rhs_bit)."""
    result = seed_mask
    changed = True
    while changed:
        changed = False
        for lhs, rhs in fd_masks:
            if (lhs & result) == lhs and not (rhs & result):
                result |= rhs
                changed = True
    return result


def brute_force_lossless(attrs, fd_input, decomp_sets):
    """Definition-based losslessness over all relations on {0,1}."""
    n = len(attrs)
    idx = {a: i for i, a in enumerate(attrs)}
    fds = [
        (
            sum(1 << idx[a] for a in lhs),
            tuple(1 << idx[a] for a in rhs),
        )
        for lhs, rhs in fd_input
    ]
    parts = [frozenset(p) for p in decomp_sets]
    all_tuples = list(range(1 << n))

    def satisfies(relation):
        for lhs_mask, rhs_bits in fds:
            for t1 in relation:
                for t2 in relation:
                    if (t1 & lhs_mask) == (t2 & lhs_mask) and any(
                        (t1 & b) != (t2 & b) for b in rhs_bits
                    ):
                        return False
        return True

    for r_mask in range(1 << len(all_tuples)):
        relation = frozenset(
            t for i, t in enumerate(all_tuples) if (r_mask >> i) & 1
        )
        if not satisfies(relation):
            continue
        for part in parts:
            col_bits = sum(1 << idx[a] for a in part)
        projected = []
        for part in parts:
            col_bits = sum(1 << idx[a] for a in part)
            projected.append(frozenset(t & col_bits for t in relation))
        join = frozenset(
            t
            for t in all_tuples
            if all((t & sum(1 << idx[a] for a in part)) in proj
                   for part, proj in zip(parts, projected))
        )
        if join != relation:
            return False
    return True


def verify_counterexample(attrs, fd_input, decomp_sets, ce):
    """Independent structural verification of a lossy witness."""
    idx = {a: i for i, a in enumerate(attrs)}
    instance = ce.instance
    for lhs, rhs in fd_input:
        lhs_cols = sorted(idx[a] for a in lhs)
        for a in rhs:
            col = idx[a]
            for t1 in instance:
                for t2 in instance:
                    if all(t1[c] == t2[c] for c in lhs_cols):
                        assert t1[col] == t2[col], "witness violates an FD"
    for part, proj in zip(decomp_sets, ce.projections):
        cols = sorted(idx[a] for a in part)
        assert tuple(ce.spurious_tuple[c] for c in cols) in proj, (
            "spurious tuple missing from a projection"
        )
        assert tuple(sorted({tuple(t[c] for c in cols) for t in instance})) \
            == tuple(proj), "stored projection does not match instance"
    assert ce.spurious_tuple not in instance


def check_candidate_keys(attrs, fd_input, reported_keys):
    """Enumerate every subset; reported keys must be exactly the minimal
    attribute sets whose closure is the universe."""
    n = len(attrs)
    idx = {a: i for i, a in enumerate(attrs)}
    fd_masks = set()
    for lhs, rhs in fd_input:
        for a in rhs:
            fd_masks.add((sum(1 << idx[x] for x in lhs), 1 << idx[a]))
    universe = (1 << n) - 1
    expected = []
    for mask in range(1 << n):
        if independent_closure(attrs, fd_masks, mask) == universe:
            if not any(
                independent_closure(attrs, fd_masks, sub) == universe
                for sub in range(mask)
                if (sub & mask) == sub and sub != mask
            ):
                expected.append(mask)
    expected_keys = tuple(
        sorted(
            tuple(sorted(names(attrs, m))) for m in expected
        )
    )
    reported = tuple(sorted(tuple(sorted(k)) for k in reported_keys))
    assert reported == expected_keys, (reported, expected_keys)


class TrialBasicsTests(unittest.TestCase):
    def setUp(self):
        # R(A,B,C); F = {A->B, B->C, A->C (redundant)}; lossless decomp.
        self.init_fds = [({"A"}, {"B"}), ({"B"}, {"C"}), ({"A"}, {"C"})]
        self.decomp = [{"A", "B"}, {"B", "C"}]
        self.session = AnalysisSession(["A", "B", "C"], self.init_fds,
                                       self.decomp)

    def test_initial_version_matches_stateless_analysis(self):
        self.assertEqual(self.session.version, 1)
        self.assertEqual(
            self.session.committed,
            analyze(["A", "B", "C"], self.init_fds, self.decomp),
        )
        ids = [d.id for d in self.session.committed_declarations]
        self.assertEqual(ids, [0, 1, 2])

    def test_empty_trial_view_differs_nothing(self):
        view = self.session.begin_trial()
        self.assertTrue(view.open)
        self.assertEqual(view.base_version, 1)
        self.assertFalse(view.delta.changed)
        self.assertEqual(view.result, self.session.committed)

    def test_chase_evidence_still_present_in_trial(self):
        view = self.session.begin_trial()
        self.assertTrue(view.result.lossless)
        self.assertGreaterEqual(len(view.result.chase_log), 1)
        # Each logged merge is a genuine application of one of the FDs.
        live = set(view.result.fds)
        for step in view.result.chase_log:
            self.assertIn(step.fd, live)
            self.assertIn(step.fd.rhs, step.column)


class RedundantFdRevocationTests(unittest.TestCase):
    def setUp(self):
        self.init_fds = [({"A"}, {"B"}), ({"B"}, {"C"}), ({"A"}, {"C"})]
        self.decomp = [{"A", "B"}, {"B", "C"}]
        self.session = AnalysisSession(["A", "B", "C"], self.init_fds,
                                       self.decomp)

    def test_revoking_redundant_fd_changes_no_property(self):
        before = self.session.committed
        view = self.session.begin_trial()
        after = self.session.remove_fds(view.trial_id, [2])  # revoke A->C

        # The declaration is really gone (removal is by declaration)...
        self.assertEqual(
            [str(d) for d in after.delta.declared_removed], ["#2: A -> C"]
        )
        self.assertEqual([d.id for d in after.declared_fds], [0, 1])
        # ...but the minimal cover, keys, projections and both verdicts are
        # identical, because A->C was derivable from the other two FDs.
        self.assertEqual(after.result.minimal_cover, before.minimal_cover)
        self.assertEqual(after.result.candidate_keys, before.candidate_keys)
        self.assertEqual(after.result.projections, before.projections)
        self.assertEqual(after.result.dependency_preserving,
                         before.dependency_preserving)
        self.assertEqual(after.result.lossless, before.lossless)
        self.assertFalse(after.delta.minimal_cover_added)
        self.assertFalse(after.delta.minimal_cover_removed)
        self.assertFalse(after.delta.candidate_keys_added)
        self.assertFalse(after.delta.candidate_keys_removed)
        self.assertFalse(after.delta.dependency_preserving_changed)
        self.assertFalse(after.delta.lossless_changed)
        # Declaration set changed even though derived properties did not.
        self.assertTrue(after.delta.changed)

        outcome = self.session.commit(view.trial_id)
        self.assertEqual(outcome.version, 2)
        self.assertEqual(self.session.version, 2)

    def test_removing_declaration_does_not_touch_implication(self):
        # Declarations #0 A->B, #1 B->C, #2 A->C.  Remove #0 (A->B); A->B
        # is NOT implied by B->C, A->C, so A stops determining B.  Remove
        # #2 instead (the redundant transitive edge): A+ still contains C.
        view = self.session.begin_trial()
        after = self.session.remove_fds(view.trial_id, [0])
        check_candidate_keys(
            ATTRS,
            [({"A"}, {"C"}), ({"B"}, {"C"})],
            after.result.candidate_keys,
        )
        self.assertEqual(after.result.candidate_keys,
                         (("A", "B"),))

    def test_multi_attribute_rhs_is_one_declaration(self):
        s = AnalysisSession(
            ["A", "B", "C"],
            [({"A"}, {"B", "C"})],
            [{"A", "B", "C"}],
        )
        view = s.begin_trial()
        self.assertEqual([d.id for d in view.declared_fds], [0])
        self.assertEqual(view.declared_fds[0].rhs, ("B", "C"))
        after = s.remove_fds(view.trial_id, [0])  # one id removes both
        self.assertEqual(after.declared_fds, ())
        # No FDs left: only ABC is a key.
        self.assertEqual(after.result.candidate_keys, (("A", "B", "C"),))

    def test_multi_attribute_rhs_added_in_trial_removed_as_unit(self):
        s = AnalysisSession(["A", "B", "C"], [], [{"A", "B", "C"}])
        view = s.begin_trial()
        after = s.add_fds(view.trial_id, [({"A"}, {"B", "C"})])
        self.assertEqual([d.id for d in after.declared_fds], [0])
        self.assertEqual(after.declared_fds[0].rhs, ("B", "C"))
        self.assertEqual(
            set(after.result.fds),
            {FD(("A",), "B"), FD(("A",), "C")},
        )
        gone = s.remove_fds(view.trial_id, [0])
        self.assertEqual(gone.declared_fds, ())
        self.assertEqual(gone.result.candidate_keys, (("A", "B", "C"),))


class CriticalFdRevocationTests(unittest.TestCase):
    def test_revoking_critical_fd_makes_join_lossy(self):
        # F = {A->B, B->C} is lossless over {A,B},{B,C}; dropping B->C
        # leaves {A->B}, under which the same decomposition is lossy.
        s = AnalysisSession(
            ["A", "B", "C"],
            [({"A"}, {"B"}), ({"B"}, {"C"})],
            [{"A", "B"}, {"B", "C"}],
        )
        self.assertTrue(s.committed.lossless)
        view = s.begin_trial()
        after = s.remove_fds(view.trial_id, [1])

        self.assertFalse(after.result.lossless)
        self.assertTrue(after.delta.lossless_changed)
        self.assertIsNotNone(after.result.counterexample)
        ce = after.result.counterexample
        verify_counterexample(
            ATTRS, [({"A"}, {"B"})],
            [{"A", "B"}, {"B", "C"}], ce,
        )
        # The independent definition over all binary relations agrees.
        self.assertFalse(
            brute_force_lossless(
                ATTRS, [({"A"}, {"B"})],
                [{"A", "B"}, {"B", "C"}],
            )
        )
        # Keys change from (A,) to (A,C).
        self.assertEqual(after.result.candidate_keys, (("A", "C"),))
        check_candidate_keys(
            ATTRS, [({"A"}, {"B"})], after.result.candidate_keys
        )
        # The trial result must equal a from-scratch stateless analysis
        # with exactly the surviving declarations — no stale closure reuse.
        fresh = analyze(
            ["A", "B", "C"], [({"A"}, {"B"})],
            [{"A", "B"}, {"B", "C"}],
        )
        self.assertEqual(after.result, fresh)
        # Discarding leaves the committed (lossless) version untouched.
        s.discard_trial(view.trial_id)
        self.assertTrue(s.committed.lossless)
        self.assertEqual(s.version, 1)


class RecomputeFromScratchTests(unittest.TestCase):
    def test_add_remove_sequences_match_stateless_analysis(self):
        rng = random.Random(20260925)
        s = AnalysisSession(
            ["A", "B", "C"],
            [({"A"}, {"B"})],
            [{"A", "B"}, {"B", "C"}],
        )
        view = s.begin_trial()
        tid = view.trial_id
        choices = [
            ({"A"}, {"B"}), ({"B"}, {"C"}), ({"A"}, {"C"}),
            ({"C"}, {"A"}), ({"A", "B"}, {"C"}), (set(), {"A"}),
        ]
        for _ in range(40):
            if rng.random() < 0.65:
                pick = choices[rng.randrange(len(choices))]
                try:
                    view = s.add_fds(tid, [[pick[0], pick[1]]])
                except InvalidInputError:
                    continue  # duplicate normalized FD: session must reject
            else:
                live = [d.id for d in view.declared_fds]
                if not live:
                    continue
                k = rng.randrange(1, min(3, len(live)) + 1)
                ids = rng.sample(live, k)
                view = s.remove_fds(tid, ids)
            # Rebuild the candidate FD input from declarations (RHS kept
            # grouped) and demand equality with the stateless engine.
            fd_input = [
                [set(d.lhs), set(d.rhs)] for d in view.declared_fds
            ]
            fresh = analyze(
                ["A", "B", "C"], fd_input,
                [{"A", "B"}, {"B", "C"}],
            )
            self.assertEqual(
                view.result.minimal_cover, fresh.minimal_cover
            )
            self.assertEqual(
                view.result.candidate_keys, fresh.candidate_keys
            )
            self.assertEqual(
                view.result.projections, fresh.projections
            )
            self.assertEqual(
                view.result.dependency_preserving,
                fresh.dependency_preserving,
            )
            self.assertEqual(view.result.lossless, fresh.lossless)
            check_candidate_keys(ATTRS, fd_input, fresh.candidate_keys)
            if not fresh.lossless:
                verify_counterexample(
                    ATTRS, fd_input,
                    [{"A", "B"}, {"B", "C"}],
                    view.result.counterexample,
                )

    def test_revoke_after_add_does_not_reuse_old_closure(self):
        s = AnalysisSession(
            ["A", "B", "C"], [], [{"A", "B"}, {"B", "C"}]
        )
        view = s.begin_trial()
        tid = view.trial_id
        view = s.add_fds(tid, [({"A"}, {"B"}), ({"B"}, {"C"})])
        self.assertTrue(view.result.lossless)
        added_ids = [d.id for d in view.declared_fds]
        # Drop both: the closure built for the {A->B,B->C} candidate set
        # must not linger — with no FDs the join is lossy and ABC the key.
        view = s.remove_fds(tid, added_ids)
        self.assertFalse(view.result.lossless)
        self.assertEqual(view.result.candidate_keys, (("A", "B", "C"),))
        self.assertEqual(view.declared_fds, ())


class RemovalByIdentityTests(unittest.TestCase):
    def test_duplicate_relationship_from_other_fd_not_misremoved(self):
        # A->C declared directly (#1) and also derived via A->B, B->C.
        # Removing the *derived* declaration #1 removes only that
        # declaration; the implication A->C survives through #0/#2, and
        # removing #0 must not remove #1 even though both end in A+ = ABC.
        s = AnalysisSession(
            ["A", "B", "C"],
            [({"A"}, {"B"}), ({"A"}, {"C"}), ({"B"}, {"C"})],
            [{"A", "B", "C"}],
        )
        view = s.begin_trial()
        after = s.remove_fds(view.trial_id, [1])  # the direct A->C
        self.assertEqual([d.id for d in after.declared_fds], [0, 2])
        # A still functionally determines C via the surviving chain.
        self.assertEqual(
            set(after.result.minimal_cover),
            {FD(("A",), "B"), FD(("B",), "C")},
        )

    def test_unknown_id_rejected_without_state_change(self):
        s = AnalysisSession(
            ["A", "B", "C"], [({"A"}, {"B"})], [{"A", "B", "C"}]
        )
        view = s.begin_trial()
        with self.assertRaisesRegex(InvalidInputError, "not present"):
            s.remove_fds(view.trial_id, [99])
        still = s.trial_view(view.trial_id)
        self.assertEqual([d.id for d in still.declared_fds], [0])

    def test_bad_id_types_rejected(self):
        s = AnalysisSession(
            ["A", "B"], [({"A"}, {"B"})], [{"A", "B"}]
        )
        view = s.begin_trial()
        with self.assertRaises(InvalidInputError):
            s.remove_fds(view.trial_id, 0)          # bare int
        with self.assertRaises(InvalidInputError):
            s.remove_fds(view.trial_id, ["0"])      # str
        with self.assertRaises(InvalidInputError):
            s.remove_fds(view.trial_id, [True])     # bool
        with self.assertRaises(InvalidInputError):
            s.remove_fds(view.trial_id, [-1])
        with self.assertRaises(InvalidInputError):
            s.remove_fds(view.trial_id, [0, 0])
        with self.assertRaises(InvalidInputError):
            s.remove_fds(view.trial_id, [])

    def test_duplicate_add_rejected_atomically(self):
        s = AnalysisSession(
            ["A", "B", "C"], [({"A"}, {"B"})], [{"A", "B", "C"}]
        )
        view = s.begin_trial()
        # The batch contains a live duplicate and a fresh FD; nothing may
        # be applied.
        with self.assertRaises(InvalidInputError):
            s.add_fds(view.trial_id,
                      [({"B"}, {"C"}), ({"A"}, {"B"})])
        still = s.trial_view(view.trial_id)
        self.assertEqual([d.id for d in still.declared_fds], [0])
        # A fresh, non-duplicate batch works afterwards.
        view2 = s.add_fds(view.trial_id, [({"B"}, {"C"})])
        self.assertEqual([d.id for d in view2.declared_fds], [0, 1])


class DeltaTests(unittest.TestCase):
    def test_delta_reports_added_and_removed_keys(self):
        s = AnalysisSession(
            ["A", "B", "C", "D"],
            [({"A", "B"}, {"C"}), ({"C"}, {"D"}), ({"D"}, {"A"})],
            [{"A", "B", "C", "D"}],
        )
        self.assertEqual(
            s.committed.candidate_keys,
            (("A", "B"), ("B", "C"), ("B", "D")),
        )
        view = s.begin_trial()
        after = s.add_fds(view.trial_id, [({"B"}, {"A"})])
        # B alone becomes the unique key: three size-2 keys removed.
        self.assertEqual(after.result.candidate_keys, (("B",),))
        self.assertEqual(after.delta.candidate_keys_added, (("B",),))
        self.assertEqual(
            tuple(sorted(after.delta.candidate_keys_removed)),
            (("A", "B"), ("B", "C"), ("B", "D")),
        )

    def test_projection_diff_aligned_with_subrelations(self):
        s = AnalysisSession(
            ["A", "B", "C"],
            [({"A"}, {"B"})],
            [{"A", "B"}, {"A", "C"}],
        )
        view = s.begin_trial()
        after = s.add_fds(view.trial_id, [({"A"}, {"C"})])
        # Subrelation 0 already sees A->B (unchanged); subrelation 1 gains
        # A->C.
        self.assertEqual(after.delta.projections_added[0], ())
        self.assertIn(FD(("A",), "C"), after.delta.projections_added[1])
        self.assertEqual(after.delta.projections_removed, ((), ()))


class CommitConcurrencyTests(unittest.TestCase):
    def test_two_trials_compete_first_wins(self):
        s = AnalysisSession(
            ["A", "B", "C"],
            [({"A"}, {"B"}), ({"B"}, {"C"})],
            [{"A", "B"}, {"B", "C"}],
        )
        ta = s.begin_trial()
        tb = s.begin_trial()
        s.add_fds(ta.trial_id, [({"C"}, {"A"})])
        s.add_fds(tb.trial_id, [({"C"}, {"B"})])

        outcome = s.commit(ta.trial_id, expected_version=1)
        self.assertEqual(outcome.version, 2)
        # Declaration ids are session-monotonic: the two trials' new FDs
        # did not collide on id 1.
        winner_ids = [d.id for d in s.committed_declarations]
        self.assertEqual(winner_ids, [0, 1, 2])

        with self.assertRaises(StaleVersionError):
            s.commit(tb.trial_id)
        with self.assertRaises(StaleVersionError):
            s.commit(tb.trial_id, expected_version=1)
        # Pinning a version the trial was not based on is also rejected.
        # The loser's scratch state is still inspectable...
        loser = s.trial_view(tb.trial_id)
        self.assertTrue(loser.open)
        self.assertTrue(any(d.rhs == ("B",) and d.lhs == ("C",)
                            for d in loser.declared_fds))
        # ...and the committed analysis is exactly the winner's.
        self.assertEqual(s.committed, outcome.result)
        self.assertEqual(s.version, 2)
        self.assertIn(FD(("C",), "A"), s.committed.fds)
        self.assertNotIn(FD(("C",), "B"), s.committed.fds)
        s.discard_trial(tb.trial_id)

    def test_commit_then_rebase_and_commit_again(self):
        s = AnalysisSession(
            ["A", "B", "C"], [({"A"}, {"B"})], [{"A", "B", "C"}]
        )
        t1 = s.begin_trial()
        s.add_fds(t1.trial_id, [({"B"}, {"C"})])
        self.assertEqual(s.commit(t1.trial_id).version, 2)
        t2 = s.begin_trial()
        s.add_fds(t2.trial_id, [({"C"}, {"A"})])
        self.assertEqual(s.commit(t2.trial_id).version, 3)
        self.assertEqual(
            set(s.committed.fds),
            {FD(("A",), "B"), FD(("B",), "C"), FD(("C",), "A")},
        )

    def test_committed_trial_id_is_dead(self):
        s = AnalysisSession(
            ["A", "B"], [({"A"}, {"B"})], [{"A", "B"}]
        )
        t = s.begin_trial()
        s.commit(t.trial_id)
        with self.assertRaises(TrialClosedError):
            s.add_fds(t.trial_id, [({"B"}, {"A"})])
        with self.assertRaises(TrialClosedError):
            s.remove_fds(t.trial_id, [0])
        with self.assertRaises(TrialClosedError):
            s.commit(t.trial_id)
        with self.assertRaises(TrialClosedError):
            s.discard_trial(t.trial_id)

    def test_unknown_trial_id_rejected(self):
        s = AnalysisSession(["A"], [], [{"A"}])
        with self.assertRaises(TrialClosedError):
            s.trial_view(1234)

    def test_concurrent_threads_commit_exactly_one_winner(self):
        import threading

        s = AnalysisSession(
            ["A", "B", "C"], [({"A"}, {"B"})], [{"A", "B", "C"}]
        )
        # Both trials are open against version 1 before any committing
        # starts, so one of the racing commits must be rejected.
        va = s.begin_trial()
        vc = s.begin_trial()
        barrier = threading.Barrier(2)
        outcomes = []
        errors = []

        def worker(view, suffix):
            try:
                s.add_fds(view.trial_id, [({"B"}, {suffix})])
                barrier.wait(timeout=5)
                s.commit(view.trial_id)
                outcomes.append(suffix)
            except StaleVersionError:
                errors.append(suffix)

        threads = [
            threading.Thread(target=worker, args=(va, "A")),
            threading.Thread(target=worker, args=(vc, "C")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(len(errors), 1)
        self.assertEqual(s.version, 2)
        committed_pairs = {(f.lhs, f.rhs) for f in s.committed.fds}
        winners = [
            c for c in ("A", "C") if (("B",), c) in committed_pairs
        ]
        self.assertEqual(winners, outcomes)


class BudgetTests(unittest.TestCase):
    def test_mutation_exhausting_budget_is_rejected_wholesale(self):
        # The same inputs the stateless budget test uses: one chase merge
        # is required, so budget 0 rejects; budget 10 succeeds.
        s = AnalysisSession(
            ["A", "B", "C"], [],
            [{"A", "B"}, {"B", "C"}],
            step_budget=0,
        )
        view = s.begin_trial()
        with self.assertRaises(SearchBudgetExhausted):
            s.add_fds(view.trial_id,
                      [({"A"}, {"B"}), ({"B"}, {"C"})])
        # Nothing was published: the trial still has zero declarations and
        # its last (complete) analysis, and remains usable.
        still = s.trial_view(view.trial_id)
        self.assertEqual(still.declared_fds, ())
        self.assertFalse(still.result.lossless)
        # A fresh session with enough budget completes the same analysis.
        s2 = AnalysisSession(
            ["A", "B", "C"], [],
            [{"A", "B"}, {"B", "C"}],
            step_budget=10,
        )
        v2 = s2.begin_trial()
        ok = s2.add_fds(v2.trial_id,
                        [({"A"}, {"B"}), ({"B"}, {"C"})])
        self.assertTrue(ok.result.lossless)

    def test_construction_and_removal_with_tight_budget(self):
        # Construction itself rejects when the initial chase cannot finish
        # (no half-built session), and removal recomputes completely with
        # a tight but sufficient budget.
        with self.assertRaises(SearchBudgetExhausted):
            AnalysisSession(
                ["A", "B", "C"],
                [({"A"}, {"B"}), ({"B"}, {"C"})],
                [{"A", "B"}, {"B", "C"}],
                step_budget=0,
            )
        s = AnalysisSession(
            ["A", "B", "C"],
            [({"A"}, {"B"}), ({"B"}, {"C"})],
            [{"A", "B"}, {"B", "C"}],
            step_budget=1,
        )
        view = s.begin_trial()
        # Drop A->B; with B->C alone the chase still performs exactly one
        # merge, which budget 1 must allow.
        after = s.remove_fds(view.trial_id, [0])
        self.assertEqual([d.id for d in after.declared_fds], [1])
        self.assertTrue(after.result.lossless)
        self.assertEqual(len(after.result.chase_log), 1)


class SessionValidationTests(unittest.TestCase):
    def test_invalid_constructor_inputs_rejected(self):
        with self.assertRaises(InvalidInputError):
            AnalysisSession(["A", "A"], [], [{"A"}])
        with self.assertRaises(InvalidInputError):
            AnalysisSession(["A"], [({"Z"}, {"A"})], [{"A"}])
        with self.assertRaises(InvalidInputError):
            AnalysisSession(["A", "B"], [], [{"A"}])  # B not covered

    def test_add_fds_validates_attributes(self):
        s = AnalysisSession(["A", "B"], [], [{"A", "B"}])
        view = s.begin_trial()
        with self.assertRaisesRegex(InvalidInputError, r"FDs\[0\]"):
            s.add_fds(view.trial_id, [({"A"}, {"Z"})])
        with self.assertRaises(InvalidInputError):
            s.add_fds(view.trial_id, [({"A"}, set())])

    def test_expected_version_must_be_valid_int(self):
        s = AnalysisSession(["A"], [], [{"A"}])
        t = s.begin_trial()
        with self.assertRaises(InvalidInputError):
            s.commit(t.trial_id, expected_version=0)
        with self.assertRaises(InvalidInputError):
            s.commit(t.trial_id, expected_version="1")
        with self.assertRaises(InvalidInputError):
            s.commit(t.trial_id, expected_version=True)


class EnumeratedCrossCheckTests(unittest.TestCase):
    def test_all_two_fd_sets_keys_and_lossiness_through_a_trial(self):
        # Every <=2 FD set over {A,B,C}: run it THROUGH the trial API
        # (empty session + add + commit), then independently enumerate
        # closures for keys and brute-force binary relations for
        # losslessness over the fixed two-part decomposition.
        n = 3
        universe = list(range(1 << n))
        fd_universe = []
        for lhs_size in range(n):
            for lhs in itertools.combinations(range(n), lhs_size):
                lm = sum(1 << i for i in lhs)
                for a in range(n):
                    if not (lm >> a) & 1:
                        fd_universe.append((lm, 1 << a))
        fd_sets = [()]
        for size in (1, 2):
            fd_sets.extend(itertools.combinations(fd_universe, size))
        decomp = [{"A", "B"}, {"B", "C"}]

        checked = lossy_count = 0
        for spec in fd_sets:
            fd_input = [
                [names(ATTRS, lm), names(ATTRS, rb)] for lm, rb in spec
            ]
            s = AnalysisSession(ATTRS, [], decomp)
            t = s.begin_trial()
            if fd_input:
                view = s.add_fds(t.trial_id, fd_input)
            else:
                view = s.trial_view(t.trial_id)
            check_candidate_keys(ATTRS, fd_input,
                                 view.result.candidate_keys)
            expected = brute_force_lossless(ATTRS, fd_input, decomp)
            self.assertIs(
                view.result.lossless, expected,
                f"mismatch for {fd_input}",
            )
            if not expected:
                lossy_count += 1
                verify_counterexample(
                    ATTRS, fd_input, decomp,
                    view.result.counterexample,
                )
            # Every trial analysis commits cleanly and becomes version 2.
            self.assertEqual(s.commit(t.trial_id).version, 2)
            self.assertEqual(s.committed.lossless, expected)
            checked += 1
        # 12 non-trivial singleton-RHS FDs over {A,B,C}; 1 empty set +
        # 12 one-FD sets + C(12,2) = 79 cases (same universe as the
        # stateless exhaustive test).
        self.assertEqual(checked, 79)
        self.assertGreater(lossy_count, 20)


class SnapshotLifecycleTests(unittest.TestCase):
    def _snapshots(self, session):
        return session._base_snapshots

    def test_old_snapshot_kept_while_trial_open_then_dropped(self):
        s = AnalysisSession(
            ["A", "B"], [({"A"}, {"B"})], [{"A", "B"}]
        )
        t = s.begin_trial()
        self.assertEqual(set(self._snapshots(s)), {1})
        s.commit(t.trial_id)  # version 2; no open trial references 1
        self.assertEqual(set(self._snapshots(s)), {2})

    def test_old_snapshot_retained_for_open_stale_trial(self):
        s = AnalysisSession(
            ["A", "B"], [({"A"}, {"B"})], [{"A", "B"}]
        )
        ta = s.begin_trial()
        tb = s.begin_trial()
        s.commit(ta.trial_id)  # -> 2; tb still open against 1
        self.assertEqual(set(self._snapshots(s)), {1, 2})
        # tb's delta is still computed against its real base version 1.
        view = s.trial_view(tb.trial_id)
        self.assertEqual(view.base_version, 1)
        self.assertFalse(view.delta.changed)
        s.discard_trial(tb.trial_id)
        self.assertEqual(set(self._snapshots(s)), {2})

    def test_two_trials_on_same_base_share_one_snapshot(self):
        s = AnalysisSession(["A"], [], [{"A"}])
        t1 = s.begin_trial()
        t2 = s.begin_trial()
        self.assertEqual(s._snapshot_refs[1], 2)
        s.discard_trial(t1.trial_id)
        self.assertEqual(s._snapshot_refs[1], 1)
        s.discard_trial(t2.trial_id)
        self.assertNotIn(1, s._snapshot_refs)
        self.assertEqual(set(self._snapshots(s)), {1})  # current kept


if __name__ == "__main__":
    unittest.main()
