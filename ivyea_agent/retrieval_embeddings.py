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
# 配置值 "sparse" = 显式关掉语义。**不能存成 "hash"**：那个字符串被当作"老的默认值"
# 迁移到 builtin 了，存 hash 等于关不掉。
SPARSE_BACKEND = "sparse"
BUILTIN_BACKEND = "builtin"
SENTENCE_BACKEND = "sentence-transformers"
API_BACKEND = "api"
# 默认后端。**刻意是 builtin 而不是 hash**：hash 只是词频稀疏向量，根本不是语义，
# 而语义检索以前要么配 API（多一个 key + 记忆正文出网）、要么装 2G 依赖——
# 门槛的结果是绝大多数用户装完压根没有语义。builtin 是随包发布的 bge-small-zh
# （ONNX int8，24MB），零配置零联网装完即用；要更强的仍可切 api / sentence-transformers。
DEFAULT_BACKEND = BUILTIN_BACKEND
DEFAULT_SENTENCE_MODEL = "BAAI/bge-small-zh-v1.5"
# 通用 OpenAI 兼容 /v1/embeddings 的默认值。刻意不绑定任何供应商：
# ivyea-agent 是自托管 CLI，主脑可能是 DeepSeek（**实测无 embeddings 接口，404**）、
# 也可能是硅基流动/OpenAI/Jina/本地 Ollama。用户指哪打哪，才不会因为换了主脑就没语义检索。
DEFAULT_API_MODEL = "BAAI/bge-m3"
# 内置模型的来源，只用于对外显示"这批向量是谁生成的"
BUILTIN_MODEL = "bge-small-zh-v1.5-int8"
API_TIMEOUT = 30.0

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


def api_settings() -> dict[str, str]:
    """通用 OpenAI 兼容 embeddings 端点配置。key 走环境变量名而不是明文存 settings.json，
    和主脑 provider 的既有做法保持一致（settings.json 不该躺着密钥）。"""
    import os
    config.load_env()   # ~/.ivyea/.env → os.environ（不覆盖已有），和主脑取 key 走同一条路
    base = str(config.get_setting("retrieval_embedding_api_base", "") or "").rstrip("/")
    model = str(config.get_setting("retrieval_embedding_api_model", "") or DEFAULT_API_MODEL)
    key_env = str(config.get_setting("retrieval_embedding_api_key_env", "") or "EMBEDDING_API_KEY")
    return {"base": base, "model": model, "key_env": key_env,
            "key": os.environ.get(key_env, "")}


