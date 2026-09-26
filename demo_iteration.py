"""Iteration demo: MVDs, lossless-join chase witness, and the 4NF check.

Everything below is computed live (no canned output, no sleeps) and finishes
in a few seconds:

  1. person/skill/language: why independent multivalued attributes force the
     cross combination, and the 4NF decomposition that the chase certifies;
  2. the same schema left undecomposed: the 4NF violations found, with the
     determinant/superkey verdict for each;
  3. a counterexample for the correlated case: when language actually depends
     on skill, the "independent" decomposition is lossy and the join invents
     tuples, shown on a small enumerated relation;
  4. two boundary behaviours: the 8-attribute MVD limit and chase budget
     exhaustion (reported as unfinished, never as lossy).
"""

from fdlossless import (
    InvalidInputError,
    SearchBudgetExhausted,
    analyze,
    implies_mvd,
)

ATTRS = ["person", "skill", "language"]
INDEPENDENT = [({"person"}, {"skill"}), ({"person"}, {"language"})]


def show_chase(result):
    for step in result.chase_log:
        print(f"    step {step.step} [{step.rule}]: {step.detail}")


def case_1():
    print("=" * 76)
    print("CASE 1: independent skills and languages need the cross combination")
    print("=" * 76)
    print("""
  If person p1 has skills {s1, s2} and languages {l1, l2} and the two are
  independent, the relation cannot store just two rows: projecting and
  re-joining would invent (p1, s1, l2) and (p1, s2, l1).  So the relation
  must list every skill x language combination:""")
    combos = [
        ("p1", s, l) for s in ("s1", "s2") for l in ("l1", "l2")
    ]
    for t in combos:
        print(f"    {t}")
    print(f"  -> {len(combos)} rows for 2 skills and 2 languages (cross product).")
    print()
    print("  This is exactly the MVD person ->> skill (and person ->> language).")
    print("  Implication checks, computed by the two-row chase:")
    print("    {person ->> skill} implies person ->> language:",
          implies_mvd(ATTRS, [], [({"person"}, {"skill"})], {"person"}, {"language"}))
    print("    {person ->> skill} implies skill ->> language:",
          implies_mvd(ATTRS, [], [({"person"}, {"skill"})], {"skill"}, {"language"}))
    print()
    print("  Decomposition {person,skill}, {person,language} under both MVDs:")
    r = analyze(
        ATTRS, [], [{"person", "skill"}, {"person", "language"}],
        MVDs=INDEPENDENT,
    )
    print(f"    lossless join : {r.lossless}   4NF: {r.four_nf}")
    print("    chase witness (row additions come from the MVDs):")
    show_chase(r)


def case_2():
    print()
    print("=" * 76)
    print("CASE 2: the undecomposed relation violates 4NF")
    print("=" * 76)
    r = analyze(ATTRS, [], [ATTRS], MVDs=INDEPENDENT)
    print(f"  4NF: {r.four_nf};  violations found: {len(r.four_nf_violations)}")
    for v in r.four_nf_violations:
        print(f"    relation {v.relation}: MVD {v.mvd}")
        print(f"      determinant is superkey: {v.determinant_is_superkey}")
        print(f"      {v.detail}")


def case_3():
    print()
    print("=" * 76)
    print("CASE 3: counterexample when skills and languages are actually related")
    print("=" * 76)
    print("""
  Suppose each skill is taught in exactly one language (FD skill -> language).
  Then skills and languages are NOT independent.  The enumerated relation
      r = {(p1, s1, l1), (p1, s2, l2)}
  satisfies skill -> language, but decomposing as if they were independent
  joins back into 4 rows: the spurious tuples (p1, s1, l2) and (p1, s2, l1)
  never existed.  The analyzer confirms the decomposition is lossy and
  certifies it with a verified counterexample instance:""")
    r = analyze(
        ATTRS,
        [({"skill"}, {"language"})],
        [{"person", "skill"}, {"person", "language"}],
        MVDs=[],
    )
    print(f"    lossless join: {r.lossless}   4NF: {r.four_nf}")
    ce = r.counterexample
    print(f"    counterexample instance (ints encode symbols): {list(ce.instance)}")
    for sub, proj in zip(r.decomposition, ce.projections):
        print(f"      projection on {sub}: {list(proj)}")
    print(f"    spurious tuple produced by the join: {ce.spurious_tuple}")
    print(f"    {ce.explanation}")


def case_4():
    print()
    print("=" * 76)
    print("CASE 4: boundary behaviours")
    print("=" * 76)
    attrs9 = [f"A{i}" for i in range(9)]
    try:
        analyze(attrs9, [], [attrs9], MVDs=[({"A0"}, {"A1"})])
    except InvalidInputError as exc:
        print(f"  9 attributes with MVDs rejected: {type(exc).__name__}: {exc}")
    try:
        analyze(
            ATTRS, [], [{"person", "skill"}, {"person", "language"}],
            MVDs=INDEPENDENT, step_budget=0,
        )
    except SearchBudgetExhausted as exc:
        print("  chase budget 0 -> unfinished, NOT a lossy verdict:")
        print(f"    {type(exc).__name__}: {exc}")


def main():
    case_1()
    case_2()
    case_3()
    case_4()


if __name__ == "__main__":
    main()
