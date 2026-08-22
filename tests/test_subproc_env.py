"""子进程环境变量隔离：MCP server 与 hook 都不该看见本机全部密钥。

背景
----
在此之前 `mcp_client._ensure_stdio()` 的 Popen 不带 `env=`、`hooks._hook_env()`
直接 `dict(os.environ)`，两者都把父进程的**全部**环境交给子进程 —— 里面有
DEEPSEEK_API_KEY、领星凭据等等。自己配一两个服务器时没人注意，一旦开始装别人
写的 MCP server，那就是一条凭据直通道。

策略是白名单：只放行跨平台通用的那几个（PATH/HOME/LANG/TZ/TEMP…），
其余一律要在配置里显式写。逃生舱有两个：`env` 里直接给值或 `${VAR}` 从父环境
取，以及 settings 里的 `subprocess_env_inherit_all` 一键退回旧行为。
"""
from __future__ import annotations

import os
import sys

import pytest

from ivyea_agent import hooks, subproc_env
from ivyea_agent.mcp_client import MCPClient


SECRET = "DEEPSEEK_API_KEY"
SECRET_VALUE = "sk-should-never-reach-a-child"


@pytest.fixture(autouse=True)
def _fake_secret(monkeypatch):
    """父进程里放一个密钥，用来验证它到不了子进程。"""
    monkeypatch.setenv(SECRET, SECRET_VALUE)
    monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))
    monkeypatch.setenv("HOME", os.environ.get("HOME", "/root"))


# ── build_env 本身 ──────────────────────────────────────────────────────────

def test_secret_is_not_inherited():
    env = subproc_env.build_env()
    assert SECRET not in env


def test_allowlisted_keys_pass_through():
    env = subproc_env.build_env()
    assert env.get("PATH") == os.environ["PATH"]
    assert env.get("HOME") == os.environ["HOME"]


def test_explicit_env_literal_value():
    env = subproc_env.build_env({"MY_TOKEN": "abc123"})
    assert env["MY_TOKEN"] == "abc123"


def test_explicit_env_expands_dollar_brace_from_parent():
    """`${VAR}` 是逃生舱：想把父进程的某个密钥给这个子进程，显式写出来。"""
    env = subproc_env.build_env({"PASSED": "${%s}" % SECRET})
    assert env["PASSED"] == SECRET_VALUE
    # 显式传了一个，不代表其它的也跟着漏出去
    assert SECRET not in env


def test_expand_missing_var_becomes_empty_not_literal():
    env = subproc_env.build_env({"NOPE": "${DEFINITELY_NOT_SET_12345}"})
    assert env["NOPE"] == ""


def test_passthrough_shorthand():
    env = subproc_env.build_env(passthrough=[SECRET])
    assert env[SECRET] == SECRET_VALUE


def test_passthrough_of_unset_var_is_omitted():
    env = subproc_env.build_env(passthrough=["DEFINITELY_NOT_SET_12345"])
    assert "DEFINITELY_NOT_SET_12345" not in env


def test_inherit_all_is_the_escape_hatch():
    """退回旧行为的开关；给升级后发现自己依赖环境变量的人留的。"""
    env = subproc_env.build_env(inherit_all=True)
    assert env[SECRET] == SECRET_VALUE


def test_extra_wins_over_everything():
    """extra 是内核自己注入的（如 IVYEA_HOOK_EVENT），优先级最高。"""
    env = subproc_env.build_env({"K": "from-config"}, extra={"K": "from-kernel"})
    assert env["K"] == "from-kernel"


def test_allowlist_covers_windows_essentials():
    """SYSTEMROOT 之类缺了，Windows 上子进程起不来。"""
    for key in ("SYSTEMROOT", "COMSPEC", "PATHEXT", "APPDATA", "USERPROFILE"):
        assert key in subproc_env.ALLOWLIST_WINDOWS


# ── MCPClient ───────────────────────────────────────────────────────────────

def test_mcp_client_reads_env_from_spec():
    """这个字段以前被静默忽略 —— 本机 mcp.json 里早就写着 env，却从没生效过。"""
    c = MCPClient({"transport": "stdio", "command": "true",
                   "env": {"SELLERSPRITE_KEY": "k-123"}})
    assert c.child_env()["SELLERSPRITE_KEY"] == "k-123"


def test_mcp_child_env_excludes_secrets():
    """主动收紧（inherit_env: false）才走白名单。"""
    c = MCPClient({"transport": "stdio", "command": "true", "inherit_env": False})
    assert SECRET not in c.child_env()


def test_mcp_child_env_keeps_path():
    c = MCPClient({"transport": "stdio", "command": "true", "inherit_env": False})
    assert c.child_env().get("PATH") == os.environ["PATH"]


# ── 升级必须无感：老配置无论长什么样都照常跑 ──────────────────────────────
#
# v1.15.7 把默认切成白名单，老配置当场失效。v1.15.8 收回了一半 —— 但把
# "写过 env" 读成了"要收紧"，而 `env` 在 v1.15.7 之前是**被静默忽略**的：
# 用户写它时既不知道有收紧这回事，也没打算收紧，他只是想加一个变量，而那个
# 服务器同时还靠 shell / systemd 里的其它变量工作。
#
# 结论：**`inherit_env` 是唯一的收紧信号**，其余一律叠加。

