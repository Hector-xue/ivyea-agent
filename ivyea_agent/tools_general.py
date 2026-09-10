"""通用工具层 —— 让 agent 不止会广告巡检，能干真实运营活。

读类（read_file/list_dir/web_fetch/web_search）自动放行；写/执行类
（write_file/edit_file/run_python/run_command）经人工审批门控（复用 permission），
且在计划模式下一律拒绝。执行类带沙箱：限工作目录、超时、输出截断。

设计取自 Claude API agent-design：读广、写/执行经门控（可审计、可拦截）。
"""
from __future__ import annotations

import json
import os
import re
import time
import subprocess
from pathlib import Path

from . import config, panels, permission, policy, progress_reporting, security

_MAX_OUT = 4000          # 工具返回截断（防爆上下文）
_EXEC_TIMEOUT = 30       # 执行类默认超时（秒）
DEADEND_MARK = "⚠"       # 死胡同信号前缀（0 文件/无文件/路径错）：供 ui.tool_result 高亮 + 提示模型换策略
_MUTATING = {"write_file", "edit_file", "run_python", "run_command"}
_DANGEROUS_COMMANDS = [
    r"\brm\s+-rf\s+/(?:\s|$)",
    r"\bgit\s+reset\s+--hard\b",
    r"\bmkfs(?:\.[\w-]+)?\b",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bdd\s+if=.*\s+of=/dev/",
]


def _truncate(s: str, n: int = _MAX_OUT) -> str:
    s = security.redact_text(s)
    return s if len(s) <= n else s[:n] + f"\n…（已截断，共 {len(s)} 字）"


def _dangerous_command(command: str) -> str:
    for pattern in _DANGEROUS_COMMANDS:
        if re.search(pattern, command, re.I):
            return pattern
    return ""


def _with_line_numbers(text: str, start: int = 1) -> str:
    """cat -n 式给每行加行号（仅供模型定位/引用；edit 的 old 不要带行号）。"""
    lines = text.split("\n")
    width = max(4, len(str(start + len(lines) - 1)))
    return "\n".join(f"{start + i:>{width}}\t{ln}" for i, ln in enumerate(lines))


_LINE_NO_RE = re.compile(r"^\s*\d+\t", re.M)


def _strip_line_no(text: str) -> str:
    """去掉每行前导的 `行号\\t`（兜底模型误把 read_file 的行号粘进 old）。"""
    return _LINE_NO_RE.sub("", text)


def _disp(p) -> str:
    """审批预览用的友好路径：在 cwd 下显示相对路径，否则原样。仅展示用，写入仍用绝对路径。"""
    try:
        rel = os.path.relpath(p, os.getcwd())
        return rel if not rel.startswith("..") else str(p)
    except Exception:
        return str(p)


def _require_read(ctx, paths) -> str:
    """改前必读硬护栏（对标 Claude Code）：本会话未 read_file 过的已存在目标文件，
    直接挡回让模型先读。返回空串=放行；非空=应直接 return 的错误信息。"""
    read = getattr(ctx, "read_paths", None) or set()
    unread = [p for p in paths if str(p) not in read and Path(p).exists()]
    if not unread:
        return ""
    names = "、".join(_disp(p) for p in unread)
    return (f"已拦截：本会话还没 read_file 过 {names}，不能盲改。"
            f"请先 read_file 看真实内容、确认 old 唯一匹配，再重试本次编辑。")


def _gate(ctx, kind: str, preview: str, detail: dict | None = None) -> tuple[bool, str]:
    """写/执行前门控：计划模式拒绝；否则人工审批。返回 (放行?, 拒绝消息)。
    detail：给 policy 档无人值守判定用的结构化信息（command/path），并入 intent。"""
    if getattr(ctx, "plan_mode", False):
        # 记一笔：这一轮有多少个写操作是被"只读"挡下的。
        # 界面上必须说得出这件事 —— 用户看到的是模型转述的"被拦截"，然后跑去
        # 「待审批」页找，而只读档**根本不会产生审批请求**，那一页永远是空的。
        # 真实反馈："经常跑一半跟我说被拦截，待审批那一页也从来没看到任何审批项"。
        setattr(ctx, "readonly_blocks", int(getattr(ctx, "readonly_blocks", 0) or 0) + 1)
        return False, (f"计划模式（只读）：不执行 {kind}。这是**档位**限制，不是出错，"
                       f"也不会产生待审批项；要真做就让用户把审批档位切到「审批放行」"
                       f"或「完全放行」。现在请先把方案给出来。")
    decision = permission.request_intent({"op_type": kind, **(detail or {})}, preview, ctx.perm)
    if decision == permission.APPROVE:
        return True, ""
    if decision == permission.ABORT:
        return False, "用户终止。"
    return False, f"已跳过：{preview}"


# ── 读类（自动放行）──────────────────────────────────────────────────────────
def t_read_file(args: dict, ctx) -> str:
    p = _ws_path(ctx, args.get("path", ""))
    ok, msg = policy.check_path(p, "read")
    if not ok:
        return msg
    if not p.exists():
        return f"文件不存在：{p}"
    if p.is_dir():
        return f"{p} 是目录，请用 list_dir。"
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        return f"读取失败：{e}"
    try:
        ctx.read_paths.add(str(p))   # 记录已读，供 edit_file/code_apply_patch 的改前必读软护栏
    except AttributeError:
        pass
    offset = args.get("offset")
    limit = args.get("limit")
    if offset is None and limit is None:
        return _truncate(_with_line_numbers(text, 1))
    # 行区间读取：offset 从 1 开始；大文件只取一段，避免被迫用 run_command 分段读。
    lines = text.splitlines()
    total = len(lines)
    start = max(1, int(offset or 1))
    if start > total:
        return f"（{p.name} 共 {total} 行，offset={start} 超出范围）"
    end = total if limit is None else min(total, start + max(1, int(limit)) - 1)
    body = _with_line_numbers("\n".join(lines[start - 1:end]), start)
    return f"（{p.name} 第 {start}–{end} 行，共 {total} 行）\n" + _truncate(body)


def t_list_dir(args: dict, ctx) -> str:
    p = _ws_path(ctx, args.get("path", "."), ".")
    ok, msg = policy.check_path(p, "read")
    if not ok:
        return msg
    if not p.exists():
        return f"目录不存在：{p}"
    if p.is_file():
        return f"{p.name}\t{p.stat().st_size} bytes"
    try:
        rows = []
        for c in sorted(p.iterdir()):
            kind = "d" if c.is_dir() else "f"
            size = c.stat().st_size if c.is_file() else ""
            rows.append(f"  [{kind}] {c.name}\t{size}")
        return f"{p}（{len(rows)} 项）：\n" + _truncate("\n".join(rows))
    except Exception as e:  # noqa: BLE001
        return f"列目录失败：{e}"


def t_web_fetch(args: dict, ctx) -> str:
    url = args.get("url", "")
    if not url.startswith(("http://", "https://")):
        return "url 必须以 http(s):// 开头。"
    try:
        import httpx
        r = httpx.get(url, timeout=30, follow_redirects=True,
                      headers={"User-Agent": "ivyea-agent/0.2"})
    except Exception as e:  # noqa: BLE001
        return f"抓取失败：{e}"
    if r.status_code >= 400:
        return f"HTTP {r.status_code}"
    text = r.text
    ct = r.headers.get("content-type", "")
    if "html" in ct:
        import re
        text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()
    # 末尾把**这一页的地址**再交给模型一次，并说清楚引用它的写法。
    # 起因：用户看完一份网页调研的回答说"末尾的引用来源还是不能直接点击跳转"——
    # 那份来源清单长这样：「ccaf101.com《FDE 薪资：分档与数据》（2026）」，八条里
    # 一个 URL 都没有。地址一直在模型手上（就在它自己那次 web_fetch 的入参里），
    # 它只是没写进回答。所以把地址放到**结果这一侧**、紧挨着内容，并直说要 markdown
    # 链接 —— 屏幕上的来源能不能点，取决于回答里有没有这个链接。
    return _truncate(text) + (
        f"\n\n[来源] {url}\n"
        "（回答里引用这一页时，把来源写成可点击的 markdown 链接 "
        f"`[网站或文章名]({url})`，不要只写站名。）")


