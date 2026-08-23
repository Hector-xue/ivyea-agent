"""飞书交互卡片构建 —— 纯函数，不碰网络。

为什么是纯函数：卡片长什么样、按钮带什么值、脱敏有没有漏，全都能在没有飞书凭据的
情况下用快照测试锁死。等凭据到位只差"发送"那一行。

**卡片 schema 用 1.0**（ADR-4）：``{config, header, elements}``。这是 hermes 生产代码
在用并验证过可发、可通过回调原地替换的版本；Card 2.0 未经本机实测，不采用。

按钮契约（relay 与本模块必须同步改）：
    {"ivyea_action": "approve"|"deny"|"rollback"|"detail", "approval_id": "<id>"}

脱敏纪律：卡片是嵌套 JSON，必须**递归**脱敏。只处理顶层的话，evidence 里的
凭据照样会发到群里。统一走 ``security.redact_obj``。
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from . import security

#: 飞书单条消息的安全长度上限（留出卡片结构的开销）
MAX_CHUNK = 3800

ACTION_APPROVE = "approve"
ACTION_DENY = "deny"
ACTION_ROLLBACK = "rollback"
ACTION_DETAIL = "detail"
#: 批量批准与开写开关不带 approval_id —— 前者由 relay 用「被点的那张卡的 message_id」
#: 定位（发卡前拿不到 message_id，硬塞会是鸡生蛋），后者是全局开关。
ACTION_APPROVE_ALL = "approve_all"
ACTION_APPROVE_ALL_CONFIRM = "approve_all_confirm"
ACTION_OPERATE_ON = "operate_on"

_SEV_TEMPLATE = {"crit": "red", "warn": "orange", "info": "blue"}
#: 跨店合并时要自己排序（单店卡片拿到的已经是 sorted_findings 的结果）
_SEV_RANK_ORDER = {"crit": 0, "warn": 1, "info": 2}
_SEV_ICON = {"crit": "🚨", "warn": "⚠️", "info": "ℹ️"}
_CLASS_LABEL = {"stanch": "止血", "structural": "结构", "advisory": "建议"}


# ── 基础元件 ────────────────────────────────────────────────────────────────
def _md(content: str) -> dict[str, Any]:
    return {"tag": "markdown", "content": str(content or "")}


def _hr() -> dict[str, Any]:
    return {"tag": "hr"}


def _button(label: str, action: str, approval_id: str,
            btn_type: str = "default") -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": btn_type,
        "value": {"ivyea_action": action, "approval_id": str(approval_id)},
    }


def _bare_button(label: str, action: str, btn_type: str = "default",
                 **extra: Any) -> dict[str, Any]:
    """不绑定具体 approval 的按钮（批量批准 / 开写开关）。"""
    value: dict[str, Any] = {"ivyea_action": action}
    value.update(extra)
    return {"tag": "button", "text": {"tag": "plain_text", "content": label},
            "type": btn_type, "value": value}


def _actions(buttons: list[dict[str, Any]]) -> dict[str, Any]:
    return {"tag": "action", "actions": buttons}


def _header(title: str, template: str) -> dict[str, Any]:
    return {"title": {"tag": "plain_text", "content": str(title)}, "template": template}


def _card(header: dict[str, Any], elements: list[dict[str, Any]]) -> dict[str, Any]:
    return security.redact_obj({
        "config": {"wide_screen_mode": True},
        "header": header,
        "elements": elements,
    })


def chunk(text: str, size: int = MAX_CHUNK) -> list[str]:
    """按行切分长文本，尽量不把一行劈成两半。"""
    text = str(text or "")
    if len(text) <= size:
        return [text] if text else []
    out, buf = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > size:          # 单行就超长，只能硬切
            if buf:
                out.append(buf); buf = ""
            out.append(line[:size]); line = line[size:]
        if len(buf) + len(line) > size:
            out.append(buf); buf = line
        else:
            buf += line
    if buf:
        out.append(buf)
    return out


# ── 证据区 ──────────────────────────────────────────────────────────────────
def _evidence_lines(evidence: dict[str, Any], limit: int = 8) -> list[str]:
    """证据必须能看见——用户要能自己核对 agent 有没有瞎说。

    **必须在数据层脱敏**：本函数用全角冒号渲染（中文可读性），而项目的文本脱敏正则
    只认半角 ``[:=]``，先字符串化再脱敏会漏掉 ``api_key：xxx``。
    按 key 匹配的 ``redact_obj`` 不受分隔符影响，所以在这里先过一遍。
    """
    evidence = security.redact_obj(dict(evidence or {}))
    lines = []
    for i, (k, v) in enumerate(evidence.items()):
        if i >= limit:
            lines.append(f"…（另有 {len(evidence) - limit} 项）")
            break
        if isinstance(v, float):
            v = f"{v:,.2f}".rstrip("0").rstrip(".")
        lines.append(f"- {k}：{v}")
    return lines


def _finding_body(finding: Any) -> list[str]:
    lines = [str(getattr(finding, "message", ""))]
    window = str(getattr(finding, "window", "") or "")
    if window:
        lines.append(f"**窗口**：{window}")
    prov = str(getattr(finding, "provenance", "") or "")
    if prov:
        lines.append(f"**数据**：{prov}")
    ev = dict(getattr(finding, "evidence", {}) or {})
    if ev:
        lines.append("**证据**")
        lines.extend(_evidence_lines(ev))
    return lines


# ── 单条 Finding 卡片 ───────────────────────────────────────────────────────
def build_finding_card(finding: Any, approval_id: str = "",
                       *, sid: Any = "", store_name: str = "") -> dict[str, Any]:
    """一条异常 + 建议 + 按钮。

    没有 approval_id（即没有可执行 intent）时**不放批准按钮**——
    放了会让人点完以为处理了，实际什么都没发生。
    """
    sev = str(getattr(finding, "severity", "info"))
    action_class = str(getattr(finding, "action_class", "advisory"))
    icon = _SEV_ICON.get(sev, "ℹ️")
    label = _CLASS_LABEL.get(action_class, "建议")
    who = store_name or (f"sid {sid}" if sid else "")
    title = f"{icon} {getattr(finding, 'code', '')}" + (f" · {who}" if who else "")

    elements: list[dict[str, Any]] = [_md("\n".join(_finding_body(finding)))]

    if approval_id:
        elements.append(_hr())
        if action_class == "structural":
            elements.append(_md("⚠️ **这是不可逆操作**（否词会永久掐掉一条流量入口），"
                                "确认后无法一键恢复原状。"))
        else:
            elements.append(_md(f"✅ 这是**{label}型**操作，可逆；执行后卡片会给出回滚按钮。"))
        elements.append(_actions([
            _button("批准执行", ACTION_APPROVE, approval_id, "primary"),
            _button("忽略", ACTION_DENY, approval_id, "danger"),
        ]))
    else:
        elements.append(_hr())
        elements.append(_md("_本条为告警，无可自动执行的动作。_"))

    return _card(_header(title, _SEV_TEMPLATE.get(sev, "blue")), elements)


# ── 批量告警卡片 ────────────────────────────────────────────────────────────
def build_alert_card(findings: Iterable[Any], *, sid: Any = "", store_name: str = "",
                     layer: str = "", approval_ids: Optional[dict[str, str]] = None,
                     max_items: int = 10) -> dict[str, Any]:
    """一次巡检的多条异常合并成一张卡（§5.3 批量合并，防刷屏）。"""
    findings = list(findings)
    approval_ids = approval_ids or {}
    worst = "info"
    for f in findings:
        sev = str(getattr(f, "severity", "info"))
        if sev == "crit" or (sev == "warn" and worst == "info"):
            worst = sev
    who = store_name or (f"sid {sid}" if sid else "")
    counts: dict[str, int] = {}
    for f in findings:
        s = str(getattr(f, "severity", "info"))
        counts[s] = counts.get(s, 0) + 1
    summary = " · ".join(f"{_SEV_ICON.get(k, '')}{v}"
                         for k, v in sorted(counts.items(),
                                            key=lambda kv: {"crit": 0, "warn": 1}.get(kv[0], 2)))

    title = f"{_SEV_ICON.get(worst, 'ℹ️')} 店铺异常 {len(findings)} 条" + (f" · {who}" if who else "")
    elements: list[dict[str, Any]] = []
    if layer:
        elements.append(_md(f"**巡检层**：{layer}　**概览**：{summary or '—'}"))

    buttons: list[dict[str, Any]] = []
    for i, f in enumerate(findings[:max_items]):
        sev = str(getattr(f, "severity", "info"))
        cls = _CLASS_LABEL.get(str(getattr(f, "action_class", "advisory")), "建议")
        elements.append(_md(f"{_SEV_ICON.get(sev, '')} **[{cls}]** "
                            f"{getattr(f, 'message', '')}"))
        aid = approval_ids.get(str(getattr(f, "target_id", "")) + "|"
                               + str(getattr(f, "code", "")))
        if aid:
            buttons.append(_button(f"批准 {i + 1}", ACTION_APPROVE, aid, "primary"))
    if len(findings) > max_items:
        elements.append(_md(f"_…另有 {len(findings) - max_items} 条，详见完整报告。_"))
    if buttons:
        elements.append(_hr())
        row = buttons[:4]
        if len(approval_ids) > 1:
            row.append(_bare_button(f"全部批准（{len(approval_ids)}）",
                                    ACTION_APPROVE_ALL, "danger"))
        elements.append(_actions(row))             # 飞书单行按钮不宜过多
    if not findings:
        elements.append(_md("本次巡检未发现异常。"))

    return _card(_header(title, _SEV_TEMPLATE.get(worst, "blue")), elements)


# ── 早报卡片 ────────────────────────────────────────────────────────────────
def build_daily_card(*, date: str, store_name: str, metrics_lines: Iterable[str],
                     findings: Iterable[Any] = (), gaps: Iterable[str] = (),
                     approval_ids: Optional[dict[str, str]] = None,
                     report_url: str = "") -> dict[str, Any]:
    findings = list(findings)
    approval_ids = approval_ids or {}
    elements: list[dict[str, Any]] = []

    lines = list(metrics_lines)
    elements.append(_md("\n".join(lines) if lines else "_昨日无数据_"))

    actionable = [f for f in findings if getattr(f, "intent", None)]
    alerts = [f for f in findings if not getattr(f, "intent", None)]

    if alerts:
        elements.append(_hr())
        elements.append(_md(f"**异常 {len(alerts)} 条**\n" + "\n".join(
            f"{_SEV_ICON.get(str(getattr(f, 'severity', 'info')), '')} "
            f"{getattr(f, 'message', '')}" for f in alerts[:6])))

    buttons: list[dict[str, Any]] = []
    if actionable:
        elements.append(_hr())
        body = [f"**待你决定 {len(actionable)} 条**"]
        for i, f in enumerate(actionable[:5], 1):
            cls = _CLASS_LABEL.get(str(getattr(f, "action_class", "advisory")), "建议")
            body.append(f"{i}. [{cls}] {getattr(f, 'message', '')}")
            aid = approval_ids.get(str(getattr(f, "target_id", "")) + "|"
                                   + str(getattr(f, "code", "")))
            if aid:
                buttons.append(_button(f"批准 {i}", ACTION_APPROVE, aid, "primary"))
        elements.append(_md("\n".join(body)))
        if buttons:
            row = buttons[:4]
            if len(buttons) > 1:
                row.append(_bare_button(f"全部批准（{len(buttons)}）",
                                        ACTION_APPROVE_ALL, "danger"))
            elements.append(_actions(row))

    if gaps:
        elements.append(_hr())
        elements.append(_md("**数据缺口**（这些规则本次没跑）\n"
                            + "\n".join(f"- {g}" for g in list(gaps)[:5])))
    if report_url:
        elements.append(_md(f"[查看完整报告]({report_url})"))

    return _card(_header(f"📊 店铺日报 · {date} · {store_name}", "blue"), elements)


# ── 多店铺汇总早报 ──────────────────────────────────────────────────────────
def multi_store_key(finding: Any) -> str:
    """多店场景下定位一条 Finding 的键。**必须带 sid。**

    单店卡片用的是 ``target_id|code``，跨店合并时那个键会撞车：实测同一批货铺
    11 个欧洲站，UK 与 DE 之间有 112 个 MSKU 完全同名，
    ``L4-NDXL-BULA|listing.rating_low`` 在两个店里长得一模一样。
    键一撞，卡片上「批准 3」绑的就可能是另一个国家的那条建议——
    那是会真去改钱的按钮，不能靠运气。
    """
    return (f"{getattr(finding, 'sid', '')}|{getattr(finding, 'target_id', '')}"
            f"|{getattr(finding, 'code', '')}")



def build_multi_store_daily_card(*, date: str, stores: Iterable[dict[str, Any]],
                                 approval_ids: Optional[dict[str, str]] = None,
                                 report_url: str = "",
                                 max_alerts: int = 8,
                                 max_actions: int = 5) -> dict[str, Any]:
    """一张卡装下所有店铺的早报。

    ``stores`` 每项：``{name, sid, metrics_lines, findings, gaps}``。

    为什么不是每店一张卡：11 个店就是每天早上 11 条推送，人会直接把这个群静音，
    然后真出事的那张卡也一起看不见了。汇总成一张之后，**每店一行状态**用于扫读，
    异常与待决定跨店合并按严重度排序——需要动手的东西永远在同一个位置。

    单店时仍走 ``build_daily_card``（指标明细更全），这里只服务多店。
    """
    approval_ids = approval_ids or {}
    rows = list(stores)
    elements: list[dict[str, Any]] = []

    all_findings: list[tuple[str, Any]] = []
    all_gaps: list[str] = []
    lines: list[str] = []
    for r in rows:
        name = str(r.get("name") or f"sid {r.get('sid')}")
        fs = list(r.get("findings") or [])
        gaps = list(r.get("gaps") or [])
        all_findings.extend((name, f) for f in fs)
        all_gaps.extend(f"{name}：{g}" for g in gaps)
        crit = sum(1 for f in fs if str(getattr(f, "severity", "")) == "crit")
        warn = sum(1 for f in fs if str(getattr(f, "severity", "")) == "warn")
        # 状态图标按最坏的一条走：扫一眼就知道今天该先看哪个店
        icon = "🚨" if crit else ("⚠️" if warn else ("📭" if gaps else "✅"))
        bits = []
        if crit:
            bits.append(f"紧急 {crit}")
        if warn:
            bits.append(f"注意 {warn}")
        if gaps:
            bits.append(f"缺口 {len(gaps)}")
        head = str((r.get("metrics_lines") or [""])[0]).replace("**", "")
        lines.append(f"{icon} **{name}**　{'　'.join(bits) if bits else '正常'}"
                     + (f"\n　　{head}" if head else ""))
    elements.append(_md("\n".join(lines) if lines else "_无店铺_"))

    def _rank(item: tuple[str, Any]) -> int:
        return _SEV_RANK_ORDER.get(str(getattr(item[1], "severity", "info")), 9)

    all_findings.sort(key=_rank)
    actionable = [(n, f) for n, f in all_findings if getattr(f, "intent", None)]
    alerts = [(n, f) for n, f in all_findings if not getattr(f, "intent", None)]

    if alerts:
        elements.append(_hr())
        body = [f"**异常 {len(alerts)} 条**"]
        for name, f in alerts[:max_alerts]:
            body.append(f"{_SEV_ICON.get(str(getattr(f, 'severity', 'info')), '')} "
                        f"[{name}] {getattr(f, 'message', '')}")
        if len(alerts) > max_alerts:
            body.append(f"…另有 {len(alerts) - max_alerts} 条")
        elements.append(_md("\n".join(body)))

    buttons: list[dict[str, Any]] = []
    if actionable:
        elements.append(_hr())
        body = [f"**待你决定 {len(actionable)} 条**"]
        for i, (name, f) in enumerate(actionable[:max_actions], 1):
            cls = _CLASS_LABEL.get(str(getattr(f, "action_class", "advisory")), "建议")
            body.append(f"{i}. [{cls}][{name}] {getattr(f, 'message', '')}")
            aid = approval_ids.get(multi_store_key(f))
            if aid:
                buttons.append(_button(f"批准 {i}", ACTION_APPROVE, aid, "primary"))
        if len(actionable) > max_actions:
            body.append(f"…另有 {len(actionable) - max_actions} 条，"
                        f"用 `ivyea approval list` 查看")
        elements.append(_md("\n".join(body)))
        if buttons:
            row = buttons[:4]
            if len(buttons) > 1:
                row.append(_bare_button(f"全部批准（{len(buttons)}）",
                                        ACTION_APPROVE_ALL, "danger"))
            elements.append(_actions(row))

    if all_gaps:
        elements.append(_hr())
        elements.append(_md("**数据缺口**（这些规则本次没跑）\n"
                            + "\n".join(f"- {g}" for g in all_gaps[:6])
                            + (f"\n…另有 {len(all_gaps) - 6} 条" if len(all_gaps) > 6 else "")))
    if report_url:
        elements.append(_md(f"[查看完整报告]({report_url})"))

    worst = min((_rank(i) for i in all_findings), default=9)
    template = {0: "red", 1: "orange"}.get(worst, "blue")
    return _card(_header(f"📊 每日早报 · {date} · {len(rows)} 个店铺", template), elements)


# ── 周报 / 月报 ─────────────────────────────────────────────────────────────
def build_period_card(*, period: str, window: str, stores: Iterable[dict[str, Any]],
                      activity: Optional[dict[str, Any]] = None,
                      report_url: str = "", max_alerts: int = 10) -> dict[str, Any]:
    """周报 / 月报。一张卡，可单店可多店。

    **刻意不带审批按钮。** 同一条建议在日报里已经给过按钮了；周报再给一次，
    同一个目标就会挂着两条 approval，批了其中一条另一条还在 pending，
    容易变成对同一个活动改两次预算。周报的职责是**回顾**：
    这一周花了多少、卖了多少、批了几条、执行了几条、还有什么一直没解决。

    ``stores`` 每项：``{name, sid, metrics_lines, findings, gaps}``。
    """
    rows = list(stores)
    elements: list[dict[str, Any]] = []
    multi = len(rows) > 1

    # 1) 指标：单店给全量明细，多店一店一行
    if multi:
        head_lines = []
        for r in rows:
            name = str(r.get("name") or f"sid {r.get('sid')}")
            fs = list(r.get("findings") or [])
            crit = sum(1 for f in fs if str(getattr(f, "severity", "")) == "crit")
            warn = sum(1 for f in fs if str(getattr(f, "severity", "")) == "warn")
            icon = "🚨" if crit else ("⚠️" if warn else "✅")
            head = str((r.get("metrics_lines") or [""])[0]).replace("**", "")
            head_lines.append(f"{icon} **{name}**" + (f"　{head}" if head else ""))
        elements.append(_md("\n".join(head_lines)))
    else:
        lines = list((rows[0].get("metrics_lines") if rows else []) or [])
        elements.append(_md("\n".join(lines) if lines else "_本期无数据_"))

    # 2) 执行回顾：这一期真正发生了什么改动
    if activity:
        elements.append(_hr())
        a = activity
        elements.append(_md(
            f"**本期动作**　新建议 {a.get('created', 0)} 条　"
            f"已批准 {a.get('approved', 0)}　已执行 {a.get('executed', 0)}　"
            f"已否决 {a.get('denied', 0)}　回滚 {a.get('rolled_back', 0)}\n"
            f"　　失败 {a.get('failed', 0)}　超时未处理 {a.get('expired', 0)}　"
            f"当前待处理 {a.get('pending_now', 0)}"))

    # 3) 本期仍在的问题（按严重度，跨店合并）
    all_findings: list[tuple[str, Any]] = []
    all_gaps: list[str] = []
    for r in rows:
        name = str(r.get("name") or f"sid {r.get('sid')}")
        all_findings.extend((name, f) for f in (r.get("findings") or []))
        all_gaps.extend(f"{name}：{g}" for g in (r.get("gaps") or []))
    all_findings.sort(key=lambda i: _SEV_RANK_ORDER.get(
        str(getattr(i[1], "severity", "info")), 9))

    if all_findings:
        elements.append(_hr())
        body = [f"**仍未解决 {len(all_findings)} 条**"]
        for name, f in all_findings[:max_alerts]:
            prefix = f"[{name}] " if multi else ""
            body.append(f"{_SEV_ICON.get(str(getattr(f, 'severity', 'info')), '')} "
                        f"{prefix}{getattr(f, 'message', '')}")
        if len(all_findings) > max_alerts:
            body.append(f"…另有 {len(all_findings) - max_alerts} 条")
        body.append("\n_要动手的按钮在每天的早报里，这张卡只做回顾。_")
        elements.append(_md("\n".join(body)))

    if all_gaps:
        elements.append(_hr())
        elements.append(_md("**数据缺口**（这些规则本期没跑）\n"
                            + "\n".join(f"- {g}" for g in all_gaps[:5])
                            + (f"\n…另有 {len(all_gaps) - 5} 条" if len(all_gaps) > 5 else "")))
    if report_url:
        elements.append(_md(f"[查看完整报告]({report_url})"))

    icon = "🗓" if period == "月报" else "📈"
    scope = f"{len(rows)} 个店铺" if multi else str(
        (rows[0].get("name") if rows else "") or "")
    return _card(_header(f"{icon} 店铺{period} · {window} · {scope}", "wathet"), elements)


# ── 回调后的原地替换卡片 ────────────────────────────────────────────────────
def build_resolved_card(*, choice: str, operator: str, preview: str = "") -> dict[str, Any]:
    """点完按钮立刻原地替换，防重复点击（抄 hermes 的做法）。"""
    approved = choice == ACTION_APPROVE
    icon = "⏳" if approved else "❌"
    label = "已批准，执行中…" if approved else "已忽略"
    elements = [_md(f"{icon} **{label}**　操作人：{operator or '未知'}")]
    if preview:
        elements.append(_md(f"> {preview}"))
    return _card(_header(f"{icon} {label}", "grey" if not approved else "orange"), elements)


def build_executed_card(*, preview: str, operator: str, audit_id: str = "",
                        approval_id: str = "", detail: str = "") -> dict[str, Any]:
    elements = [_md(f"✅ **已执行**　操作人：{operator or '未知'}")]
    if preview:
        elements.append(_md(f"> {preview}"))
    if detail:
        elements.append(_md(detail))
    if audit_id:
        elements.append(_md(f"审计号：`{audit_id}`"))
    if approval_id and audit_id:
        elements.append(_actions([_button("回滚", ACTION_ROLLBACK, approval_id, "danger")]))
    return _card(_header("✅ 已执行", "green"), elements)


def build_failed_card(*, preview: str, reason: str, operator: str = "") -> dict[str, Any]:
    elements = [_md(f"❌ **执行失败**\n\n{reason}")]
    if preview:
        elements.append(_md(f"> {preview}"))
    if operator:
        elements.append(_md(f"操作人：{operator}"))
    return _card(_header("❌ 执行失败", "red"), elements)


def build_rolled_back_card(*, preview: str, operator: str,
                           detail: str = "") -> dict[str, Any]:
    elements = [_md(f"↩️ **已回滚**　操作人：{operator or '未知'}")]
    if preview:
        elements.append(_md(f"> {preview}"))
    if detail:
        elements.append(_md(detail))
    return _card(_header("↩️ 已回滚", "turquoise"), elements)


def build_operate_off_card(detail: str, *, minutes: int = 120) -> dict[str, Any]:
    """写开关未开时的卡片。给一个当场开开关的按钮 ——
    用户在手机上被挡住时，出路不该是"你去登服务器敲命令"。"""
    return _card(_header("⏸ 待执行（写开关未开）", "orange"), [
        _md(detail),
        _actions([_bare_button(f"开启写开关 {minutes} 分钟", ACTION_OPERATE_ON,
                               "primary", minutes=minutes)]),
    ])


def build_text_card(title: str, body: str, *, template: str = "blue") -> dict[str, Any]:
    """纯文本卡片（降级路径、系统提示用）。超长自动只取首片，其余由调用方续发。"""
    parts = chunk(body)
    return _card(_header(title, template), [_md(parts[0] if parts else "")])


def parse_action_value(value: Any) -> tuple[str, str]:
    """从按钮回调里取出 (action, approval_id)。非法输入返回空串，不抛异常——
    回调路径上抛异常会让飞书一直重投。"""
    if not isinstance(value, dict):
        return "", ""
    action = str(value.get("ivyea_action") or "")
    if action not in (ACTION_APPROVE, ACTION_DENY, ACTION_ROLLBACK, ACTION_DETAIL,
                      ACTION_APPROVE_ALL, ACTION_APPROVE_ALL_CONFIRM, ACTION_OPERATE_ON):
        return "", ""
    return action, str(value.get("approval_id") or "")
