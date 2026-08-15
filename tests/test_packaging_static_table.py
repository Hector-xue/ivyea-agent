"""静态向量表必须真的进 wheel。

漏了不会报错——`static_embedding` 会安静降级回词法检索，用户只觉得"换个说法搜不到"，
没有任何线索指向打包配置。所以这件事必须有测试压住，而不是靠发版时记得检查。
"""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TABLE_REL = "ivyea_agent/data/static_embedding.npz"


def test_table_exists_in_repo():
    p = REPO / TABLE_REL
    assert p.exists(), f"{TABLE_REL} 不在仓库里（用 scripts/build_static_embedding.py 生成）"
    assert p.stat().st_size > 1_000_000, "表小得不像话，八成是 LFS 指针或者生成失败"


def test_package_data_pattern_covers_table():
    """盯 pyproject 里的 package-data 模式，比跑一次完整构建快得多。"""
    text = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "data/*.npz" in text, "package-data 里没有 data/*.npz，表不会进 wheel"


@pytest.mark.slow
def test_built_wheel_contains_table(tmp_path):
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
    assert TABLE_REL in names, f"wheel 里没有 {TABLE_REL}"
    info = next(i for i in zipfile.ZipFile(wheels[0]).infolist() if i.filename == TABLE_REL)
    assert info.file_size > 1_000_000