def _search_results(q: str, limit: int = 8) -> list[tuple[str, str]]:
    """搜索 → [(标题, URL)]。尽力而为：DuckDuckGo lite（无 key，可能受限）。

    抽成独立函数是因为 web_search 和 web_images 要的是同一批结果：前者只把它排版
    成文本，后者还要顺着这些 URL 去摸配图。
    """
    import httpx
    r = httpx.post("https://lite.duckduckgo.com/lite/", data={"q": q}, timeout=30,
                   headers={"User-Agent": "Mozilla/5.0"})
    rows = re.findall(r'<a[^>]+class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S)
    if not rows:
        rows = re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*>(.*?)</a>', r.text, re.S)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, title in rows:
        title = re.sub(r"<[^>]+>", "", title).strip()
        if not title or not href.startswith("http") or href in seen:
            continue
        seen.add(href)
        out.append((title, href))
        if len(out) >= limit:
            break
    return out


def t_web_search(args: dict, ctx) -> str:
    """尽力而为：DuckDuckGo lite（无 key，可能受限）。"""
    q = args.get("query", "")
    if not q:
        return "query 为空。"
    try:
        rows = _search_results(q, 8)
    except Exception as e:  # noqa: BLE001
        return f"搜索失败（尽力而为）：{e}"
    if not rows:
        return "（无结果，或搜索源受限）"
    out = [f"  · {title}\n    {href}" for title, href in rows]
    # 同 web_fetch 末尾那句：来源要能点。搜索结果本来就带 URL，缺的只是"写进回答"。
    out.append("（引用其中任何一条时，在回答里写成可点击的 markdown 链接 `[标题](URL)`。）")
    return "\n".join(out)


# ── 配图（og:image）─────────────────────────────────────────────────────────
#
# 找图不是画图：一个公司/产品/地点长什么样，网上早就有权威的照片（官网首图、
# 新闻配图），比生成一张更快也更真。绝大多数正经网页都会给自己声明一张分享用的
# 预览图（og:image / twitter:image），这就是现成的配图源，不需要任何图片搜索 key。

_IMG_META = re.compile(
    r'<meta[^>]+(?:property|name)=["\']?(og:image(?::secure_url|:url)?|twitter:image(?::src)?)["\']?'
    r'[^>]+content=["\']([^"\']+)["\']', re.I)
_IMG_META_REV = re.compile(          # content 写在 property 前面的写法也不少
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']?'
    r'(og:image(?::secure_url|:url)?|twitter:image(?::src)?)["\']?', re.I)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_DESC_META = re.compile(
    r'<meta[^>]+(?:property|name)=["\']?(?:og:description|description)["\']?'
    r'[^>]+content=["\']([^"\']{20,})["\']', re.I)
_DESC_META_REV = re.compile(
    r'<meta[^>]+content=["\']([^"\']{20,})["\'][^>]+(?:property|name)=["\']?'
    r'(?:og:description|description)["\']?', re.I)
# 图标/头像/占位图不是配图，长得再对也别要
_IMG_JUNK = re.compile(
    r"(logo|icon|favicon|sprite|avatar|placeholder|blank|spacer|1x1|pixel|图标|logo图)", re.I)
# 分阶段超时：慢站要么连不上要么读不动，卡在哪一段都不该拖住整个配图。
_NET_TIMEOUT = {"connect": 3.0, "read": 4.0, "write": 3.0, "pool": 3.0}
_BUDGET_S = 20.0        # 配图总预算：超了就用手上已有的图交差，不等齐


def _abs_url(base: str, u: str) -> str:
    from urllib.parse import urljoin
    return urljoin(base, (u or "").strip())


def _stream_text(url: str, max_bytes: int = 200_000) -> tuple[str, str]:
    """读一个页面的开头，返回 (content-type, 文本)。抓不到就 ("", "")。

    只读开头：og:* 全在 `<head>` 里，为了一行 meta 把整篇文章拉下来纯属浪费。
    单独抽出来是为了可测 —— 上层的挑图逻辑不该为了测试去 mock httpx 的流。
    """
    import httpx
    try:
        with httpx.stream("GET", url, timeout=httpx.Timeout(**_NET_TIMEOUT), follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0 (compatible; ivyea-agent/0.2)"}) as r:
            if r.status_code >= 400:
                return "", ""
            ctype = r.headers.get("content-type", "")
            head = b""
            for chunk in r.iter_bytes():
                head += chunk
                if len(head) > max_bytes:
                    break
            return ctype, head.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return "", ""


def _page_hero_image(url: str) -> tuple[str, str, str] | None:
    """抓一个页面，返回 (配图URL, 页面标题, 页面摘要)。抓不到配图就 None。

    摘要是顺手捡的：HTML 已经在手上了，`og:description` 就在旁边那一行。带上它，
    模型问"某某公司是做什么的"时**一次调用**就同时拿到图和资料，不必先 web_search
    再 web_images —— 实测那一轮为此多走了两步，每步都是一次完整的模型往返。
    """
    ctype, html = _stream_text(url)
    if "html" not in ctype or not html:
        return None
    src = ""
    m = _IMG_META.search(html)
    if m:
        src = m.group(2)
    else:
        m2 = _IMG_META_REV.search(html)
        if m2:
            src = m2.group(1)
    if not src:
        return None
    src = _abs_url(url, src)
    if not src.startswith(("http://", "https://")) or _IMG_JUNK.search(src):
        return None
    t = _TITLE_RE.search(html)
    title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", t.group(1))).strip()[:60] if t else ""
    dm = _DESC_META.search(html) or _DESC_META_REV.search(html)
    desc = re.sub(r"\s+", " ", dm.group(1)).strip()[:220] if dm else ""
    return src, title, desc


def _image_dims(url: str) -> tuple[int, int] | None:
    """图真的在、真是图、并量出它的像素尺寸。抓不到或不是图就 None。

    量尺寸而不是量字节数：字节数分不清"一张压得很狠的大图"和"一枚 PNG 图标"，
    实测站点 logo 有 40KB 的、正经配图有 12KB 的。Pillow 的增量 Parser 拿到文件头
    就能报 size，所以只需要读开头几十 KB，不用整张下载。
    """
    import httpx
    from PIL import ImageFile
    try:
        with httpx.stream("GET", url, timeout=httpx.Timeout(**_NET_TIMEOUT), follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"}) as r:
            if r.status_code >= 400:
                return None
            if not r.headers.get("content-type", "").lower().startswith("image/"):
                return None
            parser = ImageFile.Parser()
            read = 0
            for chunk in r.iter_bytes(8192):
                parser.feed(chunk)
                read += len(chunk)
                if parser.image is not None:
                    return parser.image.size
                if read > 96_000:          # 头都读不出尺寸，不折腾了
                    break
    except Exception:  # noqa: BLE001
        return None
    return None


def _good_picture(dims: tuple[int, int] | None) -> bool:
    """够不够格当配图。挡掉图标/头像/细长的装饰条。"""
    if not dims:
        return False
    w, h = dims
    if w < 320 or h < 200:
        return False
    ratio = w / h if h else 99
    return 0.25 <= ratio <= 4.0


def _map_within(fn, items: list, deadline: float, workers: int = 8) -> list:
    """并发跑 fn，但只等到 deadline 为止 —— 没跑完的按"没结果"处理。

    配图是锦上添花：慢站拖到天荒地老也不能让整轮回答跟着卡住。ThreadPoolExecutor
    的 map(timeout=) 会抛异常丢掉全部结果，所以自己按 future 收，收到几个算几个。
    """
    from concurrent.futures import ThreadPoolExecutor, wait
    out: list = [None] * len(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(fn, it): i for i, it in enumerate(items)}
        wait(list(futs), timeout=max(deadline - time.monotonic(), 0.1))
        for fut, i in futs.items():
            if fut.done() and not fut.cancelled():
                try:
                    out[i] = fut.result()
                except Exception:  # noqa: BLE001
                    out[i] = None
            else:
                fut.cancel()
    return out


def t_web_images(args: dict, ctx) -> str:
    """搜一批相关网页，把它们各自声明的配图收上来。"""
    q = (args.get("query") or "").strip()
    if not q:
        return "query 为空。"
    try:
        limit = max(1, min(int(args.get("limit") or 4), 8))
    except (TypeError, ValueError):
        limit = 4

    try:
        # 候选要开得比 limit 大得多：一半页面没声明配图，声明了的还有一批是 logo。
        results = _search_results(q, max(limit * 4, 12))
    except Exception as e:  # noqa: BLE001
        return f"配图搜索失败（尽力而为）：{e}"
    if not results:
        return "（没搜到相关网页，配不出图）"

    deadline = time.monotonic() + _BUDGET_S
    heroes = _map_within(lambda row: _page_hero_image(row[1]), results, deadline)

    candidates: list[tuple[str, str, str, str]] = []   # (图片URL, 说明, 来源页, 摘要)
    seen_img: set[str] = set()
    for (title, page), hero in zip(results, heroes):
        if not hero or hero[0] in seen_img:
            continue
        seen_img.add(hero[0])
        candidates.append((hero[0], hero[1] or title, page, hero[2]))
    if not candidates:
        return "（这些页面都没有声明配图，配不出图）"

    dims = _map_within(lambda c: _image_dims(c[0]), candidates, deadline)
    picked = [c for c, d in zip(candidates, dims) if _good_picture(d)][:limit]

    if not picked:
        return "（找到的图都取不回来，配不出图）"
    lines = ["配到 %d 张图，来源页的摘要一起给你了 —— 要介绍这个主题的话，"
             "**这一次调用拿到的信息就够了，不用再 web_search**。"
             "用图就把 markdown **原样**抄进回答正文，一张图配一句说明，别只贴链接：" % len(picked)]
    for img, cap, page, desc in picked:
        lines.append(f"![{cap}]({img})")
        lines.append(f"  ↑ 来源：{page}")
        if desc:
            lines.append(f"  摘要：{desc}")
    return "\n".join(lines)


# ── 代码导航（只读，自动放行）────────────────────────────────────────────────
_GREP_BINARY = re.compile(rb"\x00")


def _ws_root(ctx):
    from . import workspace
    return workspace.resolve_root(getattr(ctx, "workspace", "") or None)


def _ws_path(ctx, raw: str, default: str = "") -> Path:
    """把工具参数里的路径解析成绝对路径：**相对路径按 ctx.workspace 解，不按进程 cwd。**

    ``ToolContext.workspace`` 一直被文档描述为"通用工具的工作目录"，但只有
    grep/glob/code_* 这几个走 `_ws_root` 的工具真的照做；read_file / list_dir /
    write_file / edit_file 都是直接 `Path(...).resolve()`，也就是按**进程**的 cwd。

    CLI 下两者恰好一致（`ctx.workspace = os.getcwd()`），所以一直没暴露。但嵌进
    IvyeaOps 跑时，进程 cwd 是 ops 的安装目录，工作区却可能指向别处 —— 实测
    `list_dir(".")` 列出来的是 /root/ivyea-ops，而不是用户绑定的工作区目录。

    绝对路径不受影响；`~` 照常展开。
    """
    raw = str(raw or default or "")
    expanded = os.path.expanduser(raw)
    p = Path(expanded)
    if not p.is_absolute():
        p = _ws_root(ctx) / p
    return p.resolve()


def _expand_braces(pattern: str) -> list[str]:
    """把单层花括号 glob 展开成多个模式：**/*.{ts,tsx,js} → [**/*.ts, **/*.tsx, **/*.js]。
    fnmatch/PurePath.match 都不认花括号，缺这一步会静默扫 0 文件。不支持嵌套（够用即可）。"""
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    head, tail = pattern[:m.start()], pattern[m.end():]
    out: list[str] = []
    for opt in m.group(1).split(","):
        out.extend(_expand_braces(head + opt + tail))   # 递归展开可能存在的第二组花括号
    return out


def _normalize_glob_pattern(pattern: str, root: Path) -> str:
    """Make common model-produced absolute/repo-prefixed globs root-relative.

    Once task scope has locked ``root=/x/ivyea-agent``, models still sometimes
    emit ``/x/ivyea-agent/**/*.py`` or ``ivyea-agent/**/*.py``. Path matching
    expects a root-relative pattern; normalize only prefixes that provably
    identify the active root and leave outside paths untouched for policy/error
    handling.
    """
    raw = (pattern or "").strip().replace("\\", "/")
    if raw.startswith("./"):
        raw = raw[2:]
    root_text = root.resolve().as_posix().rstrip("/")
    if raw == root_text:
        return "*"
    if raw.startswith(root_text + "/"):
        return raw[len(root_text) + 1:]
    prefix = root.name + "/"
    if root.name and raw.startswith(prefix):
        return raw[len(prefix):]
    return raw


def t_grep(args: dict, ctx) -> str:
    """内容正则搜索（ripgrep 风格），返回 file:line 命中行。只读，自动放行。"""
    pattern = args.get("pattern", "")
    if not pattern:
        return "pattern 为空。"
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"正则无效：{e}"
    from . import workspace
    root = _ws_root(ctx)
    ok, msg = policy.check_path(root, "read")
    if not ok:
        return msg
    glob = (args.get("glob") or "").strip()
    normalized_glob = _normalize_glob_pattern(glob, root) if glob else ""
    globs = _expand_braces(normalized_glob) if normalized_glob else []  # 花括号 + 根相对容错
    max_hits = min(int(args.get("max_results") or 80), 300)
    hits: list[str] = []
    scanned = 0
    for path in workspace.iter_files(root):
        if globs and not any(path.match(g) for g in globs):
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if _GREP_BINARY.search(raw[:4096]):
            continue  # 跳过二进制
        scanned += 1
        text = raw.decode("utf-8", errors="replace")
        try:
            rel = path.relative_to(root).as_posix()   # 统一正斜杠（与 t_glob 一致，跨平台稳定；修 Windows 反斜杠）
        except ValueError:
            rel = path.as_posix()
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                if len(hits) >= max_hits:
                    break
        if len(hits) >= max_hits:
            break
    if not hits:
        if scanned == 0:   # 死胡同：根/glob 写错，不是"真没匹配"——报红旗、逼换策略而非换关键词重搜
            return (f"{DEADEND_MARK} 扫描了 0 个文件（根 {root}"
                    f"{('，glob=' + glob) if glob else ''}）——多半是搜索根或 glob 写错。"
                    "先用 list_dir 核对根目录，别换关键词重搜。")
        return f"无匹配（扫描 {scanned} 文件）：{pattern}"
    return f"命中 {len(hits)} 处（扫描 {scanned} 文件）：\n" + _truncate("\n".join(hits))


def t_glob(args: dict, ctx) -> str:
    """按文件名 glob 模式找文件（如 **/*.py）。只读、ignore-aware（复用 workspace.iter_files）。"""
    import fnmatch
    from . import workspace
    pattern = (args.get("pattern") or "").strip()
    if not pattern:
        return "pattern 为空。"
    base = args.get("path")
    root = Path(os.path.expanduser(base)).resolve() if base else Path(_ws_root(ctx))
    ok, msg = policy.check_path(root, "read")
    if not ok:
        return msg
    max_n = min(int(args.get("max_results") or 100), 500)
    # fnmatch 的 * 本就跨 /，把 **/ 归一为空、** 归一为 * 即可正确支持 **/*.py 这类（含根目录）；
    # 并展开 {a,b} 花括号（fnmatch 不认），避免 **/*.{ts,tsx} 静默 0 匹配。
    pats = _expand_braces(_normalize_glob_pattern(pattern, root))
    norms = [p.replace("**/", "").replace("**", "*") for p in pats]
    hits: list[str] = []
    scanned = 0
    for path in workspace.iter_files(root):
        scanned += 1
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.name
        if any(fnmatch.fnmatch(rel, n) for n in norms) or any(fnmatch.fnmatch(path.name, p) for p in pats):
            hits.append(rel)
            if len(hits) >= max_n:
                break
    if not hits:
        if scanned == 0:   # 死胡同：根目录下压根没文件 → path 参数多半写错
            return f"{DEADEND_MARK} 根目录 {root} 下没有文件——检查 path 参数是否写错。"
        return (f"{DEADEND_MARK} 没有匹配 {pattern} 的文件（扫描 {scanned} 文件）"
                "——确认 glob 与路径是否正确，别换关键词反复重搜。")
    return f"匹配 {len(hits)} 个文件：\n" + _truncate("\n".join(sorted(hits)))


def t_code_search(args: dict, ctx) -> str:
    """按符号/路径/预览检索代码库相关文件（索引搜索）。只读。"""
    from . import workspace
    q = args.get("query", "")
    if not q:
        return "query 为空。"
    rows = workspace.search(q, root=_ws_root(ctx), limit=min(int(args.get("limit") or 10), 30))
    return _truncate(workspace.render_search(rows, q))


def t_code_symbols(args: dict, ctx) -> str:
    """列出/搜索代码库里的函数/类等符号定义位置。只读。"""
    from . import workspace
    data = workspace.symbol_index(root=_ws_root(ctx), query=args.get("query", "") or "",
                                  limit=min(int(args.get("limit") or 40), 120))
    return _truncate(workspace.render_symbols(data))


def t_code_impact(args: dict, ctx) -> str:
    """查某个符号/文件的调用方、导入方与受影响测试。只读。"""
    from . import workspace
    target = args.get("target", "")
    if not target:
        return "target 为空。"
    data = workspace.impact_analysis(target, root=_ws_root(ctx), limit=min(int(args.get("limit") or 60), 120))
    return _truncate(workspace.render_impact(data))


# ── 代码闭环（结构化补丁 / 测试 / 修复计划）──────────────────────────────────
def t_code_apply_patch(args: dict, ctx) -> str:
    """一次性应用结构化补丁并跑测试（多文件/多处关联改动优先用它）。
    内部固定流程：先校验(不写)→失败直接返回；通过则给彩色 diff 预览→一次人工审批→落盘+跑测试。
    补丁 ops=[{path, old, new}]，old 必须在文件中唯一出现。"""
    from . import code_agent, patcher
    ops = args.get("ops")
    if not isinstance(ops, list) or not ops:
        return "ops 为空：需要 [{path, old, new}, ...]，old 必须在文件中唯一出现。"
    spec = {"ops": ops}
    root = _ws_root(ctx)
    # 1) 先校验，不写。校验失败不弹审批，直接把问题回给模型。
    validation = patcher.validate_spec(spec, root=root)
    if not validation.get("ok"):
        return _truncate(patcher.render_validation(validation))
    # 2) 改前必读硬护栏 + 构造彩色 diff 预览
    abs_paths = [str((Path(root) / o.get("path", "")).resolve()) for o in ops if isinstance(o, dict) and o.get("path")]
    blocked = _require_read(ctx, abs_paths)
    if blocked:
        return blocked
    n = len([o for o in ops if isinstance(o, dict)])
    lines = [f"应用结构化补丁（{n} 处）并跑测试："]
    for o in ops:
        if not isinstance(o, dict):
            continue
        lines.append(f"  · {_disp((Path(root) / o.get('path', '')).resolve())}")
        try:
            lines.append(panels.render_diff(o.get("old", ""), o.get("new", ""), str(o.get("path", ""))))
        except Exception:
            pass
    preview = "\n".join(lines)
    # 3) 一次审批
    _paths = [str((Path(root) / o.get("path", "")).resolve()) for o in ops if isinstance(o, dict)]
    ok, msg = _gate(ctx, "code_apply_patch", preview, detail={"paths": _paths})
    if not ok:
        return msg
    # 4) 落盘 + 跑测试
    try:
        result = code_agent.patch_apply_loop(
            spec, root=root,
            test_command=args.get("test_command", "") or "", execute=True)
    except Exception as e:  # noqa: BLE001
        return f"补丁执行出错：{e}"
    return _truncate(code_agent.render_run(result))


def t_run_tests(args: dict, ctx) -> str:
    """在工作目录跑测试命令并返回结果（执行，会弹审批）。默认 `python -m pytest`。"""
    command = (args.get("command") or "python -m pytest").strip()
    ok, msg = _gate(ctx, "run_tests", "运行测试：" + command)
    if not ok:
        return msg
    from . import code_agent
    res = code_agent.run_tests(command, root=_ws_root(ctx), timeout=int(args.get("timeout") or 120))
    head = "✓ 测试通过" if res.get("ok") else f"✗ 测试失败（exit {res.get('returncode')}）"
    return _truncate(head + "\n" + (res.get("output") or ""))


def t_code_repair(args: dict, ctx) -> str:
    """解析失败的测试输出，生成下一轮修复计划（可疑文件/失败摘要/重跑命令）。只读。"""
    output = args.get("test_output", "") or ""
    if not output.strip():
        return "test_output 为空：把失败的测试输出贴进来。"
    from . import code_agent
    return _truncate(code_agent.render_repair(code_agent.repair_plan(output, root=_ws_root(ctx))))


# ── MCP（连任意已配置的 MCP 服务器：工具/资源/prompt）─────────────────────────
def _mcp_servers() -> dict:
    return config.load_mcp().get("mcpServers", {})


def _mcp_run(server: str, fn):
    """连 server → initialize → fn(client) → close。返回 (result, err_str)；任一为 None。"""
    from .mcp_client import MCPClient, MCPError
    spec = _mcp_servers().get(server)
    if not spec:
        return None, f"未配置 MCP 服务器：{server}（ivyea mcp list 查看 / ivyea mcp add 添加）"
    client = None
    try:
        client = MCPClient(spec)
        client.initialize()
        return fn(client), None
    except MCPError as e:
        return None, f"MCP 错误（{server}）：{e}"
    except Exception as e:  # noqa: BLE001
        return None, f"MCP 调用出错（{server}）：{e}"
    finally:
        if client is not None:
            client.close()


def _mcp_targets(server: str):
    servers = _mcp_servers()
    if not servers:
        return None, "未配置任何 MCP 服务器（ivyea mcp add 添加）。"
    return ([server] if server else list(servers)), None


def _render_mcp_content(res) -> str:
    if not isinstance(res, dict):
        return json.dumps(res, ensure_ascii=False) if res else "（空结果）"
    parts = []
    for block in res.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(block.get("text") or "")
        else:
            parts.append(json.dumps(block, ensure_ascii=False))
    text = "\n".join(p for p in parts if p) or json.dumps(res, ensure_ascii=False)
    return ("[工具返回错误]\n" + text) if res.get("isError") else text


def t_mcp_list_tools(args: dict, ctx) -> str:
    """列出已配置 MCP 服务器的可用工具（只读）。server 省略=全部。"""
    targets, err = _mcp_targets((args.get("server") or "").strip())
    if err:
        return err
    out = []
    for s in targets:
        res, e = _mcp_run(s, lambda c: c.list_tools())
        if e:
            out.append(f"[{s}] {e}")
            continue
        lines = [f"[{s}] {len(res)} 个工具："]
        lines += [f"  · {t.get('name')} — {(t.get('description') or '')[:80]}" for t in res]
        out.append("\n".join(lines))
    return _truncate("\n".join(out))


def t_mcp_call_tool(args: dict, ctx) -> str:
    """调用某个 MCP 服务器的工具。计划模式拒绝；trusted 服务器免审，否则人工审批。"""
    server = (args.get("server") or "").strip()
    tool = (args.get("tool") or "").strip()
    if not server or not tool:
        return "需要 server 和 tool。先用 mcp_list_tools 查看可用工具。"
    spec = _mcp_servers().get(server)
    if not spec:
        return f"未配置 MCP 服务器：{server}（ivyea mcp list / add）"
    arguments = args.get("arguments") or {}
    if getattr(ctx, "plan_mode", False):
        return f"计划模式（只读）：不调用 MCP 工具 {server}.{tool}。/approve 后再做。"
    if not spec.get("trusted"):
        preview = f"调用 MCP 工具 {server}.{tool}　参数：{json.dumps(arguments, ensure_ascii=False)[:300]}"
        decision = permission.request_intent({"op_type": "mcp_call_tool"}, preview, ctx.perm)
        if decision == permission.ABORT:
            return "用户终止。"
        if decision != permission.APPROVE:
            return f"已跳过：{preview}"
    res, e = _mcp_run(server, lambda c: c.call_tool(tool, arguments))
    return e if e else _truncate(_render_mcp_content(res))


def t_mcp_list_resources(args: dict, ctx) -> str:
    """列出 MCP 服务器的资源（只读）。server 省略=全部。"""
    targets, err = _mcp_targets((args.get("server") or "").strip())
    if err:
        return err
    out = []
    for s in targets:
        res, e = _mcp_run(s, lambda c: c.list_resources())
        if e:
            out.append(f"[{s}] {e}")
            continue
        lines = [f"[{s}] {len(res)} 个资源："]
        lines += [f"  · {r.get('uri')} — {(r.get('name') or r.get('description') or '')[:80]}" for r in res]
        out.append("\n".join(lines))
    return _truncate("\n".join(out))


def t_mcp_read_resource(args: dict, ctx) -> str:
    """读取 MCP 服务器某个资源的内容（只读）。"""
    server = (args.get("server") or "").strip()
    uri = (args.get("uri") or "").strip()
    if not server or not uri:
        return "需要 server 和 uri。先用 mcp_list_resources 查看。"
    res, e = _mcp_run(server, lambda c: c.read_resource(uri))
    if e:
        return e
    parts = [(c.get("text") or c.get("blob") or json.dumps(c, ensure_ascii=False))
             for c in (res or []) if isinstance(c, dict)]
    return _truncate("\n".join(p for p in parts if p) or "（空）")


def t_mcp_list_prompts(args: dict, ctx) -> str:
    """列出 MCP 服务器提供的 prompt 模板（只读）。server 省略=全部。"""
    targets, err = _mcp_targets((args.get("server") or "").strip())
    if err:
        return err
    out = []
    for s in targets:
        res, e = _mcp_run(s, lambda c: c.list_prompts())
        if e:
            out.append(f"[{s}] {e}")
            continue
        lines = [f"[{s}] {len(res)} 个 prompt："]
        lines += [f"  · {p.get('name')} — {(p.get('description') or '')[:80]}" for p in res]
        out.append("\n".join(lines))
    return _truncate("\n".join(out))


def t_mcp_get_prompt(args: dict, ctx) -> str:
    """获取 MCP 服务器某个 prompt 模板的内容（只读）。"""
    server = (args.get("server") or "").strip()
    name = (args.get("name") or "").strip()
    if not server or not name:
        return "需要 server 和 name。先用 mcp_list_prompts 查看。"
    res, e = _mcp_run(server, lambda c: c.get_prompt(name, args.get("arguments") or {}))
    if e:
        return e
    parts = []
    if isinstance(res, dict):
        if res.get("description"):
            parts.append(str(res["description"]))
        for m in res.get("messages") or []:
            content = m.get("content") if isinstance(m, dict) else None
            txt = content.get("text") if isinstance(content, dict) else (content if isinstance(content, str) else "")
            parts.append(f"[{m.get('role', '?')}] {txt}")
    return _truncate("\n".join(p for p in parts if p) or json.dumps(res, ensure_ascii=False))


# ── 写/执行类（门控）─────────────────────────────────────────────────────────
# 一轮里最多记多少条文件变更。写循环跑飞时不至于把事件流灌爆。
_MAX_FILE_CHANGES = 40


def _record_change(ctx, path, action: str, old: str, new: str, scope: str = "file") -> None:
    """把一次文件改动记到 ctx 上，交给 agent_loop 发成 file_change 事件。

    **只在真的写成功之后调**：写之前记的话，被审批拒掉、或写盘失败的改动也会
    出现在界面的 diff 里 —— 那比不显示更糟，用户会以为改已经落地了。

    diff 复用审批卡那套 `panels.render_diff`，但要 color=False：这份是给网页看的，
    带 ANSI 转义只会变成一串乱码。
    """
    try:
        changes = getattr(ctx, "file_changes", None)
        if changes is None or len(changes) >= _MAX_FILE_CHANGES:
            return
        changes.append({
            "path": str(path), "action": action, "scope": scope,
            "diff": panels.render_diff(old, new, getattr(path, "name", ""), color=False),
        })
    except Exception:  # noqa: BLE001 — 记录失败绝不能影响这次写入本身
        return


def t_write_file(args: dict, ctx) -> str:
    path = str(args.get("path", "") or "")
    content = args.get("content", "")
    if not path:
        return "path 为空。"
    p = _ws_path(ctx, path)
    ok, msg = policy.check_path(p, "write")
    if not ok:
        return msg
    exists = p.exists()
    preview = f"写文件 {_disp(p)}（{'覆盖' if exists else '新建'}，{len(content)} 字）"
    if exists:
        try:
            preview += "\n" + panels.render_diff(p.read_text(encoding="utf-8"), content, p.name)
        except Exception:
            pass
    ok, msg = _gate(ctx, "write_file", preview, detail={"path": str(p)})
    if not ok:
        return msg
    try:
        before = ""
        if exists:
            try:
                before = p.read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001 — 读不到就当新建，diff 退化成全量新增
                before = ""
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        _record_change(ctx, p, "overwrite" if exists else "create", before, content)
        return f"已写入 {p}（{len(content)} 字）"
    except Exception as e:  # noqa: BLE001
        return f"写入失败：{e}"


def t_edit_file(args: dict, ctx) -> str:
    p = _ws_path(ctx, args.get("path", ""))
    ok, msg = policy.check_path(p, "write")
    if not ok:
        return msg
    old, new = args.get("old", ""), args.get("new", "")
    if not p.exists():
        return f"文件不存在：{p}"
    if not old:
        return "old 为空（要替换的原文）。"
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        return f"读取失败：{e}"
    cnt = text.count(old)
    if cnt == 0 and _LINE_NO_RE.search(old):
        old = _strip_line_no(old)   # 容错：模型把 read_file 的行号粘进了 old
        cnt = text.count(old)
    if cnt == 0:
        return "未找到要替换的原文（old 不匹配）。注意 old 用文件真实内容，不要带 read_file 的行号。"
    if cnt > 1:
        return f"原文出现 {cnt} 次，不唯一；请提供更长的 old 以唯一定位。"
    blocked = _require_read(ctx, [str(p)])
    if blocked:
        return blocked
    preview = f"编辑 {_disp(p)}：替换 1 处\n" + panels.render_diff(old, new, p.name)
    ok, msg = _gate(ctx, "edit_file", preview, detail={"path": str(p)})
    if not ok:
        return msg
    try:
        p.write_text(text.replace(old, new, 1), encoding="utf-8")
        # scope=fragment：这里的 diff 只是被替换的那一段，不是整文件。
        # 行号是片段内的相对行号，界面上要标明，否则会被当成文件行号去对。
        _record_change(ctx, p, "edit", old, new, scope="fragment")
        return f"已编辑 {p}（替换 1 处）"
    except Exception as e:  # noqa: BLE001
        return f"编辑失败：{e}"


def _make_preexec(timeout: int):
    """POSIX 资源限额（在子进程 fork 后、exec 前生效）：内存(地址空间)、CPU 时间、
    单文件大小、禁 core dump。Windows / 无 resource 模块时返回 None（优雅降级）。"""
    if os.name == "nt":
        return None
    try:
        import resource
    except ImportError:
        return None
    mem_mb = int(config.get_setting("exec_memory_limit_mb", 2048))
    fsize_mb = int(config.get_setting("exec_file_limit_mb", 512))
    cpu_s = max(1, int(timeout)) + 5

    def _apply():
        limits = [(resource.RLIMIT_CPU, cpu_s, cpu_s + 5), (resource.RLIMIT_CORE, 0, 0)]
        if mem_mb > 0:
            b = mem_mb * 1024 * 1024
            limits.append((resource.RLIMIT_AS, b, b))  # 内存上限：Linux 强制；macOS 忽略 RLIMIT_AS
        if fsize_mb > 0:
            b = fsize_mb * 1024 * 1024
            limits.append((resource.RLIMIT_FSIZE, b, b))
        for res, soft, hard in limits:
            try:
                resource.setrlimit(res, (soft, hard))
            except (ValueError, OSError):
                pass

    return _apply


_SPILL_SEQ = [0]


def _spill_output(ctx, out: str) -> str:
    """超长命令输出全量落盘（先脱敏），返回路径；失败返回空串。
    按会话分目录存 ~/.ivyea/outputs/<session>/，暂无自动清理（体量小，后续可入 doctor）。"""
    try:
        import time as _t
        sess = getattr(ctx, "session_id", "") or "nosession"
        d = config.IVYEA_DIR / "outputs" / sess
        d.mkdir(parents=True, exist_ok=True)
        _SPILL_SEQ[0] += 1
        p = d / f"{_t.strftime('%Y%m%d-%H%M%S')}-{_SPILL_SEQ[0]:03d}.txt"
        p.write_text(security.redact_text(out), encoding="utf-8")
        return str(p)
    except Exception:  # noqa: BLE001
        return ""


# 扫描命令产物时跳过的目录。不跳的话，一次 `npm install` 就能把事件流灌爆，
# 而那几万个文件里没有一个是用户要的"任务产物"。
_SCAN_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", ".next",
    "target", ".cache", ".idea", ".vscode", "site-packages", ".tox", "coverage",
}

# 扫描的硬上限。工作区可能是个几十万文件的仓库，扫穿它比命令本身还慢。
_SCAN_MAX_FILES = 4000
_SCAN_MAX_DEPTH = 5


def _scan_command_outputs(ctx, workdir: str, since: float) -> None:
    """把命令**写出来的文件**记成文件变更。

    存在的理由：``file_change`` 事件原本只有 write_file / edit_file 会发。可是真实
    任务里，报表和图表大多是 `run_python` 跑脚本、`run_command` 跑命令写出来的 ——
    那些产物一条都没被记上，界面上的「文件」那格于是永远是空的，功能等于不存在。

    判据是 mtime 晚于命令开始的时间。没有 diff（拿不到改之前的内容），所以 action
    记 ``write``：不知道是新建还是覆盖就别猜，UI 上照实说"写出"。
    """
    changes = getattr(ctx, "file_changes", None)
    if changes is None or len(changes) >= _MAX_FILE_CHANGES:
        return
    root = Path(workdir)
    if not root.is_dir():
        return
    seen = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            rel_depth = len(Path(dirpath).relative_to(root).parts)
            if rel_depth >= _SCAN_MAX_DEPTH:
                dirnames[:] = []
            # 原地改 dirnames 才能真的不下钻（os.walk 的约定）
            dirnames[:] = [d for d in dirnames
                           if d not in _SCAN_SKIP_DIRS and not d.startswith(".")]
            for fname in filenames:
                seen += 1
                if seen > _SCAN_MAX_FILES:
                    return
                fpath = Path(dirpath) / fname
                try:
                    if fpath.stat().st_mtime < since:
                        continue
                except OSError:
                    continue
                changes.append({"path": str(fpath), "action": "write",
                                "scope": "file", "diff": ""})
                if len(changes) >= _MAX_FILE_CHANGES:
                    return
    except Exception:  # noqa: BLE001 — 扫描失败绝不能影响命令本身的结果
        return


def _run(cmd, args, ctx, kind: str, preview: str, *, auto_ok: bool = False,
         detail: dict | None = None) -> str:
    if not auto_ok:   # 只读命令(auto_ok)免审批，也可在计划模式下跑（本就只读）
        ok, msg = _gate(ctx, kind, preview, detail=detail)
        if not ok:
            return msg
    workdir = getattr(ctx, "workspace", "") or os.getcwd()
    timeout = int(args.get("timeout") or _EXEC_TIMEOUT)
    # 减 1 秒：文件系统的 mtime 粒度和这里取的时间可能差一点，卡太紧会漏掉
    # 命令一开始就写出来的那个文件。宁可多扫一个也别漏。
    started = time.time() - 1.0
    try:
        proc = subprocess.run(cmd, cwd=workdir, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace",
                              preexec_fn=_make_preexec(timeout))
    except subprocess.TimeoutExpired:
        return f"超时（>{timeout}s）已终止。"
    except Exception as e:  # noqa: BLE001
        return f"执行失败：{e}"
    _scan_command_outputs(ctx, workdir, started)
    out = proc.stdout or ""
    head = f"[退出码 {proc.returncode}]\n"
    if not out:
        return head + "（无输出）"
    body = _truncate(out)
    if len(out) > _MAX_OUT:   # 被截断：全量落盘，模型可 read_file 续读剩余部分
        saved = _spill_output(ctx, out)
        if saved:
            body += f"\n（完整输出已保存：{saved}，需要剩余部分时用 read_file 带 offset/limit 读该文件）"
    return head + body


# ── 后台/长任务 bash：非阻塞 Popen + 输出轮询（对标 Claude Code 的后台 bash）──
_BG_PROCS: dict = {}   # bash_id -> {proc, logpath, cmd, read_pos, started}
_BG_SEQ = [0]


def _run_background(cmd, args, ctx, preview: str, *, auto_ok: bool,
                    detail: dict | None = None) -> str:
    """在后台起进程，立即返回 bash_id；输出写临时日志，供 bash_output 轮询。"""
    if not auto_ok:
        ok, msg = _gate(ctx, "run_command", preview, detail=detail)
        if not ok:
            return msg
    workdir = getattr(ctx, "workspace", "") or os.getcwd()
    import tempfile
    _BG_SEQ[0] += 1
    bash_id = f"bg-{_BG_SEQ[0]}"
    logf = tempfile.NamedTemporaryFile(prefix=f"ivyea-{bash_id}-", suffix=".log", delete=False)
    try:
        proc = subprocess.Popen(cmd, cwd=workdir, stdout=logf, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                preexec_fn=_make_preexec(0) if os.name != "nt" else None)
    except Exception as e:  # noqa: BLE001
        logf.close()
        return f"后台启动失败：{e}"
    logf.close()
    _BG_PROCS[bash_id] = {"proc": proc, "logpath": logf.name,
                          "cmd": (args.get("command") or "")[:200], "read_pos": 0}
    return (f"已在后台启动：bash_id={bash_id}（pid {proc.pid}）。"
            f"用 bash_output(bash_id=\"{bash_id}\") 查看输出/状态，kill_bash 终止。")


def t_bash_output(args: dict, ctx) -> str:
    """读取某后台 bash 自上次以来的新增输出 + 运行状态（只读，自动放行）。"""
    bash_id = str(args.get("bash_id") or "").strip()
    rec = _BG_PROCS.get(bash_id)
    if not rec:
        running = ", ".join(k for k, v in _BG_PROCS.items() if v["proc"].poll() is None)
        return f"没有该后台任务：{bash_id}。" + (f"运行中的：{running}" if running else "当前无运行中的后台任务。")
    proc = rec["proc"]
    try:
        with open(rec["logpath"], "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(rec["read_pos"])
            chunk = fh.read()
            rec["read_pos"] = fh.tell()
    except Exception as e:  # noqa: BLE001
        chunk = f"(读日志失败：{e})"
    code = proc.poll()
    status = "运行中" if code is None else f"已结束（退出码 {code}）"
    body = _truncate(chunk) if chunk else "（无新增输出）"
    return f"[{bash_id} · {status}]\n{body}"


def t_kill_bash(args: dict, ctx) -> str:
    """终止某后台 bash（写操作，需审批）。"""
    bash_id = str(args.get("bash_id") or "").strip()
    rec = _BG_PROCS.get(bash_id)
    if not rec:
        return f"没有该后台任务：{bash_id}。"
    ok, msg = _gate(ctx, "run_command", f"终止后台任务 {bash_id}：{rec['cmd']}")
    if not ok:
        return msg
    proc = rec["proc"]
    if proc.poll() is not None:
        return f"{bash_id} 已经结束（退出码 {proc.poll()}）。"
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception as e:  # noqa: BLE001
        return f"终止失败：{e}"
    return f"已终止 {bash_id}。"


def t_run_python(args: dict, ctx) -> str:
    import sys
    code = args.get("code", "")
    if not code:
        return "code 为空。"
    return _run([sys.executable, "-c", code], args, ctx, "run_python",
                "运行 Python：\n" + _truncate(code, 600))


def t_run_command(args: dict, ctx) -> str:
    command = args.get("command", "")
    if not command:
        return "command 为空。"
    ok, msg = policy.check_command(command)
    if not ok:
        return msg
    blocked = _dangerous_command(command)
    if blocked:
        return f"安全策略拒绝高风险命令：{blocked}"
    import os as _os
    shell = ["cmd", "/c", command] if _os.name == "nt" else ["bash", "-lc", command]
    auto_ok = policy.is_readonly_command(command)   # 只读命令自动放行，省去逐次审批
    preview = "运行命令：" + _truncate(command, 400)
    if args.get("run_in_background"):   # 长任务(dev server/watch/构建)：后台非阻塞，返回 bash_id
        return _run_background(shell, args, ctx, preview, auto_ok=auto_ok, detail={"command": command})
    return _run(shell, args, ctx, "run_command", preview, auto_ok=auto_ok, detail={"command": command})


def t_todo_write(args: dict, ctx) -> str:
    """更新任务计划（供长任务可视化）。todos:[{content,status}]。"""
    todos = args.get("todos") or []
    clean = []
    for t in todos:
        if isinstance(t, dict) and t.get("content"):
            st = t.get("status", "pending")
            clean.append({"content": str(t["content"]),
                          "status": st if st in progress_reporting.TODO_STATUSES else "pending"})
    if len(clean) > 1:
        ctx.progress_required = True
    problem = progress_reporting.validate_todo_update(ctx, clean)
    if problem:
        return "⚠ Todo 更新已拒绝：" + problem
    if clean != list(getattr(ctx, "todos", []) or []):
        ctx.progress_final = {}
    ctx.todos = clean
    # 计划同时落台账：ctx.todos 只是缓存，掉电即失，而计划要能扛住压缩、重启和续跑。
    # 没有 session_id（只读子 agent、裸 ToolContext）时 plan_store 整个空转，行为不变。
    try:
        from . import plan_store
        plan_store.sync_todos(
            getattr(ctx, "session_id", "") or "", clean,
            task_id=getattr(ctx, "task_id", "") or "",
            query=getattr(ctx, "progress_query", "") or "",
            plan_mode=bool(getattr(ctx, "plan_mode", False)),
        )
    except Exception:  # noqa: BLE001 —— 台账写不进去不该让计划更新失败
        pass
    if not clean:
        return "计划已清空。"
    done = sum(1 for t in clean if t["status"] == "completed")
    running = sum(1 for t in clean if t["status"] == "in_progress")
    msg = f"已更新计划：{done}/{len(clean)} 完成。"
    if running > 1:   # 纪律提醒：同一时间应恰好一个进行中
        msg += f"（注意：有 {running} 个 in_progress，建议同一时间只保留一个进行中）"
    elif running == 0 and done < len(clean):
        msg += "（还有未完成步骤，记得把下一步标 in_progress）"
    return msg


def t_progress_update(args: dict, ctx) -> str:
    """记录并展示复杂任务的开始、阶段和最终汇报。"""
    ctx.progress_last_event = {}
    result = progress_reporting.apply_update(args, ctx)
    event = result.get("event") or {}
    if event:
        ctx.progress_last_event = event
    return str(result.get("text") or "")


def _task_id(args: dict, ctx) -> str:
    return str(args.get("task_id") or getattr(ctx, "task_id", "") or "").strip()


def _plan_owns_steps_hint(ctx) -> str:
    """会话已有计划时，提醒模型步骤的真相在计划台账那边。

    ADR-0026 之后任务文件的 `steps` 是计划的**投影**：下一次 `todo_write` 会把整张表
    按计划重写一遍，`task_step` 单独改的状态到那时就没了。这里只出一句提示、不拦下
    调用 —— 没绑会话（纯 CLI `ivyea task step`）的老用法必须一字不差地照旧能用。
    """
    try:
        from . import plan_store
        plan = plan_store.load(getattr(ctx, "session_id", "") or "")
    except Exception:  # noqa: BLE001
        return ""
    if not plan or not (plan.get("steps") or []):
        return ""
    return ("\n\n提示：本会话的步骤真相是计划台账，任务文件里的 steps 是它的投影。"
            "请用 todo_write 推进步骤 —— 下一次 todo_write 会按计划重写这张表，"
            "这次 task_step 的改动到那时会被覆盖。")


def t_task_read(args: dict, ctx) -> str:
    task_id = _task_id(args, ctx)
    if not task_id:
        return "未绑定 task_id。请用 ivyea chat --task-id <id>，或在工具参数传 task_id。"
    try:
        from . import task_runner
        return _truncate(task_runner.render(task_runner.load(task_id)))
    except Exception as e:  # noqa: BLE001
        return f"读取任务失败：{e}"


def t_task_step(args: dict, ctx) -> str:
    task_id = _task_id(args, ctx)
    if not task_id:
        return "未绑定 task_id，无法更新任务步骤。"
    try:
        from . import task_runner
        task = task_runner.update_step(
            task_id,
            int(args.get("index") or 1),
            str(args.get("status") or ""),
            note=str(args.get("notes") or args.get("note") or ""),
        )
        return _truncate(task_runner.render(task) + _plan_owns_steps_hint(ctx))
    except Exception as e:  # noqa: BLE001
        return f"更新任务步骤失败：{e}"


def t_task_log(args: dict, ctx) -> str:
    task_id = _task_id(args, ctx)
    if not task_id:
        return "未绑定 task_id，无法写入任务日志。"
    try:
        from . import task_runner
        task = task_runner.append_log(
            task_id,
            str(args.get("text") or args.get("notes") or ""),
            kind=str(args.get("kind") or "agent"),
        )
        return _truncate(task_runner.render(task))
    except Exception as e:  # noqa: BLE001
        return f"写入任务日志失败：{e}"


def t_task_resume(args: dict, ctx) -> str:
    task_id = _task_id(args, ctx)
    if not task_id:
        return "未绑定 task_id，无法读取续跑提示。"
    try:
        from . import task_runner
        data = task_runner.resume_payload(task_id)
        resume = data.get("resume") or {}
        return _truncate(str(resume.get("prompt") or task_runner.render_resume(data["task"])))
    except Exception as e:  # noqa: BLE001
        return f"读取续跑提示失败：{e}"


def t_self_critique(args: dict, ctx) -> str:
    """收尾前对自己的草稿答案做一次 rubric 自查（只读，不写）。用当前主脑复核。"""
    from . import critique as _crit
    draft = (args.get("draft") or "").strip()
    if not draft:
        return "draft 为空：把你准备交付的最终答案放进 draft 再自查。"
    provider = getattr(ctx, "provider", None)
    res = _crit.critique(args.get("task") or "", draft, provider, kind=str(args.get("kind") or ""))
    if not res.get("ok"):
        return res.get("note") or "自我批判不可用。"
    return _truncate(res["markdown"] or "未见明显问题。")


def t_skill_view(args: dict, ctx) -> str:
    """读技能全文，或读技能目录下的附属文件（只读，自动放行）。

    这个工具补的是一个硬伤：自动注入只给正文开头一段，而剩下的部分此前**根本够不着**。
    """
    from . import skills as _skills
    skill_id = str(args.get("skill_id") or "").strip()
    if not skill_id:
        return "skill_id 为空。先用 skill_search 找到技能 id。"
    sk = _skills.get_skill(skill_id)
    if sk is None:
        near = _skills.render_search(skill_id, limit=5)
        return f"未找到 skill：{skill_id}\n相近的：\n{near}"
    rel = str(args.get("file_path") or "").strip()
    try:
        from . import skill_usage
        skill_usage.record(sk.id, query=rel or "全文", source="view")
    except Exception:  # noqa: BLE001
        pass
    if rel:
        try:
            return _truncate(_skills.read_asset(sk, rel))
        except (ValueError, FileNotFoundError, OSError) as e:
            assets = _skills.list_assets(sk)
            listing = "、".join(assets[:20]) if assets else "（这个技能没有附属文件）"
            return f"{e}\n可读的附属文件：{listing}"
    text = _skills.render_skill(sk, include_knowledge=True)
    assets = _skills.list_assets(sk)
    if assets:
        text += ("\n\n附属文件（用 file_path 参数按需读）：\n"
                 + "\n".join(f"- {a}" for a in assets[:30]))
    return _truncate(text, 24000)


def _skill_write_meta(args: dict) -> dict:
    return {
        "name": str(args.get("name") or "").strip(),
        "description": str(args.get("description") or "").strip(),
        "triggers": [str(t).strip() for t in (args.get("triggers") or []) if str(t).strip()],
        "tools": [str(t).strip() for t in (args.get("tools") or []) if str(t).strip()],
        "knowledge_ids": [str(k).strip() for k in (args.get("knowledge_ids") or []) if str(k).strip()],
        "version": str(args.get("version") or "").strip() or "0.1.0",
    }


def t_skill_write(args: dict, ctx) -> str:
    """新建/改写技能、写附属文件、归档（写操作，走审批）。

    落盘前先过 `skill_authoring.validate`：**校验不过直接拒绝**，不写半成品。
    规范写成代码而不是写进 prompt —— 只要不可执行，规范就一定会慢慢烂掉。
    """
    from . import skill_authoring, skills as _skills
    action = str(args.get("action") or "write").strip().lower()
    skill_id = str(args.get("skill_id") or "").strip()
    if not skill_id:
        return "skill_id 为空。用 domain.name 的形式，如 lingxing.ad_patrol。"

    if action == "archive":
        preview = f"归档技能 {skill_id}（移进 _archive/，可恢复，不删除）"
        ok, msg = _gate(ctx, "skill_write", preview, detail={"skill_id": skill_id})
        if not ok:
            return msg
        try:
            dest = _skills.archive_skill(skill_id)
        except (FileNotFoundError, ValueError, OSError) as e:
            return f"归档失败：{e}"
        return f"已归档 {skill_id} → {dest}（`ivyea skill restore {dest.name}` 可恢复）"

    if action == "write_file":
        rel = str(args.get("file_path") or "").strip()
        content = args.get("content") or ""
        if not rel:
            return "file_path 为空：附属文件要给相对路径，如 references/ch01.md 或 scripts/run.py。"
        preview = f"写技能附属文件 {skill_id}/{rel}（{len(content)} 字）"
        ok, msg = _gate(ctx, "skill_write", preview, detail={"skill_id": skill_id, "path": rel})
        if not ok:
            return msg
        try:
            path = _skills.write_skill_asset(skill_id, rel, content)
        except (ValueError, OSError) as e:
            return f"写入失败：{e}"
        return f"已写入 {path}"

    # write / create：正文 + 元信息，落 SKILL.md
    meta = _skill_write_meta(args)
    if not meta["name"]:
        meta["name"] = skill_id.rpartition(".")[2] or skill_id
    body = args.get("body") or ""
    result = skill_authoring.validate(meta, body)
    report = skill_authoring.render_report(result)
    if not result["ok"]:
        return ("技能未写入 —— 校验没过：\n" + report +
                "\n\n改完再调一次 skill_write。这些是硬规则，不是建议：不满足的技能"
                "要么检索不到、要么读的人会被带偏。")
    exists = _skills.get_skill(skill_id) is not None
    preview = (f"{'改写' if exists else '新建'}技能 {skill_id}：{meta['description']}\n"
               f"触发词：{'、'.join(meta['triggers'])}\n正文 {len(body)} 字")
    if report:
        preview += "\n校验提醒：\n" + report
    ok, msg = _gate(ctx, "skill_write", preview, detail={"skill_id": skill_id})
    if not ok:
        return msg
    try:
        path = _skills.write_user_skill(skill_id, meta, body)
    except (ValueError, FileExistsError, OSError) as e:
        return f"写入失败：{e}"
    out = f"已{'改写' if exists else '新建'}技能 {skill_id} → {path}"
    if report:
        out += "\n（还有几点建议，不影响使用）\n" + report
    return out


# ── schema + dispatch ────────────────────────────────────────────────────────
def _fn(name, desc, props, required=()):
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props, "required": list(required)}}}