def status() -> dict[str, Any]:
    backend = _normal_backend(str(config.get_setting("retrieval_embedding_backend", DEFAULT_BACKEND)))
    model = str(config.get_setting("retrieval_embedding_model", DEFAULT_SENTENCE_MODEL) or DEFAULT_SENTENCE_MODEL)
    model_path = str(config.get_setting("retrieval_embedding_model_path", "") or "")
    allow_download = bool(config.get_setting("retrieval_embedding_allow_download", False))
    package_available = importlib.util.find_spec("sentence_transformers") is not None
    path_exists = bool(model_path and Path(model_path).expanduser().exists())
    local_candidates = _local_model_candidates()

    from . import onnx_embedding

    api = api_settings()
    api_requested = backend == API_BACKEND
    api_ready = api_requested and bool(api["base"]) and bool(api["key"])

    builtin_requested = backend == BUILTIN_BACKEND
    # 内置模型是否**可用**，与用户配没配它无关：它是所有 dense 后端的兜底。
    # 用户配了 API 但 key 没填、配了本地模型但没装依赖——这些情况该退到内置语义，
    # 而不是一路退回"没有语义"。配错一个字就把整个语义层关掉，太苛刻了。
    builtin_available = onnx_embedding.available()
    builtin_ready = builtin_requested and builtin_available
    sparse_requested = backend == SPARSE_BACKEND

    semantic_requested = backend == SENTENCE_BACKEND
    semantic_ready = semantic_requested and package_available and (path_exists or allow_download)
    fallback_reason = ""
    if semantic_requested and not package_available:
        fallback_reason = "sentence-transformers is not installed"
    elif semantic_requested and not path_exists and not allow_download:
        fallback_reason = "model path is not configured and auto-download is disabled"
        if local_candidates:
            fallback_reason += "; local model candidates are available"
    elif api_requested and not api["base"]:
        fallback_reason = "retrieval_embedding_api_base is not configured"
    elif api_requested and not api["key"]:
        fallback_reason = f"环境变量 {api['key_env']} 未设置（在 ~/.ivyea/.env 里配）"
    elif builtin_requested and not builtin_ready:
        fallback_reason = onnx_embedding.unavailable_reason() or "内置 embedding 模型不可用"

    if sparse_requested:
        active, kind, dense = HASH_BACKEND, "sparse", False
    elif api_ready:
        active, kind, dense = API_BACKEND, "dense", True
    elif semantic_ready:
        active, kind, dense = SENTENCE_BACKEND, "dense", True
    elif builtin_available:
        active, kind, dense = BUILTIN_BACKEND, "dense", True
    else:
        active, kind, dense = HASH_BACKEND, "sparse", False

    return {
        "configured_backend": backend,
        "active_backend": active,
        "semantic_enabled": dense,
        "vector_kind": kind,
        "api_base": api["base"],
        "api_model": api["model"],
        "api_key_env": api["key_env"],
        "api_ready": api_ready,
        "builtin_ready": builtin_ready,
        "builtin_model": onnx_embedding.info(),
        "model": model,
        "model_path": model_path,
        "model_path_exists": path_exists,
        "local_model_dir": str(_local_model_root()),
        "local_model_candidates": local_candidates,
        "offline_model_available": bool(local_candidates),
        "allow_download": allow_download,
        "package_available": package_available,
        "fallback_reason": fallback_reason,
        "external_dependency": semantic_ready,
        "offline_safe": not semantic_ready,
        "probe_required_for_dense": semantic_ready,
        "cache_hint": "pre-download the sentence-transformers model into retrieval_embedding_model_path or ~/.ivyea/models/embedding for offline use",
        "install_hint": "python -m pip install 'ivyea-agent[semantic]'"
        if semantic_requested and not package_available else (
            "run `ivyea retrieval embeddings --backend sentence-transformers --model-path <candidate> --no-download`"
            if semantic_requested and local_candidates and not path_exists and not allow_download else ""
        ),
    }


def configure(
    *,
    backend: str = "",
    model: str = "",
    model_path: str | None = None,
    allow_download: bool | None = None,
    api_base: str = "",
    api_model: str = "",
    api_key_env: str = "",
) -> dict[str, Any]:
    if backend:
        config.set_setting("retrieval_embedding_backend", _normal_backend(backend))
    if model:
        config.set_setting("retrieval_embedding_model", model)
    if model_path is not None:
        config.set_setting("retrieval_embedding_model_path", model_path)
    if allow_download is not None:
        config.set_setting("retrieval_embedding_allow_download", bool(allow_download))
    if api_base:
        config.set_setting("retrieval_embedding_api_base", api_base.rstrip("/"))
    if api_model:
        config.set_setting("retrieval_embedding_api_model", api_model)
    if api_key_env:
        config.set_setting("retrieval_embedding_api_key_env", api_key_env)
    return status()


def encode_document(text: str) -> dict[str, Any]:
    return _encode(text)


def encode_sparse(text: str) -> dict[str, Any]:
    """强制走词频稀疏向量，不看当前配置。

    给 `retrieval_index` 这类**成千上万个分块**的场景用：它们要的是"零成本、确定性、
    可即时重建"的基底，不是语义。用真模型编码 1538 个分块要 6 分钟，塞进一次搜索里
    就是卡死——而搜索本来就会在索引缺失时同步重建。
    """
    return _hash_payload(text)


def encode_sparse_query(text: str) -> dict[str, Any]:
    """稀疏向量的**查询**侧编码：先做别名扩展再编码。

    别漏掉扩展这一步：知识卡正文是英文，用户查的是中文，全靠 QUERY_ALIASES 把
    "主图"扩成 "main image hero image" 才能命中。少了它，中文查询对英文语料直接零命中
    （实测 `retrieval_index.search("主图 转化")` 从有结果变成空）。
    """
    return _hash_payload(_expand_query(text))


def encode_query(text: str) -> dict[str, Any]:
    return _encode(_expand_query(text))


