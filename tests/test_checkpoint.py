"""/rewind 检查点：git_workflow.checkpoint / restore_checkpoint（对话+代码快照）。

硬隔离：全部在 tmp_path 建全新 git 仓，绝不碰真实仓库（禁止默认 cwd、禁 reset 真实仓）。
"""
from __future__ import annotations

import subprocess

import pytest

from ivyea_agent import git_workflow as gw


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=str(root), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True)


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@t.co")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "a.py").write_text("v1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


def test_checkpoint_none_outside_git(tmp_path):
    assert gw.checkpoint(str(tmp_path)) is None                 # 非 git 仓 → None


def test_checkpoint_and_restore_roundtrip(repo):
    cp = gw.checkpoint(str(repo))                                # 干净时快照（stash=""）
    assert cp is not None and cp["head"]
    (repo / "a.py").write_text("v2-EDITED\n", encoding="utf-8")  # 改文件
    ok, msg = gw.restore_checkpoint(cp, str(repo))               # 恢复到快照
    assert ok
    assert (repo / "a.py").read_text() == "v1\n"                 # 文件回到 v1


def test_checkpoint_captures_dirty_state(repo):
    (repo / "a.py").write_text("v2\n", encoding="utf-8")         # 先改成 v2
    cp = gw.checkpoint(str(repo))                                # 快照 v2（stash 非空）
    assert cp and cp["stash"]
    (repo / "a.py").write_text("v3\n", encoding="utf-8")         # 再改成 v3
    ok, _ = gw.restore_checkpoint(cp, str(repo))
    assert ok and (repo / "a.py").read_text() == "v2\n"          # 恢复到快照时的 v2


def test_restore_outside_git_only_conversation(tmp_path):
    ok, msg = gw.restore_checkpoint({"head": "x"}, str(tmp_path))
    assert ok is False and "仅回退" in msg                       # 非 git 仓：只回退对话
