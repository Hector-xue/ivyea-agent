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
import tempfile

import pytest


@pytest.fixture()
def ivyea_home(monkeypatch):
    """每个用例一个干净的临时 ~/.ivyea。返回该目录 Path。"""
    import sys
    d = tempfile.mkdtemp(prefix="ivyea_test_")
    monkeypatch.setenv("IVYEA_HOME", d)
    # config 在 import 时按 IVYEA_HOME 定目录；依赖它路径的模块也要重载
    from ivyea_agent import config
    importlib.reload(config)
    policy_file = config.IVYEA_DIR / "policy.json"
    if policy_file.exists():
        policy_file.unlink()
    for mod in ("ivyea_agent.memory", "ivyea_agent.lingxing_openapi",
                "ivyea_agent.lingxing_cache", "ivyea_agent.pricing",
                "ivyea_agent.sessions", "ivyea_agent.audit", "ivyea_agent.shadow",
                "ivyea_agent.action_queue", "ivyea_agent.doctor", "ivyea_agent.profiles",
                "ivyea_agent.traces", "ivyea_agent.policy"):
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
    yield config.IVYEA_DIR
