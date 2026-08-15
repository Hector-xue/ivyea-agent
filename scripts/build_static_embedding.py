"""构建随包发布的静态语义向量表（ivyea_agent/data/static_embedding.npz）。

这张表是 `ivyea_agent.static_embedding` 的全部输入，也是"装完就有语义检索"的前提。
它由 bge-small-zh-v1.5 蒸馏而来：把词表里每个 token 单独过一遍模型，把输出存成查表，
运行时就不用再跑神经网络了。

**只有改配方或换源模型时才需要重跑**，日常开发不需要——产物已经在仓库里。
重跑需要 torch + transformers（仅此脚本需要，运行时只要 numpy）：

    pip install torch --index-url https://download.pytorch.org/whl/cpu
    pip install transformers
    python scripts/build_static_embedding.py \
        --model ~/.ivyea/models/embedding/bge-small-zh-v1.5

配方（每一步都有实测理由，别凭直觉改）：
  1. 单 token 前向，不加 [CLS]/[SEP] —— 取的就是这个 token 脱离上下文的表示。
  2. 去词表均值 + SVD 降到 256 维 —— 体积除以 2，实测不掉点。
  3. SIF 词频加权：w = a/(a+p(token))，高频虚词压低。
  4. 减语料均值 + 第一主成分 —— **这步不能省**：不减的话任意两段文本余弦都在
     0.93±0.02，排序毫无分辨力；减完是 -0.01±0.15。
  5. int8 逐维量化 —— 体积再除以 4，平均绝对误差约 0.004（值域 ±6）。

第 3、4 步的统计量用随包的知识库算，**固化进表**。刻意不用用户自己的记忆：
那样每加一条记忆统计量就变，向量缓存里的历史条目全部作废。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from ivyea_agent import wordpiece  # noqa: E402

KB = REPO / "ivyea_agent" / "knowledge_base"
OUT_DEFAULT = REPO / "ivyea_agent" / "data" / "static_embedding.npz"
BODY_CHARS = 1500


def load_vocab(model_dir: Path) -> list[str]:
    """必须和 HuggingFace 的 load_vocab 逐条一致（已实测相等）。

    两个坑：vocab.txt 里有 3 个**空行**，HF 是"后来者覆盖"（空串最终指向最后那个位置）；
    以及**不能用 splitlines()**——它对这个文件会多切出两行，索引整体错位，查表全查到隔壁。
    """
    lines = (model_dir / "vocab.txt").read_text(encoding="utf-8").split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return lines


def distill(model_dir: Path, batch: int = 256) -> np.ndarray:
    import torch
    from transformers import AutoModel

    model = AutoModel.from_pretrained(str(model_dir))
    model.eval()
    torch.set_num_threads(2)   # 蒸馏机器可能内存很小，别让 torch 开满线程

    size = len(load_vocab(model_dir))
    dim = model.config.hidden_size
    out = np.zeros((size, dim), dtype=np.float32)
    t0 = time.time()
    with torch.no_grad():
        for start in range(0, size, batch):
            ids = torch.arange(start, min(start + batch, size), dtype=torch.long).unsqueeze(1)
            hidden = model(input_ids=ids, attention_mask=torch.ones_like(ids)).last_hidden_state
            out[start:start + ids.shape[0]] = hidden[:, 0, :].numpy()
    print(f"  蒸馏 {size} 个 token，{time.time() - t0:.0f}s")
    return out


def corpus_docs() -> list[str]:
    cards = json.loads((KB / "index.json").read_text(encoding="utf-8"))
    docs = []
    for c in cards:
        body = (KB / c["path"]).read_text(encoding="utf-8")[:BODY_CHARS]
        docs.append(f"{c['title']} {' '.join(c.get('tags') or [])} {body}")
    return docs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="bge-small-zh-v1.5 本地目录")
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--raw", default="", help="复用已蒸馏好的 float32 矩阵（跳过跑模型）")
    ap.add_argument("--dim", type=int, default=256)
    ap.add_argument("--sif-a", type=float, default=1e-3)
    ap.add_argument("--npc", type=int, default=1)
    args = ap.parse_args()

    model_dir = Path(args.model).expanduser()
    vocab_list = load_vocab(model_dir)
    vocab = {tok: i for i, tok in enumerate(vocab_list)}

    mat = np.load(args.raw).astype(np.float32) if args.raw else distill(model_dir)
    assert mat.shape[0] == len(vocab_list), f"矩阵行数 {mat.shape[0]} != 词表 {len(vocab_list)}"

    # 1) 降维
    centered = mat - mat.mean(axis=0, keepdims=True)
    if args.dim and args.dim < mat.shape[1]:
        _, _, vt = np.linalg.svd(centered, full_matrices=False)
        vecs = centered @ vt[: args.dim].T
    else:
        vecs = centered
    print(f"  向量表 {vecs.shape}")

    # 2) SIF 权重
    docs = corpus_docs()
    tf: Counter = Counter()
    total = 0
    doc_ids = []
    for d in docs:
        ids = [vocab.get(t, 100) for t in wordpiece.tokenize(d, vocab)]
        doc_ids.append(ids)
        tf.update(ids)
        total += len(ids)
    probs = np.zeros(vecs.shape[0], dtype=np.float32)
    for tid, n in tf.items():
        probs[tid] = n / max(total, 1)
    weights = (args.sif_a / (args.sif_a + probs)).astype(np.float32)

    def embed(ids):
        if not ids:
            return np.zeros(vecs.shape[1], dtype=np.float32)
        w = weights[ids][:, None]
        return (vecs[ids] * w).sum(axis=0) / max(float(w.sum()), 1e-6)

    # 3) 语料均值 + 主成分
    doc_mat = np.vstack([embed(i) for i in doc_ids])
    doc_mean = doc_mat.mean(axis=0)
    _, _, dvt = np.linalg.svd(doc_mat - doc_mean, full_matrices=False)
    pcs = dvt[: args.npc] if args.npc > 0 else np.zeros((0, vecs.shape[1]), dtype=np.float32)

    def finish(v):
        v = v - doc_mean
        for pc in pcs:
            v = v - (v @ pc) * pc
        n = np.linalg.norm(v)
        return v / n if n > 0 else v

    after = np.vstack([finish(embed(i)) for i in doc_ids])
    sims = (after @ after.T)[np.triu_indices(len(docs), 1)]
    print(f"  语料两两相似度 均值{sims.mean():+.3f} 标准差{sims.std():.3f}（越散越有分辨力）")

    # 4) int8 逐维量化
    scale = np.abs(vecs).max(axis=0)
    scale[scale == 0] = 1.0
    q = np.clip(np.round(vecs / scale * 127.0), -127, 127).astype(np.int8)
    err = float(np.abs(q.astype(np.float32) * scale / 127.0 - vecs).mean())

    # 指纹：向量缓存的 key 里带上它。没有它的话，重建一张同维度但配方不同的表之后，
    # 缓存里的历史向量会被当成有效继续用——数值全错，而且不会报任何错。
    import hashlib
    fingerprint = hashlib.sha256(q.tobytes()).hexdigest()[:16]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        out,
        fingerprint=np.array(fingerprint),
        vectors=q,
        scale=scale.astype(np.float32),
        weights=weights,
        doc_mean=doc_mean.astype(np.float32),
        pcs=pcs.astype(np.float32),
        # 词表存成**一整块换行分隔的字符串**，而不是 21128 个元素的 unicode 数组：
        # 后者会把每条按最长 token 补齐（浪费），加载时还要逐个转 str（30ms）。
        # 整块 split 只要 13ms。token 本身不可能含换行（vocab.txt 就是按行存的）。
        vocab_blob=np.array("\n".join(vocab_list)),
    )
    out.with_suffix(".meta.json").write_text(json.dumps({
        "source_model": "BAAI/bge-small-zh-v1.5",
        "vocab_size": len(vocab_list), "dim": int(vecs.shape[1]),
        "sif_a": args.sif_a, "npc": args.npc,
        "quantization": "int8 per-dim", "quantization_mae": round(err, 6),
        "fingerprint": fingerprint,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  指纹 {fingerprint}")
    print(f"  量化平均绝对误差 {err:.5f}")
    print(f"  {out}  {out.stat().st_size / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
