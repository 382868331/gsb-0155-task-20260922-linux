# fdlossless — 函数依赖/多值依赖下的无损分解见证与 4NF 检查

离线数据库理论工具库：给定关系模式、函数依赖集（FD，可与多值依赖一起提供）
和一个分解，计算最小覆盖、候选键、各子关系上的完整投影依赖，判定依赖保持，
并用符号表 chase 判定无损连接；若有损，输出一个经过自检的小反例。本次迭代新增
多值依赖 X→→Y：闭包/蕴含判断（MVD 用独立的两行 chase，不当作 FD）、含 MVD 的
无损连接 chase 见证、以及给定分解的四范式验证（输出违反 4NF 的具体依赖、决定项
是否为超键）。全部核心算法均为本仓库从零实现，不调用任何第三方同类实现。

## 环境

- Python 3.14.7，Linux 原生运行。
- 仅使用标准库，安装 Python 后无需安装任何其他依赖（`requirements.txt` 为空注释）。
- 核心、测试、演示全部离线：不需要账号、网络、数据库、Docker 或任何外部服务。

## 接口

```python
from fdlossless import analyze

result = analyze(
    attributes,      # 属性名 iterable[str]，1..12 个，唯一、非空
    FDs,             # [(lhs, rhs), ...]，左右均为属性名集合；空决定项合法，rhs 非空
    decomposition,   # [子关系属性集合, ...]，非空、互不相同，并集必须等于 attributes
    *, step_budget=100_000,  # chase 合并步数预算（int，拒绝 bool/NaN/Inf/负数）
)
```

`analyze` 是纯函数，返回不可变的 `AnalysisResult`：

| 字段 | 含义 |
|---|---|
| `minimal_cover` | 最小覆盖，`FD(lhs=tuple, rhs=str)` 元组 |
| `candidate_keys` | 候选键，按大小再按字典序排序 |
| `projections` | 每个子关系上的完整投影依赖（每个被蕴含 FD 的最小左部生成集） |
| `dependency_preserving` | 依赖保持判定（投影之并是否蕴含全部原 FD） |
| `lossless` | 无损连接判定（chase 终止后是否存在全可区分符号行） |
| `chase_log` | 逐步等同记录 `ChaseStep`（所用 FD、行对、列、保留/替换符号、可读说明） |
| `counterexample` | 有损时为 `CounterExample`：满足全部 FD 的小实例、各投影、寄生元组及说明；无损时为 `None` |

依赖保持与无损连接是**独立计算**的两个判定，互不替代（有测试覆盖
“保持但有损”与“无损但不保持”两个方向）。

## 多值依赖与四范式（本次迭代新增）

在保留上述 FD 接口不变的前提下，新增多值依赖（MVD）X→→Y 与 4NF 分解验证：

```python
from fdlossless import analyze4nf, check_mvd, check_fd, attribute_closure

result = analyze4nf(
    attributes,      # 属性名 iterable[str]，1..8 个（MVD 分析上限 8）
    FDs,             # 函数依赖，可与 MVD 一起提供，语义同 analyze
    MVDs,            # [(lhs, rhs), ...]；Y 中已在 X 里的属性先规范去掉，
                     # 去掉后为空则拒绝（InvalidInputError）
    decomposition,   # 待验证的分解（不自动搜索最便宜分解）
    *, step_budget=100_000,  # chase 预算：合并步数 + 生成行数合并计数
)
```

`analyze4nf` 返回不可变的 `FourNFResult`：

| 字段 | 含义 |
|---|---|
| `mvds` | 规范化后的输入 MVD（`MVD(lhs, rhs)`，两侧不相交、rhs 非空） |
| `candidate_keys` | 候选键（仅由 FD 闭包决定；MVD 不产生键） |
| `lossless` / `chase_log` | 无损连接判定与逐步见证 `ChaseEvent`：`fd-merge`（符号等同）与 `mvd-row`（按 MVD 生成新行）两类步骤 |
| `counterexample` | 有损时的小反例实例（满足全部 FD 与 MVD，寄生元组在连接中而不在实例中，返回前自检） |
| `violations` | 违反 4NF 的具体依赖 `FourNFViolation`：子关系、非平凡且被蕴含的 MVD、`determinant_superkey`（违例时恒为 False）及可读说明 |
| `is_4nf` | 无违例即为 True |

4NF 判据（教科书标准）：对每个子关系 S，每个被输入依赖**蕴含**、落在 S 内的
非平凡 MVD X→→Y（Y ⊄ X 且 X∪Y ≠ S），X 必须是 S 的超键（按 FD 闭包判定）。
只报告极小左部与包含极小的右部；不做自动分解推荐。

辅助接口：

- `attribute_closure(attributes, FDs, seed)`：FD 闭包，按规范属性序返回。
- `check_fd(attributes, FDs, lhs, rhs)`：FD 蕴含判断（闭包法）。
- `check_mvd(attributes, FDs, MVDs, lhs, rhs, *, step_budget)`：MVD 蕴含判断，
  用两行符号表 chase（一行在 X∪Y 可区分、一行在 X∪Z 可区分，Z 为补集；
  推出全可区分行当且仅当蕴含）。**不蕴含**时返回有限反例关系
  （满足全部输入 FD/MVD、违反被查询 MVD），该否定见证在返回前以小域
  穷举方式逐条核对。MVD 绝不当作普通 FD 处理：`A ->> B` 不蕴含 `A -> B`。

## 错误语义

