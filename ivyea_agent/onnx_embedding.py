"""自带的语义向量后端：随包发布的 bge-small-zh，用 onnxruntime 跑，零配置零联网。

**为什么要有它**：语义检索一直是"可选项"——要么配一个 embedding API（多一个 key，
而且记忆正文要出网），要么装 sentence-transformers（pip 默认给 Linux 拉 GB 级的
CUDA 版 torch，小内存机器直接被 OOM 杀掉）。两条路都有门槛，结果是绝大多数用户
装完根本没有语义检索——换个说法一问就抓瞎。

**怎么做到自带**：模型导出成 ONNX 并 int8 动态量化，24MB 进 wheel；运行时只要
onnxruntime（纯 CPU 轮子约 19MB，各平台都有），比 torch 那条路小两个数量级。
分词用自己的纯 Python WordPiece（见 `wordpiece.py`），连 tokenizers 都不用引。

**实测（156 条中文口语提问 / 52 篇中文记忆语料，问题措辞由模型改写过，不含原文词）**：

    词法（基线）              MRR 0.482
    自带 ONNX int8 + 融合     MRR 0.534   词法召不回的 60 条里净救回 14 条
    torch fp32 + 融合         MRR 0.560   净救回 13 条

也就是说量化几乎不掉点，而依赖只有 torch 那条路的 1/50。

**别改错的两件事**（都是实测踩出来的）：
  1. 截断长度必须 512。曾经按 256 截断评测，结论直接反了——当时以为"int8 掉点严重"，
     其实是把文档砍掉一半。
  2. 池化是 CLS + L2 归一化，这两步已经编进 ONNX 图里，这里不要再算一遍。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

MODEL_NAME = "embedding_int8.onnx"
VOCAB_NAME = "embedding_vocab.txt"
META_NAME = "embedding_meta.json"
MAX_SEQ = 512
# 单批最多几条。批大太占内存（长文档 padding 到最长那条），太小又浪费；
# 8 条 × 512 token 在实测机器上稳定，内存峰值约 84MB。
BATCH = 8

_LOCK = threading.Lock()
_STATE: Optional[Dict[str, Any]] = None
_FAILED = ""


def data_dir() -> Path:
    return Path(__file__).resolve().parent / "data"


def model_path() -> Path:
    return data_dir() / MODEL_NAME


def _load_vocab(path: Path) -> Dict[str, int]:
    """必须和 HuggingFace 的 load_vocab 逐条一致（已实测相等）。

    两个坑：vocab.txt 里有 3 个**空行**，HF 是"后来者覆盖"；以及**不能用
    splitlines()**——它对这个文件会多切出两行，id 整体错位，向量全错却不报错。
    """
    lines = path.read_text(encoding="utf-8").split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return {tok: i for i, tok in enumerate(lines)}


def _load() -> Optional[Dict[str, Any]]:
    """惰性加载并常驻。失败只记原因不抛——语义是增强项，坏了要能安静退回词法。"""
    global _STATE, _FAILED
    if _STATE is not None or _FAILED:
        return _STATE
    with _LOCK:
        if _STATE is not None or _FAILED:
            return _STATE
        model = model_path()
        vocab_file = data_dir() / VOCAB_NAME
        if not model.exists():
            _FAILED = f"内置 embedding 模型缺失：{model}"
            return None
        if not vocab_file.exists():
            _FAILED = f"内置词表缺失：{vocab_file}"
            return None
        try:
            import onnxruntime as ort
        except Exception as exc:  # noqa: BLE001
            _FAILED = ("onnxruntime 未安装（它是 ivyea-agent 的依赖，正常安装会自带；"
                       f"冷门平台可能没有轮子）：{exc}")
            return None
        try:
            import numpy as np

            opts = ort.SessionOptions()
            # 不限制线程数的话 onnxruntime 会按核数起满，在小机器上和主进程抢资源，
            # 而我们这里是"顺手算个向量"，不是要榨干 CPU。
            opts.intra_op_num_threads = 2
            opts.inter_op_num_threads = 1
            opts.log_severity_level = 3
            session = ort.InferenceSession(str(model), opts, providers=["CPUExecutionProvider"])
            vocab = _load_vocab(vocab_file)
            meta = {}
            meta_file = data_dir() / META_NAME
            if meta_file.exists():
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            _FAILED = f"内置 embedding 模型加载失败：{exc}"
            return None
        _STATE = {
            "session": session,
            "vocab": vocab,
            "cls": vocab.get("[CLS]", 101),
            "sep": vocab.get("[SEP]", 102),
            "unk": vocab.get("[UNK]", 100),
            "pad": vocab.get("[PAD]", 0),
            "meta": meta,
            "dim": int(meta.get("dimensions") or 512),
            "np": np,
        }
        return _STATE


def available() -> bool:
    return _load() is not None


def unavailable_reason() -> str:
    _load()
    return _FAILED


def dimensions() -> int:
    st = _load()
    return int(st["dim"]) if st else 0


def identity() -> str:
    """这批向量的唯一标识，用于向量缓存分区。换了模型/量化方式，旧缓存必须失效。"""
    st = _load()
    if not st:
        return ""
    fp = str(st["meta"].get("fingerprint") or "nofp")
    return f"bge-small-zh-int8-{fp}"


def _token_ids(text: str, st: Dict[str, Any]) -> List[int]:
    from . import wordpiece

    vocab = st["vocab"]
    ids = [vocab.get(t, st["unk"]) for t in wordpiece.tokenize(text or "", vocab)]
    if not ids:
        return []
    # 留两格给 [CLS]/[SEP]。**512 不是随便定的**：按 256 截断会把长文档砍掉一半，
    # 实测足以让整个评测结论反过来。
    return [st["cls"], *ids[: MAX_SEQ - 2], st["sep"]]


def encode_many(texts: Sequence[str]) -> List[Optional[List[float]]]:
    """批量接口，但**内部逐条编码**。返回与输入等长的列表，切不出 token 的位置是 None。

    为什么不真的批：int8 动态量化的激活 scale 是按**整个张量**现算的，同一段文本
    和不同的邻居凑一批，算出来的向量就不一样（实测余弦 0.99，且等长同批也一样会差，
    所以不是 padding 的问题，改不掉）。后果很隐蔽——缓存里的向量和重算的对不上，
    重建一次索引结果就变，排序在近似并列处会莫名其妙地抖。
    而实测批量对**长文档**（建索引的真实场景）只快 1.2×，为这点速度换掉确定性不划算。
    """
    st = _load()
    if not st:
        return [None] * len(texts)
    np = st["np"]
    out: List[Optional[List[float]]] = []
    for text in texts:
        ids = _token_ids(text, st)
        if not ids:
            out.append(None)
            continue
        arr = np.array([ids], dtype=np.int64)
        vec = st["session"].run(None, {"input_ids": arr,
                                       "attention_mask": np.ones_like(arr)})[0][0]
        out.append([round(float(v), 8) for v in vec])
    return out


def encode(text: str) -> Optional[List[float]]:
    """一段文本 → 归一化后的稠密向量（归一化已编进 ONNX 图，这里不再算）。"""
    return encode_many([text])[0]


def info() -> Dict[str, Any]:
    st = _load()
    if not st:
        return {"available": False, "reason": _FAILED, "path": str(model_path())}
    meta = st["meta"]
    return {
        "available": True,
        "path": str(model_path()),
        "source_model": meta.get("source_model", ""),
        "dimensions": st["dim"],
        "max_seq_length": int(meta.get("max_seq_length") or MAX_SEQ),
        "quantization": meta.get("quantization", ""),
        "identity": identity(),
        "size_bytes": model_path().stat().st_size,
    }
