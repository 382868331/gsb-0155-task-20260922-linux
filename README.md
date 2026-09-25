# fdlossless — 函数依赖下的无损分解见证

离线数据库理论工具库：给定关系模式、函数依赖集（FD）和一个分解，计算最小覆盖、
候选键、各子关系上的完整投影依赖，判定依赖保持，并用符号表 chase 判定无损连接；
若有损，输出一个经过自检的小反例。全部核心算法（闭包、最小覆盖、候选键、投影、
chase、反例构造）均为本仓库从零实现，不调用任何第三方同类实现。

在一次性纯函数 `analyze` 之上，本库还提供**内存中的版本化试算层**
`AnalysisVersionStore`：调用方可以在一个试算（trial）里一次增加和撤销若干 FD，
从候选集整体重算最小覆盖、候选键、投影、依赖保持与无损性，并得到相对当前正式
版本的差异；只有 `commit` 才替换正式分析，过期版本的提交被拒绝。不做数据库
服务、持久化或管理界面。

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

## 版本化试算层（内存中）

`AnalysisVersionStore` 固定关系模式与分解，只让 FD 集随版本变化。所有状态只在
内存中，无线程外共享；方法内部加锁，单次 open/extend/rollback/commit 原子，
两个试算并发提交时恰有一个成功。

```python
from fdlossless import AnalysisVersionStore, StaleVersionError

# 初始正式版本固定为 version=1；FDs 用 (fd_id, lhs, rhs) 三元组，
# 也接受 {fd_id: (lhs, rhs)} 映射。左右仍是属性名集合。
store = AnalysisVersionStore(
    ["A", "B", "C"],
    [("ab", {"A"}, {"B"}), ("bc", {"B"}, {"C"})],
    [{"A", "B"}, {"A", "C"}],
    step_budget=100_000,          # 与 analyze 相同的 chase 合并预算
)

store.version            # 1
store.current()          # 当前正式版本的完整 AnalysisResult
store.declarations()     # 当前正式声明（含 fd_id 身份）

# 一次试算可以同时增加和撤销若干 FD：
out = store.open_trial(
    add_fds=[("ca", {"C"}, {"A"})],
    remove_fds=["bc"],
)
out.trial_id             # 试算句柄（正整数）
out.base_version         # 该试算所基于的正式版本（乐观并发依据）
out.candidate_fds        # 候选声明元组 DeclaredFD(fd_id, lhs, rhs, singletons)
out.added_ids            # 相对正式版本的净新增 id
out.removed_ids          # 相对正式版本的净删除 id
out.result               # 候选集上的完整 AnalysisResult
out.diff                 # AnalysisDiff：相对正式版本的逐项变化

# 在同一试算上继续增删（每次都从候选集整体重算）：
out = store.extend_trial(out.trial_id, remove_fds=["ca"])

# 放弃试算：候选分析立即失效，正式版本不变：
store.rollback(out.trial_id)

# 提交：仅当试算基于的版本仍是当前版本时才替换正式分析，版本号 +1：
receipt = store.commit(out.trial_id)                 # 或 expected_version=1
receipt.version            # 新版本号（单调递增）
receipt.result             # 成为正式分析
```

### 声明身份与规范化

- 每个 FD 声明带调用方给定的非空字符串 `fd_id`。增删与差异**以声明 id 为准**：
  删除 `fd_id="x"` 只移除 x 这一条声明；若另一条声明 y 声明了规范化后相同的
  依赖（含多右部拆分出的同一单右部 FD），y 保留、该依赖仍在。试算层因此允许
  两条声明陈述同一依赖（无状态 `analyze` 出于输入无身份而拒绝重复 FD，二者语义
  不同）。
- FD 仍按左右属性集合规范化：右部在内部拆成单右部 `FD(lhs, rhs)`，左部按属性
  顺序排列。`out.result.fds` 是规范化后的候选依赖；`out.candidate_fds` 保留声明
  身份。
- 一次试算的任何一步（open / extend）都从候选集**重新计算**最小覆盖、候选键、
  投影、依赖保持与 chase，不复用之前候选集的任何闭包；撤销一个新增 FD 后，由它
  推出的键/性质立即消失（有 512 个子集的独立枚举测试核对）。
- `added_ids`/`removed_ids` 是按 id 成员关系计算的**净差**：试算中加了又删、
  或正式声明删了又以新内容加回的 id 两个列表都不出现；内容替换本身体现在
  `candidate_fds` 与 `diff` 中。

### 差异 `AnalysisDiff`

| 字段 | 含义 |
|---|---|
| `minimal_cover_added` / `minimal_cover_removed` | 最小覆盖中规范化 FD 的增/删 |
| `candidate_keys_added` / `candidate_keys_removed` | 候选键元组的增/删 |
| `projections_added` / `projections_removed` | 与各子关系对齐的投影 FD 增/删（逐分量元组） |
| `dependency_preserving_changed` / `lossless_changed` | 两个布尔判定是否翻转 |
| `empty` | 以上全部无变化（删除一条本就被蕴含、未进最小覆盖的冗余 FD 时为 True） |

