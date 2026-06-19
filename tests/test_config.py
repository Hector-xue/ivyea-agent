"""config：settings/env 读写、密钥增删、active key —— 全在隔离的临时 IVYEA_HOME。"""
from __future__ import annotations

import importlib
import os as _os

import pytest


def _cfg():
    from ivyea_agent import config
    return config


def test_settings_roundtrip(ivyea_home):
    config = _cfg()
    config.set_setting("target_acos", 0.25)
    assert config.get_setting("target_acos") == 0.25
    # 默认值在未设时可读
    assert config.get_setting("site") == "US"


def test_env_key_add_update_remove(ivyea_home):
    config = _cfg()
    config.set_env_key("LINGXING_OPENAPI_SECRET", "s1")
    importlib.reload(config)  # 重置进程内 env 缓存，强制从文件读
    config.load_env()
    assert config.get_setting  # 模块可用
    import os
    assert os.environ.get("LINGXING_OPENAPI_SECRET") == "s1"
    # 更新
    config.set_env_key("LINGXING_OPENAPI_SECRET", "s2")
    assert os.environ.get("LINGXING_OPENAPI_SECRET") == "s2"
    # 删除（空值）
    config.set_env_key("LINGXING_OPENAPI_SECRET", "")
    txt = config.ENV_FILE.read_text(encoding="utf-8")
    assert "LINGXING_OPENAPI_SECRET" not in txt


@pytest.mark.skipif(_os.name == "nt", reason="Windows 无 POSIX 文件权限位（chmod 在此为 best-effort 空操作）")
def test_env_file_permissions(ivyea_home):
    config = _cfg()
    config.set_env_key("FOO", "bar")
    import stat
    mode = config.ENV_FILE.stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_active_key(ivyea_home):
    config = _cfg()
    config.set_setting("key_env", "DEEPSEEK_API_KEY")
    config.set_env_key("DEEPSEEK_API_KEY", "sk-test")
    assert config.get_active_key() == "sk-test"
