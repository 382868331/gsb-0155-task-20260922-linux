"""
独立参考实现 —— 不导入 fd_analysis, 不用被测核心的任何代码或结论。

用途:
  1. 教科书算法的另一份独立实现 (frozenset 闭包 / union-find chase);
  2. 关系层级语义预言机: FD 满足性、投影、自然连接;
  3. 小二值关系穷举: 有损性的"存在真实反例关系"语义判定。

这份代码刻意写得朴素直白, 与 fd_analysis.py 的位掩码/符号替换实现结构不同,
避免两边共享同一个 bug。
"""

from __future__ import annotations

from itertools import combinations

FD = tuple  # (frozenset[str] lhs, frozenset[str] rhs)


# --------------------------------------------------------------------------- #
# Armstrong 公理闭包 (最朴素的不动点)
# --------------------------------------------------------------------------- #
def armstrong_closure(attrs, fds, x):
    """fds: [(lhs_set, rhs_set), ...]; 返回 x 在 fds 下的闭包 (set)。"""
    result = set(x)
    changed = True
    while changed:
        changed = False
        for lhs, rhs in fds:
            if lhs <= result and not rhs <= result:
                result |= rhs
                changed = True
    return result


def implies(attrs, fds, x, y):
    return set(y) <= armstrong_closure(attrs, fds, x)


# --------------------------------------------------------------------------- #
# 候选键 (独立实现: 全集逐个枚举, 按大小)
# --------------------------------------------------------------------------- #
def candidate_keys(attrs, fds):
    attrs = frozenset(attrs)
    keys = []
    for size in range(0, len(attrs) + 1):
        for comb in combinations(sorted(attrs), size):
            x = frozenset(comb)
            if armstrong_closure(attrs, fds, x) == set(attrs):
                if not any(k < x for k in keys):
                    keys.append(x)
    return [set(k) for k in keys]


# --------------------------------------------------------------------------- #
# union-find chase (与被测核心的全局符号替换完全不同的实现路径)
# --------------------------------------------------------------------------- #
class _UF:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != x:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            # 常量符号 ('a', j) 优先作根
            rank = lambda s: (0 if s[0] == "a" else 1, s)
            if rank(ra) <= rank(rb):
                self.parent[rb] = ra
            else:
                self.parent[ra] = rb
            return True
        return False


def chase_lossless(attrs, fds, subs):
    """返回 (lossless: bool, equate_count: int)。"""
    attrs = "".join(attrs)
    n, m = len(attrs), len(subs)

    def a_sym(j):
        return ("a", j)

    def b_sym(i, j):
        return ("b", i, j)

    # 初始: 行 i 在 Ri 列上为常量, 否则为独有变量
    table = [
        [a_sym(j) if attrs[j] in subs[i] else b_sym(i, j) for j in range(n)]
        for i in range(m)
    ]
    uf = _UF()
    for row in table:
        for s in row:
            uf.find(s)

    equates = 0
    progress = True
    while progress:
        progress = False
        for lhs, rhs in fds:
            lcols = [attrs.index(c) for c in sorted(lhs)]
            rcols = [attrs.index(c) for c in sorted(rhs)]
            groups = {}
            for i in range(m):
                key = tuple(uf.find(table[i][j]) for j in lcols)
                groups.setdefault(key, []).append(i)
            for rows in groups.values():
                if len(rows) < 2:
                    continue
                for j in rcols:
                    roots = {uf.find(table[r][j]) for r in rows}
                    const = next((x for x in roots if x[0] == "a"), None)
                    target = const or min(roots)
                    for x in list(roots):
                        if x != target and uf.union(x, target):
                            equates += 1
                            progress = True
        for i in range(m):
            if all(uf.find(table[i][j])[0] == "a" for j in range(n)):
                return True, equates
    for i in range(m):
        if all(uf.find(table[i][j])[0] == "a" for j in range(n)):
            return True, equates
    return False, equates


