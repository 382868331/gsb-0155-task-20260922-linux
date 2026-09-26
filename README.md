# fdlossless — 函数依赖与多值依赖下的无损分解 / 四范式见证

离线数据库理论工具库：给定关系模式、函数依赖集（FD）、可选的多值依赖集
（MVD，X→→Y）和一个分解，计算最小覆盖、候选键、各子关系上的完整投影依赖，
判定依赖保持；用符号表 chase（FD 合并符号、MVD 生成新行）判定无损连接并输出
逐步见证；若有损，输出一个经过自检的小反例。提供 MVD 时，额外检查分解是否满足
四范式（4NF）条件：逐个列出被蕴含的非平凡 MVD、其决定项是否为超键。
全部核心算法（闭包、最小覆盖、候选键、投影、chase、蕴含判定、反例构造）均为
本仓库从零实现，不调用任何第三方同类实现。

## 环境

- Python 3.14.7，Linux 原生运行。
- 仅使用标准库，安装 Python 后无需安装任何其他依赖（`requirements.txt` 为空注释）。
- 核心、测试、演示全部离线：不需要账号、网络、数据库、Docker 或任何外部服务。

### 准备 venv（可选但推荐）

```bash
python3.14 -m venv .venv          # 任意 Python 3.14.7 解释器即可
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python demo_iteration.py
```

不建 venv 也可以：库只依赖标准库，直接用系统 `python3.14` 运行同样命令即可。

## 接口

```python
from fdlossless import analyze, attribute_closure, implies_mvd

result = analyze(
    attributes,      # 属性名 iterable[str]，唯一、非空
    FDs,             # [(lhs, rhs), ...]，左右均为属性名集合；空决定项合法，rhs 非空
    decomposition,   # [子关系属性集合, ...]，非空、互不相同，并集必须等于 attributes
    *,
    MVDs=None,           # 可选：[(lhs, rhs), ...] 多值依赖 X→→Y
    step_budget=100_000, # chase 步数预算（int，拒绝 bool/NaN/Inf/负数）
)

attribute_closure(attributes, FDs, X)        # FD 下的属性闭包（frozenset）
implies_mvd(attributes, FDs, MVDs, X, Y)     # FDs+MVDs 是否蕴含 X→→Y（bool）
```

- 不提供 `MVDs`（默认 `None`）：保持原 FD 接口，属性数 ≤ 12（`MAX_ATTRIBUTES`），
  不做 4NF 检查（`four_nf` 恒为 `True`，`four_nf_violations` 为空）。
- 提供 `MVDs`（哪怕空列表）：启用 MVD 推理与 4NF 检查，属性数 ≤ 8
  （`MAX_ATTRIBUTES_MVD`）。MVD 的右部中已出现在左部的属性会先被规范去掉
  （如 `{P,S}→→{P,S}` 规范为 `{P,S}→→∅`，即平凡 MVD）；规范化后重复视为非法输入。
- `step_budget` 同时约束 FD 合并步数与 MVD 行生成步数。

`analyze` 是纯函数，返回不可变的 `AnalysisResult`：

| 字段 | 含义 |
|---|---|
| `minimal_cover` | 最小覆盖，`FD(lhs=tuple, rhs=str)` 元组 |
| `candidate_keys` | 候选键，按大小再按字典序排序（仅由 FD 决定；MVD 不确定属性值） |
| `projections` | 每个子关系上的完整投影 FD（每个被蕴含 FD 的最小左部生成集） |
| `dependency_preserving` | 依赖保持判定（投影之并是否蕴含全部原 FD） |
| `lossless` | 无损连接判定（chase 终止后是否存在全可区分符号行） |
| `chase_log` | 逐步记录 `ChaseStep`：`rule="FD"` 为符号合并，`rule="MVD"` 为行生成（`added_row` 给出新行号） |
| `counterexample` | 有损时为 `CounterExample`：满足全部 FD 与 MVD 的小实例、各投影、寄生元组及说明；无损时为 `None` |
| `mvds` | 规范化后的输入 MVD，`MVD(lhs=tuple, rhs=tuple)`，左右部不相交 |
| `four_nf` | 分解的每个子关系是否都满足 4NF 条件 |
| `four_nf_violations` | 每条违反 4NF 的具体依赖：`FourNFViolation(relation, mvd, determinant_is_superkey, detail)`，报告的是包含极小的非平凡被蕴含 MVD |

依赖保持、无损连接、4NF 是**独立计算**的三个判定，互不替代。

## 多值依赖与 4NF 的判定方法

- **为什么需要交叉组合**：一个人有独立的技能集合和语言集合时，若关系只存
  “人会哪些技能”“人会哪些语言”的两行，投影再连接会凭空造出技能×语言的组合，
  所以原关系必须列出全部交叉组合——这正是 `person→→skill`。相关反例：若语言其实
  由技能决定（FD `skill→language`），两者并不独立，按独立假设分解反而有损
  （`demo_iteration.py` CASE 3 给出枚举关系上的具体反例）。
- **MVD 不是 FD**：chase 中 FD 触发符号等同，MVD 触发生成新行（两行使 X 相同，
  则加入“X∪Y 取自行 i、其余取自行 j”的行）。符号只复制不新造，行空间有限，
  chase 必然终止；预算仅作保险。
