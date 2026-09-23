"""Fixed-input demo for fdlossless (offline, deterministic, no sleeps).

Prints:
  1. a lossless (but not dependency-preserving) analysis with the full,
     re-checkable chase log;
  2. a lossy analysis with its verified counterexample;
  3. one actually-triggered rejection at the input boundary;
  4. an optimal decomposition selected by total cost from candidates;
  5. an infeasible instance with the classified reason;
  6. an actually-triggered invalid-input rejection for a negative cost.
"""

from fdlossless import InvalidInputError, analyze, select_decomposition


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
    print("CASE 4: select_decomposition — cheapest feasible subset")
    print("R(A,B,C,D), F = {A->B, B->C}")
    print("candidates: AB cost 1, BC cost 1, ACD cost 10, AD cost 1")
    print("=" * 72)
    s4 = select_decomposition(
        ["A", "B", "C", "D"],
        [({"A"}, {"B"}), ({"B"}, {"C"})],
        [{"A", "B"}, {"B", "C"}, {"A", "C", "D"}, {"A", "D"}],
        [],
        [1, 1, 10, 1],
    )
    print(f"  feasible          : {s4.feasible}")
    print(f"  chosen            : {[list(p) for p in s4.chosen]}")
    print(f"  total cost        : {s4.total_cost}")
    print(f"  relations         : {s4.relation_count}")
    print(f"  subsets considered: {s4.subsets_considered}")
    print(f"  witness lossless  : {s4.analysis.lossless}")
    print(f"  witness preserving: {s4.analysis.dependency_preserving}")
    print("  hand check: {AB,AD,BC} cost 3 preserves A->B (on AB) and")
    print("  B->C (on BC); chase merges row AD with AB on A, then B->C")
    print("  forces column C, producing an all-distinguished row.")

    print()
    print("=" * 72)
    print("CASE 5: select_decomposition — infeasible, classified reason")
    print("R(A,B,C), F = {A->B, B->C}, candidates: AB, AC")
    print("=" * 72)
    s5 = select_decomposition(
        ["A", "B", "C"],
        [({"A"}, {"B"}), ({"B"}, {"C"})],
        [{"A", "B"}, {"A", "C"}],
        [],
        [1, 1],
    )
    print(f"  feasible: {s5.feasible}")
    print(f"  reason  : {s5.reason}")
    print("  hand check: {AB,AC} shares A and is lossless (A->B), but")
    print("  B->C is lost because B and C never share a relation.")

    print()
    print("=" * 72)
    print("CASE 6: select_decomposition — invalid input (negative cost)")
    print("=" * 72)
    try:
        select_decomposition(
            ["A", "B"],
            [({"A"}, {"B"})],
            [{"A"}, {"B"}],
            [],
            [1, -5],
        )
    except InvalidInputError as exc:
        print(f"  rejected as expected: {type(exc).__name__}: {exc}")


if __name__ == "__main__":
    main()
