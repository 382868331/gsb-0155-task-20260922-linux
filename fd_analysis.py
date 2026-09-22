"""
fd_analysis — 函数依赖下的无损分解见证 (lossless-join witness) 库。

只依赖 Python 3.14 标准库。核心入口: analyze(attributes, fds, decomposition)。

公开数据结构
============
属性用单个字符表示 (例如 'A')。FD 是 {"lhs": "ABC", "rhs": "D"} (顺序无关,
内部规范化为排序后的字符串)。analyze 返回 AnalysisResult, 字段:

    attributes          : str            排序去重后的全部属性
    minimal_cover       : list[FD]       最小覆盖 (canonical: 右部单属性)
    candidate_keys      : list[str]      候选键, (长度, 字典序) 排序
    projections         : list[list[FD]] 每个子模式上 F 的投影 (非平凡)
    dependency_preserving : bool
    lossless            : bool
    chase_steps         : list[dict]     逐步等同记录
    counterexample      : dict | None    仅在有损时给出
    budget_exhausted    : bool

错误语义: 非法输入抛 InputError(ValueError)。算法预算耗尽时不伪造结论:
analyze 抛 BudgetExhaustedError (与"证明无解"严格分开)。当前给定上限
(|R| <= 12) 内使用的精确穷举算法不会触发预算耗尽; 预算参数保留为显式契约。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

__all__ = [
    "FD",
    "AnalysisResult",
    "InputError",
    "BudgetExhaustedError",
    "analyze",
    "closure",
]

MAX_ATTRIBUTES = 12


# --------------------------------------------------------------------------- #
# 错误类型
# --------------------------------------------------------------------------- #
class InputError(ValueError):
    """输入违反公开契约。message 携带可定位信息 (参数名 + 原因)。"""


class BudgetExhaustedError(RuntimeError):
    """搜索预算耗尽: 与"已证明无解"严格区分, 不允许伪造结论。"""


# --------------------------------------------------------------------------- #
# FD 容器
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FD:
    """函数依赖 X -> Y。lhs/rhs 均为排序去重后的属性字符串。"""

    lhs: str
    rhs: str

    def __str__(self) -> str:  # pragma: no cover - 仅显示用
        return f"{self.lhs or '∅'} -> {self.rhs or '∅'}"


@dataclass
class AnalysisResult:
    attributes: str
    minimal_cover: list[FD]
    candidate_keys: list[str]
    projections: list[list[FD]]
    dependency_preserving: bool
    lossless: bool
    chase_steps: list[dict] = field(default_factory=list)
    initial_table: list[list[str]] = field(default_factory=list)
    final_table: list[list[str]] = field(default_factory=list)
    counterexample: dict | None = None
    budget_exhausted: bool = False


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #
def _validate_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"{name}: 必须是 int, 得到 {type(value).__name__}")
    return value


def _validate_attributes(attributes) -> frozenset[str]:
    if isinstance(attributes, str):
        seq = list(attributes)
    else:
        try:
            seq = list(attributes)
        except TypeError as exc:
            raise InputError("attributes: 必须是字符的可迭代对象或字符串") from exc
    out: set[str] = set()
    for i, a in enumerate(seq):
        if not isinstance(a, str) or len(a) != 1:
            raise InputError(
                f"attributes[{i}]: 每个属性必须是单字符 str, 得到 {a!r}"
            )
        if not a.isalnum() or not a.isascii():
            raise InputError(
                f"attributes[{i}]: 属性字符必须是 ASCII 字母或数字, 得到 {a!r}"
            )
        if a in out:
            raise InputError(f"attributes[{i}]: 重复属性 {a!r}")
        out.add(a)
    if not out:
        raise InputError("attributes: 关系模式至少包含 1 个属性")
    if len(out) > MAX_ATTRIBUTES:
        raise InputError(
            f"attributes: 属性数 {len(out)} 超过上限 {MAX_ATTRIBUTES}"
        )
    return frozenset(out)


def _validate_attr_set(value, universe: frozenset[str], path: str) -> str:
    """把 FD 左/右部规范化为排序字符串; 同时是防注入屏障。"""
    if value is None:
        value = ""  # 允许空决定项/空右部: None 与 "" 同义, 均合法
    if isinstance(value, str):
        items = list(value)
    else:
        try:
            items = list(value)
        except TypeError as exc:
            raise InputError(f"{path}: 必须是属性集合 (str/可迭代)") from exc
    out: set[str] = set()
    for i, a in enumerate(items):
        if not isinstance(a, str) or len(a) != 1:
            raise InputError(f"{path}[{i}]: 必须是单字符 str, 得到 {a!r}")
        if a not in universe:
            raise InputError(f"{path}[{i}]: 属性 {a!r} 不在关系模式中")
        if a in out:
            raise InputError(f"{path}[{i}]: 集合内属性重复 {a!r}")
        out.add(a)
    return "".join(sorted(out))


def _validate_fds(fds, universe: frozenset[str]) -> list[FD]:
    if fds is None:
        return []
    if isinstance(fds, (str, bytes)) or not _is_iterable(fds):
        raise InputError("fds: 必须是 FD 字典/对象的可迭代对象")
    result: list[FD] = []
    for i, item in enumerate(fds):
        if isinstance(item, FD):
            lhs_raw, rhs_raw = item.lhs, item.rhs
        elif isinstance(item, dict):
            if "lhs" not in item or "rhs" not in item:
                raise InputError(
                    f"fds[{i}]: FD 字典必须含 'lhs' 与 'rhs' 键"
                )
            lhs_raw, rhs_raw = item["lhs"], item["rhs"]
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            lhs_raw, rhs_raw = item
        else:
            raise InputError(
                f"fds[{i}]: 必须是 {{'lhs':..,'rhs':..}} 或 (lhs, rhs)"
            )
        lhs = _validate_attr_set(lhs_raw, universe, f"fds[{i}].lhs")
        rhs = _validate_attr_set(rhs_raw, universe, f"fds[{i}].rhs")
        if not rhs:
            # 空右部是平凡依赖 X -> {} , 对推理无贡献; 拒绝而非静默丢弃。
            raise InputError(f"fds[{i}]: 右部不能为空 (平凡依赖 {lhs}->∅ 无意义)")
        if set(lhs) & set(rhs):
            # 右部与左部有交集就含有平凡部分 (如 AB->BC 中的 B)。
            # 不静默删去交集属性, 而是定位拒绝: 该 FD 等价于去掉交集后的写法。
            overlap = "".join(sorted(set(lhs) & set(rhs)))
            raise InputError(
                f"fds[{i}]: {lhs} -> {rhs} 右部属性 {overlap} 已在左部 "
                "(平凡部分); 请从右部删除这些属性后重新提交"
            )
        result.append(FD(lhs, rhs))
    return result


def _is_iterable(x) -> bool:
    try:
        iter(x)
    except TypeError:
        return False
    return True


def _validate_decomposition(decomp, universe: frozenset[str]) -> list[str]:
    if decomp is None or isinstance(decomp, (str, bytes)) or not _is_iterable(decomp):
        raise InputError("decomposition: 必须是属性集合的可迭代对象")
    schemas: list[str] = []
    covered: set[str] = set()
    for i, item in enumerate(decomp):
        s = _validate_attr_set(item, universe, f"decomposition[{i}]")
        if not s:
            raise InputError(f"decomposition[{i}]: 子关系模式不能为空")
        if s in schemas:
            raise InputError(f"decomposition[{i}]: 重复的子关系模式 {s!r}")
        schemas.append(s)
        covered.update(s)
    if not schemas:
        raise InputError("decomposition: 至少需要一个子关系模式")
    missing = universe - covered
    if missing:
        raise InputError(
            "decomposition: 未覆盖属性 " + "".join(sorted(missing))
        )
    return schemas


# --------------------------------------------------------------------------- #
# 集合在 int 位掩码上的运算 (属性 <= 12, 快速且无浮点)
# --------------------------------------------------------------------------- #
def _index_map(attrs: str) -> dict[str, int]:
    return {a: 1 << i for i, a in enumerate(attrs)}


def _to_mask(s: str, idx: dict[str, int]) -> int:
    m = 0
    for a in s:
        m |= idx[a]
    return m


def _to_str(mask: int, attrs: str) -> str:
    return "".join(a for i, a in enumerate(attrs) if mask >> i & 1)


# --------------------------------------------------------------------------- #
# 属性闭包 (公开工具, X+ under F)
# --------------------------------------------------------------------------- #
def closure(attributes, lhs, fds) -> str:
    """公开工具: 在 F 下计算 lhs 的属性闭包。输入同样经过完整校验。"""
    universe = set(_validate_attributes(attributes))
    fds_n = _validate_fds(fds, universe)
    x = _validate_attr_set(lhs, universe, "lhs")
    attrs_s = "".join(sorted(universe))
    idx = _index_map(attrs_s)
    rules = [(_to_mask(f.lhs, idx), _to_mask(f.rhs, idx)) for f in fds_n]
    c = _to_mask(x, idx)
    c = _fixpoint(c, rules)
    return _to_str(c, attrs_s)


def _fixpoint(start: int, rules: list[tuple[int, int]]) -> int:
    c = start
    changed = True
    while changed:
        changed = False
        for lh, rh in rules:
            if lh & ~c == 0:  # lh ⊆ c
                nc = c | rh
                if nc != c:
                    c, changed = nc, True
    return c


# --------------------------------------------------------------------------- #
# 最小覆盖
# --------------------------------------------------------------------------- #
def _minimal_cover(
    attrs: str, rules: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """
    计算最小函数依赖集 (canonical cover 的最小覆盖形式):
      1. 右部拆为单属性;
      2. 消去左部冗余属性;
      3. 消去冗余 FD。
    结果去重, 不保证跨等价输入的唯一表示, 但保证: 等价于 F、左部无冗余属性、
    无冗余依赖、右部单属性。
    """
    # 1. 右部单属性化, 去重
    split: set[tuple[int, int]] = set()
    for lh, rh in rules:
        for i in range(len(attrs)):
            if rh >> i & 1:
                split.add((lh, 1 << i))
    work = list(split)

    # 2. 左部约简
    reduced: list[tuple[int, int]] = []
    for lh, rh in work:
        # 逐个尝试删除左部属性; 多属性时属性删除顺序可能影响最终表示,
        # 固定按位序遍历保证确定性。
        cur = lh
        for i in range(len(attrs)):
            bit = 1 << i
            if cur & bit:
                trial = cur ^ bit
                if _implies(work, trial, rh):
                    cur = trial
        reduced.append((cur, rh))

    # 3. 消去冗余依赖: 必须在"演化中的工作集"上逐条检验
    #    (用已删除的依赖为另一条依赖辩护会导致结果非最小甚至不构成覆盖)。
    work = sorted(set(reduced))
    minimal: list[tuple[int, int]] = []
    while work:
        lh, rh = work.pop(0)
        if not _implies(minimal + work, lh, rh):
            minimal.append((lh, rh))
    return minimal


def _implies(rules: list[tuple[int, int]], lhs: int, rhs: int) -> bool:
    """rules 是否蕴含 lhs -> rhs (rhs 为单属性或属性集均可)。"""
    return _fixpoint(lhs, rules) & rhs == rhs


# --------------------------------------------------------------------------- #
# 候选键
# --------------------------------------------------------------------------- #
def _candidate_keys(attrs: str, rules: list[tuple[int, int]]) -> list[int]:
    """
    穷举求全部候选键: 按左部大小升序枚举, X+ = R 即为超键;
    剔除包含已发现键的超键。|R| <= 12 时规模可控。
    """
    n = len(attrs)
    full = (1 << n) - 1
    keys: list[int] = []
    # 从空集开始: ∅->... 能推出全 R 时 ∅ 本身就是 (唯一) 候选键。
    for size in range(0, n + 1):
        for comb in combinations(range(n), size):
            x = 0
            for i in comb:
                x |= 1 << i
            if _fixpoint(x, rules) == full:
                if not any(k & ~x == 0 for k in keys):  # 无更小真子集键
                    keys.append(x)
    return keys


# --------------------------------------------------------------------------- #
# 依赖保持: 投影后逐 FD 用分布 chase 检验
# --------------------------------------------------------------------------- #
def _projection_dependencies(
    attrs: str, rules: list[tuple[int, int]], sub: int
) -> list[tuple[int, int]]:
    """
    F 在子模式 S 上的完整投影 F[S]:
      { X -> Y | X,Y ⊆ S, X 是 S 上极大闭包种子(去冗余), Y = X+∩S \\ X 非空 }
    枚举 S 的全部子集求闭包, 只保留 X+∩S 严格大于 X 且 X 在该闭包下极大的
    种子, 再去掉可被其他种子蕴含的重复项。
    """
    n = len(attrs)
    sub_bits = [i for i in range(n) if sub >> i & 1]
    # 每个子集 X⊆S 的闭包 (在全集 F 下)
    closure_of: dict[int, int] = {}
    for size in range(len(sub_bits) + 1):
        for comb in combinations(sub_bits, size):
            x = 0
            for i in comb:
                x |= 1 << i
            closure_of[x] = _fixpoint(x, rules) & sub

    # 只保留闭包严格扩张的种子
    seeds = [x for x, c in closure_of.items() if c & ~x]
    # 去冗余: 若存在更小种子 x' 使 x' ⊆ x 且 closure(x')⊇closure(x)∩... 实际
    # 判定用蕴含: x -> y 已被 x' 的投影蕴含。先构造全部 X->(X+\\X),
    # 再逐条用其他投影依赖检验冗余。
    raw: list[tuple[int, int]] = []
    for x in seeds:
        y = closure_of[x] & ~x
        if y:
            raw.append((x, y))
    # 约简: 在演化工作集上逐条删除可被其余规则蕴含的规则
    #    (与最小覆盖同样的理由, 不能用已删除规则为保留规则辩护)。
    work = sorted(set(raw))
    kept: list[tuple[int, int]] = []
    while work:
        x, y = work.pop(0)
        if _fixpoint(x, kept + work) & y != y:
            kept.append((x, y))
    return kept


def _dependency_preserving(
    attrs: str,
    rules: list[tuple[int, int]],
    subs: list[int],
    projections: list[list[tuple[int, int]]],
) -> bool:
    """
    分布 chase 判定: 对每条原始 FD X->Y (右部单属性), 反复用各局部投影闭包
    扩张 X; X 在 ∪F[Ri] 下达到 Y 当且仅当保持。
    """
    flat = [r for proj in projections for r in proj]
    for lh, rh in rules:
        if _fixpoint(lh, flat) & rh != rh:
            return False
    return True


# --------------------------------------------------------------------------- #
# 符号表 chase (无损连接) + 逐步记录
# --------------------------------------------------------------------------- #
@dataclass
class _ChaseTrace:
    lossless: bool
    steps: list[dict]
    initial_rows: list[list[str]]
    final_rows: list[list[str]]
    sub_attrs: list[str]


def _chase(
    attrs: str, rules: list[tuple[int, int]], subs: list[int]
) -> _ChaseTrace:
    """
    经典符号表 chase。初表: 行 i (对应子模式 Ri) 在 Ri 的列上放同名常量
    a_j, 其余列放互不相同的变量 b_ij。

    每条 X->A: 找出 X 列上全等的行组, 把 A 列在组内等同 (取组内最小符号,
    若含常量 a_A 则常量优先)。全程记录每次等同。出现全 a 行 => 无损。

    确定性: 规则按输入顺序, 行组按最上行排序。空左部规则 (∅->A) 使全表 A 列
    等同, 自然被处理。
    """
    n = len(attrs)
    m = len(subs)

    def const_symbol(j: int) -> str:
        return f"a{j + 1}"

    # 初始符号表 (拷贝保留, 供可复验输出)
    initial_table = [
        [const_symbol(j) if subs[i] >> j & 1 else f"b{i + 1}{j + 1}"
         for j in range(n)]
        for i in range(m)
    ]
    table = [row[:] for row in initial_table]

    steps: list[dict] = []
    full_const_row = {const_symbol(j) for j in range(n)}

    def is_constant(sym: str) -> bool:
        return sym.startswith("a")

    def equalize_column(rows: list[int], j: int, fd_desc: str) -> bool:
        """把 rows 中各行在第 j 列等同; 返回是否发生改变。"""
        symbols = [table[r][j] for r in rows]
        consts = [s for s in symbols if is_constant(s)]
        target = consts[0] if consts else min(symbols)
        changed = False
        renames: list[dict] = []
        if len(set(symbols)) == 1:
            return False
        for r in rows:
            old = table[r][j]
            if old != target:
                # 旧符号在整表内被替换 (等同是等价类合并, 作用于全表)
                for rr in range(m):
                    for cc in range(n):
                        if table[rr][cc] == old:
                            table[rr][cc] = target
                renames.append({"row": r, "column": attrs[j],
                                "from": old, "to": target})
                changed = True
        if changed:
            steps.append({
                "fd": fd_desc,
                "matched_rows": [r + 1 for r in rows],
                "column": attrs[j],
                "equated": renames,
            })
        return changed

    progress = True
    while progress:
        progress = False
        for ri, (lh, rh) in enumerate(rules):
            fd_desc = f"{_to_str(lh, attrs) or '∅'} -> {_to_str(rh, attrs)}"
            rhs_cols = [j for j in range(n) if rh >> j & 1]
            # 按左部取值分组
            groups: dict[tuple[str, ...], list[int]] = {}
            for i in range(m):
                key = tuple(table[i][j] for j in range(n) if lh >> j & 1)
                groups.setdefault(key, []).append(i)
            for rows in groups.values():
                if len(rows) < 2:
                    continue
                for j in rhs_cols:
                    if equalize_column(sorted(rows), j, fd_desc):
                        progress = True
            if any(set(row) == full_const_row or
                   all(is_constant(s) for s in row) for row in table):
                return _ChaseTrace(True, steps, initial_table, table,
                                   [_to_str(s, attrs) for s in subs])
    lossless = any(all(is_constant(s) for s in row) for row in table)
    return _ChaseTrace(lossless, steps, initial_table, table,
                       [_to_str(s, attrs) for s in subs])


# --------------------------------------------------------------------------- #
# 有损反例: 从最终 chase 表构造二值关系
# --------------------------------------------------------------------------- #
def _counterexample(
    attrs: str, trace: _ChaseTrace
) -> dict:
    """
    chase 失败 => 最终符号表本身就是反例模板 (Aho–Beeri–Ullman tableau):
    每个符号类赋一个互不相同的整数值, 各行构成满足全部给定 FD 的关系 r
    (chase 已到不动点: 任两行左部相等则其右部列早应被等同)。

    "幽灵元组" t 在第 j 列取常量 a_j 的值。它不在 r 中: chase 失败意味着没有
    全 a 行, 每个原行至少在某列仍是 b 符号, 故 t 与每个原行不同; 而对每个
    子模式 Ri, 原表第 i 行在 Ri 各列上全是 a, 故 t 在每个投影中都有匹配,
    从而出现在自然连接结果里。这一般需要多于 2 个不同值 (tableau 赋值);
    二值域上的反例由测试用独立穷举预言机另行核对。

    返回 dict:
      values              : dict 符号 -> 整数值 (可复验的赋值)
      relation            : list[dict]  原关系 r (满足所有 FD)
      projection_relations: list[list[dict]]
      rejoined            : list[dict]  πR1(r) ⋈ ... ⋈ πRk(r)
      spurious_tuple      : dict        连接产生而 r 中没有的元组
      explanation         : str
    """
    n = len(attrs)
    m = len(trace.final_rows)

    # 确定性赋值: 最终表中出现的每个符号类按字典序编号
    symbols = sorted({s for row in trace.final_rows for s in row})
    values = {s: i + 1 for i, s in enumerate(symbols)}

    def val(sym: str) -> int:
        return values[sym]

    rows = [
        tuple(val(trace.final_rows[i][j]) for j in range(n))
        for i in range(m)
    ]
    relation = [{attrs[j]: row[j] for j in range(n)} for row in rows]

    # 各子模式上的投影
    proj_dicts: list[list[dict]] = []
    for i, sa in enumerate(trace.sub_attrs):
        cols = [attrs.index(a) for a in sa]
        seen: set[tuple] = set()
        tuples: list[dict] = []
        for row in rows:
            key = tuple(row[c] for c in cols)
            if key not in seen:
                seen.add(key)
                tuples.append({sa[k]: key[k] for k in range(len(cols))})
        proj_dicts.append(tuples)

    # 自然连接 (哈希连接)
    def nat_join(rel_a: list[dict], rel_b: list[dict]) -> list[dict]:
        if not rel_a:
            return [dict(t) for t in rel_b]
        if not rel_b:
            return [dict(t) for t in rel_a]
        common = sorted(set(rel_a[0]) & set(rel_b[0]))
        index: dict[tuple, list[dict]] = {}
        for t in rel_b:
            index.setdefault(tuple(t[c] for c in common), []).append(t)
        out: list[dict] = []
        for ta in rel_a:
            for tb in index.get(tuple(ta[c] for c in common), []):
                t = dict(ta)
                t.update(tb)
                out.append(t)
        return out

    joined: list[dict] = []
    for pd in proj_dicts:
        joined = nat_join(joined, pd)
    uniq: dict[tuple, dict] = {}
    for t in joined:
        key = tuple(t[a] for a in attrs)
        uniq[key] = {a: t[a] for a in attrs}
    rejoined = list(uniq.values())

    # 幽灵元组: 每列取 a_j 符号的值
    ghost_row = tuple(values[f"a{j + 1}"] for j in range(n))
    ghost = {attrs[j]: ghost_row[j] for j in range(n)}
    assert ghost_row not in set(rows), \
        "chase 失败却构造不出幽灵元组 (内部错误)"
    assert tuple(ghost[a] for a in attrs) in uniq, \
        "幽灵元组未出现在连接中 (内部错误)"

    # 叙述每个子模式由原表哪一行贡献幽灵元组的取值
    explain_lines: list[str] = []
    for i, sa in enumerate(trace.sub_attrs):
        gv = tuple(ghost[a] for a in sa)
        for k, row in enumerate(rows):
            if tuple(row[attrs.index(a)] for a in sa) == gv:
                explain_lines.append(
                    f"π_{sa}(r) 中取值 {dict(zip(sa, gv))} 来自 r 的第 {k + 1} 行"
                )
                break
    explanation = (
        "最终 chase 表不含全常量行 => 分解有损。对最终表的每个符号类赋互异整数 "
        "(见 values: 常量 a_j 与变量 b 均为独立符号), 各行给出满足全部给定 FD "
        "的关系 r; 逐列取常量符号得到的 spurious_tuple 与 r 中每一行都不同 "
        "(每行至少有一列仍是未提升的变量), 却在每个子模式投影中都有匹配, "
        "因此出现在投影的自然连接中——连接产生了原关系没有的元组。"
        + (" 贡献链: " + "; ".join(explain_lines) if explain_lines else "")
    )

    return {
        "values": values,
        "relation": relation,
        "projection_relations": proj_dicts,
        "rejoined": rejoined,
        "spurious_tuple": ghost,
        "explanation": explanation,
    }


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def analyze(
    attributes,
    fds,
    decomposition,
    *,
    max_chase_steps: int | None = None,
) -> AnalysisResult:
    """
    分析一个关系分解。

    参数
    ----
    attributes   : 属字符集合, 如 "ABCD" 或 ['A','B',...], 1..12 个。
    fds          : [{"lhs": "A", "rhs": "BC"}, ...]; lhs 允许 ""/None (空决定项)。
    decomposition: 子模式属性集合的列表, 如 ["AB","BC","CD"], 必须覆盖全部属性。
    max_chase_steps : chase 等同步数的显式预算; None 表示按上限取充分大的
                   确定性界。预算耗尽抛 BudgetExhaustedError, 不返回伪造结论。

    返回 AnalysisResult (见模块 docstring)。非法输入抛 InputError。
    """
    universe = _validate_attributes(attributes)
    fd_list = _validate_fds(fds, universe)
    subs_raw = _validate_decomposition(decomposition, universe)

    if max_chase_steps is not None:
        _validate_int(max_chase_steps, "max_chase_steps")
        if max_chase_steps <= 0:
            raise InputError("max_chase_steps: 必须为正整数或 None")

    attrs = "".join(sorted(universe))
    n = len(attrs)
    idx = _index_map(attrs)
    raw_rules = [(_to_mask(f.lhs, idx), _to_mask(f.rhs, idx)) for f in fd_list]
    # chase 在原始 F 上做 (与在最小覆盖上做等价; 用原始 F 记录更贴近题面)。
    chase_rules = raw_rules
    # 右部拆单属性供最小覆盖/键/投影使用
    split_rules: list[tuple[int, int]] = []
    for lh, rh in raw_rules:
        for j in range(n):
            if rh >> j & 1:
                split_rules.append((lh, 1 << j))
    split_rules = sorted(set(split_rules))

    minimal_masks = _minimal_cover(attrs, split_rules)
    key_masks = _candidate_keys(attrs, split_rules)

    sub_masks = [_to_mask(s, idx) for s in subs_raw]
    proj_masks = [
        _projection_dependencies(attrs, split_rules, sm) for sm in sub_masks
    ]
    preserving = _dependency_preserving(
        attrs, split_rules, sub_masks, proj_masks
    )

    trace = _chase(attrs, chase_rules, sub_masks)

    # 预算检查: 符号等同每步至少合并两个符号类, 符号类数从 n*m 单调下降,
    # 故 n*m 步内必终止; 记录步数超预算只可能来自调用方显式收紧。
    budget = max_chase_steps if max_chase_steps is not None else n * len(sub_masks)
    if len(trace.steps) > budget:
        raise BudgetExhaustedError(
            f"chase 步数 {len(trace.steps)} 超过预算 {budget}: "
            "搜索预算耗尽, 未作无损性结论"
        )

    result = AnalysisResult(
        attributes=attrs,
        minimal_cover=[
            FD(_to_str(lh, attrs), _to_str(rh, attrs))
            for lh, rh in minimal_masks
        ],
        candidate_keys=[_to_str(k, attrs) for k in key_masks],
        projections=[
            [FD(_to_str(lh, attrs), _to_str(rh, attrs)) for lh, rh in proj]
            for proj in proj_masks
        ],
        dependency_preserving=preserving,
        lossless=trace.lossless,
        chase_steps=trace.steps,
        initial_table=[row[:] for row in trace.initial_rows],
        final_table=[row[:] for row in trace.final_rows],
        counterexample=None if trace.lossless else _counterexample(attrs, trace),
    )
    return result
