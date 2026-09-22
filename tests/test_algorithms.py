"""
教科书用例 + 与独立参考实现 (tests/reference.py) 的交叉验证。

参考实现不导入 fd_analysis; 期望值全部由独立算法或人工给出。
"""

import unittest

from fd_analysis import analyze, FD, closure
from tests import reference as ref


def _ref_fds(fds):
    return [(frozenset(f["lhs"]), frozenset(f["rhs"])) for f in fds]


def _minimal_covers_equivalent(attrs, cover, ref_fds):
    """被测最小覆盖与原 F 互相蕴含 (用参考闭包检验)。"""
    cover_pairs = [(frozenset(f.lhs), frozenset(f.rhs)) for f in cover]
    # F 蕴含 cover 每条
    for lhs, rhs in cover_pairs:
        if not ref.implies(attrs, ref_fds, lhs, rhs):
            return False
    # cover 蕴含 F 每条 (右部逐属性)
    for lhs, rhs in ref_fds:
        for a in rhs:
            if not ref.implies(attrs, cover_pairs, lhs, {a}):
                return False
    return True


def _is_really_minimal(cover):
    """最小性: 右部单属性; 左部无冗余属性; 无冗余依赖 (用参考闭包)。"""
    pairs = [(set(f.lhs), set(f.rhs)) for f in cover]
    for lhs, rhs in pairs:
        if len(rhs) != 1:
            return False
        # 左部每个属性都必要
        for a in list(lhs):
            trial = [(set(l), set(r)) for l, r in pairs]
            if list(rhs)[0] in ref.armstrong_closure(
                "", trial, lhs - {a}
            ):
                return False
    # 每条依赖都不能被其余依赖推出
    for k, (lhs, rhs) in enumerate(pairs):
        others = [(set(l), set(r)) for i, (l, r) in enumerate(pairs)
                  if i != k]
        if list(rhs)[0] in ref.armstrong_closure("", others, lhs):
            return False
    return True


class TestClosureTool(unittest.TestCase):
    def test_textbook_closure(self):
        # 经典教材例子: A->BC, B->C, AB->D 下 A+ = ABCD?
        # A->BC 得 ABC, 再无新依赖推 D (AB->D 中 B 已在, 实际 D 可得):
        # A+ = ABC, 然后 AB->D => ABCD
        fds = [{"lhs": "A", "rhs": "BC"},
               {"lhs": "B", "rhs": "C"},
               {"lhs": "AB", "rhs": "D"}]
        self.assertEqual(closure("ABCD", "A", fds), "ABCD")

    def test_closure_matches_reference(self):
        fds = [{"lhs": "A", "rhs": "B"}, {"lhs": "CB", "rhs": "D"}]
        rf = _ref_fds(fds)
        got = closure("ABCD", "AC", fds)
        want = "".join(sorted(ref.armstrong_closure("ABCD", rf, "AC")))
        self.assertEqual(got, want)
        self.assertEqual(got, "ABCD")


class TestMinimalCover(unittest.TestCase):
    def test_removes_redundant_attribute(self):
        # AB->C, A->B => AB->C 的 B 是冗余左部属性 (A 自己能推 B 再推 C)
        fds = [{"lhs": "AB", "rhs": "C"}, {"lhs": "A", "rhs": "B"}]
        r = analyze("ABC", fds, ["ABC"])
        self.assertTrue(_minimal_covers_equivalent("ABC", r.minimal_cover,
                                                   _ref_fds(fds)))
        self.assertTrue(_is_really_minimal(r.minimal_cover))
        # 显式断言: A->C 必须出现, AB->C 不应保留
        cover = {(f.lhs, f.rhs) for f in r.minimal_cover}
        self.assertIn(("A", "C"), cover)
        self.assertNotIn(("AB", "C"), cover)

    def test_removes_redundant_fd(self):
        # A->B, B->C, A->C 中 A->C 冗余
        fds = [{"lhs": "A", "rhs": "B"},
               {"lhs": "B", "rhs": "C"},
               {"lhs": "A", "rhs": "C"}]
        r = analyze("ABC", fds, ["ABC"])
        self.assertTrue(_is_really_minimal(r.minimal_cover))
        self.assertEqual({(f.lhs, f.rhs) for f in r.minimal_cover},
                         {("A", "B"), ("B", "C")})

    def test_split_right_side(self):
        fds = [{"lhs": "A", "rhs": "BC"}]
        r = analyze("ABC", fds, ["ABC"])
        self.assertTrue(_is_really_minimal(r.minimal_cover))
        self.assertEqual({(f.lhs, f.rhs) for f in r.minimal_cover},
                         {("A", "B"), ("A", "C")})


