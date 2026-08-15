"""纯 Python WordPiece 分词的守卫测试。

**为什么值得单独测**：它是静态语义查表的入口——切出来的 token 和蒸馏时差一个，
就查到别的行去了，向量变成噪音，而且**不会报任何错**，只会让检索悄悄变差。

装了 transformers 时会拿 HuggingFace 的 BertTokenizer 逐 token 对拍（开发机/CI）；
没装就退回固定用例（用户机不该为了跑测试装 2G 依赖）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from ivyea_agent import static_embedding, wordpiece


@pytest.fixture(scope="module")
def vocab():
    state = static_embedding._load()
    if not state:
        pytest.skip(f"静态向量表不可用：{static_embedding.unavailable_reason()}")
    return state["vocab"]


SAMPLES = [
    "广告优化预算怎么调",
    "高点击零单要不要否词",
    "B08XYZ123 的 ACoS 是 45%",
    "Listing 主图转化率低",
    "trail camera 4G WiFi 售后",
    "1688 供应商报价 ¥12.5/件",
    "  多个   空格\t和换行\n混在一起 ",
    "Émile naïve café",
    "①②③ 全角？！",
    "混合CJKand英文nospace",
    "」「【】〖〗",
]


def test_chinese_splits_per_character(vocab):
    toks = wordpiece.tokenize("广告优化", vocab)
    assert toks == ["广", "告", "优", "化"]


def test_english_uses_wordpiece_continuation(vocab):
    toks = wordpiece.tokenize("advertising", vocab)
    assert toks[0] != "[UNK]"
    assert all(t in vocab for t in toks)
    if len(toks) > 1:
        assert toks[1].startswith("##"), "续接片段必须带 ## 前缀"


def test_punctuation_is_split_off(vocab):
    assert wordpiece.tokenize("预算,增加", vocab) == ["预", "算", ",", "增", "加"]


def test_all_tokens_exist_in_vocab(vocab):
    for text in SAMPLES:
        for tok in wordpiece.tokenize(text, vocab):
            assert tok in vocab, f"{text!r} 切出了词表里没有的 {tok!r}"


def test_empty_input(vocab):
    assert wordpiece.tokenize("", vocab) == []
    assert wordpiece.tokenize("   \n\t", vocab) == []


def test_overlong_word_becomes_unk(vocab):
    assert wordpiece.tokenize("a" * 200, vocab) == ["[UNK]"]


def test_matches_huggingface_exactly():
    """和 HuggingFace 逐 token 对拍——这是这个模块唯一真正的正确性标准。"""
    transformers = pytest.importorskip("transformers")
    model_dir = Path.home() / ".ivyea/models/embedding/bge-small-zh-v1.5"
    if not (model_dir / "vocab.txt").exists():
        pytest.skip("本地没有 bge-small-zh-v1.5，跳过对拍")

    hf = transformers.AutoTokenizer.from_pretrained(str(model_dir))
    hf_vocab = hf.get_vocab()
    lower = bool(getattr(hf, "do_lower_case", False))

    # 我们自己的词表加载规则也必须和 HF 一致（空行 + 后来者覆盖，别用 splitlines）
    lines = (model_dir / "vocab.txt").read_text(encoding="utf-8").split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    assert {tok: i for i, tok in enumerate(lines)} == hf_vocab, "词表 id 映射与 HF 不一致"

    texts = list(SAMPLES)
    kb = Path(__file__).resolve().parents[1] / "ivyea_agent" / "knowledge_base"
    for p in sorted(kb.rglob("*.md"))[:20]:
        texts.append(p.read_text(encoding="utf-8")[:2000])

    for text in texts:
        assert wordpiece.tokenize(text, hf_vocab, lower=lower) == hf.tokenize(text), \
            f"分词与 HF 不一致：{text[:60]!r}"
