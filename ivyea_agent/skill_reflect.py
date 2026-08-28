"""会话结束后自问：这一轮有没有值得沉淀成技能的东西。

**默认关。** 这是刻意的，理由不是保守而是具体的：技能会被自动注入进后续对话、
实打实改变模型行为。让一个后台程序自动往里加东西，第一次误判就会污染检索、
把不相关的流程塞进别人的任务里。先让用户用一段时间 `/learn`，看清楚它写出来的东西
是什么水准，再决定要不要放开。

开：`ivyea config set skill_auto_learn true`。

两道闸门，照抄 memory_reflect 已经被验证过的那套
------------------------------------------------
1. **显著性门槛**：这一轮得真干了活（工具步数够、走完多阶段执行、有真实证据），
   才值得花一次模型调用去问"要不要沉淀"。一次问答、一次闲聊没有可复用的流程。
2. **证据门槛**：同一类流程**跨会话出现过 ≥2 次**才建技能。一次性的具体任务不是技能 ——
   `memory_reflect` 那边写着同样的道理：防止把一次性的事固化成"你的长期做法"。
   没到次数的先进待定区（`~/.ivyea/skills/_pending.json`），等它再出现一次。

安全边界
--------
* fork 出去的 agent **只拿 skill_view / skill_write / skill_search**，别的一律没有。
* 它跑在后台线程里，`skill_write` 的审批没有人应答 —— 所以给它 `accept_edits`，
  但**只在这条受限工具链上**：它能写的只有 `~/.ivyea/skills/` 下的技能文件。
* 任何异常都吞掉。沉淀是锦上添花，绝不能让一轮对话因为它失败。
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from . import config, security

#: 这一轮至少要有多少次实质工具调用，才值得问"要不要沉淀"。
MIN_TOOL_STEPS = 8
#: 同一类流程跨会话见过几次才真的建技能。
PROMOTE_AFTER_SIGHTINGS = 2
#: 待定区最多攒多少条，防止无限长。
MAX_PENDING = 50

_RUNNING = False
_RUN_LOCK = threading.Lock()


def enabled() -> bool:
    return bool(config.get_setting("skill_auto_learn", False))


def _pending_file():
    return config.IVYEA_DIR / "skills" / "_pending.json"


def _load_pending() -> dict[str, Any]:
    path = _pending_file()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_pending(data: dict[str, Any]) -> None:
    path = _pending_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        return


def note_sighting(kind: str, *, summary: str = "") -> int:
    """记一次"见到这类流程"。返回累计见到几次。

    `kind` 是流程的粗分类（模型给的一句话标签，归一化后当键）。它不需要多准 ——
    准的是**次数**：同一个标签跨会话出现两次，才说明这是个重复发生的流程。
    """
    key = " ".join(str(kind or "").split()).lower()[:80]
    if not key:
        return 0
    data = _load_pending()
    row = data.get(key) or {"sightings": 0, "summaries": []}
    row["sightings"] = int(row.get("sightings") or 0) + 1
    row["last_at"] = time.time()
    clean = " ".join(security.redact_text(str(summary or "")).split())[:200]
    if clean:
        row["summaries"] = ([s for s in (row.get("summaries") or []) if s != clean] + [clean])[-3:]
    data[key] = row
    if len(data) > MAX_PENDING:      # 丢最久没见到的
        data = dict(sorted(data.items(), key=lambda kv: -float(kv[1].get("last_at") or 0))[:MAX_PENDING])
    _save_pending(data)
    return int(row["sightings"])


def ready_to_promote(kind: str) -> bool:
    key = " ".join(str(kind or "").split()).lower()[:80]
    row = _load_pending().get(key) or {}
    return int(row.get("sightings") or 0) >= PROMOTE_AFTER_SIGHTINGS


def clear_pending(kind: str) -> None:
    key = " ".join(str(kind or "").split()).lower()[:80]
    data = _load_pending()
    if key in data:
        data.pop(key)
        _save_pending(data)


def render_pending() -> str:
    data = _load_pending()
    if not data:
        return "（待定区是空的）"
    rows = []
    for key, row in sorted(data.items(), key=lambda kv: -int(kv[1].get("sightings") or 0)):
        when = time.strftime("%Y-%m-%d", time.localtime(row.get("last_at") or 0))
        mark = "→ 可沉淀" if int(row.get("sightings") or 0) >= PROMOTE_AFTER_SIGHTINGS else ""
        rows.append(f"{key:<50} 见过 {row.get('sightings')} 次  最近 {when}  {mark}")
    return "\n".join(rows)


def should_reflect(*, tool_steps: int, had_phases: bool, had_evidence: bool) -> bool:
    """显著性门槛：这一轮够不够格被问一句"要不要沉淀"。"""
    if not enabled():
        return False
    return bool(tool_steps >= MIN_TOOL_STEPS and had_phases and had_evidence)


_PROMPT = """回顾刚才这次任务，判断有没有**值得沉淀成可复用技能**的流程。

