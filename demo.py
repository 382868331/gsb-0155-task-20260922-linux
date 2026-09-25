"""Fixed-input demo for fdlossless (offline, deterministic, no sleeps).

Prints:
  1. a lossless (but not dependency-preserving) analysis with the full,
     re-checkable chase log;
  2. a lossy analysis with its verified counterexample;
  3. one actually-triggered rejection at the input boundary;
  4. a TRIAL that removes a redundant FD: every decomposition property
     survives, then commit publishes the new in-memory version;
  5. a TRIAL that removes a critical FD: keys change and the join turns
     lossy with a freshly verified counterexample — rolled back, so the
     official version is untouched;
  6. two TRIALS racing to commit: exactly one wins, the stale one is
     refused, recomputed against the new version and retried.

Every number below is produced here by the real algorithms; nothing is
pre-recorded.  The whole run finishes in about a second.
"""

from fdlossless import (
    AnalysisVersionStore,
    InvalidInputError,
    StaleVersionError,
    analyze,
)


def show(result, indent="  "):
    print(f"{indent}attributes        : {result.attributes}")
    print(f"{indent}declared FDs      : {[str(f) for f in result.fds]}")
    print(f"{indent}minimal cover     : {[str(f) for f in result.minimal_cover]}")
    print(f"{indent}candidate keys    : {list(result.candidate_keys)}")
    for i, (sub, proj) in enumerate(zip(result.decomposition, result.projections)):
        print(f"{indent}projection on {sub}: {[str(f) for f in proj]}")
    print(f"{indent}dependency preserving: {result.dependency_preserving}")
    print(f"{indent}lossless join        : {result.lossless}")


