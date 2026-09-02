"""通用工具：读自动放行、写/执行门控、plan 模式拦截、沙箱、web_fetch。

门控的「放行」路径用 ctx.perm.session_allow 预置（免 stdin）；「拦截」用 plan_mode。
"""
from __future__ import annotations

from ivyea_agent import tools_general as tg
from ivyea_agent.agent_tools import ToolContext


def _ctx(tmp_path, allow=()):
    from ivyea_agent import policy
    if policy.POLICY_FILE.exists():
        policy.POLICY_FILE.unlink()
    c = ToolContext(workspace=str(tmp_path))
    for k in allow:
        c.perm.session_allow.add(k)
    return c


# ── 读类（自动放行）──
def test_read_and_list(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("你好 ivyea", encoding="utf-8")
    ctx = _ctx(tmp_path)
    assert "你好 ivyea" in tg.t_read_file({"path": str(f)}, ctx)
    out = tg.t_list_dir({"path": str(tmp_path)}, ctx)
    assert "a.txt" in out


def test_read_missing(tmp_path):
    assert "不存在" in tg.t_read_file({"path": str(tmp_path / "nope")}, _ctx(tmp_path))


# ── write_file 门控 ──
def test_write_approved(tmp_path):
    ctx = _ctx(tmp_path, allow=["write_file"])
    f = tmp_path / "out.txt"
    r = tg.t_write_file({"path": str(f), "content": "data"}, ctx)
    assert "已写入" in r and f.read_text() == "data"


def test_write_blocked_in_plan_mode(tmp_path):
    ctx = _ctx(tmp_path, allow=["write_file"])
    ctx.plan_mode = True
    f = tmp_path / "out.txt"
    r = tg.t_write_file({"path": str(f), "content": "data"}, ctx)
    assert "计划模式" in r and not f.exists()


# ── 后台 bash（Phase 1）──
def test_run_command_background_and_output(tmp_path):
    import re
    import time
    ctx = _ctx(tmp_path, allow=["run_command"])
    r = tg.t_run_command({"command": "echo hello-bg", "run_in_background": True}, ctx)
    assert "bash_id=" in r
    bid = re.search(r"bash_id=(bg-\d+)", r).group(1)
    # t_bash_output 是**增量**的：读过的不再返回。所以必须累加，不能只留最后一次——
    # 机器一忙，"hello-bg" 和 "已结束" 就会落在两次轮询里，只看最后一次必然漏掉输出，
    # 于是这条用例在负载高时随机变红（实测撞到过）。
    seen = ""
    for _ in range(50):
        seen += tg.t_bash_output({"bash_id": bid}, ctx)
        if "已结束" in seen:
            break
        time.sleep(0.1)
    assert "hello-bg" in seen and "已结束" in seen     # 轮询到输出 + 退出状态


def test_kill_bash(tmp_path):
    import re
    ctx = _ctx(tmp_path, allow=["run_command"])
    r = tg.t_run_command({"command": "sleep 30", "run_in_background": True}, ctx)
    bid = re.search(r"bash_id=(bg-\d+)", r).group(1)
    assert "已终止" in tg.t_kill_bash({"bash_id": bid}, ctx)
    assert "没有该后台任务" in tg.t_bash_output({"bash_id": "bg-9999"}, ctx)


# ── edit_file ──
def test_edit_unique(tmp_path):
    f = tmp_path / "e.txt"
    f.write_text("bid 1.00 done", encoding="utf-8")
    ctx = _ctx(tmp_path, allow=["edit_file"])
    tg.t_read_file({"path": str(f)}, ctx)   # 改前必读硬护栏：先读再编辑
    r = tg.t_edit_file({"path": str(f), "old": "1.00", "new": "0.85"}, ctx)
    assert "已编辑" in r and f.read_text() == "bid 0.85 done"


def test_edit_blocked_without_prior_read(tmp_path):
    f = tmp_path / "e.txt"
    f.write_text("bid 1.00 done", encoding="utf-8")
    ctx = _ctx(tmp_path, allow=["edit_file"])
    r = tg.t_edit_file({"path": str(f), "old": "1.00", "new": "0.85"}, ctx)
    assert "已拦截" in r and "read_file" in r and f.read_text() == "bid 1.00 done"  # 未读→挡回，未改


def test_edit_nonunique_refused(tmp_path):
    f = tmp_path / "e.txt"
    f.write_text("x x", encoding="utf-8")
    ctx = _ctx(tmp_path, allow=["edit_file"])
    r = tg.t_edit_file({"path": str(f), "old": "x", "new": "y"}, ctx)
    assert "不唯一" in r and f.read_text() == "x x"  # 未改


# ── 执行类（沙箱）──
def test_run_python(tmp_path):
    ctx = _ctx(tmp_path, allow=["run_python"])
    r = tg.t_run_python({"code": "print(6*7)"}, ctx)
    assert "42" in r and "退出码 0" in r


def test_run_python_blocked_plan(tmp_path):
    ctx = _ctx(tmp_path, allow=["run_python"])
    ctx.plan_mode = True
    assert "计划模式" in tg.t_run_python({"code": "print(1)"}, ctx)


def test_run_command(tmp_path):
    ctx = _ctx(tmp_path, allow=["run_command"])
    r = tg.t_run_command({"command": "echo ivyea-ok"}, ctx)
    assert "ivyea-ok" in r


def test_run_command_rejects_dangerous(tmp_path):
    ctx = _ctx(tmp_path, allow=["run_command"])
    r = tg.t_run_command({"command": "git reset --hard"}, ctx)
    assert "拒绝" in r


def test_run_python_runs_in_workspace(tmp_path):
    ctx = _ctx(tmp_path, allow=["run_python"])
    r = tg.t_run_python({"code": "import os; print(os.getcwd())"}, ctx)
    assert str(tmp_path) in r


# ── web_fetch（monkeypatch，不打真实网络）──
def test_web_fetch_strips_html(tmp_path, monkeypatch):
    class _R:
        status_code = 200
        text = "<html><body><script>x()</script><p>价格 9.9</p></body></html>"
        headers = {"content-type": "text/html"}
    monkeypatch.setattr("httpx.get", lambda *a, **k: _R())
    out = tg.t_web_fetch({"url": "https://example.com"}, _ctx(tmp_path))
    assert "价格 9.9" in out and "<script>" not in out and "x()" not in out


def test_web_fetch_bad_url(tmp_path):
    assert "http" in tg.t_web_fetch({"url": "ftp://x"}, _ctx(tmp_path))


# ── web_images：配图（monkeypatch，不打真实网络）──
def _hero_html(img: str, title: str = "某公司 官网") -> str:
    return (f'<html><head><title>{title}</title>'
            f'<meta property="og:image" content="{img}"></head><body>正文</body></html>')


def test_page_hero_image_reads_og_image(monkeypatch):
    monkeypatch.setattr(tg, "_stream_text",
                        lambda url, **k: ("text/html", _hero_html("/pic/a.jpg")))
    assert tg._page_hero_image("https://example.com/x") == (
        "https://example.com/pic/a.jpg", "某公司 官网", "")


def test_page_hero_image_reads_reversed_meta_order(monkeypatch):
    """content 写在 property 前面的 meta 也很常见，漏掉就白丢一半候选。"""
    html = ('<html><head><meta content="https://cdn.x/b.png" name="twitter:image">'
            '</head><body></body></html>')
    monkeypatch.setattr(tg, "_stream_text", lambda url, **k: ("text/html", html))
    assert tg._page_hero_image("https://example.com/")[0] == "https://cdn.x/b.png"


def test_page_hero_image_skips_logo_and_non_html(monkeypatch):
    monkeypatch.setattr(tg, "_stream_text",
                        lambda url, **k: ("text/html", _hero_html("https://x.com/logo.png")))
    assert tg._page_hero_image("https://example.com/") is None
    monkeypatch.setattr(tg, "_stream_text", lambda url, **k: ("application/pdf", ""))
    assert tg._page_hero_image("https://example.com/") is None


def test_page_hero_image_also_grabs_description(monkeypatch):
    """摘要要顺手带回来 —— 少这一段，模型就得再跑一次 web_search。"""
    html = ('<html><head><title>某公司</title>'
            '<meta property="og:image" content="https://cdn/x.jpg">'
            '<meta property="og:description" content="一家做数字孪生的公司，2015 年成立。">'
            '</head></html>')
    monkeypatch.setattr(tg, "_stream_text", lambda url, **k: ("text/html", html))
    assert tg._page_hero_image("https://a.com")[2] == "一家做数字孪生的公司，2015 年成立。"


def test_good_picture_rejects_icons_and_strips():
    assert tg._good_picture((1200, 630))
    assert not tg._good_picture(None)
    assert not tg._good_picture((64, 64))          # 图标
    assert not tg._good_picture((1600, 90))        # 装饰条
    assert not tg._good_picture((320, 120))        # 太矮


def test_web_images_returns_markdown(monkeypatch):
    monkeypatch.setattr(tg, "_search_results",
                        lambda q, limit=8: [("某公司", "https://a.com/1"), ("旧闻", "https://b.com/2")])
    monkeypatch.setattr(tg, "_page_hero_image",
                        lambda url: ("https://cdn/" + url[-1] + ".jpg", "页面标题", "一段摘要"))
    monkeypatch.setattr(tg, "_image_dims", lambda url: (1200, 630))
    out = tg.t_web_images({"query": "某公司", "limit": 2}, None)
    assert "![页面标题](https://cdn/1.jpg)" in out
    assert "https://a.com/1" in out                 # 来源要留，方便核实
    assert "摘要：一段摘要" in out                   # 摘要要一起给，省掉一次 web_search
    assert out.count("![") == 2


def test_web_images_dedupes_and_honours_limit(monkeypatch):
    monkeypatch.setattr(tg, "_search_results",
                        lambda q, limit=8: [("x", f"https://s{i}.com") for i in range(6)])
    monkeypatch.setattr(tg, "_page_hero_image", lambda url: ("https://cdn/same.jpg", "同一张", ""))
    monkeypatch.setattr(tg, "_image_dims", lambda url: (900, 600))
    out = tg.t_web_images({"query": "x", "limit": 4}, None)
    assert out.count("![") == 1                     # 六个页面共用一张图 → 只留一张


def test_web_images_says_so_when_nothing_usable(monkeypatch):
    """配不出图要明说，不能回一句空的让模型以为拿到了图。"""
    monkeypatch.setattr(tg, "_search_results", lambda q, limit=8: [("x", "https://a.com")])
    monkeypatch.setattr(tg, "_page_hero_image", lambda url: None)
    assert "配不出图" in tg.t_web_images({"query": "x"}, None)
    assert "query 为空" in tg.t_web_images({"query": "  "}, None)


def test_web_images_is_readonly_and_parallel_safe():
    from ivyea_agent.agent_tools import PARALLEL_SAFE, READONLY_TOOLS
    assert "web_images" in READONLY_TOOLS and "web_images" in PARALLEL_SAFE


# ── 注册完整性 ──
def test_registered_in_agent_tools():
    from ivyea_agent.agent_tools import TOOL_SCHEMAS, _DISPATCH
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    for n in tg.GENERAL_DISPATCH:
        assert n in names and n in _DISPATCH


def test_task_tools_update_bound_task(tmp_path, monkeypatch):
    from ivyea_agent import task_runner

    monkeypatch.setattr(task_runner, "TASK_DIR", tmp_path / "tasks")
    task = task_runner.create("Tool task", steps=["inspect", "finish"])
    ctx = _ctx(tmp_path)
    ctx.task_id = task["id"]

    assert "Tool task" in tg.t_task_read({}, ctx)
    out = tg.t_task_step({"index": 1, "status": "in_progress", "notes": "reading"}, ctx)
    assert "reading" in out
    out = tg.t_task_step({"index": 1, "status": "completed", "notes": "done"}, ctx)
    assert "done" in out
    out = tg.t_task_log({"text": "agent progress"}, ctx)
    assert "agent progress" in out
    assert "继续 Ivyea 长任务" in tg.t_task_resume({}, ctx)

    saved = task_runner.load(task["id"])
    assert saved["steps"][0]["status"] == "completed"
    assert saved["events"][-1]["kind"] == "agent"


# ── 检索死胡同信号 + 花括号 glob（复盘 20260705-073002：0 文件空转/花括号静默不匹配）──
def test_grep_deadend_zero_files(tmp_path):
    """空/错根：grep 扫 0 文件 → 返回 ⚠ 死胡同信号而非普通'无匹配'，逼换策略不换关键词。"""
    r = tg.t_grep({"pattern": "anything"}, _ctx(tmp_path))
    assert r.startswith(tg.DEADEND_MARK)
    assert "扫描了 0 个文件" in r


def test_grep_brace_glob_matches(tmp_path):
    """grep 的 glob 支持 **/*.{ts,tsx} 花括号（此前 path.match 不认 → 静默扫 0 文件）。"""
    (tmp_path / "a.ts").write_text("needle here", encoding="utf-8")
    (tmp_path / "b.py").write_text("needle here", encoding="utf-8")
    sub = tmp_path / "sub"; sub.mkdir()
    (sub / "c.ts").write_text("needle here", encoding="utf-8")
    r = tg.t_grep({"pattern": "needle", "glob": "**/*.{ts,tsx}"}, _ctx(tmp_path))
    assert "a.ts" in r and "sub/c.ts" in r and "b.py" not in r


def test_glob_brace_expansion(tmp_path):
    """t_glob 支持 {py,md} 花括号展开。"""
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    (tmp_path / "b.md").write_text("x", encoding="utf-8")
    (tmp_path / "c.txt").write_text("x", encoding="utf-8")
    r = tg.t_glob({"pattern": "**/*.{py,md}"}, _ctx(tmp_path))
    assert "a.py" in r and "b.md" in r and "c.txt" not in r


def test_search_globs_normalize_absolute_and_repo_prefixed_patterns(tmp_path):
    repo = tmp_path / "ivyea-agent"
    repo.mkdir()
    (repo / "a.py").write_text("needle\n", encoding="utf-8")
    ctx = _ctx(repo)

    absolute = tg.t_grep({"pattern": "needle", "glob": str(repo / "**" / "*.py")}, ctx)
    prefixed = tg.t_grep({"pattern": "needle", "glob": "ivyea-agent/**/*.py"}, ctx)
    globbed = tg.t_glob({"pattern": str(repo / "**" / "*.py")}, ctx)

    assert "a.py" in absolute
    assert "a.py" in prefixed
    assert "a.py" in globbed


def test_glob_deadend_empty_root(tmp_path):
    """空根：t_glob → ⚠ 根目录没有文件（path 参数多半写错）。"""
    r = tg.t_glob({"pattern": "**/*.py"}, _ctx(tmp_path))
    assert r.startswith(tg.DEADEND_MARK) and "没有文件" in r


def test_glob_deadend_no_match_with_files(tmp_path):
    """有文件但 0 匹配：t_glob → ⚠ 提示确认 glob/路径，别反复重搜。"""
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    r = tg.t_glob({"pattern": "**/*.rs"}, _ctx(tmp_path))
    assert r.startswith(tg.DEADEND_MARK) and "没有匹配" in r


def test_ui_tool_result_highlights_deadend(monkeypatch):
    """ui.tool_result：⚠ 开头的死胡同结果用 warn 黄色 + ▲ 高亮，不被灰掉。"""
    from ivyea_agent import ui
    monkeypatch.delenv("NO_COLOR", raising=False)
    colored = ui.tool_result(tg.DEADEND_MARK + " 扫描了 0 个文件（根 /x）", color=True)
    assert "\033[" in colored                      # 有 ANSI 上色（非 muted 灰）
    plain = ui.strip_ansi(colored)
    assert "▲" in plain and tg.DEADEND_MARK not in plain   # 图标换成 ▲、⚠ 前缀已剥离
    normal = ui.strip_ansi(ui.tool_result("命中 3 处（扫描 10 文件）", color=True))
    assert "✓" in normal                           # 有效命中走 success，和死胡同 warning 明确区分