- `InvalidInputError`（`ValueError` 子类）：非法输入一律拒绝并给出可定位信息
  （如 `FDs[2].lhs[0]: unknown attribute 'X'`），不做静默纠正。
  数值位置拒绝 NaN/Infinity；整数参数 `step_budget` 拒绝 bool。
- `SearchBudgetExhausted`：chase 合并预算耗尽。这**不是**有损证明，
  与“已证明有损”严格区分；加大 `step_budget` 重跑即可。
- `analyze` 无状态、无批次副作用：失败不会留下任何部分变更。
- MVD 同样精确定位（如 `MVDs[1].rhs[0]`）；Y 减去 X 后为空时按 `MVDs[i].rhs` 拒绝。
- MVD chase 的预算把 **FD 符号合并**与 **MVD 新行生成**合并计数；耗尽时同样
  抛 `SearchBudgetExhausted`，表示判定未完成，**不得**当作有损。

## 输入上限

- 属性数 ≤ 12（`MAX_ATTRIBUTES`）。
- 子关系个数、FD 个数无单独上限，受属性数与 `step_budget` 间接约束。
- MVD/4NF 接口（`analyze4nf`、`check_mvd`）属性数 ≤ 8（`MAX_ATTRIBUTES_MVD`）；
  旧 FD 接口 `analyze` 仍为 12。4NF 检查对每个子关系枚举子集并逐对做 chase，
  复杂度随属性数指数增长（8 属性、默认预算下常规输入秒级完成）。
- 4NF 判据使用“被输入 FD∪MVD 蕴含、且属性全部落在子关系内的非平凡 MVD”
  （教科书分解算法标准判据）；不单独处理投影可能引入的额外 MVD。

## 精度口径

本库不产出任何浮点结果：所有输出为精确的符号、布尔、字符串与整数常量，
不存在误差区间，也不存在用宽容差掩盖边界的问题。

## 设计取舍

- **完整投影依赖的表示**：对每个子关系 S、每个 a∈S，枚举所有使 a∈X⁺ 成立的
  极小左部 X⊆S。该集合是投影的完整生成集（投影中每个 FD 的左部都包含某个
  列出的 X），同时避免列出非极小冗余左部。
- **候选键排序**：键内属性按名字字典序排列，键之间按 (大小, 字典序) 排序，确定性输出。
- **chase 符号**：可区分符号按列命名，非可区分符号为 `b<行>_<列>`（1 起始）；
  合并时保留“更优”符号（可区分优先，其次行号小者），全表替换并记录日志。
- **反例构造**：最终未合并表逐行映射为常量元组（可区分符号→列号，
  非可区分符号→新常量），返回前自检：实例满足全部 FD、寄生元组属于各投影的
  自然连接且不在实例中。
- **有限子集声明**：本库只实现本题契约（FD、无损/依赖保持判定），
  不宣称兼容完整关系数据库理论工具链或任何行业标准。
+- **MVD chase**：在原符号表上增加多值依赖规则——两行在 X 上一致时，按
  X∪Y / 补集交换生成新行；与 FD 的符号合并在同一轮迭代到不动点。
  MVD 蕴含用两行表的全可区分行判定，绝不套用 FD 闭包。

## 已知限制

- 属性数上限 12；投影与候选键为子集枚举，复杂度随属性数指数增长（12 内可秒级完成）。
- chase 预算耗尽时抛 `SearchBudgetExhausted` 而非给出判定（默认预算 100000 步，
  对 12 属性远远足够）。
- 反例常量为整数编码，仅保证区分性，不对应任何业务语义。
- MVD/4NF 分析的蕴含 chase 预算耗尽同理：给出 `SearchBudgetExhausted`，不能当
  作 MVD 不蕴含或分解有损的结论；`check_mvd` 的否定见证只在 chase 正常终止时产出。

## 验证与演示


```bash
python -m venv .venv && . .venv/bin/activate   # 可选；标准库，无需 pip install
python -m unittest discover -s tests -v         # 单元测试 + 穷举交叉核对
python demo.py                                  # FD 接口演示，约 1 秒内完成
python demo_iteration.py                        # MVD/4NF 新功能演示，约 1 秒内完成
```

- `tests/test_exhaustive.py`：独立参考实现——对 {A,B,C} 上全部 ≤2 条的 FD 集
  （79 个）与全部二部覆盖分解加三单例分解（13 种），穷举二值域 {0,1} 上的
  全部 256 个关系，按定义判定无损性并与 chase 判定逐一比对；对每个有损情形
  独立验证反例（满足 FD、寄生元组在连接中且不在实例中）。参考实现不调用被测核心。
- `demo.py`：真实计算并打印一个无损（但不保持依赖）的完整可复验 chase、
  一个有损反例，以及一个实际触发的输入边界拒绝（13 个属性）。
+- `tests/test_mvd.py`：MVD 规范化与错误语义、闭包/蕴含（互补规则、FD 也是 MVD、
  MVD 不反推 FD）、4NF 违例与超键、MVD chase 无损性，以及两组穷举交叉核对：
  对 {A,B,C} 上全部 ≤2 条 MVD 集与全部查询 MVD，穷举二值域 {0,1} 上全部 256 个
  关系按定义核对蕴含判定与否定见证；对全部单条 MVD 与 6 种分解核对无损判定及反例。
- `demo_iteration.py`：真实计算（无预录、无 sleep，远少于 90 秒）展示技能/语言
  交叉组合与 4NF 验证、技能与语言相关时 MVD 不成立的有限反例、以及 chase 预算
  耗尽时“未完成、非有损”的边界语义。