def test_legacy_no_fields_inherits():
    c = MCPClient({"transport": "stdio", "command": "true"})
    assert c.child_env()[SECRET] == SECRET_VALUE


def test_legacy_with_env_still_inherits_and_env_now_works():
    """本机 sellersprite 就是这个形状：写了 env，但它以前根本没被读。

    升级后既要继续拿到环境里的其它变量（无感），env 也要真的生效（修好的 bug）。
    """
    c = MCPClient({"transport": "stdio", "command": "true",
                   "env": {"SELLERSPRITE_KEY": "k-123"}})
    e = c.child_env()
    assert e[SECRET] == SECRET_VALUE          # 环境照常继承
    assert e["SELLERSPRITE_KEY"] == "k-123"   # 而且 env 终于生效了


def test_legacy_with_passthrough_still_inherits():
    c = MCPClient({"transport": "stdio", "command": "true",
                   "env_passthrough": ["PATH"]})
    assert c.child_env()[SECRET] == SECRET_VALUE


def test_only_explicit_false_tightens():
    c = MCPClient({"transport": "stdio", "command": "true", "inherit_env": False})
    assert SECRET not in c.child_env()


def test_legacy_hook_entry_inherits_even_with_env():
    entry = hooks._normalize([{"command": "true",                # noqa: SLF001
                               "env": {"L": "1"}}])[0]
    env = hooks._hook_env("stop", None, entry=entry)             # noqa: SLF001
    assert env[SECRET] == SECRET_VALUE
    assert env["L"] == "1"


def test_hook_entry_tightens_only_on_explicit_false():
    entry = hooks._normalize([{"command": "true",                # noqa: SLF001
                               "inherit_env": False}])[0]
    env = hooks._hook_env("stop", None, entry=entry)             # noqa: SLF001
    assert SECRET not in env
    assert env["IVYEA_HOOK_EVENT"] == "stop"


def test_mcp_spec_passthrough(monkeypatch):
    monkeypatch.setenv("SOME_SERVER_TOKEN", "tok")
    c = MCPClient({"transport": "stdio", "command": "true", "inherit_env": False,
                   "env_passthrough": ["SOME_SERVER_TOKEN"]})
    assert c.child_env()["SOME_SERVER_TOKEN"] == "tok"


def test_mcp_spec_inherit_all_opt_in():
    c = MCPClient({"transport": "stdio", "command": "true", "inherit_env": True})
    assert c.child_env()[SECRET] == SECRET_VALUE


def test_mcp_stdio_actually_spawns_with_scrubbed_env(tmp_path):
    """端到端：真起一个子进程，让它把自己看到的环境写出来。

    只断言"拿不到密钥"和"拿得到显式传的"，不断言完整集合 —— 白名单将来会变。
    """
    out = tmp_path / "seen.txt"
    script = tmp_path / "dump.py"
    script.write_text(
        "import os,sys\n"
        f"open({str(out)!r},'w').write(repr(dict(os.environ)))\n",
        encoding="utf-8")
    # 用 sys.executable 而不是写死 "python3" —— Windows 上没有 python3 这个名字。
    c = MCPClient({"transport": "stdio", "command": sys.executable, "inherit_env": False,
                   "args": [str(script)], "env": {"GIVEN": "yes"}})
    proc = c._ensure_stdio()          # noqa: SLF001 — 就是要验这一层
    proc.wait(timeout=20)
    seen = out.read_text(encoding="utf-8")
    assert "'GIVEN': 'yes'" in seen
    assert SECRET_VALUE not in seen


# ── hooks ───────────────────────────────────────────────────────────────────

def test_hook_env_excludes_secrets():
    """不带条目直调 = 没人表态要继承 → 收紧。"""
    env = hooks._hook_env("pre_tool_use", {"tool_name": "x"})   # noqa: SLF001
    assert SECRET not in env


def test_hook_env_keeps_its_own_contract():
    """IVYEA_HOOK_EVENT / IVYEA_HOOK_PAYLOAD 是 hook 的 API，不能因为清洗丢掉。"""
    env = hooks._hook_env("pre_tool_use", {"tool_name": "x"})   # noqa: SLF001
    assert env["IVYEA_HOOK_EVENT"] == "pre_tool_use"
    assert "tool_name" in env["IVYEA_HOOK_PAYLOAD"]


def test_hook_env_keeps_path_and_home():
    env = hooks._hook_env("stop", None)                          # noqa: SLF001
    assert env.get("PATH") == os.environ["PATH"]
    assert env.get("HOME") == os.environ["HOME"]


def test_hook_entry_env_and_passthrough(monkeypatch, tmp_path):
    monkeypatch.setenv("HOOK_ONLY_TOKEN", "ht")
    entry = hooks._normalize([{                                  # noqa: SLF001
        "command": "true", "inherit_env": False,
        "env": {"LITERAL": "L"}, "env_passthrough": ["HOOK_ONLY_TOKEN"]}])[0]
    env = hooks._hook_env("stop", None, entry=entry)             # noqa: SLF001
    assert env["LITERAL"] == "L"
    assert env["HOOK_ONLY_TOKEN"] == "ht"
    assert SECRET not in env


def test_hook_inherit_all_escape_hatch():
    entry = hooks._normalize([{"command": "true",                # noqa: SLF001
                               "inherit_env": True}])[0]
    env = hooks._hook_env("stop", None, entry=entry)             # noqa: SLF001
    assert env[SECRET] == SECRET_VALUE
