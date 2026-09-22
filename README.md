# fd_analysis — 函数依赖下的无损分解见证

离线数据库理论工具：给定关系模式、函数依赖 (FD) 集合与一个分解，计算
**最小覆盖、全部候选键、各子模式上的完整投影依赖、依赖保持性**，并用
**符号表 chase** 判定**无损连接**、输出逐步等同记录；有损时从最终符号表
构造可复验的具体反例（自然连接产生原关系没有的"幽灵元组"）。

- **Python 版本**：Python 3.14.7（仅用标准库；3.10+ 语法即可运行）
- **依赖**：无第三方依赖，安装 Python 后直接运行；全程离线，不访问网络、
  账号、密钥、数据库、中间件或云服务
- **平台**：Linux 原生（任何有 Python 3 的平台均可）

## 运行命令

```bash
# 必要测试（单元 + 教科书用例 + n=3 全量穷举交叉验证，实测约 2 分 45 秒）
python -m unittest discover -s tests -v

# 只跑快速单元/用例测试（不含穷举，0.1 秒级）
python -m unittest tests.test_inputs tests.test_algorithms -v

# 固定输入演示（真实计算，毫秒级，远低于 8 秒）
python demo.py
```

## 公开接口

```python
from fd_analysis import analyze, FD, AnalysisResult, InputError, BudgetExhaustedError

result = analyze(
    attributes,            # "ABCD" 或 ['A','B',...]，1..12 个单字符属性
    fds,                   # [{"lhs": "A", "rhs": "BC"}, ...]
    decomposition,         # ["AB", "BC", "AD"]：子模式列表，须覆盖全部属性
    *,
    max_chase_steps=None,  # 可选正整数预算；None 用确定性上界
)
```

`FD` 左右部均为**属性集合**（`str` 顺序无关，内部排序规范化）。空决定项
写作 `""` 或 `None`（即 ∅ → A，合法）。`fds` 元素也接受 `("A","B")` 或
`FD` 对象。

`AnalysisResult` 字段：

| 字段 | 含义 |
|---|---|
| `attributes` | 排序去重后的属性串 |
| `minimal_cover` | 最小覆盖 `list[FD]`（右部单属性、左部无冗余属性、无冗余 FD） |
| `candidate_keys` | 全部候选键 `list[str]`，**按 (长度, 字典序)** 排序；空键为 `""` |
| `projections[i]` | F 在第 i 个子模式上的**完整投影** F[R_i]（非平凡 FD） |
| `dependency_preserving` | 依赖保持（分布 chase 判定） |
| `lossless` | 无损连接（符号表 chase 判定，**独立于**依赖保持） |
| `initial_table` / `final_table` | chase 初始/最终符号表（`a_j` 常量、`b_ij` 变量） |
| `chase_steps` | 逐步等同记录：`fd` / `matched_rows` / `column` / `equated[{row,column,from,to}]` |
| `counterexample` | 仅有损时非空：`values`（符号→整数赋值）、`relation`（满足 F 的 r）、`projection_relations`、`rejoined`、`spurious_tuple`、`explanation` |
| `budget_exhausted` | 保留字段；预算耗尽时直接抛异常而非置位 |

另有公开工具 `closure(attributes, lhs, fds)` 计算属性闭包 X⁺。

## 输入上限与错误语义

- 属性数 **1–12**（接口硬边界，超限拒绝）；属性为单个 ASCII 字母/数字。
- FD 右部不能为空；右部与左部有任何属性交集都拒绝（含平凡 FD，如
  `AB->A`，以及含平凡部分的 `AB->BC`），要求调用方显式去掉交集属性，
  不静默纠正；左部允许为空。
- 子模式不能为空、不能重复，且其并集必须等于全部属性。
- 所有非法输入抛 **`InputError(ValueError)`**，消息带参数路径（如
  `fds[2].lhs[0]`），**不静默纠正**。
- 整数参数（`max_chase_steps`）**拒绝 `bool`**（尽管 `bool` 是 `int` 子类），
  也拒绝 `float`，因此 `NaN`/`Infinity` 无从进入；非正整数被拒。
- **预算耗尽 ≠ 证明无解**：chase 等同步数超过 `max_chase_steps` 时抛
  **`BudgetExhaustedError(RuntimeError)`**，绝不返回伪造的有损/无损结论。
  本库在 12 属性内使用精确算法，默认预算（= 表中符号数）下不会耗尽。
