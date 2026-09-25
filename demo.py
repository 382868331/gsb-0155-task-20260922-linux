"""Fixed-input demo for fdlossless (offline, deterministic, no sleeps).

Prints:
  1. a lossless (but not dependency-preserving) analysis with the full,
     re-checkable chase log;
  2. a lossy analysis with its verified counterexample;
  3. one actually-triggered rejection at the input boundary;
  4. the what-if trial workflow:
     4a. revoking a REDUNDANT FD changes no decomposition property;
     4b. revoking a CRITICAL FD turns the join lossy (counterexample);
     4c. two trials compete to commit — the stale one is rejected.

Everything shown is computed on the spot by the package; no results are
pre-recorded.  Inputs are tiny on purpose (at most 3 attributes; the
engine's hard limit is 12 and the chase merge budget defaults to
100000) — this is a behaviour demo, not a stress test.
"""

from fdlossless import (
    AnalysisSession,
    InvalidInputError,
    SearchBudgetExhausted,
    StaleVersionError,
    analyze,
)


def show(result):
    print(f"  attributes        : {result.attributes}")
    print(f"  minimal cover     : {[str(f) for f in result.minimal_cover]}")
    print(f"  candidate keys    : {list(result.candidate_keys)}")
    for i, (sub, proj) in enumerate(
        zip(result.decomposition, result.projections)
    ):
        print(f"  projection on {sub}: {[str(f) for f in proj]}")
    print(f"  dependency preserving: {result.dependency_preserving}")
    print(f"  lossless join        : {result.lossless}")


def show_delta(view):
    d = view.delta
    print(f"  (delta vs committed version {d.base_version})")
    print(f"    declarations +: {[str(x) for x in d.declared_added]}")
    print(f"    declarations -: {[str(x) for x in d.declared_removed]}")
    print(f"    cover + / -  : {[str(x) for x in d.minimal_cover_added]} / "
          f"{[str(x) for x in d.minimal_cover_removed]}")
    print(f"    keys  + / -  : {list(d.candidate_keys_added)} / "
          f"{list(d.candidate_keys_removed)}")
    print(f"    preserving   : {d.dependency_preserving_old} -> "
          f"{d.dependency_preserving_new}")
    print(f"    lossless     : {d.lossless_old} -> {d.lossless_new}")


