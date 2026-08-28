"""子 agent 角色：分工、白名单、自定义、以及那条不能破的只读边界。"""
from __future__ import annotations

from ivyea_agent import agent_tools, subagents


def _readonly():
    return agent_tools._subagent_schemas()


def test_builtin_roles_cover_the_roadmap_split():
    names = set(subagents.BUILTIN_ROLES)
    assert {"researcher", "code_explorer", "data_analyst",
            "listing_auditor", "ads_reviewer", "knowledge_auditor"} <= names


def test_default_role_keeps_the_old_behaviour():
    """不传 role 时行为必须和加这套东西之前一样：只读全集。"""
    role = subagents.get_role("")
    assert role.name == subagents.DEFAULT_ROLE
    assert role.tools == ()
    assert len(subagents.tools_for(role, _readonly())) == len(_readonly())


def test_an_unknown_role_falls_back_instead_of_failing():
    """派个调研员总比整件事失败强。"""
    assert subagents.get_role("并不存在的角色").name == subagents.DEFAULT_ROLE


def test_roles_narrow_the_toolset():
    role = subagents.get_role("code_explorer")
    names = {t["function"]["name"] for t in subagents.tools_for(role, _readonly())}
    assert names <= set(role.tools)
    assert "grep" in names
    assert "run_listing_audit" not in names


def test_no_role_can_ever_get_a_write_tool():
    """白名单只在只读集内部收窄 —— 角色写了越界工具也拿不到。"""
    rogue = subagents.Role(name="rogue", description="x", system="y",
                           tools=("write_file", "run_command", "execute_actions", "grep"))
    names = {t["function"]["name"] for t in subagents.tools_for(rogue, _readonly())}
    assert names == {"grep"}


def test_no_role_can_ever_recurse():
    """dispatch_subagent 不在只读集里 —— 靠结构保证，不靠各个角色自己记得别写。"""
    rogue = subagents.Role(name="rogue", description="x", system="y",
                           tools=("dispatch_subagent", "read_file"))
    names = {t["function"]["name"] for t in subagents.tools_for(rogue, _readonly())}
    assert "dispatch_subagent" not in names
    for role in subagents.BUILTIN_ROLES.values():
        got = {t["function"]["name"] for t in subagents.tools_for(role, _readonly())}
        assert "dispatch_subagent" not in got, role.name


def test_a_role_that_narrows_to_nothing_falls_back_to_everything():
    """一个工具都没有的子 agent 只会白跑一轮。"""
    rogue = subagents.Role(name="rogue", description="x", system="y", tools=("并不存在的工具",))
    assert len(subagents.tools_for(rogue, _readonly())) == len(_readonly())


def test_every_role_prompt_forbids_writing_and_recursing():
    for role in subagents.BUILTIN_ROLES.values():
        assert "不要再派子 agent" in role.system, role.name
        assert "不能写文件" in role.system, role.name


# ── 用户自定义角色 ───────────────────────────────────────────────────────────
def _write_agent(home, name, text):
    d = home / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(text, encoding="utf-8")


def test_a_custom_role_is_picked_up(ivyea_home):
    _write_agent(ivyea_home, "pricing-scout",
                 "---\nname: pricing-scout\ndescription: 盯竞品定价\n"
                 "tools: [read_file, grep]\nmax_steps: 5\n---\n你是定价侦察兵。")
    role = subagents.get_role("pricing-scout")
    assert role.source == "user"
    assert role.max_steps == 5
    assert role.system == "你是定价侦察兵。"
    names = {t["function"]["name"] for t in subagents.tools_for(role, _readonly())}
    assert names == {"read_file", "grep"}


def test_a_custom_role_overrides_a_builtin_one(ivyea_home):
    _write_agent(ivyea_home, "researcher",
                 "---\nname: researcher\n---\n我自己的调研员。")
    role = subagents.get_role("researcher")
    assert role.source == "user"
    assert role.system == "我自己的调研员。"


def test_a_file_without_frontmatter_still_works(ivyea_home):
    _write_agent(ivyea_home, "plain", "就是一段 system prompt，没有 frontmatter。")
    role = subagents.get_role("plain")
    assert role.source == "user"
    assert "没有 frontmatter" in role.system


def test_broken_agent_files_are_skipped_not_fatal(ivyea_home):
    _write_agent(ivyea_home, "empty", "")
    _write_agent(ivyea_home, "bad-yaml", "---\n: : :\n---\n正文还在。")
    _write_agent(ivyea_home, "BADNAME", "---\nname: Not A Name!\n---\n正文")
    roles = subagents.user_roles()
    assert "empty" not in roles
    assert "bad-yaml" in roles              # frontmatter 坏了，正文照用
    assert "Not A Name!" not in roles


def test_render_list_marks_custom_roles(ivyea_home):
    _write_agent(ivyea_home, "mine", "---\nname: mine\ndescription: 我的\n---\n正文")
    out = subagents.render_list()
    assert "* mine" in out
    assert "  researcher" in out


# ── 派发接线 ─────────────────────────────────────────────────────────────────
def test_dispatch_uses_the_role_prompt_and_tools(ivyea_home, monkeypatch):
    seen = {}

    def fake_run_turn(provider, ctx, messages, max_steps=None, narrate=None, tools=None):
        seen["system"] = messages[0]["content"]
        seen["tools"] = {t["function"]["name"] for t in tools or []}
        seen["max_steps"] = max_steps
        seen["plan_mode"] = ctx.plan_mode
        return "结论"

    from ivyea_agent import agent_loop
    monkeypatch.setattr(agent_loop, "run_turn", fake_run_turn)
    ctx = agent_tools.ToolContext(workspace=".", provider=object())
    out = agent_tools.t_dispatch_subagent({"task": "查一下", "role": "code_explorer"}, ctx)
    assert "code_explorer" in out
    assert "代码勘察" in seen["system"]
    assert seen["tools"] <= set(subagents.get_role("code_explorer").tools)
    assert seen["max_steps"] == 20
    assert seen["plan_mode"] is True          # 只读边界：子 agent 一律计划模式


def test_dispatch_without_a_role_is_unchanged(ivyea_home, monkeypatch):
    seen = {}

    def fake_run_turn(provider, ctx, messages, max_steps=None, narrate=None, tools=None):
        seen["tools"] = len(tools or [])
        seen["max_steps"] = max_steps
        return "结论"

    from ivyea_agent import agent_loop
    monkeypatch.setattr(agent_loop, "run_turn", fake_run_turn)
    ctx = agent_tools.ToolContext(workspace=".", provider=object())
    out = agent_tools.t_dispatch_subagent({"task": "查一下"}, ctx)
    assert out.startswith("【子 agent 结论】")
    assert seen["tools"] == len(_readonly())
    assert seen["max_steps"] == 12