def probe(text: str = "ivyea retrieval embedding probe") -> dict[str, Any]:
    """Try the active embedding backend and report whether dense vectors really work."""
    st = status()
    if not st["semantic_enabled"]:
        return {
            "ok": True,
            "ready": True,
            "active_backend": HASH_BACKEND,
            "vector_kind": "sparse",
            "fallback_reason": st.get("fallback_reason", ""),
            "status": st,
        }
    active = st["active_backend"]
    try:
        if active == API_BACKEND:
            values = _api_vector(text, st)
        elif active == BUILTIN_BACKEND:
            values = _builtin_vector(text)
        else:
            values = _sentence_vector(text, st)
    except Exception as exc:
        return {
            "ok": False,
            "ready": False,
            "active_backend": HASH_BACKEND,
            "vector_kind": "sparse",
            "fallback_reason": f"{type(exc).__name__}: {exc}",
            "status": st,
        }
    return {
        "ok": True,
        "ready": True,
        "active_backend": active,
        "vector_kind": "dense",
        "dimensions": len(values),
        "status": st,
    }


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
    active = st["active_backend"]
    if active == API_BACKEND:
        try:
            values = _api_vector(text, st)
            return {"kind": "dense", "backend": API_BACKEND, "model": st["api_model"], "values": values}
        except Exception as exc:
            # 网络抖动/额度用尽不该让检索整个失效——降级回 hash，调用方仍拿得到结果
            return _hash_payload(text, f"{type(exc).__name__}: {exc}")
    if active == SENTENCE_BACKEND:
        try:
            values = _sentence_vector(text, st)
            return {"kind": "dense", "backend": SENTENCE_BACKEND, "model": st["model"], "values": values}
        except Exception as exc:
            return _hash_payload(text, f"{type(exc).__name__}: {exc}")
    if active == BUILTIN_BACKEND:
        try:
            values = _builtin_vector(text)
            if values:
                return {"kind": "dense", "backend": BUILTIN_BACKEND,
                        "model": BUILTIN_MODEL, "values": values}
        except Exception as exc:
            return _hash_payload(text, f"{type(exc).__name__}: {exc}")
        # 纯标点/空串这类切不出 token 的输入：没有语义可言，退回稀疏就好
        return _hash_payload(text)
    return _hash_payload(text)


def _builtin_vector(text: str) -> list[float]:
    from . import onnx_embedding
    return onnx_embedding.encode(text) or []


def _api_vector(text: str, st: dict[str, Any]) -> list[float]:
    """调用 OpenAI 兼容的 /v1/embeddings。只用标准库，不引 openai SDK。"""
    import json as _json
    import os
    import urllib.request

    base = st.get("api_base") or ""
    key = os.environ.get(st.get("api_key_env") or "", "")
    if not base or not key:
        raise RuntimeError("embedding API 未配置完整（base 或 key 缺失）")
    url = base if base.endswith("/embeddings") else f"{base}/embeddings"
    req = urllib.request.Request(
        url,
        data=_json.dumps({"model": st.get("api_model"), "input": text}).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
        payload = _json.loads(resp.read().decode("utf-8"))
    try:
        values = payload["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"embeddings 响应结构异常：{str(payload)[:200]}") from exc
    return [round(float(v), 8) for v in values]


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
    raw = (value or DEFAULT_BACKEND).strip().lower().replace("_", "-")
    if raw in ("sentence", "sentence-transformer", "sentence-transformers", "semantic"):
        return SENTENCE_BACKEND
    if raw in ("api", "openai", "openai-compatible", "remote", "http"):
        return API_BACKEND
    if raw in ("builtin", "static", "bundled", "local", "onnx"):
        return BUILTIN_BACKEND
    if raw in ("sparse", "none", "off", "lexical"):
        # 显式关掉语义的逃生口（调试、或者真的不想要向量）
        return SPARSE_BACKEND
    # "hash" 是**旧的默认值**，不是谁主动选的——它压根不是语义，只是当年"零依赖"
    # 的占位。升级到自带静态表之后，老部署 settings.json 里躺着的这个 hash 应当
    # 跟着走到 builtin（同样零配置零联网，但真的有语义）。要保留稀疏请写 "sparse"。
    return DEFAULT_BACKEND


def _local_model_root() -> Path:
    return config.IVYEA_DIR / "models" / "embedding"


def _local_model_candidates() -> list[dict[str, str]]:
    root = _local_model_root()
    if not root.exists():
        return []
    rows = []
    for path in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_dir():
            continue
        rows.append({"name": path.name, "path": str(path)})
    return rows


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


def _hash_payload(text: str, fallback_error: str = "") -> dict[str, Any]:
    data: dict[str, Any] = {"kind": "sparse", "backend": HASH_BACKEND, "values": _hash_vector(text)}
    if fallback_error:
        data["fallback_error"] = fallback_error
    return data


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
