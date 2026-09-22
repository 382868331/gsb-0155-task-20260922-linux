"""
穷举交叉验证 (n=3 全量)。

  * 枚举全部 4096 个非平凡单右部 FD 集合 × 109 个属性覆盖分解 (446464 场景);
  * 每个场景: 被测 analyze 与参考 union-find chase / 分布闭包 / 候选键比对;
  * 二值关系穷举预言机 (tests/reference.py) 对全部有损场景双向核对
    "有损 <=> 存在满足 F 但连接产生幽灵元组的真实关系";
  * 每个有损场景都验证被测自带 tableau 反例 (关系满足 F、幽灵元组在连接中
    且不在 r 中)。

参考实现不导入 fd_analysis, 期望值不来自被测核心。
"""

import unittest
from itertools import combinations

from fd_analysis import analyze
from tests import reference as ref
from tests.test_algorithms import (
    _minimal_covers_equivalent,
    _is_really_minimal,
)


ATTRS = "ABC"


def _all_fd_sets():
    """n=3 的全部非平凡单右部 FD: (lhs_frozenset, rhs_frozenset), 共 12 条。"""
    singles = []
    for size in range(0, len(ATTRS) + 1):
        for comb in combinations(ATTRS, size):
            lhs = frozenset(comb)
            for a in ATTRS:
                if a not in lhs:
                    singles.append((lhs, frozenset([a])))
    assert len(singles) == 12
    for mask in range(1 << len(singles)):
        yield [singles[i] for i in range(len(singles)) if mask >> i & 1]


def _all_covers():
    """全部'属性集合覆盖' (子模式集合, 顺序无关去重)。"""
    subsets = []
    for size in range(1, len(ATTRS) + 1):
        for comb in combinations(ATTRS, size):
            subsets.append(frozenset(comb))
    covers = []
    full = frozenset(ATTRS)
    for mask in range(1, 1 << len(subsets)):
        chosen = [subsets[i] for i in range(len(subsets)) if mask >> i & 1]
        if frozenset().union(*chosen) == full:
            covers.append(chosen)
    return covers


def _to_input(fds, cover):
    fd_in = [{"lhs": "".join(sorted(l)), "rhs": "".join(sorted(r))}
             for l, r in fds]
    subs = ["".join(sorted(s)) for s in
            sorted(cover, key=lambda x: (len(x), "".join(sorted(x))))]
    return fd_in, subs


class TestExhaustiveN3(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fd_sets = list(_all_fd_sets())
        cls.covers = _all_covers()
        cls.all_count = len(cls.fd_sets) * len(cls.covers)

    def test_exhaustive_structure_and_semantics(self):
        """
        单次遍历全部 446464 个场景:
          1. 无损性/保持性/候选键与独立实现零分歧;
          2. 候选键满足 (长度, 字典序) 排序契约;
          3. 对每个有损场景: 二值穷举必找到真实反例关系, 且被测自带
             tableau 反例的每条性质 (满足 F、幽灵元组在连接中且不在 r) 成立。
        反方向 (无损 => 无反例) 由 chase 定理与两份独立 chase 互证保证。
        """
        checked = 0
        lossy = 0
        for fds in self.fd_sets:
            for cover in self.covers:
                fd_in, subs = _to_input(fds, cover)
                sub_sets = [set(s) for s in subs]
                r = analyze(ATTRS, fd_in, subs)
                lossless, _ = ref.chase_lossless(ATTRS, fds, subs)
                preserving = ref.dependency_preserving(
                    ATTRS, fds, sub_sets)
                self.assertEqual(r.lossless, lossless,
                                 f"lossless 分歧: {fd_in} / {subs}")
                self.assertEqual(r.dependency_preserving, preserving,
                                 f"保持性分歧: {fd_in} / {subs}")
                ref_keys = sorted(
                    ("".join(sorted(k)) for k in
                     ref.candidate_keys(ATTRS, fds)),
                    key=lambda s: (len(s), s))
                self.assertEqual(r.candidate_keys, ref_keys)
                self.assertEqual(
                    r.candidate_keys,
                    sorted(r.candidate_keys, key=lambda s: (len(s), s)))

                # 最小覆盖: 与 F 等价且真正最小 (右单属性/左部无冗余/无冗余 FD)
                self.assertTrue(
                    _minimal_covers_equivalent(ATTRS, r.minimal_cover, fds),
                    f"最小覆盖不等价: {fd_in}")
                self.assertTrue(
                    _is_really_minimal(r.minimal_cover),
                    f"覆盖非最小: {fd_in}: {r.minimal_cover}")

                # 每个投影: 可靠 (被 F 蕴含) 且完备 (推出子模式上全部 X+\\X)
                for si, sa in enumerate(subs):
                    plist = [(set(f.lhs), set(f.rhs))
                             for f in r.projections[si]]
                    for l, rr in plist:
                        self.assertTrue(l <= set(sa) and rr <= set(sa))
                        self.assertTrue(ref.implies(ATTRS, fds, l, rr))
                    for size in range(len(sa) + 1):
                        for xc in combinations(sorted(sa), size):
                            x = set(xc)
                            cl = ref.armstrong_closure(
                                ATTRS, fds, x) & set(sa)
                            for a in cl - x:
                                self.assertIn(
                                    a,
                                    ref.armstrong_closure(ATTRS, plist, x),
                                    f"投影不完备 {fd_in} / R({sa}) / {x}->{a}")
                checked += 1

                if r.lossless:
                    continue
                lossy += 1
                bin_rel = ref.binary_counterexample(ATTRS, fds, sub_sets)
                self.assertIsNotNone(
                    bin_rel, f"有损但二值域无反例: {fd_in}/{subs}")
                self.assertTrue(ref.relation_satisfies(bin_rel, fds))
                self.assertTrue(ref.has_spurious(bin_rel, ATTRS, sub_sets))
                ce = r.counterexample
                self.assertTrue(ref.relation_satisfies(ce["relation"], fds))
                self.assertTrue(
                    ref.has_spurious(ce["relation"], ATTRS, sub_sets))
                rel_keys = {tuple(t[c] for c in ATTRS)
                            for t in ce["relation"]}
                self.assertNotIn(
                    tuple(ce["spurious_tuple"][c] for c in ATTRS),
                    rel_keys)
        self.assertEqual(checked, self.all_count)
        self.assertGreater(lossy, 10000)  # 确认覆盖海量有损场景  # 确认覆盖海量有损场景

    def test_specific_required_cases_present_in_enumeration(self):
        # 题面点名的情形都必须在 n=3 空间中实际出现并被正确判定
        found = {"cyclic": 0, "empty_lhs": 0, "redundant_attr": 0,
                 "lossless_not_preserving": 0}
        for fds in self.fd_sets:
            fd_map = {(l, next(iter(r))) for l, r in fds}
            if {(frozenset("A"), "B"), (frozenset("B"), "A")} <= fd_map:
                found["cyclic"] += 1
            if any(not l for l, _ in fds):
                found["empty_lhs"] += 1
            if (frozenset("AB"), "C") in fd_map and \
                    (frozenset("A"), "B") in fd_map:
                found["redundant_attr"] += 1
        r = analyze(ATTRS,
                    [{"lhs": "A", "rhs": "B"},
                     {"lhs": "B", "rhs": "C"}],
                    ["AB", "AC"])
        if r.lossless and not r.dependency_preserving:
            found["lossless_not_preserving"] = 1
        self.assertTrue(all(v > 0 for v in found.values()), found)


if __name__ == "__main__":
    unittest.main()