class TestCandidateKeys(unittest.TestCase):
    def test_single_key(self):
        # A->BCD, key = A
        fds = [{"lhs": "A", "rhs": "BCD"}]
        r = analyze("ABCD", fds, ["ABCD"])
        self.assertEqual(r.candidate_keys, ["A"])

    def test_multiple_keys_sorted_by_size_then_lex(self):
        # A->C, B->C, CD->A ... 构造多键: 经典例子 R(ABCD),
        # AB->C, C->D, D->A: AB+ =ABCD; BC+ =BCDA; BD+ =BDAC
        # (CD+ =CDA 缺 B, 不是键) => 候选键 AB, BC, BD (大小再字典序)
        fds = [{"lhs": "AB", "rhs": "C"},
               {"lhs": "C", "rhs": "D"},
               {"lhs": "D", "rhs": "A"}]
        r = analyze("ABCD", fds, ["ABCD"])
        ref_keys = sorted(("".join(sorted(k)) for k in
                           ref.candidate_keys("ABCD", _ref_fds(fds))),
                          key=lambda s: (len(s), s))
        self.assertEqual(r.candidate_keys, ref_keys)
        self.assertEqual(r.candidate_keys, ["AB", "BC", "BD"])

    def test_no_fds_all_attributes_is_key(self):
        r = analyze("ABC", [], ["ABC"])
        self.assertEqual(r.candidate_keys, ["ABC"])

    def test_empty_lhs_gives_empty_key(self):
        fds = [{"lhs": "", "rhs": "A"}, {"lhs": "A", "rhs": "BC"}]
        r = analyze("ABC", fds, ["ABC"])
        self.assertEqual(r.candidate_keys, [""])

    def test_cyclic_fds(self):
        # 循环 A->B, B->A 在 R(AB) 上: A, B 都是键
        fds = [{"lhs": "A", "rhs": "B"}, {"lhs": "B", "rhs": "A"}]
        r = analyze("AB", fds, ["AB"])
        self.assertEqual(r.candidate_keys, ["A", "B"])


class TestDependencyPreservation(unittest.TestCase):
    def test_lossless_but_not_preserving(self):
        # 经典: R(ABC), A->B, 分解 {AB, BC} 无损但不保持?
        # {AB},{BC}: B->? 无。AB 上投影 A->B 保留。实际 A->B 被保持。
        # 改用标准反例: R(ABCD), A->B, B->C, 分解 {AB, BC, CD} ——
        # A->? A 只出现在 AB, 投影 A->B 有; B->C 在 BC 有 => 保持。
        # 教科书标准例子: R(ABCD), AB->C, C->D, D->A,
        # 分解 {AB(无C...)}; 采用 Ullman 例: R(A,B,C),
        # F={A->B,B->C} 分解 {AB, AC}:
        #   AB 投影 A->B; AC 投影 A->C (经 A->B->C); B->C 无法在局部检验
        #   => 不保持。chase: 行1 AB = a a b; 行2 AC = a b a。
        #   A->B: B 列 a,b => 提升为 a => 行1 全 a => 无损。
        fds = [{"lhs": "A", "rhs": "B"}, {"lhs": "B", "rhs": "C"}]
        r = analyze("ABC", fds, ["AB", "AC"])
        self.assertTrue(r.lossless)
        self.assertFalse(r.dependency_preserving)
        self.assertIsNone(r.counterexample)

    def test_preserving_but_lossy_is_possible(self):
        # 保持但有损: R(ABC), A->C, 分解 {AB, AC}? chase:
        # 行1 a a b; 行2 a b a。A->C: C 列 b,a => a; 行1 a a a => 无损。
        # 换: R(ABC), B->C, 分解 {AB, BC}?
        # 行1 a a b; 行2 b a a。B 列均 a => B->C 把 C 提升 => 行1 全 a 无损。
        # 经典保持但有损: R(ABC), A->C, 分解 {AB, BC}。
        # 行1 a a b; 行2 b b a。A 列 a,b 不同; B 列 a,b 不同 => chase 不动
        # => 有损。依赖: A->C 的属性被 AB∪BC 拆开, 局部投影没有 A->C
        # (BC 上无 A, AB 上无 C) => 不保持。两个都不满足。
        # 真正"保持但有损"的标准例: R(ABCD), F={A->B, C->D},
        # 分解 {AB, CD}: 两条 FD 分别落在两个子模式 => 保持;
        # chase 两行没有任何公共常量列 => 有损。
        fds = [{"lhs": "A", "rhs": "B"}, {"lhs": "C", "rhs": "D"}]
        r = analyze("ABCD", fds, ["AB", "CD"])
        self.assertTrue(r.dependency_preserving)
        self.assertFalse(r.lossless)
        self.assertIsNotNone(r.counterexample)

    def test_3nf_synthesis_both(self):
        # R(ABCD), F={A->B,B->C}, 分解 {AB,BC,AD}: 无损且保持
        fds = [{"lhs": "A", "rhs": "B"}, {"lhs": "B", "rhs": "C"}]
        r = analyze("ABCD", fds, ["AB", "BC", "AD"])
        self.assertTrue(r.lossless)
        self.assertTrue(r.dependency_preserving)


