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


# ── 强信号词：判"这条真的相关"而不是"碰巧撞到一个字" ────────────────────────
#
# 自动注入（每轮无条件塞进上下文）的失败方式不是漏，是**乱**。实测三例：
#   · 「版本号写在哪个位置」→ 召回「DeepSeek harness 凭据配置位置」（只对上"位置"）
#   · 「把改动推送合并发版」→ 召回「console 双卡合并口径」（"合并"是另一个意思）
#   · 「给 ivyea-agent 加功能」→ 召回一堆 ivyea-note 的记忆（对上"ivyea"）
#
# 光靠库内文档频率（DF）挡不住：实测「位置」只出现在 14% 的记忆里、「合并」7%，
# DF 再低它们也不能作为"相关"的唯一凭据。所以要两层：
#
#   ① 这张表 —— **语言层**的弱信号词：出现在哪都不说明这句话是关于什么的。
#      注意它和 `skills._STOP_GRAMS` 不是一回事：那张表是虚词（什么/怎么/这个），
#      这张表里的全是实词，只是**单独出现时不足以证明相关**。
#   ② 调用方给的库内高频词（DF ≥ 阈值）—— 那是每个库自己的"万能词"，
#      比如记忆库里的 ivyea(86%)、note(57%)。
#
# 判据是"**至少一个**强信号重合"，不是"全部重合词都得强"：一句话里出现
# 「harness 的配置位置」时，harness 是强信号，位置是弱的，这条该召回。
WEAK_TERMS = frozenset("""
位置 地方 东西 内容 信息 配置 设置 选项 参数 功能 方案 方式 方法 要点 说明 记录
数据 结果 结论 状态 情况 进度 版本 更新 变更 改动 修改 调整 优化 分析 处理 操作
文件 目录 路径 项目 工程 代码 页面 界面 图片 照片 图像 时候 时间 日期 名字 名称
问题 错误 异常 报错 需求 任务 工作 内容 部分 地址 链接 合并 提交 推送 发布 发版
""".split())


#: 虚词/碎渣：出现在哪都不说明任何事。原来只长在 `skills._STOP_GRAMS` 里，
#: 但记忆那条路同样需要 —— 实测「我想给 ivyea-agent 加**一个**导出会话的功能」
#: 里的"一个"被当成了强信号。词法层的常识就该住在词法层。
STOP_GRAMS = frozenset("""
为什 什么 怎么 么办 如何 是否 可以 能否 需要 应该 这个 那个 这些 那些 一下 一个
我的 我们 你的 你们 他的 它的 帮我 帮忙 请问 麻烦 谢谢 你好 现在 目前 已经 还有
问题 情况 时候 之后 之前 上面 下面 里面 外面 出现 发生 导致 造成 提示 显示 告诉
不能 不了 没有 无法 不对 不行 怎样 多少 哪些 哪个 什麼 為什
""".split())

#: 中文按 2-gram 切，跨词边界会切出"话的""的功"这种碎片 —— 它们在两段文本里
#: 都出现纯属巧合。带虚字的 2-gram 一律不算信号，这比穷举碎片可靠。
_FUNCTION_CHARS = set("的了是在和与也都就而及或把被让给对从向为以之其")


def _is_fragment(token: str) -> bool:
    """跨词边界切出来的碎渣：两字片段里含虚字。"""
    return len(token) == 2 and any(c in _FUNCTION_CHARS for c in token)


def is_weak_term(token: str) -> bool:
    """这个词单独出现时，够不够证明"这条记忆/技能和这句话相关"。"""
    return token in WEAK_TERMS or token in STOP_GRAMS or _is_fragment(token)


def strong_overlap(query: str, text: str, *, common: frozenset | set = frozenset()) -> list[str]:
    """query 和 text 之间**有信息量**的重合词。空列表 = 只是碰巧撞到通用词。

    `common` 由调用方给：它是那个库自己的高频词集合（DF ≥ 阈值），各库不同 ——
    「图片」在技能库里是万能词（多个技能名都有），在记忆库里一次都没出现过。
    """
    q = set(tokenize(query))
    if not q:
        return []
    hit = q & set(tokenize(text))
    return [t for t in hit
            if len(t) >= 2 and not is_weak_term(t) and t not in common
            # 纯数字/纯年份不算信号：2026、08、24 在记忆库里 DF 高达 93%
            and not t.isdigit()]


def common_terms(docs, *, ratio: float = 0.30, min_docs: int = 5) -> frozenset:
    """语料里的"万能词"：出现在 ≥ratio 比例文档中的 token。

    `min_docs` 是样本量下限：库里只有三五条时，DF 统计纯属噪音（一个词出现两次
    就是 40%），这种情况下直接返回空集，只靠 WEAK_TERMS 那一层兜着。
    """
    docs = list(docs)
    if len(docs) < min_docs:
        return frozenset()
    from collections import Counter
    df: Counter = Counter()
    for d in docs:
        for t in set(tokenize(d)):
            df[t] += 1
    cut = max(2, int(len(docs) * ratio))
    return frozenset(t for t, c in df.items() if c >= cut)
