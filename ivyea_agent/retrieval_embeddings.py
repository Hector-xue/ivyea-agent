"""Embedding backend selection for local retrieval.

Default retrieval stays dependency-free. If users explicitly configure the
optional sentence-transformers backend and provide/install the dependency, the
same index contract can store dense local embeddings instead of sparse hashes.
"""
from __future__ import annotations

import importlib.util
import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any

from . import config


HASH_BACKEND = "local_hash_embedding_v1"
SENTENCE_BACKEND = "sentence-transformers"
DEFAULT_SENTENCE_MODEL = "BAAI/bge-small-zh-v1.5"

QUERY_ALIASES = {
    "主图": "main image hero image",
    "图片": "image creative content",
    "转化": "conversion convert cvr",
    "否词": "negative keyword negative targeting",
    "预算": "budget",
    "竞价": "bid bidding",
    "出价": "bid bidding",
    "点击": "click ctr",
    "订单": "order orders",
    "广告": "advertising sponsored products",
    "搜索词": "search term query",
    "关键词": "keyword targeting",
    "竞品": "competitor conquesting",
    "评论": "review reviews",
    "库存": "inventory stock",
    "利润": "profit margin",
    "价格": "price offer",
    "放量": "scaling scale budget bid",
}

_MODEL_CACHE: dict[str, Any] = {}


def status() -> dict[str, Any]:
    backend = _normal_backend(str(config.get_setting("retrieval_embedding_backend", "hash")))
    model = str(config.get_setting("retrieval_embedding_model", DEFAULT_SENTENCE_MODEL) or DEFAULT_SENTENCE_MODEL)
    model_path = str(config.get_setting("retrieval_embedding_model_path", "") or "")
    allow_download = bool(config.get_setting("retrieval_embedding_allow_download", False))
    package_available = importlib.util.find_spec("sentence_transformers") is not None
    path_exists = bool(model_path and Path(model_path).expanduser().exists())

    semantic_requested = backend == SENTENCE_BACKEND
    semantic_ready = semantic_requested and package_available and (path_exists or allow_download)
    fallback_reason = ""
    if semantic_requested and not package_available:
        fallback_reason = "sentence-transformers is not installed"
    elif semantic_requested and not path_exists and not allow_download:
        fallback_reason = "model path is not configured and auto-download is disabled"

    return {
        "configured_backend": backend,
        "active_backend": SENTENCE_BACKEND if semantic_ready else HASH_BACKEND,
        "semantic_enabled": semantic_ready,
        "vector_kind": "dense" if semantic_ready else "sparse",
        "model": model,
        "model_path": model_path,
        "model_path_exists": path_exists,
        "allow_download": allow_download,
        "package_available": package_available,
        "fallback_reason": fallback_reason,
        "external_dependency": semantic_ready,
        "install_hint": "python -m pip install 'ivyea-agent[semantic]'"
        if semantic_requested and not package_available else "",
    }


def configure(
    *,
    backend: str = "",
    model: str = "",
    model_path: str | None = None,
    allow_download: bool | None = None,
) -> dict[str, Any]:
    if backend:
        config.set_setting("retrieval_embedding_backend", _normal_backend(backend))
    if model:
        config.set_setting("retrieval_embedding_model", model)
    if model_path is not None:
        config.set_setting("retrieval_embedding_model_path", model_path)
    if allow_download is not None:
        config.set_setting("retrieval_embedding_allow_download", bool(allow_download))
    return status()


def encode_document(text: str) -> dict[str, Any]:
    return _encode(text)


def encode_query(text: str) -> dict[str, Any]:
    return _encode(_expand_query(text))


def decode(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"kind": "sparse", "backend": HASH_BACKEND, "values": {}}
    if isinstance(data, dict) and data.get("kind") and "values" in data:
        return {
            "kind": str(data.get("kind") or "sparse"),
            "backend": str(data.get("backend") or HASH_BACKEND),
            "values": data.get("values") or {},
        }
    if isinstance(data, dict):
        return {"kind": "sparse", "backend": HASH_BACKEND, "values": {str(k): float(v) for k, v in data.items()}}
    return {"kind": "sparse", "backend": HASH_BACKEND, "values": {}}


def cosine(left: dict[str, Any], right: dict[str, Any]) -> float:
    if (left.get("kind") or "sparse") != (right.get("kind") or "sparse"):
        return 0.0
    if left.get("kind") == "dense":
        return _dense_cosine(_dense_values(left.get("values")), _dense_values(right.get("values")))
    return _sparse_cosine(_sparse_values(left.get("values")), _sparse_values(right.get("values")))


def _encode(text: str) -> dict[str, Any]:
    st = status()
    if st["semantic_enabled"]:
        values = _sentence_vector(text, st)
        return {"kind": "dense", "backend": SENTENCE_BACKEND, "model": st["model"], "values": values}
    return {"kind": "sparse", "backend": HASH_BACKEND, "values": _hash_vector(text)}


def _sentence_vector(text: str, st: dict[str, Any]) -> list[float]:
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("sentence-transformers is not installed") from exc
    key = st.get("model_path") or st.get("model") or DEFAULT_SENTENCE_MODEL
    if key not in _MODEL_CACHE:
        _MODEL_CACHE[key] = SentenceTransformer(str(key))
    model = _MODEL_CACHE[key]
    encoded = model.encode([text], normalize_embeddings=True, show_progress_bar=False)
    first = encoded[0]
    if hasattr(first, "tolist"):
        first = first.tolist()
    return [round(float(v), 8) for v in first]


def _normal_backend(value: str) -> str:
    raw = (value or "hash").strip().lower().replace("_", "-")
    if raw in ("sentence", "sentence-transformer", "sentence-transformers", "semantic"):
        return SENTENCE_BACKEND
    return "hash"


def _expand_query(text: str) -> str:
    low = text.lower()
    extras = [alias for term, alias in QUERY_ALIASES.items() if term.lower() in low]
    return " ".join([text, *extras])


def _tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in re.findall(r"[A-Za-z0-9+.-]+|[\u4e00-\u9fff]+", text.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", raw):
            if len(raw) <= 2:
                tokens.append(raw)
            else:
                tokens.extend(raw[i:i + 2] for i in range(len(raw) - 1))
        else:
            tokens.append(raw)
    return tokens


def _hash_vector(text: str) -> dict[str, float]:
    counts = Counter(_tokens(text))
    total = sum(counts.values()) or 1
    return {k: round(v / total, 8) for k, v in counts.items()}


def _sparse_values(value: Any) -> dict[str, float]:
    return {str(k): float(v) for k, v in value.items()} if isinstance(value, dict) else {}


def _dense_values(value: Any) -> list[float]:
    return [float(v) for v in value] if isinstance(value, list) else []


def _sparse_cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(value * right.get(key, 0.0) for key, value in left.items())
    if dot <= 0:
        return 0.0
    ln = math.sqrt(sum(v * v for v in left.values()))
    rn = math.sqrt(sum(v * v for v in right.values()))
    return dot / (ln * rn) if ln and rn else 0.0


def _dense_cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    if dot <= 0:
        return 0.0
    ln = math.sqrt(sum(v * v for v in left))
    rn = math.sqrt(sum(v * v for v in right))
    return dot / (ln * rn) if ln and rn else 0.0
