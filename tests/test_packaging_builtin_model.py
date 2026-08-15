"""内置 embedding 模型必须真的进 wheel。

漏了不会报错——`onnx_embedding` 会安静降级回词法检索，用户只觉得"换个说法搜不到"，
没有任何线索指向打包配置。所以这件事必须有测试压住，而不是靠发版时记得检查。
"""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
MODEL_REL = "ivyea_agent/data/embedding_int8.onnx"
VOCAB_REL = "ivyea_agent/data/embedding_vocab.txt"


def test_model_exists_in_repo():
    p = REPO / MODEL_REL
    assert p.exists(), f"{MODEL_REL} 不在仓库里（用 scripts/build_onnx_embedding.py 生成）"
    assert p.stat().st_size > 10_000_000, "模型小得不像话，八成是 LFS 指针或者生成失败"
    v = REPO / VOCAB_REL
    assert v.exists(), f"{VOCAB_REL} 不在仓库里 —— 没有词表就没法分词，模型等于废的"


def test_package_data_pattern_covers_table():
    """盯 pyproject 里的 package-data 模式，比跑一次完整构建快得多。"""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "data/*.onnx" in text, "package-data 里没有 data/*.onnx，模型不会进 wheel"
    assert "data/*.txt" in text, "package-data 里没有 data/*.txt，词表不会进 wheel"


@pytest.mark.slow
def test_built_wheel_contains_model(tmp_path):
    """真构一次 wheel 并翻开看——模式写对了不等于构出来就有。"""
    build = pytest.importorskip("build")  # noqa: F841
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(tmp_path)],
        cwd=str(REPO), capture_output=True, text=True, timeout=900,
    )
    assert proc.returncode == 0, f"构建失败：{proc.stderr[-2000:]}"
    wheels = list(tmp_path.glob("*.whl"))
    assert wheels, "没产出 wheel"
    with zipfile.ZipFile(wheels[0]) as z:
        names = z.namelist()
    assert MODEL_REL in names, f"wheel 里没有 {MODEL_REL}"
    assert VOCAB_REL in names, f"wheel 里没有 {VOCAB_REL}"
    info = next(i for i in zipfile.ZipFile(wheels[0]).infolist() if i.filename == MODEL_REL)
    assert info.file_size > 10_000_000