- **蕴含判定**：`X→→Y` 被蕴含当且仅当两行蕴含表（行 1 全可区分、行 2 仅在 X 上
  可区分）chase 后出现“在 X∪Y 上与行 1 一致、其余与行 2 一致”的行。
- **4NF 检查**：对每个子关系 S、每个不是 S 超键的决定项 X⊆S，用一次蕴含 chase
  读出全部被蕴含的 `X→→Y`，报告其中非平凡（Y 非空且 X∪Y≠S）且包含极小的条目；
  每条报告前再用独立蕴含 chase 复核。

## 错误语义

- `InvalidInputError`（`ValueError` 子类）：非法输入一律拒绝并给出可定位信息
  （如 `MVDs[0].lhs[0]: unknown attribute` 并指明属性名），不做静默纠正。
  唯一的“纠正”是题面要求的 MVD 右部规范化（去掉已在左部的属性），规范化结果
  在 `result.mvds` 中可见。数值位置拒绝 NaN/Infinity；整数参数拒绝 bool。
- `SearchBudgetExhausted`：chase 预算（FD 合并 + MVD 行生成）耗尽。这**不是**
  有损证明、也**不是**对蕴含问题的否定回答，只是“未完成”；加大 `step_budget`
  重跑即可。
- `analyze` 无状态、无批次副作用：失败不会留下任何部分变更。

## 输入上限

- 纯 FD：属性数 ≤ 12（`MAX_ATTRIBUTES`）。
- 提供 MVD 或调用 `implies_mvd`：属性数 ≤ 8（`MAX_ATTRIBUTES_MVD`）。
- 子关系个数、FD/MVD 条数无单独上限，受属性数与 `step_budget` 间接约束。

## 精度口径

本库不产出任何浮点结果：所有输出为精确的符号、布尔、字符串与整数常量，
不存在误差区间，也不存在用宽容差掩盖边界的问题。

## 设计取舍

- **完整投影依赖的表示**：对每个子关系 S、每个 a∈S，枚举所有使 a∈X⁺ 成立的
  极小左部 X⊆S。该集合是投影的完整生成集，同时避免列出非极小冗余左部。
- **候选键排序**：键内属性按名字字典序排列，键之间按 (大小, 字典序) 排序。
  候选键只由 FD 决定：MVD 从不函数确定属性。
- **chase 符号**：可区分符号按列命名，非可区分符号为 `b<行>_<列>`（1 起始）；
  合并时保留“更优”符号（可区分优先，其次行号小者），全表替换并记录日志。
  MVD 行生成复制既有符号，因此全部行来自有限符号空间，chase 保证终止。
- **4NF 报告粒度**：每个（子关系, 非超键决定项）报告包含极小的非平凡被蕴含
  MVD；不自动寻找“最便宜”的分解（题面不要求）。
- **反例构造**：最终未合并表逐行映射为常量元组，返回前自检：实例满足全部 FD
  与 MVD、寄生元组属于各投影的自然连接且不在实例中。
- **有限子集声明**：本库只实现本题契约（FD+MVD、无损/依赖保持/4NF 判定），
  不宣称兼容完整关系数据库理论工具链或任何行业标准。

## 已知限制

- 纯 FD 属性数上限 12；启用 MVD 后上限 8。投影、候选键、4NF 检查均为子集枚举，
  复杂度随属性数指数增长（8 内可秒级完成）。
- chase 预算耗尽时抛 `SearchBudgetExhausted` 而非给出判定（默认预算 100000 步，
  对上限规模远远足够）。
- 反例常量为整数编码，仅保证区分性，不对应任何业务语义。

## 验证与演示

```bash
python -m unittest discover -s tests -v   # 单元测试 + 穷举交叉核对
python demo.py                            # 原 FD 功能演示（保留）
python demo_iteration.py                  # 本次迭代演示：MVD、4NF、边界情况
```

- `tests/test_exhaustive.py`（保留）：FD 情形的穷举交叉核对——对 {A,B,C} 上全部
  ≤2 条的 FD 集与全部分解，穷举二值域 {0,1} 上的全部 256 个关系，按定义判定
  无损性并与 chase 判定逐一比对。
- `tests/test_mvd.py`：MVD 规范化、蕴含、chase 行生成、4NF 判定与错误语义的
  单元测试。
- `tests/test_mvd_exhaustive.py`：MVD 情形的独立参考实现——对 {A,B,C} 上的
  MVD 集（含与 FD 混合）与代表性分解，穷举全部 256 个二值关系，按定义核对
  无损判定、MVD 蕴含（`implies_mvd`）与 4NF 判定；未被蕴含的 MVD 必有两行
  chase 见证，故二值域上的有限核对是精确的。参考实现不调用被测核心。
- `demo_iteration.py`：真实计算并打印——独立技能/语言为何需要交叉组合及
  4NF 分解的 chase 见证、未分解关系的 4NF 违反清单、两者相关（`skill→language`）
  时的有损反例、以及两个实际触发的边界（9 属性拒绝、chase 预算耗尽报“未完成”）。
  全程不足 1 秒，无 sleep、无预录。
