# fdlossless — 函数依赖下的无损分解见证 + 试算事务

离线数据库理论工具库：给定关系模式、函数依赖集（FD）和一个分解，计算最小覆盖、
候选键、各子关系上的完整投影依赖，判定依赖保持，并用符号表 chase 判定无损连接；
若有损，输出一个经过自检的小反例。全部核心算法（闭包、最小覆盖、候选键、投影、
chase、反例构造）均为本仓库从零实现，不调用任何第三方同类实现。

在无状态的 `analyze` 之外，本版本增加了**内存中的试算事务**：调用方可以在一次
试算（trial）里增加和撤销若干 FD，查看候选依赖集下的候选键、依赖保持性、无损性
以及相对当前已提交版本的逐项变化；只有 `commit` 才替换正式分析版本，提交带
版本号，过期版本被拒绝。

## 环境

- Python 3.14.7，Linux 原生运行。
- 仅使用标准库，安装 Python 后无需安装任何其他依赖（`requirements.txt` 为空注释）。
- 核心、测试、演示全部离线：不需要账号、网络、数据库、Docker 或任何外部服务。
- 试算版本只存在于内存中的 `AnalysisSession` 对象内：没有持久化、数据库服务或
  管理界面；进程结束即消失。

## 无状态接口

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
| `fds` | 规范化为单右部的输入 FD（按 (左部, 右部) 排序） |
| `minimal_cover` | 最小覆盖，`FD(lhs=tuple, rhs=str)` 元组 |
| `candidate_keys` | 候选键，按大小再按字典序排序 |
| `projections` | 每个子关系上的完整投影依赖（每个被蕴含 FD 的最小左部生成集） |
| `dependency_preserving` | 依赖保持判定（投影之并是否蕴含全部原 FD） |
| `lossless` | 无损连接判定（chase 终止后是否存在全可区分符号行） |
| `chase_log` | 逐步等同记录 `ChaseStep`（所用 FD、行对、列、保留/替换符号、可读说明） |
| `counterexample` | 有损时为 `CounterExample`：满足全部 FD 的小实例、各投影、寄生元组及说明；无损时为 `None` |

依赖保持与无损连接是**独立计算**的两个判定，互不替代（有测试覆盖
“保持但有损”与“无损但不保持”两个方向）。

## 试算事务接口（AnalysisSession）

```python
from fdlossless import AnalysisSession

session = AnalysisSession(attributes, FDs, decomposition, *, step_budget=100_000)
session.version                 # 当前正式版本号，初始为 1（int）
session.committed               # 当前正式版本的 AnalysisResult（不可变）
session.committed_declarations  # 当前正式声明元组 (DeclaredFD, ...)

view = session.begin_trial()              # 基于当前正式版本开试算
view = session.add_fds(trial_id, FDs)     # 一次增加多条声明，返回重算后的 TrialView
view = session.remove_fds(trial_id, ids)  # 按原始声明 id 一次撤销多条
view = session.trial_view(trial_id)       # 查看试算当前视图
outcome = session.commit(trial_id, expected_version=None)  # 仅提交才替换正式版本
session.discard_trial(trial_id)           # 放弃试算（close_trial 为同义别名）
```

**声明与按身份删除。** 构造时每个输入 `(lhs, rhs)` 对得到一条带原始 id 的
`DeclaredFD(id, lhs, rhs)`（id 从 0 起）；`add_fds` 中每个输入对是一条新声明，
多属性右部（如 `A -> {B, C}`）始终是**一条**声明，按其 id 一次性撤销。引擎内部
仍把右部规范化为单属性 FD 参与计算，但删除以**原始声明 id** 为准：删除 id=k 只删
那条声明，不会误删由其他 FD 推出同一关系的声明；试算中不存在的 id 直接报错，
不删除任何东西。同一规范化 FD（含由同一批多属性右部展开产生的重复）在试算中
只能存在一条，重复添加整批拒绝。新增声明 id 在整个会话内单调分配，不同试算并发
添加也不会撞 id；id 一旦分配不被复用。

