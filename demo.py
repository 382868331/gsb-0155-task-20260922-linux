#!/usr/bin/env python3
"""
demo —— fd_analysis 固定输入演示 (真实计算, 无预录结果, 无网络)。

运行: python demo.py   (Python 3.14, 仅标准库; 计算量为常数级, 约 1 秒内)

内容:
  1. 正常结果: R(ABCD), F={A->B, B->C}, 分解 {AB, BC, AD}
     打印最小覆盖/候选键/各子模式投影依赖/依赖保持判定, 以及可手工复验的
     符号表 chase 全过程 (初始表 -> 逐步等同 -> 最终表) 与结论。
  2. 有损分解 R(AB), 分解 {A},{B} 的 chase 反例。
  3. 实际触发的错误边界: 属性超过 12 个上限; bool 冒充 int 预算参数。
"""

from fd_analysis import analyze, InputError, BudgetExhaustedError


def fmt_table(rows, attrs):
    width = max(4, max((len(s) for row in rows for s in row), default=4))
    head = "     " + " ".join(f"{a:^{width}}" for a in attrs)
    line = "     " + "-" * (len(attrs) * (width + 1))
    out = [head, line]
    for i, row in enumerate(rows):
        out.append(f"r{i + 1:<3} " + " ".join(f"{s:^{width}}" for s in row))
    return "\n".join(out)


def section(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def demo_normal():
    section("1. 正常分析: R(ABCD), F={A -> B, B -> C}, 分解 {AB, BC, AD}")
    attrs = "ABCD"
    fds = [{"lhs": "A", "rhs": "B"}, {"lhs": "B", "rhs": "C"}]
    decomp = ["AB", "BC", "AD"]
    r = analyze(attrs, fds, decomp)

    print(f"关系模式      : R({r.attributes})")
    print("给定 FD       : A -> B, B -> C")
    print(f"分解          : " + ", ".join(f"R{i + 1}({s})"
                                          for i, s in enumerate(decomp)))
    print(f"最小覆盖      : " + ", ".join(str(f) for f in r.minimal_cover))
    print(f"候选键        : {', '.join(r.candidate_keys)}"
          f"  (按长度再字典序)")
    for i, proj in enumerate(r.projections):
        print(f"F 在 R{i + 1}({decomp[i]}) 上的投影: "
              + (", ".join(str(f) for f in proj) if proj else "(无非平凡依赖)"))
    print(f"依赖保持      : {r.dependency_preserving}")
    print(f"无损连接(chase): {r.lossless}")

    print("\n符号表约定: a_j 是第 j 列的常量符号 (该列属于该子模式时放置),")
    print("           b_ij 是只属于第 i 行第 j 列的变量符号。")
    print("\n初始符号表:")
    print(fmt_table(r.initial_table, r.attributes))

    for k, st in enumerate(r.chase_steps, 1):
        print(f"\n[chase 第 {k} 步] 应用 {st['fd']}")
        print(f"  左部取值相同的行: {st['matched_rows']}")
        for e in st["equated"]:
            print(f"  行 {e['row'] + 1} 在列 {e['column']} 上: "
                  f"{e['from']} == {e['to']}  (全局合并这两个符号)")

    print("\n最终符号表:")
    print(fmt_table(r.final_table, r.attributes))
    arow = next((i + 1 for i, row in enumerate(r.final_table)
                 if all(s.startswith("a") for s in row)), None)
    print(f"\n结论: 第 {arow} 行全列为常量 a_j => 分解无损 (lossless join)。")
    print(f"同时两条原始 FD 均落在某子模式的投影中 => 依赖保持 = True。")
    assert r.lossless and r.dependency_preserving


def demo_lossy():
    section("2. 有损分解反例: R(AB), F=∅, 分解 {A}, {B}")
    r = analyze("AB", [], ["A", "B"])
    print("初始符号表:")
    print(fmt_table(r.initial_table, "AB"))
    print(f"chase 步数: {len(r.chase_steps)} (没有任何 FD 可触发等同)")
    print(f"无损连接   : {r.lossless}")
    ce = r.counterexample
    print(f"\n符号赋值 values = {ce['values']}  (每个符号类一个互异整数)")
    print("得到满足全部给定 FD 的原关系 r:")
    for t in ce["relation"]:
        print("  ", t)
    print("各投影自然连接结果:")
    for t in ce["rejoined"]:
        print("  ", t)
    print(f"\n连接产生了 r 中不存在的幽灵元组: {ce['spurious_tuple']}")
    print(ce["explanation"])
    assert not r.lossless


def demo_error_boundaries():
    section("3. 实际触发的错误边界 (非法输入不静默纠正)")

    print("(a) 属性数超过 12 个上限:")
    try:
        analyze("ABCDEFGHIJKLM", [], ["ABCDEFGHIJKLM"])
    except InputError as e:
        print(f"    InputError -> {e}")

    print("(b) bool 冒充 int 预算参数 (整数参数拒绝 bool):")
    try:
        analyze("AB", [{"lhs": "A", "rhs": "B"}], ["AB"],
                max_chase_steps=True)
    except InputError as e:
        print(f"    InputError -> {e}")

    print("(c) 分解未覆盖全部属性:")
    try:
        analyze("ABC", [{"lhs": "A", "rhs": "B"}], ["AB"])
    except InputError as e:
        print(f"    InputError -> {e}")

    print("(d) 搜索预算耗尽 与 证明无解 严格分开:")
    try:
        analyze("ABCD",
                [{"lhs": "A", "rhs": "B"}, {"lhs": "B", "rhs": "C"}],
                ["AB", "AC", "AD"], max_chase_steps=1)
    except BudgetExhaustedError as e:
        print(f"    BudgetExhaustedError -> {e}")
        print("    (不返回有损/无损结论; 放宽预算后该分解实际无损)")


if __name__ == "__main__":
    demo_normal()
    demo_lossy()
    demo_error_boundaries()
    section("演示完成: 以上结果均为现场计算, 未使用预录数据。")