- 纯函数、无批次/可变状态；校验先于一切计算，失败不留部分变更。

## 设计取舍

1. **无损与依赖保持严格分开**：二者不可互推。`demo` 与测试同时包含
   "无损但不保持"（R(ABC), A→B,B→C, 分解 {AB,AC}）与"保持但有损"
   （R(ABCD), A→B,C→D, 分解 {AB,CD}）两类用例。
2. **chase 实现**：经典符号表，行 i 对应子模式 R_i；R_i 的列放常量 a_j，
   其余放独有变量 b_ij。每次左部匹配把右列符号**全局合并**（等价类语义，
   常量优先），逐步记录；出现全 a 行即无损。符号等同单调减少符号类数，
   必然终止。
3. **有损反例**：采用 Aho–Beeri–Ullman tableau 赋值——最终表每个符号类
   赋一个互异整数。该 r 满足全部给定 FD（chase 已到不动点），而"每列取
   常量"的元组不在 r 中、却在每个投影中有匹配，故必出现在自然连接里。
   这是一般成立的构造；输出给出 `values`、r、各投影、连接结果与贡献链，
   可手工复验。二值域上的反例由测试中的独立穷举预言机另行核对。
4. **最小覆盖**：右部拆单属性 → 按固定位序删左部冗余属性 → 在**演化工作
   集**上逐条删冗余 FD（不能用已删依赖为保留依赖辩护）。
5. **投影完备性**：枚举子模式的全部子集 X，取 X⁺∩R_i，再约简为无冗余
   规则集；依赖保持用全部局部投影并集上的分布闭包判定。
6. **确定性**：所有枚举按位序/字典序固定，同输入同输出，无随机数。

## 测试与独立验证

- `tests/reference.py`：**不导入被测核心**的独立实现（frozenset 闭包、
  union-find chase、候选键、关系级 FD 满足性/投影/自然连接、二值穷举），
  刻意与核心采用不同数据结构。
- `tests/test_inputs.py`：契约与错误语义（含 bool/float/NaN 拒绝、预算
  耗尽与证明无解分离）。
- `tests/test_algorithms.py`：教科书用例、循环 FD、空决定项、冗余属性、
  无损但不保持、投影可靠性与完备性、反例性质、chase 记录结构。
- `tests/test_exhaustive.py`：枚举 n=3 的全部 **4096** 个 FD 集 × 全部
  **109** 个属性覆盖（446 464 个场景），与独立参考逐一比对无损性/保持性/
  候选键（零分歧）；对其中 **16 320** 个有损场景，二值穷举（按元组数升序
  早停）全部找到满足 F 但连接产生幽灵元组的真实关系，并验证被测 tableau
  反例。"往返一致"仅作补充，期望值不来自被测核心。

## 误差口径

本库是离散符号/整数（位掩码）计算，**没有任何浮点运算或浮点结果**，
因此不存在数值误差或容差问题；所有判定（闭包、最小覆盖、候选键、chase
无损性、依赖保持、反例）都是精确的布尔/集合结论。

## 已知限制与数学子集

- 只实现本题契约：单关系模式上的函数依赖（不含多值依赖、连接依赖、外键、
  空值、类型系统、SQL 方言），不宣称任何行业标准兼容。
- 属性上限 12；候选键与投影用精确枚举，复杂度关于属性数指数级，上限内
  毫秒级，超出上限不是本库目标。
- 最小覆盖的具体表示对"左部属性删除顺序"敏感（最小覆盖不唯一）；本库
  保证最小性与等价性、并以固定遍历顺序保证确定性，不保证是所有最小覆盖
  中字典序最小的那个。
- chase 反例按 tableau 一般构造赋值（可能使用多于 2 个不同值）；是否存在
  二值反例不影响有损性的一般结论，二者在测试中分别核对。

## 仓库内容

```
fd_analysis.py   # 本题库（唯一核心模块）
demo.py          # 固定输入演示
tests/
  reference.py       # 独立参考实现（测试用预言机）
  test_inputs.py     # 契约/错误语义
  test_algorithms.py # 教科书用例与性质
  test_exhaustive.py # n=3 全量穷举交叉验证
README.md
```
