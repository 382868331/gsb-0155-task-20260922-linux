"""Iteration demo: multivalued dependencies and 4NF decomposition checks.

Runs real computations (no prerecorded output, no sleeps) and prints:
  1. why independent skills/languages force a cross product, and the 4NF
     verdicts for the unsplit and the decomposed schemas;
  2. a negative witness: when skills and languages are correlated, the
     independence MVD fails and a finite counterexample relation proves it;
  3. one boundary case: an exhausted chase budget is reported as
     "unfinished", never as a lossy verdict.
"""

from fdlossless import (
    SearchBudgetExhausted,
    analyze4nf,
    check_mvd,
)


def show_events(log):
    for e in log:
        print(f"    step {e.step} [{e.kind}] {e.detail}")


def main():
    print("=" * 74)
    print("CASE 1: one person, independent skill set and language set")
    print("=" * 74)
    print("  R(P, S, L) with MVDs P ->> S and P ->> L.")
    print("  If person p has skills {s1, s2} and languages {l1, l2}, the MVDs")
    print("  force every skill x language combination to appear:")
    cross = [("p", s, l) for s in ("s1", "s2") for l in ("l1", "l2")]
    print(f"    instance = {cross}  (2 skills x 2 languages = 4 tuples)")
    print("  This cross product is exactly the redundancy 4NF removes.")

    r_unsplit = analyze4nf(
        ["P", "S", "L"], [],
        [({"P"}, {"S"}), ({"P"}, {"L"})],
        [{"P", "S", "L"}],
    )
    print(f"  unsplit decomposition [PSL]: is_4nf = {r_unsplit.is_4nf}")
    for v in r_unsplit.violations:
        print(f"    violation: {v.mvd} on {v.subrelation}; "
              f"determinant superkey = {v.determinant_superkey}")

    r_split = analyze4nf(
        ["P", "S", "L"], [],
        [({"P"}, {"S"}), ({"P"}, {"L"})],
        [{"P", "S"}, {"P", "L"}],
    )
    print(f"  decomposed into [PS, PL]: lossless = {r_split.lossless}, "
          f"is_4nf = {r_split.is_4nf}")
    print("  chase witness for the lossless join:")
    show_events(r_split.chase_log)

    print()
    print("=" * 74)
    print("CASE 2: correlated skills and languages — independence fails")
    print("=" * 74)
    print("  Now each skill is tied to one language: FD S -> L.")
    print("  Is P ->> S still implied? (i.e. are skills independent of languages?)")
    neg = check_mvd(["P", "S", "L"], [({"S"}, {"L"})], [], {"P"}, {"S"})
    print(f"  implied = {neg.implied}")
    print(f"  finite counterexample relation (P,S,L): {list(neg.counterexample)}")
    print("  It satisfies S -> L, but the two tuples agree on P while the")
    print("  swapped tuple is missing — independence does not hold, so no")
    print("  cross product is required and the MVD must not be asserted.")

    print()
    print("=" * 74)
    print("CASE 3 (boundary): exhausted chase budget is 'unfinished', not 'lossy'")
    print("=" * 74)
    kwargs = dict(
        FDs=[],
        MVDs=[({"P"}, {"S"})],
        decomposition=[{"P", "S"}, {"P", "L"}],
    )
    try:
        analyze4nf(["P", "S", "L"], step_budget=0, **kwargs)
    except SearchBudgetExhausted as exc:
        print(f"  step_budget=0 -> {type(exc).__name__}: {exc}")
    ok = analyze4nf(["P", "S", "L"], step_budget=100, **kwargs)
    print(f"  same input with step_budget=100 -> lossless = {ok.lossless}")


if __name__ == "__main__":
    main()

