"""Fixed-input demo for fdlossless (offline, deterministic, no sleeps).

Prints:
  1. a lossless (but not dependency-preserving) analysis with the full,
     re-checkable chase log;
  2. a lossy analysis with its verified counterexample;
  3. one actually-triggered rejection at the input boundary;
  4. a cost-optimal decomposition selection (normal case);
  5. a selection at the input boundary (6 attributes, 8 candidates);
  6. two actually-triggered selection failures: proven no-solution and
     an invalid cost.
"""

from fdlossless import (
    InvalidInputError,
    NoFeasibleDecomposition,
    analyze,
    select_decomposition,
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

    print()
    print("=" * 72)
    print("CASE 4: select the cheapest lossless, dependency-preserving split")
    print("  R(A,B,C), F = {A->B, B->C}")
    print("  candidates: AB(1), BC(1), AC(1), ABC(5); no required groups")
    print("=" * 72)
    s1 = select_decomposition(
        ["A", "B", "C"],
        [({"A"}, {"B"}), ({"B"}, {"C"})],
        [{"A", "B"}, {"B", "C"}, {"A", "C"}, {"A", "B", "C"}],
        [],
        [1, 1, 1, 5],
    )
    print(f"  chosen decomposition : {[list(s) for s in s1.chosen]}")
    print(f"  candidate indices    : {list(s1.chosen_indices)}")
    print(f"  total cost           : {s1.total_cost}")
    print(f"  re-check: lossless={s1.analysis.lossless}, "
          f"dependency_preserving={s1.analysis.dependency_preserving}")
    print("  ({A,B},{A,C} is cheaper-looking but loses B->C; {A,B,C} costs 5)")

    print()
    print("=" * 72)
    print("CASE 5: selection at the input boundary — 6 attributes, 8 candidates")
    print("  R(A..F), F = {A->B, B->C, C->D, D->E, E->F}, must keep {E,F}")
    print("=" * 72)
    s2 = select_decomposition(
        ["A", "B", "C", "D", "E", "F"],
        [({"A"}, {"B"}), ({"B"}, {"C"}), ({"C"}, {"D"}),
         ({"D"}, {"E"}), ({"E"}, {"F"})],
        [{"A", "B"}, {"B", "C"}, {"C", "D"}, {"D", "E"},
         {"E", "F"}, {"A", "B", "C"}, {"D", "E", "F"}, {"A", "F"}],
        [{"E", "F"}],
        [1, 1, 1, 1, 1, 3, 3, 2],
    )
    print(f"  chosen decomposition : {[list(s) for s in s2.chosen]}")
    print(f"  total cost           : {s2.total_cost}")
    print(f"  re-check: lossless={s2.analysis.lossless}, "
          f"dependency_preserving={s2.analysis.dependency_preserving}")
    print("  (the chain AB,BC,CD,DE,EF at cost 5 beats ABC+CD+DE+EF at 6)")

    print()
    print("=" * 72)
    print("CASE 6: actually-triggered selection failures")
    print("=" * 72)
    try:
        select_decomposition(
            ["A", "B", "C"],
            [({"B"}, {"C"})],
            [{"A", "B"}, {"A", "C"}],
            [],
            [1, 1],
        )
    except NoFeasibleDecomposition as exc:
        print(f"  no-solution verdict : {type(exc).__name__}: {exc}")
    try:
        select_decomposition(["A"], [], [{"A"}], [], [-3])
    except InvalidInputError as exc:
        print(f"  invalid input       : {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