### 试算错误语义

- `InvalidInputError`：非法的属性集合、空右部、空/非字符串 id、同一操作内重复
  id、错误类型的 `trial_id`/`expected_version` 等，可定位、不静默纠正。
- `TrialConflictError`：删除候选集中不存在的 id、增加已存在的 id、使用未知或已
  关闭（已提交/已撤销）的试算句柄。被拒绝时不产生任何状态变更。
- `StaleVersionError`（携带 `expected_version`、`current_version`）：试算基于的
  版本已过期（另一试算先提交）。提交被拒绝、什么都不发布，试算仍保留，调用方应
  `rollback` 后基于当前版本重开并重算。
- `TrialIncomplete`（携带 `step_budget`、`reason="chase_budget_exhausted"`）：
  chase 预算不足，**只返回未完成**。open 阶段预算不足不登记试算、不动正式版本；
  extend 阶段预算不足保留该试算上一次完整分析，绝不发布半份新分析。这不是有损
  判定，加大 `step_budget` 重试即可。
- 构造 store 时（初始版本）预算不足仍抛无状态的 `SearchBudgetExhausted`。

## 错误语义

- `InvalidInputError`（`ValueError` 子类）：非法输入一律拒绝并给出可定位信息
  （如 `FDs[2].lhs[0]: unknown attribute 'X'`），不做静默纠正。
  数值位置拒绝 NaN/Infinity；整数参数 `step_budget` 拒绝 bool。
- `SearchBudgetExhausted`：chase 合并预算耗尽。这**不是**有损证明，
  与“已证明有损”严格区分；加大 `step_budget` 重跑即可。
- `analyze` 无状态、无批次副作用：失败不会留下任何部分变更。

## 输入上限

- 属性数 ≤ 12（`MAX_ATTRIBUTES`）。
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
- **试算与版本**：store 只持有一个正式状态，试算是钉在某个版本上的隔离草稿；
  提交采用乐观版本检查（不加全局长锁、不阻塞试算），并发提交由锁保证原子、
  恰有一个成功，失败者收到 `StaleVersionError` 后自行重开。预算不足一律
  “只返回未完成”，任何部分结果都不进试算记录、更不进正式版本。
- **有限子集声明**：本库只实现本题契约（FD、无损/依赖保持判定、内存试算版本），
  不宣称兼容完整关系数据库理论工具链或任何行业标准。

## 已知限制

- 属性数上限 12；投影与候选键为子集枚举，复杂度随属性数指数增长（12 内可秒级完成）。
- chase 预算耗尽时抛 `SearchBudgetExhausted`/`TrialIncomplete` 而非给出判定
  （默认预算 100000 步，对 12 属性远远足够）；预算不足绝不当作有损。
- 反例常量为整数编码，仅保证区分性，不对应任何业务语义。
- 试算版本仅存在于内存：进程结束即全部消失，无持久化、无数据库、无跨进程
  共享；一个 store 的属性集与分解在构造时固定，不随版本改变。

## 验证与演示

```bash
./.venv/bin/python -m unittest discover -s tests -v   # 单元测试 + 穷举交叉核对
./.venv/bin/python demo.py                            # 固定输入演示，约 0.1 秒完成
```

- `tests/test_exhaustive.py`：独立参考实现——对 {A,B,C} 上全部 ≤2 条的 FD 集
  （79 个）与全部二部覆盖分解加三单例分解（13 种），穷举二值域 {0,1} 上的
  全部 256 个关系，按定义判定无损性并与 chase 判定逐一比对；对每个有损情形
  独立验证反例（满足 FD、寄生元组在连接中且不在实例中）。参考实现不调用被测核心。
- `tests/test_trials.py`：试算层测试——版本/回滚语义、以声明 id 为准的删除
  （两条同依赖声明互不误删）、每次试算从候选集整体重算（撤销后旧闭包失效）、
  预算不足只返回未完成且不发布半份分析、两试算竞争提交恰有一个成功（含真实线程
  用例）；另有一个**不引用包内实现**的独立闭包/键枚举，对 9 个候选单右部 FD 的
  全部 512 个子集逐一核对正式版本与试算候选集上的候选键。
- `demo.py`：真实计算并打印一个无损（但不保持依赖）的完整可复验 chase、
  一个有损反例、一个实际触发的输入边界拒绝（13 个属性），以及试算事务三例：
  冗余 FD 撤销后性质不变并提交、关键 FD 撤销后键改变且产生经独立复核的有损反例
  （随后回滚、正式版本不变）、两个试算竞争提交一胜一拒并在新版本上重试。