**每次改动从候选集整体重算。** `add_fds` / `remove_fds` 返回的 `TrialView`
中的分析，是用试算当前**全部候选声明**重新执行最小覆盖、候选键枚举、各子关系
投影、依赖保持判定和 chase 得到的，绝不沿用上一次候选集的闭包或投影；撤销后
旧闭包不可能被继续使用。测试用随机增删序列将每个试算视图与同候选集的全新
`analyze` 结果逐字段比对（另对 {A,B,C} 上全部 ≤2 条 FD 集枚举闭包核对候选键、
穷举二值关系核对无损性）。

```python
TrialView:
  trial_id, base_version, open
  declared_fds  # 候选声明（含 id）
  result        # 候选集重算得到的完整 AnalysisResult
  delta         # 相对 base_version 的 AnalysisDelta
```

`AnalysisDelta` 给出相对试算基线版本的集合差异：`declared_added/removed`、
`minimal_cover_added/removed`、`candidate_keys_added/removed`、
按子关系对齐的 `projections_added/removed`，以及
`dependency_preserving_old/new`、`lossless_old/new` 与
`changed` / `dependency_preserving_changed` / `lossless_changed`。
一条冗余 FD 被撤销时，声明集变化但最小覆盖、键、投影和两个判定可以完全不变；
一条关键 FD 被撤销时，键与无损性会相应翻转（演示 4a/4b）。

**版本与并发提交。** 提交是乐观并发控制：试算记录它所基于的正式版本号，
`commit` 时该版本仍是当前版本才成功，正式版本号 +1（试算随提交关闭）；
期间若有其他试算先提交，则抛 `StaleVersionError`，**不覆盖**新版本，落后
试算仍可查看或放弃，库不做自动合并（请基于新版本重新开试算）。也可显式传
`expected_version` 钉版本号。过去的正式版本快照仅在仍有未关闭试算基于它时
保留（引用计数），当前版本始终保留；不会无限堆积内存。

**预算与失败原子性。** 会话构造本身会跑首次分析，因此同样可能抛
`SearchBudgetExhausted`（不会得到构造了一半的会话）。试算中某次增/删触发的
重算若 chase 预算耗尽，整次改动被拒绝：异常**不是**有损判定，试算保留上一份
完整分析，候选声明不变、声明 id 不被消耗，调用方拿不到也提交不了半份新分析。
非法输入（未知属性、重复 FD、错误 id 类型等）同样在发布前整体拒绝。

## 错误语义

- `InvalidInputError`（`ValueError` 子类）：非法输入一律拒绝并给出可定位信息
  （如 `FDs[2].lhs[0]: unknown attribute 'X'`、`ids: declaration id(s) [99]
  not present ...`），不做静默纠正。数值位置拒绝 NaN/Infinity；整数参数
  （`step_budget`、声明 id、`expected_version`）拒绝 bool。
- `SearchBudgetExhausted`：chase 合并预算耗尽。这**不是**有损证明，
  与“已证明有损”严格区分；加大 `step_budget` 重跑即可。在试算中表示本次
  增/删整体未生效。
- `StaleVersionError`：试算基于的版本已被并发提交超越，或
  `expected_version` 不匹配；正式版本未被改动。
- `TrialClosedError`：试算 id 已提交或已关闭（id 不重用，需新开试算）。
- `analyze` 无状态、无批次副作用；`AnalysisSession` 的每次失败都不留下
  未说明的部分变更。

## 输入上限

- 属性数 ≤ 12（`MAX_ATTRIBUTES`）。
- 子关系个数、FD/声明个数无单独上限，受属性数与 `step_budget` 间接约束。
- 试算个数、提交版本数没有单独上限；每个会话仅占内存，建议按工作单元短生命周期使用。

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
- **最小覆盖左归约**：按 FD 顺序单遍扫描，判定使用当前完整 FD 集下的闭包
  （(X−{A})⁺ 是否含右部）；已对 {A,B,C} 上全部 ≤2 条 FD 集与不动点版本穷举
  比对，结果完全一致。
- **声明身份规范化**：计算用单右部 FD，对外身份用原始声明（可含多属性右部），
  两者通过 `DeclaredFD.expanded` 对应；删除永远针对身份而非推导出的关系。