判据只有一条：**下次遇到同类问题，会不会还这么干？**
- 会 → 值得沉淀。
- 只是这一次的具体任务（某个 ASIN、某个文件、某次排查）→ **不要**沉淀。
- 已经有技能覆盖了 → 用 `skill_view` 看看，值得补充就 `skill_write` 扩写它，不要新建近似的第二条。

先用 `skill_search` 查库里有没有。确实值得且没有覆盖时，用 `skill_write` 写一条：
name 小写连字符、description 一句话、**triggers 必填**（3-8 个用户真会打出来的短词）、
正文写「何时使用 / 前置条件 / 怎么跑 / 步骤 / 坑 / 验证」。
命令和参数必须来自刚才**真实发生过**的操作，一个都不许编。

如果判断不值得沉淀，就直接回一句「不值得沉淀：<原因>」，**不要**为了有产出硬写一条。"""


def build_prompt(transcript_digest: str) -> str:
    return f"【刚才这次任务的经过】\n{transcript_digest}\n\n{_PROMPT}"


def maybe_reflect_async(transcript_digest: str, *, provider_factory=None,
                        tool_steps: int = 0, had_phases: bool = False,
                        had_evidence: bool = False) -> bool:
    """够门槛就在后台 fork 一个受限 agent 去沉淀技能。返回是否真起了线程。

    互斥与降级和 `memory_reflect.maybe_reflect_async` 同款：同进程内不起第二个线程，
    任何异常一律吞掉。
    """
    global _RUNNING
    try:
        if not should_reflect(tool_steps=tool_steps, had_phases=had_phases,
                              had_evidence=had_evidence):
            return False
        with _RUN_LOCK:
            if _RUNNING:
                return False
            _RUNNING = True

        def _work() -> None:
            global _RUNNING
            try:
                _run(transcript_digest, provider_factory)
            except Exception:      # noqa: BLE001 —— 沉淀失败绝不影响任何东西
                pass
            finally:
                _RUNNING = False

        threading.Thread(target=_work, daemon=True, name="ivyea-skill-reflect").start()
        return True
    except Exception:              # noqa: BLE001
        _RUNNING = False
        return False


#: fork 出去的 agent 能用的工具。**只有这几个** —— 它的活是判断和落盘，不是继续干活。
REFLECT_TOOLS = ("skill_search", "skill_view", "skill_write")


def _run(transcript_digest: str, provider_factory=None) -> str:
    from . import agent_tools, permission
    from .agent_tools import ToolContext

    if provider_factory is None:
        from . import memory_reflect
        provider_factory = memory_reflect._default_provider
    provider = provider_factory()
    if provider is None:
        return ""
    from . import agent_loop

    ctx = ToolContext(workspace=str(config.IVYEA_DIR), provider=provider,
                      perm=permission.PermissionState(),
                      progress_reporting_disabled=True)
    # 后台没有人能应答审批；给它放行，但它能碰的只有 REFLECT_TOOLS 这三个 ——
    # 也就是只能读技能、写技能。
    ctx.perm.accept_edits = True
    tools = [t for t in agent_tools.TOOL_SCHEMAS
             if t["function"]["name"] in REFLECT_TOOLS]
    messages = [{"role": "system", "content": "你是技能沉淀助手。只做一件事：判断刚才那次任务"
                                              "有没有值得复用的流程，值得就写成技能。"},
                {"role": "user", "content": build_prompt(transcript_digest)}]
    return agent_loop.run_turn(provider, ctx, messages, max_steps=12,
                               narrate=lambda _s: None, tools=tools)