def verify_counterexample(result, label):
    """Independent re-check of a lossy witness against the candidate FDs."""
    ce = result.counterexample
    assert ce is not None
    index = {a: j for j, a in enumerate(result.attributes)}
    # 1. The instance satisfies every candidate FD (pairwise check).
    for fd in result.fds:
        lhs_cols = [index[a] for a in fd.lhs]
        c = index[fd.rhs]
        for t1 in ce.instance:
            for t2 in ce.instance:
                if all(t1[x] == t2[x] for x in lhs_cols):
                    assert t1[c] == t2[c], f"witness violates {fd}"
    # 2. The spurious tuple is in the natural join of the projections ...
    for sub, proj in zip(result.decomposition, ce.projections):
        cols = [index[a] for a in sub]
        assert tuple(ce.spurious_tuple[c] for c in cols) in proj
    # 3. ... but not a tuple of the original instance.
    assert ce.spurious_tuple not in ce.instance
    print(f"  [{label}] counterexample independently re-checked: "
          f"{len(ce.instance)} tuples, spurious {ce.spurious_tuple}")


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
    verify_counterexample(r2, "case 2")

    print()
    print("=" * 72)
    print("CASE 3: boundary rejection — 13 attributes (limit is 12)")
    print("=" * 72)
    try:
        analyze([f"A{i}" for i in range(13)], [], [[f"A{i}" for i in range(13)]])
    except InvalidInputError as exc:
        print(f"  rejected as expected: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------
    # Versioned trial layer
    # ------------------------------------------------------------------
    print()
    print("=" * 72)
    print("CASE 4: trial removing a REDUNDANT FD — properties survive")
    print("=" * 72)
    # F = {ab: A->B, bc: B->C, ac: A->C}; ac is implied by ab,bc and is
    # not even in the minimal cover.  Decomposition {AB}, {BC}.
    store = AnalysisVersionStore(
        ["A", "B", "C"],
        [("ab", {"A"}, {"B"}), ("bc", {"B"}, {"C"}),
         ("ac", {"A"}, {"C"})],
        [{"A", "B"}, {"B", "C"}],
    )
    print(f"  committed version: {store.version}")
    show(store.current())
    out = store.open_trial(remove_fds=["ac"])
    print(f"  trial {out.trial_id} pinned to version {out.base_version}; "
          f"candidate now {[d.fd_id for d in out.candidate_fds]}")
    print(f"  diff empty? {out.diff.empty}  "
          f"(cover +/-: {[str(f) for f in out.diff.minimal_cover_added]} / "
          f"{[str(f) for f in out.diff.minimal_cover_removed]}, "
          f"keys +/-: {out.diff.candidate_keys_added} / "
          f"{out.diff.candidate_keys_removed}, "
          f"preserving changed: {out.diff.dependency_preserving_changed}, "
          f"lossless changed: {out.diff.lossless_changed})")
    print(f"  official version still {store.version} (trial is isolated)")
    receipt = store.commit(out.trial_id)
    print(f"  committed: version {receipt.previous_version} -> "
          f"{receipt.version}; declarations now "
          f"{[d.fd_id for d in store.declarations()]}")

    print()
    print("=" * 72)
    print("CASE 5: trial removing a CRITICAL FD — lossy witness, then rollback")
    print("=" * 72)
    # Start from {A->B, B->C} on {AB}, {AC}: lossless with sole key {A}.
    store2 = AnalysisVersionStore(
        ["A", "B", "C"],
        [("ab", {"A"}, {"B"}), ("bc", {"B"}, {"C"})],
        [{"A", "B"}, {"A", "C"}],
    )
    print(f"  committed version: {store2.version}; keys "
          f"{list(store2.current().candidate_keys)}; lossless "
          f"{store2.current().lossless}")
    out = store2.open_trial(remove_fds=["ab"])
    print(f"  trial {out.trial_id}: candidate FDs "
          f"{[str(f) for f in out.result.fds]}")
    print(f"  diff: keys removed {list(out.diff.candidate_keys_removed)}, "
          f"added {list(out.diff.candidate_keys_added)}; "
          f"lossless changed {out.diff.lossless_changed}")
    show(out.result)
    print(f"  chase used {len(out.result.chase_log)} merge(s) before "
          "terminating without an all-distinguished row")
    verify_counterexample(out.result, "case 5")
    store2.rollback(out.trial_id)
    print(f"  rolled back: official version still {store2.version}, "
          f"lossless still {store2.current().lossless}; "
          "the trial analysis is no longer usable")

    print()
    print("=" * 72)
    print("CASE 6: two trials compete — one commit wins, the stale one retries")
    print("=" * 72)
    store3 = AnalysisVersionStore(
        ["A", "B", "C"],
        [("ab", {"A"}, {"B"}), ("bc", {"B"}, {"C"})],
        [{"A", "B"}, {"A", "C"}],
    )
    t_a = store3.open_trial(remove_fds=["ab"])          # would turn lossy
    t_b = store3.open_trial(add_fds=[("ca", {"C"}, {"A"})])  # adds key C
    print(f"  trial A {t_a.trial_id}: remove ab -> lossless "
          f"{t_a.result.lossless}")
    print(f"  trial B {t_b.trial_id}: add C->A -> keys "
          f"{list(t_b.result.candidate_keys)}")
    win = store3.commit(t_b.trial_id)
    print(f"  trial B commits first: version {win.previous_version} -> "
          f"{win.version}")
    try:
        store3.commit(t_a.trial_id)
    except StaleVersionError as exc:
        print(f"  trial A refused: {type(exc).__name__} "
              f"(base {exc.expected_version}, current "
              f"{exc.current_version}); nothing published")
    store3.rollback(t_a.trial_id)
    retry = store3.open_trial(remove_fds=["ab"])
    print(f"  retry opened against version {retry.base_version}; "
          f"computed on the NEW candidate set {[d.fd_id for d in retry.candidate_fds]}")
    final = store3.commit(retry.trial_id)
    print(f"  retry commits: version {final.previous_version} -> "
          f"{final.version}; final keys "
          f"{list(store3.current().candidate_keys)}, lossless "
          f"{store3.current().lossless}")


if __name__ == "__main__":
    main()
