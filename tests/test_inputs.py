"""输入校验与错误语义测试。"""

import math
import unittest

from fd_analysis import analyze, InputError, BudgetExhaustedError, MAX_ATTRIBUTES


class TestAttributes(unittest.TestCase):
    def test_basic_ok(self):
        r = analyze("AB", [{"lhs": "A", "rhs": "B"}], ["AB"])
        self.assertEqual(r.attributes, "AB")

    def test_iterable_of_chars_ok(self):
        r = analyze(["A", "B"], [("A", "B")], [["A"], ["B"]])
        self.assertEqual(r.attributes, "AB")

    def test_empty_relations_rejected(self):
        with self.assertRaisesRegex(InputError, "至少包含"):
            analyze("", [], ["A"])

    def test_too_many_attributes(self):
        attrs = "ABCDEFGHIJKLM"  # 13 个
        with self.assertRaisesRegex(InputError, "超过上限"):
            analyze(attrs, [], [attrs])

    def test_boundary_12_accepted(self):
        attrs = "ABCDEFGHIJKL"
        r = analyze(attrs, [], [attrs])
        self.assertEqual(len(r.attributes), MAX_ATTRIBUTES)

    def test_duplicate_attribute_rejected(self):
        with self.assertRaisesRegex(InputError, "重复属性"):
            analyze("AAB", [], ["AB"])

    def test_bad_char_rejected(self):
        with self.assertRaisesRegex(InputError, "ASCII"):
            analyze("A中", [], ["A中"])

    def test_wrong_element_type(self):
        with self.assertRaisesRegex(InputError, "单字符"):
            analyze(["A", 1], [], ["A1"])


class TestFDs(unittest.TestCase):
    def test_unknown_attr(self):
        with self.assertRaisesRegex(InputError, "不在关系模式"):
            analyze("AB", [{"lhs": "A", "rhs": "C"}], ["AB"])

    def test_empty_rhs_rejected(self):
        with self.assertRaisesRegex(InputError, "右部不能为空"):
            analyze("AB", [{"lhs": "A", "rhs": ""}], ["AB"])

    def test_trivial_fd_rejected(self):
        with self.assertRaisesRegex(InputError, "平凡部分"):
            analyze("ABC", [{"lhs": "AB", "rhs": "A"}], ["ABC"])
        with self.assertRaisesRegex(InputError, "平凡部分"):
            analyze("ABC", [{"lhs": "AB", "rhs": "BA"}], ["ABC"])

    def test_partially_overlapping_fd_rejected(self):
        # AB -> BC 含平凡部分 AB -> B; 不静默删, 定位拒绝
        with self.assertRaisesRegex(InputError, r"fds\[0\].*B"):
            analyze("ABC", [{"lhs": "AB", "rhs": "BC"}], ["ABC"])

    def test_empty_lhs_allowed(self):
        # 空决定项是合法的 (∅ -> A), None 与 "" 同义
        r = analyze("AB", [{"lhs": "", "rhs": "A"}], ["A", "B"])
        self.assertIn("∅", " ".join(str(f) for f in r.minimal_cover))
        r2 = analyze("AB", [{"lhs": None, "rhs": "A"}], ["A", "B"])
        self.assertEqual(r2.lossless, r.lossless)

    def test_dup_attr_inside_fd_rejected(self):
        with self.assertRaisesRegex(InputError, "集合内属性重复"):
            analyze("ABC", [{"lhs": "AA", "rhs": "B"}], ["ABC"])

    def test_malformed_fd(self):
        with self.assertRaisesRegex(InputError, "lhs"):
            analyze("AB", [{"rhs": "B"}], ["AB"])
        with self.assertRaisesRegex(InputError, r"必须是"):
            analyze("AB", [("A", "B", "C")], ["AB"])

    def test_fds_must_be_iterable_of_fds(self):
        with self.assertRaises(InputError):
            analyze("AB", "A->B", ["AB"])

    def test_wrong_numeric_types_rejected(self):
        # 接口没有浮点参数; NaN/Infinity 与普通 float 一样按类型拒绝
        for bad in (math.nan, math.inf, -math.inf, 1.0):
            with self.assertRaisesRegex(InputError, "int"):
                analyze("AB", [{"lhs": "A", "rhs": "B"}], ["AB"],
                        max_chase_steps=bad)

    def test_bool_rejected_as_int(self):
        # bool 虽是 int 子类, 契约明确拒绝
        for bad in (True, False):
            with self.assertRaisesRegex(InputError, "int"):
                analyze("AB", [{"lhs": "A", "rhs": "B"}], ["AB"],
                        max_chase_steps=bad)

    def test_bad_budget_value(self):
        with self.assertRaisesRegex(InputError, "正整数"):
            analyze("AB", [{"lhs": "A", "rhs": "B"}], ["AB"],
                    max_chase_steps=0)


class TestDecomposition(unittest.TestCase):
    def test_must_cover_all_attributes(self):
        with self.assertRaisesRegex(InputError, "未覆盖属性"):
            analyze("ABC", [{"lhs": "A", "rhs": "B"}], ["AB"])

    def test_empty_subschema(self):
        with self.assertRaisesRegex(InputError, "不能为空"):
            analyze("AB", [], ["A", ""])

    def test_duplicate_subschema(self):
        with self.assertRaisesRegex(InputError, "重复的子关系模式"):
            analyze("AB", [], ["A", "A", "B"])

    def test_empty_decomposition(self):
        with self.assertRaisesRegex(InputError, "至少需要"):
            analyze("AB", [], [])

    def test_not_iterable(self):
        with self.assertRaises(InputError):
            analyze("AB", [], None)


class TestBudgetSeparation(unittest.TestCase):
    def test_budget_exhaustion_is_distinct_from_proof(self):
        # R(ABC D), A->B, B->C, 分解 {AB, AC, AD}: chase 恰好需要 2 次等同
        # (A->B 提升 B 列后, B->C 才能提升 C 列)。预算 1 必须明确报错,
        # 而不是静默返回有损/无损。
        kwargs = dict(
            attributes="ABCD",
            fds=[{"lhs": "A", "rhs": "B"}, {"lhs": "B", "rhs": "C"}],
            decomposition=["AB", "AC", "AD"],
        )
        r_full = analyze(**kwargs)
        self.assertTrue(r_full.lossless)
        n_steps = len(r_full.chase_steps)
        self.assertGreaterEqual(n_steps, 2)
        with self.assertRaises(BudgetExhaustedError):
            analyze(**kwargs, max_chase_steps=n_steps - 1)
        # 预算足够时正常返回
        r_ok = analyze(**kwargs, max_chase_steps=n_steps)
        self.assertTrue(r_ok.lossless)


if __name__ == "__main__":
    unittest.main()
