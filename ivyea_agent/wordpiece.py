"""BERT WordPiece 分词的纯 Python 实现（只为静态嵌入查表服务）。

**为什么要自己写**：静态嵌入（见 `static_embedding.py`）是一张 token → 向量的查表，
查表的前提是切出来的 token 和当初蒸馏时用的**完全一致**——差一个 token 就查到别的行，
向量直接是噪音。而官方的 `tokenizers` 是个 3.4MB 的二进制轮子，为了这点事再拖一个
带原生扩展的依赖不划算（还要跟着 Python 版本、平台、musl/glibc 一起做兼容矩阵）。

中文模型的词表基本是字级的（21128 个 token 里绝大多数是单个汉字），所以 WordPiece
这套规则在中文上几乎退化成"按字切"，实现起来并不复杂。真正需要小心的是英文那半边：
贪心最长匹配 + `##` 续接、标点单独成词、CJK 前后强制断开。

正确性不靠"看起来对"——`tests/test_wordpiece.py` 会拿 HuggingFace 的
`BertTokenizer` 在真实语料上逐 token 比对（装了 transformers 才跑，否则跳过）。
"""
from __future__ import annotations

import unicodedata
from typing import Dict, List

MAX_CHARS_PER_WORD = 100   # 和 BERT 原实现一致：超长"词"直接判 [UNK]，不做切分尝试


def _is_control(ch: str) -> bool:
    if ch in ("\t", "\n", "\r"):
        return False
    return unicodedata.category(ch).startswith("C")


def _is_whitespace(ch: str) -> bool:
    if ch in (" ", "\t", "\n", "\r"):
        return True
    return unicodedata.category(ch) == "Zs"


def _is_punctuation(ch: str) -> bool:
    """BERT 的定义比 Unicode 宽：ASCII 区的符号（$ + = ~ 等）也算标点。

    照搬原实现，不是随手写的——这里松一格紧一格，切出来的 token 就和词表对不上了。
    """
    cp = ord(ch)
    if (33 <= cp <= 47) or (58 <= cp <= 64) or (91 <= cp <= 96) or (123 <= cp <= 126):
        return True
    return unicodedata.category(ch).startswith("P")


def _is_cjk(ch: str) -> bool:
    """BERT 的 CJK 判定，用于"每个汉字前后都插空格"。

    刻意不含日文假名和韩文谚文——原实现就是这么划的（它们在词表里按 WordPiece 处理），
    跟着它走才能对齐。
    """
    cp = ord(ch)
    return (
        0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF
        or 0x20000 <= cp <= 0x2A6DF or 0x2A700 <= cp <= 0x2B73F
        or 0x2B740 <= cp <= 0x2B81F or 0x2B820 <= cp <= 0x2CEAF
        or 0xF900 <= cp <= 0xFAFF or 0x2F800 <= cp <= 0x2FA1F
    )


def _clean(text: str) -> str:
    out = []
    for ch in text:
        cp = ord(ch)
        if cp == 0 or cp == 0xFFFD or _is_control(ch):
            continue
        out.append(" " if _is_whitespace(ch) else ch)
    return "".join(out)


def _pad_cjk(text: str) -> str:
    out = []
    for ch in text:
        if _is_cjk(ch):
            out.extend((" ", ch, " "))
        else:
            out.append(ch)
    return "".join(out)


def _strip_accents(text: str) -> str:
    text = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")


def _split_punct(word: str) -> List[str]:
    pieces: List[str] = []
    buf: List[str] = []
    for ch in word:
        if _is_punctuation(ch):
            if buf:
                pieces.append("".join(buf))
                buf = []
            pieces.append(ch)
        else:
            buf.append(ch)
    if buf:
        pieces.append("".join(buf))
    return pieces


def basic_tokenize(text: str, lower: bool = False, strip_accents: bool = False) -> List[str]:
    """标点/空白/CJK 层面的粗切，WordPiece 之前的一步。"""
    text = _pad_cjk(_clean(text))
    words: List[str] = []
    for word in text.split():
        if lower:
            word = word.lower()
        if strip_accents:
            word = _strip_accents(word)
        words.extend(_split_punct(word))
    return words


def wordpiece(word: str, vocab: Dict[str, int], unk: str = "[UNK]") -> List[str]:
    """对单个词做贪心最长匹配；任何一段匹配不上，整个词退化成 [UNK]。

    "整个词退化"是 BERT 原语义，不是偷懒：切一半留一半会产出词表里根本没有的组合。
    """
    if len(word) > MAX_CHARS_PER_WORD:
        return [unk]
    tokens: List[str] = []
    start = 0
    while start < len(word):
        end = len(word)
        piece = None
        while start < end:
            candidate = word[start:end]
            if start > 0:
                candidate = "##" + candidate
            if candidate in vocab:
                piece = candidate
                break
            end -= 1
        if piece is None:
            return [unk]
        tokens.append(piece)
        start = end
    return tokens


def tokenize(text: str, vocab: Dict[str, int], *, lower: bool = False,
             strip_accents: bool = False, unk: str = "[UNK]") -> List[str]:
    """完整流程：粗切 → WordPiece。不加 [CLS]/[SEP]（查表不需要它们）。"""
    tokens: List[str] = []
    for word in basic_tokenize(text, lower=lower, strip_accents=strip_accents):
        tokens.extend(wordpiece(word, vocab, unk=unk))
    return tokens


def encode(text: str, vocab: Dict[str, int], **kwargs) -> List[int]:
    unk_id = vocab.get(kwargs.get("unk", "[UNK]"), 100)
    return [vocab.get(t, unk_id) for t in tokenize(text, vocab, **kwargs)]