class TestChaseTrace(unittest.TestCase):
    def test_step_record_structure_and_equality_semantics(self):
        fds = [{"lhs": "A", "rhs": "B"}, {"lhs": "B", "rhs": "C"}]
        r = analyze("ABCD", fds, ["AB", "BC", "AD"])
        self.assertTrue(r.lossless)
        self.assertGreaterEqual(len(r.chase_steps), 2)
        for st in r.chase_steps:
            self.assertIn("fd", st)
            self.assertIn("matched_rows", st)
            self.assertIn("column", st)
            self.assertIn("equated", st)
            for e in st["equated"]:
                self.assertEqual(set(e), {"row", "column", "from", "to"})
        # 第一次等同必须是 A->B 把行2/行3 的 B 变量提升为常量 a2
        first = r.chase_steps[0]
        self.assertEqual(first["fd"], "A -> B")
        self.assertEqual(first["column"], "B")
        targets = {e["to"] for e in first["equated"]}
        self.assertEqual(targets, {"a2"})

    def test_lossy_chase_has_no_all_a_row_and_no_further_merge(self):
        # {AB},{CD} under A->B,C->D: 无等同发生, 直接判有损
        fds = [{"lhs": "A", "rhs": "B"}, {"lhs": "C", "rhs": "D"}]
        r = analyze("ABCD", fds, ["AB", "CD"])
        self.assertFalse(r.lossless)
        self.assertEqual(r.chase_steps, [])


class TestCounterexample(unittest.TestCase):
    def test_lossy_ab_cd_counterexample(self):
        fds = [{"lhs": "A", "rhs": "B"}, {"lhs": "C", "rhs": "D"}]
        r = analyze("ABCD", fds, ["AB", "CD"])
        ce = r.counterexample
        rf = _ref_fds(fds)
        self.assertTrue(ref.relation_satisfies(ce["relation"], rf))
        rel_keys = {tuple(t[c] for c in "ABCD") for t in ce["relation"]}
        g = tuple(ce["spurious_tuple"][c] for c in "ABCD")
        self.assertNotIn(g, rel_keys)
        joined = {tuple(t[c] for c in "ABCD") for t in ce["rejoined"]}
        self.assertIn(g, joined)
        # 两子模式各 2 行 => 笛卡尔积 4 行, 原关系 2 行 => 恰 2 个幽灵组合
        self.assertEqual(len(joined), 4)

    def test_counterexample_chain_with_partial_merges(self):
        # 有损但 chase 发生过部分等同: R(ABCD), A->B, 分解 {AB, CD}?
        # 那 C->? 无。构造: F={A->B, C->D}, subs={AC, BD}:
        # 行1 AC: a b a b; 行2 BD: b a b a —— 无公共列 => 无损? 有损无等同。
        # 需要有损且有等同: R(ABCD), F={A->B}, subs={AB, AC, D 覆盖}:
        # {AB, AC, AD}: A->B 把行2/3 的 B 提升 => 全 a 行出现 => 无损。
        # F={A->C}, subs={AB, CD}: 行1 a a b b; 行2 b b a a。
        # A 列不同; 无左部匹配 => 有损且零步。
        # 取 F={A->B, A->C}, subs={AB, CD, AD}(必须覆盖):
        # 行1 a a b b; 行2 b b a a; 行3 a b b a。
        # A->B: 行1,3 A=a => B: a,b => 提升行3 B 为 a。
        # A->C: 行1,3 => C: b,b 相同 (都是 b13? 行1 C=b13,行3 C=b33 不同变量!)
        #   二者 C 列符号不同 => 等同 b33->b13 (变量与变量合并)。
        # 之后 B 列: 行2=b23 仍 b; 无全常量行 => 有损, 且有真实等同记录。
        fds = [{"lhs": "A", "rhs": "B"}, {"lhs": "A", "rhs": "C"}]
        r = analyze("ABCD", fds, ["AB", "CD", "AD"])
        self.assertFalse(r.lossless)
        self.assertGreaterEqual(len(r.chase_steps), 1)
        ce = r.counterexample
        rf = _ref_fds(fds)
        self.assertTrue(ref.relation_satisfies(ce["relation"], rf))
        rel_keys = {tuple(t[c] for c in "ABCD") for t in ce["relation"]}
        g = tuple(ce["spurious_tuple"][c] for c in "ABCD")
        self.assertNotIn(g, rel_keys)
        joined = {tuple(t[c] for c in "ABCD") for t in ce["rejoined"]}
        self.assertIn(g, joined)


