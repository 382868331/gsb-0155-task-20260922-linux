"""Fixed-input demo for fdlossless (offline, deterministic, no sleeps).

Prints:
  1. a lossless (but not dependency-preserving) analysis with the full,
     re-checkable chase log;
  2. a lossy analysis with its verified counterexample;
  3. one actually-triggered rejection at the input boundary.
"""

from fdlossless import InvalidInputError, analyze


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


if __name__ == "__main__":
    main()
