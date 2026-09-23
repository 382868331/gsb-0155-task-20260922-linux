# fdlossless — 函数依赖下的无损分解见证与最优分解选择

离线数据库理论工具库：给定关系模式、函数依赖集（FD）和一个分解，计算最小覆盖、
候选键、各子关系上的完整投影依赖，判定依赖保持，并用符号表 chase 判定无损连接；
若有损，输出一个经过自检的小反例。另提供分解选择：在候选子关系池中枚举选出
覆盖全部属性、保留指定属性组、无损且保持依赖、总成本最低的分解。
全部核心算法（闭包、最小覆盖、候选键、投影、chase、反例构造、选择枚举）
均为本仓库从零实现，不调用任何第三方同类实现。

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

## 接口：分解选择

```python
from fdlossless import select_decomposition

result = select_decomposition(
    attributes,      # 属性名 iterable[str]，1..6 个，唯一、非空
    FDs,             # 同 analyze 的契约
    candidates,      # 候选子关系 [属性集合, ...]，1..8 个，非空、互不相同
    must_keep,       # 必须保留的属性组 [属性集合, ...]，可为空；
                     # 每个组必须整体落在某个被选中的子关系内
    costs,           # 每个候选一个非负整数成本（拒绝 bool/float/NaN/Inf/负数），
                     # 长度必须与候选数一致
    *, step_budget=100_000,  # 每次可行性判定的 chase 预算，语义同 analyze
)
```

枚举候选的全部子集，选出同时满足以下条件的分解：**覆盖全部属性**、
**每个 must_keep 组落在某个被选子关系内**、**无损连接**（chase 判定）、
**保持依赖**（投影之并蕴含全部原 FD）。最优性比较顺序固定为：

1. 总成本最小；
2. 子关系个数最少；
3. 规范形属性字典序最小（每个子关系内属性按名字排序、子关系间再排序后逐元比较）。

返回不可变的 `SelectionResult`：

| 字段 | 含义 |
|---|---|
| `chosen` | 选中的分解（规范形：子关系内属性按名字排序、子关系间字典序排序） |
| `chosen_indices` | 选中候选在输入 `candidates` 中的下标（升序） |
| `total_cost` | 总成本（int） |
| `candidates` / `costs` / `must_keep` / `attributes` | 规范化后的输入回显 |
| `analysis` | 对选中分解重新调用 `analyze` 的完整复核结果（必然无损且保持依赖） |

无解时抛 `NoFeasibleDecomposition`（`FDToolError` 子类），消息说明失败在哪一类约束：
属性未被任何候选覆盖 / 必须保留的组放不进任何单个候选 /
有覆盖且保留组的选法但无一同时无损且保持依赖（并给出各有损、不保持的计数）。
这是在给定候选集内**已证明无解**，与预算耗尽严格区分。

## 错误语义

- `InvalidInputError`（`ValueError` 子类）：非法输入一律拒绝并给出可定位信息
  （如 `FDs[2].lhs[0]: unknown attribute 'X'`、`costs[1]: cost must be >= 0, got -3`），
  不做静默纠正。数值位置拒绝 NaN/Infinity；整数参数 `step_budget` 与每个成本
  拒绝 bool；成本只接受非负 int。
- `NoFeasibleDecomposition`：`select_decomposition` 证明候选集内无解
  （消息区分失败原因，见上节）。
- `SearchBudgetExhausted`：chase 合并预算耗尽。这**不是**有损证明，
  与“已证明有损”严格区分；加大 `step_budget` 重跑即可。
- `analyze` / `select_decomposition` 均为无状态纯函数，无批次副作用：
  失败不会留下任何部分变更。

## 输入上限

- `analyze`：属性数 ≤ 12（`MAX_ATTRIBUTES`）。
- `select_decomposition`：属性数 ≤ 6（`MAX_SELECT_ATTRIBUTES`），
  候选子关系 ≤ 8（`MAX_CANDIDATES`），枚举全部 2⁸ 个子集。
- 子关系个数、FD 个数无单独上限，受属性数与 `step_budget` 间接约束。

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

## 已知限制

- `analyze` 属性数上限 12；投影与候选键为子集枚举，复杂度随属性数指数增长（12 内可秒级完成）。
- `select_decomposition` 属性数上限 6、候选上限 8，为全子集枚举（≤255 个选法，
  每个选法一次 chase 加一次投影闭包判定），只保证候选集内的最优，不构造候选之外
  的分解（不做 3NF/BCNF 合成）。
- chase 预算耗尽时抛 `SearchBudgetExhausted` 而非给出判定（默认预算 100000 步，
  对 12 属性远远足够）。
- 反例常量为整数编码，仅保证区分性，不对应任何业务语义。

## 验证与演示

```bash
python -m unittest discover -s tests -v   # 单元测试 + 穷举交叉核对
python demo.py                            # 固定输入演示，约 1 秒内完成
```

- `tests/test_exhaustive.py`：独立参考实现——对 {A,B,C} 上全部 ≤2 条的 FD 集
  （79 个）与全部二部覆盖分解加三单例分解（13 种），穷举二值域 {0,1} 上的
  全部 256 个关系，按定义判定无损性并与 chase 判定逐一比对；对每个有损情形
  独立验证反例（满足 FD、寄生元组在连接中且不在实例中）。参考实现不调用被测核心。
- `tests/test_select.py`：分解选择的手算用例（成本、个数、字典序三级比较各一条、
  must_keep 改变最优解、四类无解消息）与独立枚举交叉核对——测试内自行枚举全部
  候选子集，用已被穷举验证过的 `analyze` 作可行性预言机，逐一比对最优键与无解判定。
- `demo.py`：真实计算并打印一个无损（但不保持依赖）的完整可复验 chase、
  一个有损反例、一个输入边界拒绝（13 个属性）、一次正常的最优分解选择、
  一次上限边界（6 属性 8 候选）的选择，以及实际触发的无解判定与非法成本拒绝。