GENERAL_TOOL_SCHEMAS = [
    _fn("read_file", "读取本地文本文件内容（只读，自动放行）。返回带行号（行号\\t内容，仅供定位/引用，"
        "edit_file 的 old 用真实内容不要带行号）。大文件用 offset/limit 读行区间，别用 run_command 分段读。",
        {"path": {"type": "string", "description": "文件路径，支持 ~"},
         "offset": {"type": "integer", "description": "起始行号（从 1 开始）；读大文件某段时填"},
         "limit": {"type": "integer", "description": "最多读多少行；配合 offset 读区间"}}, ["path"]),
    _fn("list_dir", "列出目录内容（只读）。",
        {"path": {"type": "string", "description": "目录路径，默认当前目录"}}),
    _fn("write_file", "新建或整体重写文件（写操作，一次调用即审批落盘）。改已有文件的某一处别用它，用 edit_file。",
        {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
    _fn("edit_file", "改单个文件的某一处：唯一字符串替换 old→new（写操作，一次调用即审批落盘）。"
        "old 用文件真实内容（不要带 read_file 的行号前缀）且必须唯一出现。"
        "单处改动首选它；跨多文件/多处或要顺带跑测试用 code_apply_patch。",
        {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
        ["path", "old", "new"]),
    _fn("run_python", "在沙箱(限工作目录/超时)运行 Python 代码取 stdout（执行，会弹审批）。可用 pandas/openpyxl 等。",
        {"code": {"type": "string"}, "timeout": {"type": "integer", "description": "秒，默认30"}}, ["code"]),
    _fn("run_command", "在沙箱运行 shell 命令取输出（执行，会弹审批）。长任务（dev server/watch/"
        "长构建等不会很快结束的命令）设 run_in_background=true 后台运行，立即返回 bash_id，再用 "
        "bash_output 轮询输出，别在前台阻塞等待。",
        {"command": {"type": "string"}, "timeout": {"type": "integer"},
         "run_in_background": {"type": "boolean", "description": "true=后台非阻塞运行，返回 bash_id"}},
        ["command"]),
    _fn("bash_output", "读取某后台 bash 自上次以来的新增输出与运行状态（只读，自动放行）。配合 "
        "run_command(run_in_background=true) 轮询长任务进展。",
        {"bash_id": {"type": "string", "description": "run_command 后台返回的 bash_id"}}, ["bash_id"]),
    _fn("kill_bash", "终止某个后台 bash 任务（写操作，会弹审批）。",
        {"bash_id": {"type": "string"}}, ["bash_id"]),
    _fn("web_fetch", "抓取一个 URL 的文本内容（GET，只读，自动放行）。",
        {"url": {"type": "string"}}, ["url"]),
    _fn("web_search", "网页搜索关键词（尽力而为，无 key）。",
        {"query": {"type": "string"}}, ["query"]),
    _fn("web_images", "给回答配图：搜一批相关网页，把它们各自声明的配图（og:image）取回来，"
        "返回可直接写进正文的 markdown。只读，自动放行，无需任何 key。"
        "**什么时候用**：回答里出现具体的公司、产品、地点、实物、界面、人物时，"
        "一张真实的图比三段描述有用 —— 先调它拿图，再把返回的 `![说明](地址)` 原样写进正文。"
        "抽象概念、纯数字结论、代码问题不要配图。作图请用 image_generate，这个工具只负责**找**已有的图。",
        {"query": {"type": "string", "description": "配图主题，用具体名字，如「51WORLD 五一视界 数字孪生」"},
         "limit": {"type": "integer", "description": "要几张，默认 4，最多 8"}}, ["query"]),
    _fn("grep", "在代码库里做内容正则搜索（ripgrep 风格），返回 file:line 命中行。只读，自动放行。找代码先用它，别瞎猜路径。",
        {"pattern": {"type": "string", "description": "正则表达式"},
         "glob": {"type": "string", "description": "可选：只搜匹配此 glob 的文件，如 *.py"},
         "max_results": {"type": "integer", "description": "最多命中条数，默认 80"}}, ["pattern"]),
    _fn("glob", "按文件名 glob 模式找文件（如 **/*.py、src/**/*.ts）。只读，自动放行。"
        "想按文件名/路径定位文件用它；想按内容搜用 grep。",
        {"pattern": {"type": "string", "description": "glob 模式，如 **/*.py"},
         "path": {"type": "string", "description": "起始目录，默认工作目录"},
         "max_results": {"type": "integer", "description": "最多返回条数，默认 100"}}, ["pattern"]),
    _fn("code_search", "按符号/路径/预览检索代码库里最相关的文件（索引搜索）。只读。适合“这功能在哪实现的”。",
        {"query": {"type": "string"}, "limit": {"type": "integer"}}, ["query"]),
    _fn("code_symbols", "列出/搜索代码库里的函数/类等符号定义位置。只读。",
        {"query": {"type": "string", "description": "可选，过滤符号名"}, "limit": {"type": "integer"}}),
    _fn("code_impact", "查某个符号或文件的调用方、导入方与受影响测试（改动影响面）。只读。改代码前先看它。",
        {"target": {"type": "string", "description": "符号名或文件路径"}, "limit": {"type": "integer"}}, ["target"]),
    _fn("code_apply_patch", "一次性应用一组结构化补丁并跑测试（跨多文件/多处关联改动，或要顺带跑测试时用它）。"
        "内部先校验→给彩色 diff 预览→一次人工审批→落盘+跑测试，不需要分 dry-run/execute 两步。"
        "ops=[{path,old,new}]，old 必须在文件中唯一出现。单文件单处改动用 edit_file 即可。",
        {"ops": {"type": "array", "items": {"type": "object", "properties": {
            "path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}}}},
         "test_command": {"type": "string", "description": "可选：落盘后跑的测试命令，默认自动挑选"}}, ["ops"]),
    _fn("run_tests", "在工作目录跑测试命令并返回结果（执行，会弹审批）。默认 python -m pytest。",
        {"command": {"type": "string"}, "timeout": {"type": "integer"}}),
    _fn("code_repair", "解析失败测试输出，生成下一轮修复计划（可疑文件/失败摘要/重跑命令）。只读。",
        {"test_output": {"type": "string"}}, ["test_output"]),
    _fn("mcp_list_tools", "列出已配置 MCP 服务器的可用工具（只读）。server 省略=全部。先用它发现工具，再 mcp_call_tool。",
        {"server": {"type": "string", "description": "MCP 服务器名（mcp.json 配置）；省略=全部"}}),
    _fn("mcp_call_tool", "调用某个 MCP 服务器的工具。会弹人工审批（mcp.json 标 trusted 的服务器免审）；计划模式下拒绝。",
        {"server": {"type": "string"}, "tool": {"type": "string"},
         "arguments": {"type": "object", "description": "传给工具的 JSON 参数"}}, ["server", "tool"]),
    _fn("mcp_list_resources", "列出 MCP 服务器的资源（只读）。server 省略=全部。",
        {"server": {"type": "string"}}),
    _fn("mcp_read_resource", "读取 MCP 服务器某个资源的内容（只读）。",
        {"server": {"type": "string"}, "uri": {"type": "string"}}, ["server", "uri"]),
    _fn("mcp_list_prompts", "列出 MCP 服务器提供的 prompt 模板（只读）。server 省略=全部。",
        {"server": {"type": "string"}}),
    _fn("mcp_get_prompt", "获取 MCP 服务器某个 prompt 模板的内容（只读）。",
        {"server": {"type": "string"}, "name": {"type": "string"},
         "arguments": {"type": "object", "description": "模板参数（可选）"}}, ["server", "name"]),
    _fn("todo_write", "维护多步任务计划(让长任务可视化)。每步 {content, status: pending|in_progress|completed|blocked|skipped}。多步任务动手前先列计划；执行时同一时间恰好一个 in_progress。阶段变为 completed/blocked/skipped 前必须先用 progress_update(phase_end) 汇报结果和证据。单步小任务不必用。",
        {"todos": {"type": "array", "items": {"type": "object", "properties": {
            "content": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "blocked", "skipped"]}}}}},
        ["todos"]),
    _fn("progress_update", "复杂/多步任务的结构化汇报工具。执行前：先 todo_write，再 kind=start，说明目标、范围、完成标准并同时开始第一个阶段。阶段切换：kind=phase_end 汇报做了什么、状态、证据、未完成和注意事项，再更新 Todo；新阶段用 kind=phase_start 介绍准备做什么。全部执行结束后必须 kind=final，系统会把真实 Todo、阶段报告和工具证据合并为最终汇总。简单单步任务不必调用。",
        {
            "kind": {"type": "string", "enum": ["start", "phase_start", "phase_end", "final"]},
            "phase_index": {"type": "integer", "description": "对应 Todo 的阶段序号，从 1 开始；默认当前 in_progress"},
            "status": {"type": "string", "enum": ["completed", "partial", "blocked", "skipped"],
                       "description": "仅 phase_end 使用"},
            "summary": {"type": "string", "description": "start=整体目标；phase_start=本阶段准备做什么；phase_end=本阶段做了什么；final=整体结果"},
            "scope": {"type": "array", "items": {"type": "string"}, "description": "start 可选：任务范围；项目根会自动加入"},
            "success_criteria": {"type": "array", "items": {"type": "string"}, "description": "start 必填：可验证的完成标准"},
            "completed": {"type": "array", "items": {"type": "string"}, "description": "本阶段或整体已做到的事项"},
            "incomplete": {"type": "array", "items": {"type": "string"}, "description": "未做到或部分完成事项"},
            "evidence": {"type": "array", "items": {"type": "string"}, "description": "测试、命令、文件或数据证据"},
            "attention": {"type": "array", "items": {"type": "string"}, "description": "风险、阻塞原因和注意事项"},
            "next": {"type": "string", "description": "下一阶段或下一步"},
        }, ["kind", "summary"]),
    _fn("self_critique", "收尾前自查：把你准备交付的最终答案放进 draft，用当前主脑按 rubric 复核"
        "(需求吻合/事实可靠/关键遗漏/验证到位)，返回简短批判。高风险或复杂任务交付前建议先自调一次。只读。",
        {"draft": {"type": "string", "description": "准备交付给用户的最终答案全文"},
         "task": {"type": "string", "description": "可选：本次任务/需求，帮助判断是否答非所问"},
         "kind": {"type": "string", "enum": ["code", "ads", "knowledge", "general"],
                  "description": "可选：交付类型，决定用哪套复核维度；不填按通用"}},
        ["draft"]),
    _fn("skill_view", "读某个技能的**全文**，或读它目录下的附属文件（references/ scripts/ "
        "templates/）。只读，自动放行。自动注入只给正文开头一段——要照着技能真正动手前，"
        "先用它读全文，别凭那一小段就开干。",
        {"skill_id": {"type": "string", "description": "技能 id，先用 skill_search 找"},
         "file_path": {"type": "string",
                       "description": "可选：技能目录下的相对路径，如 references/ch01.md；"
                                      "不填=读技能正文全文"}},
        ["skill_id"]),
    _fn("skill_write", "新建/改写技能、写技能附属文件、归档技能（写操作，会弹审批）。"
        "刚走完一套值得复用的流程时用它沉淀下来。落盘前会校验：name 要小写连字符、"
        "description 必填且是一句话、**triggers 必填**（检索不分词，中文全靠触发词命中）、"
        "正文非空。校验不过会直接拒绝并告诉你差什么。",
        {"action": {"type": "string", "enum": ["write", "write_file", "archive"],
                    "description": "write=写技能正文(SKILL.md)；write_file=写附属文件；archive=归档(可恢复)"},
         "skill_id": {"type": "string", "description": "domain.name 形式，如 lingxing.ad_patrol"},
         "name": {"type": "string", "description": "技能名，小写连字符，如 lingxing-ad-patrol"},
         "description": {"type": "string", "description": "一句话说清它做什么（进检索范围，别写营销词）"},
         "triggers": {"type": "array", "items": {"type": "string"},
                      "description": "3-8 个用户真会打出来的**短词**（2-6 字）"},
         "tools": {"type": "array", "items": {"type": "string"}, "description": "可选：这技能会用到的工具名"},
         "knowledge_ids": {"type": "array", "items": {"type": "string"},
                           "description": "可选：关联的知识卡 id，必须真实存在"},
         "version": {"type": "string", "description": "可选，默认 0.1.0"},
         "body": {"type": "string",
                  "description": "action=write 时的正文 Markdown。建议小节：何时使用/前置条件/"
                                 "怎么跑/速查/步骤/坑/验证"},
         "file_path": {"type": "string", "description": "action=write_file 时的相对路径"},
         "content": {"type": "string", "description": "action=write_file 时的文件内容"}},
        ["action", "skill_id"]),
    _fn("task_read", "读取当前绑定的 Ivyea 长任务状态、步骤和最近事件。续跑任务时应先调用。",
        {"task_id": {"type": "string", "description": "可选；不传则使用当前对话绑定的 task_id"}}),
    _fn("task_step", "更新当前绑定的 Ivyea 长任务步骤状态。用于执行过程中标记 in_progress/completed/blocked。",
        {
            "task_id": {"type": "string", "description": "可选；不传则使用当前对话绑定的 task_id"},
            "index": {"type": "integer", "description": "步骤序号，从 1 开始"},
            "status": {"type": "string", "enum": ["pending", "in_progress", "blocked", "completed", "skipped"]},
            "notes": {"type": "string", "description": "步骤备注"},
        },
        ["index", "status"]),
    _fn("task_log", "向当前绑定的 Ivyea 长任务追加执行日志或结论。",
        {
            "task_id": {"type": "string", "description": "可选；不传则使用当前对话绑定的 task_id"},
            "text": {"type": "string"},
            "kind": {"type": "string", "description": "日志类型，默认 agent"},
        },
        ["text"]),
    _fn("task_resume", "读取当前绑定的 Ivyea 长任务结构化续跑提示。",
        {"task_id": {"type": "string", "description": "可选；不传则使用当前对话绑定的 task_id"}}),
]

GENERAL_DISPATCH = {
    "read_file": t_read_file, "list_dir": t_list_dir,
    "write_file": t_write_file, "edit_file": t_edit_file,
    "run_python": t_run_python, "run_command": t_run_command,
    "bash_output": t_bash_output, "kill_bash": t_kill_bash,
    "web_fetch": t_web_fetch, "web_search": t_web_search, "web_images": t_web_images,
    "grep": t_grep, "glob": t_glob, "code_search": t_code_search,
    "code_symbols": t_code_symbols, "code_impact": t_code_impact,
    "code_apply_patch": t_code_apply_patch, "run_tests": t_run_tests, "code_repair": t_code_repair,
    "mcp_list_tools": t_mcp_list_tools, "mcp_call_tool": t_mcp_call_tool,
    "mcp_list_resources": t_mcp_list_resources, "mcp_read_resource": t_mcp_read_resource,
    "mcp_list_prompts": t_mcp_list_prompts, "mcp_get_prompt": t_mcp_get_prompt,
    "todo_write": t_todo_write,
    "progress_update": t_progress_update,
    "self_critique": t_self_critique,
    "skill_view": t_skill_view,
    "skill_write": t_skill_write,
    "task_read": t_task_read,
    "task_step": t_task_step,
    "task_log": t_task_log,
    "task_resume": t_task_resume,
}