# --------------------------------------------------------------------------- #
# 依赖保持 (独立实现: 子模式上所有 F+ 投影 + 分布闭包)
# --------------------------------------------------------------------------- #
def dependency_preserving(attrs, fds, subs):
    projected = []
    for s in subs:
        sset = set(s)
        for size in range(0, len(sset) + 1):
            for xcomb in combinations(sorted(sset), size):
                x = frozenset(xcomb)
                close = armstrong_closure(attrs, fds, x) & sset
                for a in sorted(close - x):
                    projected.append((x, frozenset([a])))
    for lhs, rhs in fds:
        for a in rhs:
            if a not in armstrong_closure(attrs, projected, lhs):
                return False
    return True


# --------------------------------------------------------------------------- #
# 关系层级语义 (tuple 用 dict)
# --------------------------------------------------------------------------- #
def relation_satisfies(rel, fds):
    """rel: list[dict]; fds: [(set_lhs, set_rhs)]。关系 (集合) 满足全部 FD?"""
    for lhs, rhs in fds:
        seen = {}
        for t in rel:
            key = tuple(t[c] for c in sorted(lhs))
            val = tuple(t[c] for c in sorted(rhs))
            if key in seen and seen[key] != val:
                return False
            seen[key] = val
    return True


def project(rel, schema):
    out, seen = [], set()
    for t in rel:
        key = tuple(t[c] for c in schema)
        if key not in seen:
            seen.add(key)
            out.append({c: t[c] for c in schema})
    return out


def natural_join(r1, r2):
    if not r1:
        return [dict(t) for t in r2]
    if not r2:
        return [dict(t) for t in r1]
    common = sorted(set(r1[0]) & set(r2[0]))
    index = {}
    for t in r2:
        index.setdefault(tuple(t[c] for c in common), []).append(t)
    out = []
    for ta in r1:
        for tb in index.get(tuple(ta[c] for c in common), ()):
            t = dict(ta)
            t.update(tb)
            out.append(t)
    return out


def rejoin(rel, attrs, subs):
    cur = []
    for s in subs:
        cur = natural_join(cur, project(rel, s))
    seen, out = set(), []
    for t in cur:
        key = tuple(t[c] for c in attrs)
        if key not in seen:
            seen.add(key)
            out.append({c: t[c] for c in attrs})
    return out


def has_spurious(rel, attrs, subs):
    keys = {tuple(t[c] for c in attrs) for t in rel}
    for t in rejoin(rel, attrs, subs):
        if tuple(t[c] for c in attrs) not in keys:
            return True
    return False


# --------------------------------------------------------------------------- #
# 二值关系穷举预言机
# --------------------------------------------------------------------------- #
def all_binary_relations(attrs, min_tuples=0, max_tuples=None):
    """
    生成 {0,1} 上的关系, 按元组数升序 (空关系与单tuple关系不可能违反 FD,
    反例搜索一般从 2 个元组开始)。
    """
    import itertools
    universe = [dict(zip(attrs, bits))
                for bits in itertools.product((0, 1), repeat=len(attrs))]
    hi = len(universe) if max_tuples is None else max_tuples
    for k in range(min_tuples, hi + 1):
        for comb in itertools.combinations(universe, k):
            yield list(comb)


def binary_counterexample(attrs, fds, subs):
    """
    按元组数升序穷举二值关系, 返回第一个"满足 F 但连接产生幽灵元组"的关系;
    找不到返回 None。早停: 反例通常只有 2 个元组。
    """
    for rel in all_binary_relations(attrs, min_tuples=2):
        if relation_satisfies(rel, fds) and has_spurious(rel, attrs, subs):
            return rel
    return None


def binary_oracle_lossy(attrs, fds, subs):
    """语义判定: 存在真实二值反例 => 'lossy', 否则 'no_binary_counterexample'。"""
    return "lossy" if binary_counterexample(attrs, fds, subs) is not None \
        else "no_binary_counterexample"
