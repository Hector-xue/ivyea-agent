"""构建随包发布的 embedding 模型（ivyea_agent/data/embedding_int8.onnx）。

这是"装完就有语义检索"的全部输入：bge-small-zh-v1.5 导出成 ONNX 并做 int8 动态量化，
24MB 进 wheel，运行时只要 onnxruntime（纯 CPU 轮子，约 19MB），**不需要 torch**
（pip 默认会给 Linux 拉 GB 级的 CUDA 版，小内存机器直接被 OOM 杀掉）。

**只有换源模型时才需要重跑**，日常开发不需要——产物已经在仓库里。重跑需要 torch +
transformers + onnx（仅此脚本需要）：

    pip install torch --index-url https://download.pytorch.org/whl/cpu
    pip install transformers onnx onnxruntime
    python scripts/build_onnx_embedding.py --model ~/.ivyea/models/embedding/bge-small-zh-v1.5

两个不能改错的地方（都是实测踩出来的）：

1. **池化必须是 CLS**。bge 系列的句向量取 [CLS] 位置的输出（见模型目录
   1_Pooling/config.json 的 pooling_mode_cls_token）。用 mean pooling 会得到一批
   看着正常、但和模型真实语义空间对不上的向量。

2. **截断长度必须是 512**。曾经按 256 截断做评测，结论直接反了——当时测出 int8
   "掉点严重"（净救回 +4），以为是量化的锅；改回 512 之后 int8 是 +14，和 fp32 的
   +13 基本持平。**真正掉点的是把文档砍掉一半，不是量化。**
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DATA = REPO / "ivyea_agent" / "data"
MAX_SEQ = 512


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="bge-small-zh-v1.5 本地目录")
    ap.add_argument("--out", default=str(DATA / "embedding_int8.onnx"))
    ap.add_argument("--keep-fp32", action="store_true", help="同时保留未量化版本（体积 95MB，仅供对比）")
    args = ap.parse_args()

    import torch
    from transformers import AutoModel, AutoTokenizer

    model_dir = Path(args.model).expanduser()
    tok = AutoTokenizer.from_pretrained(str(model_dir))
    model = AutoModel.from_pretrained(str(model_dir))
    model.eval()

    class Wrapped(torch.nn.Module):
        """把池化和归一化一起塞进图里，运行时就不用在 Python 里重复实现（也不会实现错）。"""

        def __init__(self, inner):
            super().__init__()
            self.inner = inner

        def forward(self, input_ids, attention_mask):
            hidden = self.inner(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
            cls = hidden[:, 0]                      # bge 是 CLS 池化，别改成 mean
            return cls / cls.norm(dim=1, keepdim=True)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fp32 = out.with_name("embedding_fp32.onnx")

    sample = tok(["广告预算怎么分配", "否词"], padding=True, return_tensors="pt")
    torch.onnx.export(
        Wrapped(model), (sample["input_ids"], sample["attention_mask"]), str(fp32),
        input_names=["input_ids", "attention_mask"], output_names=["embedding"],
        dynamic_axes={"input_ids": {0: "batch", 1: "seq"},
                      "attention_mask": {0: "batch", 1: "seq"},
                      "embedding": {0: "batch"}},
        opset_version=14)
    print(f"  fp32 {fp32.stat().st_size / 1e6:.1f} MB")

    from onnxruntime.quantization import QuantType, quantize_dynamic
    quantize_dynamic(str(fp32), str(out), weight_type=QuantType.QInt8)
    print(f"  int8 {out.stat().st_size / 1e6:.1f} MB")
    if not args.keep_fp32:
        fp32.unlink()

    # 词表随模型一起发布：运行时用自己的纯 Python WordPiece 分词（见 wordpiece.py），
    # 不引 tokenizers（3.4MB 原生轮子 + 平台兼容矩阵）。
    vocab_dst = DATA / "embedding_vocab.txt"
    shutil.copyfile(model_dir / "vocab.txt", vocab_dst)
    print(f"  词表 {vocab_dst.stat().st_size / 1e3:.0f} KB")

    digest = hashlib.sha256(out.read_bytes()).hexdigest()[:16]
    (DATA / "embedding_meta.json").write_text(json.dumps({
        "source_model": "BAAI/bge-small-zh-v1.5",
        "pooling": "cls",
        "normalized": True,
        "max_seq_length": MAX_SEQ,
        "dimensions": int(model.config.hidden_size),
        "quantization": "onnxruntime dynamic int8",
        "fingerprint": digest,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  指纹 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
