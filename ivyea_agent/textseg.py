"""中文可用的轻量分词：CJK bigram + 拉丁/数字整词保留。

**为什么需要这个模块**：FTS5 自带的 unicode61 tokenizer 把连续中文当成**一个** token
（"广告优化预算" = 1 个 token），MATCH 只有整段一模一样才命中，等于中文全文检索直接失效。
历史上 `memory.search()` 只能靠 `LIKE '%...%'` 子串兜底——换个说法就抓瞎，
这是"文件里明明有、recall 却说没有"的根因之一。

**为什么不用 jieba/结巴**：ivyea-agent 是 `pip install` 装的 CLI，为了检索拖一个带词典的
依赖不划算（而且 jieba 首次加载要建前缀树，冷启动明显变慢）。bigram 无词典、零依赖、
对检索场景召回够用——检索要的是"能捞回来"，精排交给 BM25 和后续的重排。

**切分策略与取舍**：
- **中文按相邻两字切**（"广告优化" → 广告 / 告优 / 优化）。好处是不需要词典也能匹配
  "优化广告"这类换序说法；代价是索引体积约等于原文字数，语料小无所谓。
- **拉丁与数字整词保留**（"B08XYZ123"、"ACoS"、"2026" 不切碎）。这条是刻意的：
  ASIN / SKU / 广告词原文这类精确标识符恰恰是向量检索最不擅长的
  （B08XYZ123 和 B08XYZ124 在语义空间里几乎重合），必须靠词法这一路兜住。
"""
from __future__ import annotations

import re
from typing import Iterable, List

# CJK 统一表意文字 + 扩展A + 日文假名 + 谚文。按"块"切，块内做 bigram。
_CJK_RANGES = r"㐀-䶿一-鿿぀-ヿ가-힯豈-﫿"

# 一次扫描切出两类 run：CJK 连续块，或 拉丁/数字连续块。其余字符（标点、空白、emoji）
# 天然成为分隔符被丢弃——它们对检索没有信息量，还会污染 FTS 查询语法。
_RUN = re.compile(rf"[{_CJK_RANGES}]+|[A-Za-z0-9_]+")
_IS_CJK = re.compile(rf"[{_CJK_RANGES}]")

# 单条文本的 token 上限：防止有人把一整份报告 remember 进来时索引爆掉。
# 4000 字的中文 → 约 4000 个 bigram，仍然是毫秒级，够宽松了。
MAX_TOKENS = 8000


def _cjk_tokens(run: str) -> Iterable[str]:
    """CJK 块 → bigram。单字块退化为该字本身，否则单字查询永远召不回。"""
    if len(run) == 1:
        yield run
        return
    for i in range(len(run) - 1):
        yield run[i:i + 2]


def tokenize(text: str) -> List[str]:
    """切出用于**建索引**的 token 序列（保留重复，BM25 要靠词频算分）。"""
    if not text:
        return []
    out: List[str] = []
    for run in _RUN.findall(text):
        if _IS_CJK.match(run):
            out.extend(_cjk_tokens(run))
        else:
            # 统一小写：查询 "acos" 要能命中正文里的 "ACoS"。
            out.append(run.lower())
        if len(out) >= MAX_TOKENS:
            return out[:MAX_TOKENS]
    return out


def index_text(text: str) -> str:
    """建索引用：把 token 用空格连起来，交给 FTS5 的 unicode61 按空格切。

    这样做的意义是我们**自己完成了分词**，unicode61 只负责按空白切开，
    于是中文也能进倒排索引、也能吃到 FTS5 内建的 bm25() 排序。
    """
    return " ".join(tokenize(text))


def _escape(token: str) -> str:
    """FTS5 字符串字面量里双引号需要翻倍。tokenize 的产物只含字母数字下划线和 CJK，
    正常到不了这一步；保留是为了防御未来放宽字符集时被注入语法。"""
    return token.replace('"', '""')


def match_query(text: str, limit: int = 64) -> str:
    """查询用：把查询串切成 token 并组成 FTS5 的 OR 查询。

    用 OR 而不是 AND：召回优先，排序交给 bm25()——命中 token 越多、越稀有的文档分越高，
    自然排到前面。AND 在 bigram 下过于严格（错一个字就全丢），实测会把该召回的挡掉。

    `limit` 限制参与查询的 token 数：超长查询用 OR 连成几千个子句会让 FTS5 明显变慢，
    而检索意图通常在前几十个 token 里就表达完了。
    """
    seen = []
    known = set()
    for tok in tokenize(text):
        if tok not in known:
            known.add(tok)
            seen.append(tok)
        if len(seen) >= limit:
            break
    if not seen:
        return ""
    return " OR ".join(f'"{_escape(t)}"' for t in seen)


def overlap_score(query: str, text: str) -> float:
    """无 FTS 时的兜底打分：token 集合的 Jaccard-ish 覆盖率（命中 query token 的比例）。

    只在 FTS5 不可用（极老的 sqlite 编译选项）时用到，纯 Python、无依赖。
    """
    q = set(tokenize(query))
    if not q:
        return 0.0
    t = set(tokenize(text))
    if not t:
        return 0.0
    return len(q & t) / len(q)
