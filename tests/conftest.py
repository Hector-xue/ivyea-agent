"""测试隔离：把 IVYEA_HOME 指向临时目录，绝不触碰真实 ~/.ivyea。"""
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
