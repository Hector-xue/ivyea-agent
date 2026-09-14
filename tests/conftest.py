"""测试隔离：把 IVYEA_HOME 指向临时目录，绝不触碰真实 ~/.ivyea。

写测试的约定（踩过的坑，勿再犯）：
- **跨平台路径**：断言路径时别硬编码正斜杠 `"a/b/c"`——Windows 上工具输出可能是反斜杠。
  工具的路径输出统一走 `Path.as_posix()`（正斜杠），断言也用同款或 `os.sep`-无关写法；
  CI 跑 ubuntu/macos/**windows** 三平台，本地只在一个平台过 ≠ 全绿。
  （历史：`t_grep` 曾用原生分隔符输出，Windows CI 上 `sub\\c.ts` 让断言 `sub/c.ts` 失败。）
- **隔离**：碰 ~/.ivyea 的用例用下面的 `ivyea_home` fixture；碰 git 的用例在临时目录里 init，
  绝不在真实仓库跑 git 写操作。
"""
from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile

import pytest

# 整个测试会话的兜底 IVYEA_HOME：**在任何 ivyea_agent 模块被 import 之前**就指到临时目录。
# 没有它，不用 `ivyea_home` fixture 的用例（或排在第一个 fixture 用例之前的用例）
# 会直接写真实的 ~/.ivyea；有了它，它们最多写到这个会话目录，跑完一并删掉。
_SESSION_HOME = tempfile.mkdtemp(prefix="ivyea_test_session_")
os.environ["IVYEA_HOME"] = _SESSION_HOME


@pytest.fixture(scope="session", autouse=True)
def _session_home():
    yield _SESSION_HOME
    shutil.rmtree(_SESSION_HOME, ignore_errors=True)


# 在模块级绑定了 config.IVYEA_DIR 的模块：换目录后必须重载，否则跨用例泄漏，
# 甚至写到真实 ~/.ivyea（用 grep "= config.IVYEA_DIR /" 可复查）
_REBIND_MODULES = ("ivyea_agent.memory", "ivyea_agent.memory_core", "ivyea_agent.memory_store",
                "ivyea_agent.lingxing_openapi",
                "ivyea_agent.lingxing_cache", "ivyea_agent.pricing",
                "ivyea_agent.sessions", "ivyea_agent.audit", "ivyea_agent.shadow",
                "ivyea_agent.action_queue", "ivyea_agent.doctor", "ivyea_agent.profiles",
                "ivyea_agent.snapshots", "ivyea_agent.intraday", "ivyea_agent.approvals",
                "ivyea_agent.feishu_client", "ivyea_agent.reliability",
                "ivyea_agent.alert_state", "ivyea_agent.amazon_auth",
                "ivyea_agent.serve_workers", "ivyea_agent.evidence_ledger",
                "ivyea_agent.log", "ivyea_agent.schedule", "ivyea_agent.workspace",
                "ivyea_agent.stores",
                "ivyea_agent.self_manage", "ivyea_agent.task_runner",
                "ivyea_agent.code_agent", "ivyea_agent.tools_general",
                "ivyea_agent.traces", "ivyea_agent.policy")


def _rebind_home(monkeypatch, path: str):
    """把 IVYEA_HOME 和所有按它定路径的模块指向 path，返回重载后的 config"""
    monkeypatch.setenv("IVYEA_HOME", path)
    from ivyea_agent import config
    importlib.reload(config)
    for mod in _REBIND_MODULES:
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
    return config


@pytest.fixture()
def ivyea_home(monkeypatch):
    """每个用例一个干净的临时 ~/.ivyea。返回该目录 Path。"""
    d = tempfile.mkdtemp(prefix="ivyea_test_")
    config = _rebind_home(monkeypatch, d)
    policy_file = config.IVYEA_DIR / "policy.json"
    if policy_file.exists():
        policy_file.unlink()
    yield config.IVYEA_DIR
    # 用完必须删：每个用例一个目录，不删就每跑一次测试往 /tmp 漏一个。
    # 2026-09-14 生产机上 /tmp 里堆了 23 万个 ivyea_test_*（1.3G、30 万 inode）就是这么来的。
    # 删之前先把模块指回会话目录：否则后面不用这个 fixture 的用例还会往这个已删的目录写，
    # 目录被重新建出来、照样漏（第一版只加了 rmtree，全量跑完仍剩 11 个）。
    # ignore_errors：Windows 上还没关的 sqlite 句柄会让个别文件删不掉，别因此把用例判红。
    _rebind_home(monkeypatch, _SESSION_HOME)
    shutil.rmtree(d, ignore_errors=True)