- **试算隔离**：试算持有独立的候选声明字典与完整 `AnalysisResult`，相互之间、
  对正式版本之间均不共享可变状态；会话用一把锁保护版本号、声明 id 计数器和
  试算表。
- **有限子集声明**：本库只实现本题契约（FD、无损/依赖保持判定、内存试算版本），
  不宣称兼容完整关系数据库理论工具链、MVCC 事务协议或任何行业标准。

## 已知限制

- 属性数上限 12；投影与候选键为子集枚举，复杂度随属性数指数增长（12 内可秒级完成）。
- chase 预算耗尽时抛 `SearchBudgetExhausted` 而非给出判定（默认预算 100000 步，
  对 12 属性远远足够）。
- 反例常量为整数编码，仅保证区分性，不对应任何业务语义。
- 试算与版本仅存于内存，不落盘、不跨进程；无回滚到任意历史版本的接口
  （历史快照只用于未关闭试算的差异计算）；并发控制为同进程线程内的乐观锁，
  不是多进程/分布式事务。

## 验证与演示

```bash
./.venv/bin/python -m unittest discover -s tests -v   # 全部单元测试 + 穷举交叉核对
./.venv/bin/python demo.py                            # 固定输入现场演示，约 0.1 秒完成
```

（README 命令以仓库自带的 `./.venv` 解释器为准；等价地可用任何 3.14.x
标准库 Python 执行上述两条命令。）

- `tests/test_core.py`：最小覆盖、候选键、投影、依赖保持、chase、反例、
  预算与输入校验的单元测试。
- `tests/test_exhaustive.py`：独立参考实现——对 {A,B,C} 上全部 ≤2 条的 FD 集
  （79 个）与全部二部覆盖分解加三单例分解（13 种），穷举二值域 {0,1} 上的
  全部 256 个关系，按定义判定无损性并与 chase 判定逐一比对；对每个有损情形
  独立验证反例。参考实现不调用被测核心。
- `tests/test_session.py`：试算事务测试（30 个），包括：
  - 冗余 FD 撤销后最小覆盖/键/投影/保持/无损全部不变，关键 FD 撤销后键由
    (A,) 变 (A,C)、连接翻转为有损，并独立验证反例（满足幸存 FD、寄生元组在
    连接中且不在实例中、各投影与实例一致）；
  - 按原始声明 id 删除：推出同一关系的其他声明不被误删、多属性右部作为一条
    声明整体撤销、未知/错误类型 id 与重复添加整批拒绝；
  - 40 步随机增删序列中每个试算视图都与同候选集的全新 `analyze` 逐字段相等
    （不使用旧闭包）；
  - 两个试算竞争提交：先到者成为新版本，过期提交抛 `StaleVersionError` 且不
    覆盖；提交后试算 id 失效；同进程多线程用 barrier 对齐后竞争提交，恰有
    一个赢家、正式版本只前进一次；
  - 预算不足时增/删整体拒绝、不消耗声明 id、不留下半份分析；构造期预算耗尽
    同样拒绝；
  - {A,B,C} 上全部 79 个 ≤2 条 FD 集**经由试算 API**（空会话 + add + commit）
    计算，枚举闭包核对候选键、穷举二值关系核对无损性并验证每个有损反例；
  - 历史版本快照的引用计数生命周期。
- `demo.py`：真实计算并打印——
  1. 一个无损（但不保持依赖）的完整可复验 chase；
  2. 一个有损反例；
  3. 一个实际触发的输入边界拒绝（13 个属性）；
  4. 试算事务：4a 冗余 FD（`A->C`，可由 `A->B, B->C` 推出）撤销后全部性质
     不变并提交；4b 关键 FD（`B->C`）撤销后键由 (A,) 变 (A,C)、无损变有损并
     给出反例，放弃试算后正式版本不变；4c 两个试算竞争提交，后者被
     `StaleVersionError` 拒绝；4d 预算为 0 时增加 FD 整批失败、试算无残留。

演示输入刻意很小（最多 3 个属性；引擎硬上限 12，默认 chase 预算 100000 步），
用于展示行为而非压力测试；没有任何预录结果或 sleep。