class TestCrossValidation(unittest.TestCase):
    """与独立参考实现的系统性交叉核对 (固定的小型场景集)。"""

    SCENARIOS = [
        # (attrs, fds, subs)
        ("ABC", [{"lhs": "A", "rhs": "B"}], ["AB", "AC"]),
        ("ABC", [{"lhs": "A", "rhs": "B"}, {"lhs": "B", "rhs": "C"}],
         ["AB", "AC"]),
        ("ABCD", [{"lhs": "A", "rhs": "B"}, {"lhs": "C", "rhs": "D"}],
         ["AB", "CD"]),
        ("ABCD", [{"lhs": "AB", "rhs": "C"}, {"lhs": "C", "rhs": "D"},
                  {"lhs": "D", "rhs": "A"}],
         ["AB", "BC", "CD"]),
        ("ABCD", [{"lhs": "", "rhs": "A"}, {"lhs": "A", "rhs": "BC"}],
         ["AB", "AC", "CD"]),
        ("ABC", [{"lhs": "A", "rhs": "B"}, {"lhs": "B", "rhs": "A"}],
         ["AB", "BC"]),
        ("ABCD", [{"lhs": "AB", "rhs": "CD"}, {"lhs": "C", "rhs": "A"}],
         ["ABC", "AD"]),
        ("ABC", [], ["AB", "BC"]),
        ("ABC", [], ["AB", "AC", "BC"]),
        ("ABCD", [{"lhs": "A", "rhs": "B"}, {"lhs": "B", "rhs": "C"},
                  {"lhs": "C", "rhs": "D"}],
         ["AB", "BC", "CD"]),
    ]

    def test_all_scenarios(self):
        for attrs, fds, subs in self.SCENARIOS:
            with self.subTest(attrs=attrs, fds=fds, subs=subs):
                r = analyze(attrs, fds, subs)
                rf = _ref_fds(fds)
                # 无损性
                ref_lossless, _ = ref.chase_lossless(attrs, rf, subs)
                self.assertEqual(r.lossless, ref_lossless)
                # 依赖保持
                self.assertEqual(r.dependency_preserving,
                                 ref.dependency_preserving(attrs, rf, subs))
                # 候选键 (集合相同, 被测结果另需满足排序契约)
                ref_keys = sorted(
                    ("".join(sorted(k)) for k in
                     ref.candidate_keys(attrs, rf)),
                    key=lambda s: (len(s), s))
                self.assertEqual(r.candidate_keys, ref_keys)
                # 最小覆盖等价 + 真正最小
                self.assertTrue(_minimal_covers_equivalent(
                    attrs, r.minimal_cover, rf))
                self.assertTrue(_is_really_minimal(r.minimal_cover))
                # 有损时反例关系满足 F 且连接产生幽灵元组
                if not r.lossless:
                    ce = r.counterexample
                    self.assertTrue(ref.relation_satisfies(ce["relation"], rf))
                    self.assertTrue(
                        ref.has_spurious(ce["relation"], attrs, subs))
                    rel_keys = {tuple(t[c] for c in attrs)
                                for t in ce["relation"]}
                    self.assertNotIn(
                        tuple(ce["spurious_tuple"][c] for c in attrs),
                        rel_keys)

    def test_projected_fds_are_sound_and_complete_enough(self):
        # 投影依赖: 每条必须被原 F 蕴含且属性在子模式内;
        # 且其全体足以推出子模式上全部成立的非平凡单右部 FD (完备性)。
        attrs = "ABCD"
        fds = [{"lhs": "A", "rhs": "B"}, {"lhs": "B", "rhs": "C"}]
        subs = ["AB", "BC", "AD"]
        r = analyze(attrs, fds, subs)
        rf = _ref_fds(fds)
        for si, sa in enumerate(subs):
            projected = [(frozenset(f.lhs), frozenset(f.rhs))
                         for f in r.projections[si]]
            # 可靠性
            for lhs, rhs in projected:
                self.assertTrue(lhs <= set(sa) and rhs <= set(sa))
                self.assertTrue(ref.implies(attrs, rf, lhs, rhs))
            # 完备性: 枚举 sa 的全部 X,A, F 蕴含 X->A 时投影也要蕴含
            from itertools import combinations as comb
            proj_list = [(set(l), set(x)) for l, x in projected]
            for size in range(len(sa) + 1):
                for xc in comb(sorted(sa), size):
                    x = set(xc)
                    cl = ref.armstrong_closure(attrs, rf, x) & set(sa)
                    for a in cl - x:
                        self.assertIn(
                            a,
                            ref.armstrong_closure("", proj_list, x))


if __name__ == "__main__":
    unittest.main()
