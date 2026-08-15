"""自带的语义向量后端：一张随包发布的静态查表，零配置、零联网、零额外依赖。

**为什么要有它**：语义检索一直是"可选项"——要么配一个 embedding API（多一个 key，
而且记忆正文要出网），要么装 sentence-transformers（pip 默认会拖 GB 级的 CUDA 版
torch，小内存机器直接被 OOM 杀掉）。两条路都有门槛，结果就是绝大多数用户装完
根本没有语义检索，换个说法一问就抓瞎。

**它是什么**：把 bge-small-zh-v1.5 提前"压扁"成 token → 向量的查表（模型跑一遍
词表，把每个 token 单独编码存下来）。用的时候不再跑神经网络，只是查表求加权平均。
中文模型的词表基本是字级的，只有 21128 个 token，int8 存下来几 MB，直接进 wheel。

**代价**：静态向量丢掉了上下文（"苹果手机"和"苹果水果"里的"苹果"拿到同一个向量），
质量比完整模型低一档。但对"换个说法能不能找回来"这件事，它远好于纯词法，
而且它是**默认就在**的——一个装完就能用的次优方案，胜过一个没人配的最优方案。
想要最好效果的用户仍可切到 API 或本地完整模型，见 `retrieval_embeddings`。

**做法（SIF，Arora 2017）**：token 向量按词频加权平均（高频虚词压低），再减掉语料
均值和第一主成分。最后这步不是可选的：不减的话所有文本的余弦都挤在 0.93 上下，
排序完全没有分辨力（实测均值 0.93 标准差 0.02 → 处理后 -0.01 / 0.15）。
统计量在打包时用随包的知识库算好固化进表，**不随用户数据变**，否则每加一条记忆
历史缓存里的向量就全部作废。
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

TABLE_NAME = "static_embedding.npz"

_LOCK = threading.Lock()
_STATE: Optional[Dict[str, Any]] = None
_FAILED = ""


def table_path() -> Path:
    return Path(__file__).resolve().parent / "data" / TABLE_NAME


def _load() -> Optional[Dict[str, Any]]:
    """惰性加载并常驻。失败只记原因不抛——语义是增强项，坏了要能安静退回词法。"""
    global _STATE, _FAILED
    if _STATE is not None or _FAILED:
        return _STATE
    with _LOCK:
        if _STATE is not None or _FAILED:
            return _STATE
        path = table_path()
        if not path.exists():
            _FAILED = f"静态向量表缺失：{path}"
            return None
        try:
            import numpy as np
        except Exception as exc:  # noqa: BLE001
            _FAILED = f"需要 numpy（随 pandas 一起装）：{exc}"
            return None
        try:
            z = np.load(path, allow_pickle=False)
            vocab_list = [str(t) for t in z["vocab"]]
            # 词表按"后来者覆盖"建索引：vocab.txt 里有 3 个空行，HuggingFace 就是这么处理的，
            # 换成"首次出现优先"会和蒸馏时用的 id 对不上，查表整体错位。
            vocab = {tok: i for i, tok in enumerate(vocab_list)}
            state = {
                "vectors": z["vectors"],                  # int8 [vocab, dim]
                "scale": z["scale"].astype(np.float32),   # 每维一个 scale
                "weights": z["weights"].astype(np.float32),
                "doc_mean": z["doc_mean"].astype(np.float32),
                "pcs": z["pcs"].astype(np.float32),
                "vocab": vocab,
                "unk": vocab.get("[UNK]", 100),
                "dim": int(z["scale"].shape[0]),
                # 向量缓存的 key 要带上它：重建一张同维度但配方不同的表之后，
                # 缓存里的历史向量必须失效，否则数值全错却不报错。
                "fingerprint": str(z["fingerprint"]) if "fingerprint" in z else "",
                "np": np,
            }
        except Exception as exc:  # noqa: BLE001
            _FAILED = f"静态向量表读取失败：{exc}"
            return None
        _STATE = state
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
    """这批向量的唯一标识，用于向量缓存分区。"""
    st = _load()
    if not st:
        return ""
    fp = st.get("fingerprint") or "nofp"
    return f"static-{st['dim']}-{fp}"


def encode(text: str) -> Optional[List[float]]:
    """一段文本 → 归一化后的稠密向量。空文本或表不可用时返回 None。"""
    st = _load()
    if not st:
        return None
    np = st["np"]
    from . import wordpiece

    vocab = st["vocab"]
    ids = [vocab.get(t, st["unk"]) for t in wordpiece.tokenize(text or "", vocab)]
    if not ids:
        return None
    idx = np.asarray(ids, dtype=np.int64)
    rows = st["vectors"][idx].astype(np.float32) * (st["scale"] / 127.0)
    w = st["weights"][idx][:, None]
    total = float(w.sum())
    if total <= 0:
        return None
    vec = (rows * w).sum(axis=0) / total
    vec = vec - st["doc_mean"]
    for pc in st["pcs"]:
        vec = vec - float(vec @ pc) * pc
    norm = float(np.linalg.norm(vec))
    if norm <= 0:
        return None
    return [round(float(v), 8) for v in (vec / norm)]


def info() -> Dict[str, Any]:
    st = _load()
    if not st:
        return {"available": False, "reason": _FAILED, "path": str(table_path())}
    return {
        "available": True,
        "path": str(table_path()),
        "vocab_size": len(st["vocab"]),
        "dimensions": st["dim"],
        "identity": identity(),
        "size_bytes": table_path().stat().st_size,
    }