def main():
    print("=" * 72)
    print("CASE 1: R(A,B,C), F = {A->B, B->C}, decomposition {A,B}, {A,C}")
    print("=" * 72)
    r1 = analyze(
        ["A", "B", "C"],
        [({"A"}, {"B"}), ({"B"}, {"C"})],
        [{"A", "B"}, {"A", "C"}],
    )
    show(r1)
    print("  chase log (re-checkable step by step):")
    print("    initial tableau: row1 = (A, B, b1_3), row2 = (A, b2_2, C)")
    for step in r1.chase_log:
        print(f"    step {step.step}: {step.detail}")
    print("    final row 1 is all distinguished symbols -> lossless")

    print()
    print("=" * 72)
    print("CASE 2: R(A,B,C), F = {A->C}, decomposition {A,B}, {B,C}  (lossy)")
    print("=" * 72)
    r2 = analyze(
        ["A", "B", "C"],
        [({"A"}, {"C"})],
        [{"A", "B"}, {"B", "C"}],
    )
    show(r2)
    ce = r2.counterexample
    print(f"  counterexample instance r = {list(ce.instance)}")
    for sub, proj in zip(r2.decomposition, ce.projections):
        print(f"    projection on {sub}: {list(proj)}")
    print(f"  spurious tuple t = {ce.spurious_tuple}")
    print(f"  explanation: {ce.explanation}")

    print()
    print("=" * 72)
    print("CASE 3: boundary rejection — 13 attributes (limit is 12)")
    print("=" * 72)
    try:
        analyze([f"A{i}" for i in range(13)], [], [[f"A{i}" for i in range(13)]])
    except InvalidInputError as exc:
        print(f"  rejected as expected: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # What-if trials
    # ------------------------------------------------------------------
    print()
    print("=" * 72)
    print("CASE 4a: revoking a REDUNDANT FD — every property stays the same")
    print("=" * 72)
    # F = {A->B, B->C, A->C}; A->C (#2) is derivable as A->B->C.
    session = AnalysisSession(
        ["A", "B", "C"],
        [({"A"}, {"B"}), ({"B"}, {"C"}), ({"A"}, {"C"})],
        [{"A", "B"}, {"B", "C"}],
    )
    print(f"  committed version {session.version}; declarations:")
    for d in session.committed_declarations:
        print(f"    {d}")
    show(session.committed)
    trial = session.begin_trial()
    after = session.remove_fds(trial.trial_id, [2])  # revoke A->C by id
    show_delta(after)
    print("  cover/keys/preserving/lossless identical to the committed "
          "analysis:")
    same = (
        after.result.minimal_cover == session.committed.minimal_cover
        and after.result.candidate_keys == session.committed.candidate_keys
        and after.result.projections == session.committed.projections
        and after.result.dependency_preserving
        == session.committed.dependency_preserving
        and after.result.lossless == session.committed.lossless
    )
    print(f"    unchanged = {same}")
    outcome = session.commit(trial.trial_id)
    print(f"  committed -> version {outcome.version}")

    print()
    print("=" * 72)
    print("CASE 4b: revoking a CRITICAL FD — lossless turns lossy, witnessed")
    print("=" * 72)
    # Fresh session on the classic chain; B->C is the FD the chase needs.
    session2 = AnalysisSession(
        ["A", "B", "C"],
        [({"A"}, {"B"}), ({"B"}, {"C"})],
        [{"A", "B"}, {"B", "C"}],
    )
    print(f"  committed: lossless={session2.committed.lossless}, "
          f"keys={list(session2.committed.candidate_keys)}")
    trial2 = session2.begin_trial()
    after2 = session2.remove_fds(trial2.trial_id, [1])  # revoke B->C
    show_delta(after2)
    print(f"  new keys: {list(after2.result.candidate_keys)}")
    ce2 = after2.result.counterexample
    print(f"  lossy counterexample instance r = {list(ce2.instance)}")
    for sub, proj in zip(after2.result.decomposition, ce2.projections):
        print(f"    projection on {sub}: {list(proj)}")
    print(f"  spurious tuple t = {ce2.spurious_tuple}  "
          "(in the join of projections, not in r)")
    # Independently re-check the witness right here in the demo.
    idx = {a: i for i, a in enumerate(after2.result.attributes)}
    ok_fds = all(
        t1[idx["A"]] != t2[idx["A"]] or t1[idx["B"]] == t2[idx["B"]]
        for t1 in ce2.instance for t2 in ce2.instance
    )
    print("  witness satisfies surviving FD A->B (pairs equal on A agree "
          f"on B): {ok_fds}")
    print("  trial discarded (no commit): committed version stays "
          f"{session2.version}, lossless={session2.committed.lossless}")
    session2.discard_trial(trial2.trial_id)

    print()
    print("=" * 72)
    print("CASE 4c: two trials compete to commit — stale version rejected")
    print("=" * 72)
    session3 = AnalysisSession(
        ["A", "B", "C"],
        [({"A"}, {"B"}), ({"B"}, {"C"})],
        [{"A", "B"}, {"B", "C"}],
    )
    ta = session3.begin_trial()
    tb = session3.begin_trial()
    session3.add_fds(ta.trial_id, [({"C"}, {"A"})])   # candidate: + C->A
    session3.add_fds(tb.trial_id, [({"C"}, {"B"})])   # candidate: + C->B
    print("  trial A adds C->A; trial B adds C->B; both based on version 1")
    win = session3.commit(ta.trial_id, expected_version=1)
    print(f"  trial A commits -> version {win.version}; committed FDs: "
          f"{[str(f) for f in session3.committed.fds]}")
    try:
        session3.commit(tb.trial_id)
    except StaleVersionError as exc:
        print(f"  trial B commit rejected as expected: "
              f"{type(exc).__name__}")
        print(f"    {exc}")
    loser = session3.trial_view(tb.trial_id)
    print(f"  losing trial still open={loser.open} and inspectable; "
          "committed analysis unchanged:")
    print(f"    C->B present? {any(str(f) == 'C -> B' for f in session3.committed.fds)}")
    session3.discard_trial(tb.trial_id)

    print()
    print("=" * 72)
    print("CASE 4d: budget exhaustion rejects a mutation, nothing half-done")
    print("=" * 72)
    session4 = AnalysisSession(
        ["A", "B", "C"], [],
        [{"A", "B"}, {"B", "C"}], step_budget=0,
    )
    t4 = session4.begin_trial()
    try:
        session4.add_fds(t4.trial_id, [({"A"}, {"B"}), ({"B"}, {"C"})])
    except SearchBudgetExhausted as exc:
        print(f"  add rejected: {type(exc).__name__} (not a lossy verdict)")
    live = session4.trial_view(t4.trial_id)
    print(f"  trial declarations after failure: "
          f"{[str(d) for d in live.declared_fds]} (empty — nothing applied)")
    session4.discard_trial(t4.trial_id)


if __name__ == "__main__":
    main()
