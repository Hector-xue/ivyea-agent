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
#: 正文里的严重度圆点。**与标题的 emoji 分开**：标题用大图标定调，
#: 正文每行再放一个大 emoji 会让整张卡看起来全是感叹号，反而没有重点。
_SEV_DOT = {"crit": "🔴", "warn": "🟠", "info": "🔵"}
_CLASS_LABEL = {"stanch": "止血", "structural": "结构", "advisory": "建议"}

#: 规则代码 → **人话短名**。卡片第一行要让人一眼知道"出了什么事"，
#: 而不是先读一行 ``listing.rating_low`` 再自己翻译。
#: 代码本身仍然保留在备注里 —— 排查时要能对上日志。
RULE_LABEL = {
    "stock.oos": "断货", "stock.days_low": "可供天数不足",
    "stock.unsellable_spike": "不可售激增", "stock.health_bad": "库存健康度异常",
    "stock.excess": "冗余库存偏高", "stock.fbm_low": "FBM 库存不足",
    "ads.campaign_out_of_budget": "预算耗尽停投",
    "ads.campaign_unexpected_pause": "活动被暂停",
    "ads.budget_changed_externally": "预算被外部改动",
    "ads.spend_burst": "花费突增", "ads.impression_zero": "曝光归零",
    "ads.click_no_order_intraday": "点击零转化", "ads.acos_breach": "ACOS 超标",
    "ads.cpc_jump": "CPC 跳涨", "ads.budget_capped": "预算打满",
    "ads.listing_acos_breach": "ACOS 超标", "ads.listing_spend_no_sales": "广告零销售额",
    "sales.drop": "销量下滑", "sales.stall": "销量断流",
    "sales.listing_drop": "销量下滑", "sales.listing_stall": "销量断流",
    "profit.margin_erosion": "毛利率下滑",
    "listing.deactivated": "listing 下架", "listing.reactivated": "listing 恢复在售",
    "listing.rating_low": "评分偏低", "review.rating_drop": "评分下滑",
    "rank.drop": "排名下滑", "price.changed_externally": "价格被改动",
    "buybox.competitor_appeared": "出现跟卖", "buybox.crowded": "跟卖拥挤",
}

#: 指标 → 展示单位/格式。数字要能一眼读懂：2.9 星、90%、¥1,203、12 件。
_METRIC_FMT = {
    "acos": "pct", "gross_rate": "pct", "cvr": "pct",
    "stars": "星", "rank": "名", "seller_count": "个卖家",
    "days_of_supply": "天", "cpc": "money", "price": "money",
    "daily_budget": "money", "spend_7": "money", "spend_per_hour": "money/时",
    "sales_amount": "money",
    "fulfillable": "件", "quantity": "件", "unsellable": "件", "excess_qty": "件",
    "volume_7": "件", "volume_yesterday": "件", "avg_volume_7": "件/天",
    "impressions": "次", "clicks": "次",
}


def rule_label(finding: Any) -> str:
    """一条 finding 的人话短名。优化器那批是 ``ads.opt.<动作>``，单独兜一下。"""
    code = str(getattr(finding, "code", "") or "")
    if code in RULE_LABEL:
        return RULE_LABEL[code]
    if code.startswith("ads.opt."):
        return "优化建议"
    return code or "异常"


def _fmt_metric(metric: str, value: Any) -> str:
    """把裸数字渲染成人能读的样子。**取不到就返回空**，由调用方决定不显示这一格——
    显示一个孤零零的 "0" 比不显示更误导。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value or "")
    fmt = _METRIC_FMT.get(str(metric or ""), "")
    if fmt == "pct":
        return f"{num:.0%}" if abs(num) < 10 else f"{num:.1f}"
    if fmt == "money":
        return f"{num:,.2f}"
    body = f"{num:,.0f}" if abs(num - round(num)) < 0.05 else f"{num:,.1f}"
    return f"{body} {fmt}".strip() if fmt else body


# ── 基础元件 ────────────────────────────────────────────────────────────────
def _md(content: str) -> dict[str, Any]:
    return {"tag": "markdown", "content": str(content or "")}


def _hr() -> dict[str, Any]:
    return {"tag": "hr"}


def _div(text: str, fields: Optional[list[tuple[str, str]]] = None) -> dict[str, Any]:
    """一行标题 + 若干「标签／值」小格（两列）。

    ``fields`` + ``is_short`` 是卡片 1.0 的双列布局（已对飞书官方「内容模块」
    文档核实）。用它而不是把数字堆进一行文字里：手机屏幕上，
    "评分 2.9 星（5 条评价），低于 3.5 星" 会折成三行，重点全糊在里面。
    """
    el: dict[str, Any] = {"tag": "div",
                          "text": {"tag": "lark_md", "content": str(text or "")}}
    if fields:
        el["fields"] = [{"is_short": True,
                         "text": {"tag": "lark_md", "content": f"**{k}**\n{v}"}}
                        for k, v in fields if str(v)]
    return el


def _note(text: str) -> dict[str, Any]:
    """页脚小灰字。数据来源、跳过项、规则代码这些**排查时才需要**的东西放这里：
    删掉它们等于让人无法核对 agent 有没有瞎说，放正文又会把重点淹了。"""
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": str(text or "")}]}


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
    # 标题写人话，不写 ``listing.rating_low`` —— 代码留在页脚，排查时对得上就行
    title = f"{icon} {rule_label(finding)}" + (f" · {who}" if who else "")

    elements: list[dict[str, Any]] = [
        _div(_target_line(finding), _finding_fields(finding)),
        _md(str(getattr(finding, "message", ""))),
    ]
    ev = dict(getattr(finding, "evidence", {}) or {})
    if ev:
        elements.append(_md("**证据**\n" + "\n".join(_evidence_lines(ev))))

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

    # 溯源进页脚：必须看得见（用户要能核对 agent 有没有瞎说），但不该占正文
    foot = _footnote([finding])
    if foot:
        elements.append(_note(foot))

    return _card(_header(title, _SEV_TEMPLATE.get(sev, "blue")), elements)


# ── 批量告警卡片 ────────────────────────────────────────────────────────────
def _target_line(finding: Any, *, with_store: str = "") -> str:
    """异常块的第一行：**出了什么事** · 谁。

    顺序是刻意的 —— 规则名在前、商品名在后。反过来的话，一屏 6 条异常全是
    "IVY 3m by 2M SD…"开头，扫一眼看不出哪条是断货、哪条只是评分低。
    """
    sev = str(getattr(finding, "severity", "info"))
    name = str(getattr(finding, "target_name", "") or getattr(finding, "target_id", ""))
    if len(name) > 28:
        name = name[:28] + "…"
    cls = str(getattr(finding, "action_class", "advisory"))
    tag = f"「{_CLASS_LABEL[cls]}」" if cls in ("stanch", "structural") else ""
    head = f"{_SEV_DOT.get(sev, '')} **{rule_label(finding)}**{tag}"
    bits = [head]
    if with_store:
        bits.append(with_store)
    if name:
        bits.append(name)
    return " · ".join(bits)


def _finding_fields(finding: Any) -> list[tuple[str, str]]:
    """「当前 / 基线 / 对象 / 窗口」四小格。**没有值的格子直接不出现** ——
    显示一个孤零零的 0 比不显示更误导。"""
    # 没声明 metric 的 finding，current/baseline 是无单位的裸数字，多半就是 0。
    # 「当前 0」既没意义又像在说"这个指标是零"—— 不如不显示。
    # 反过来，stock.oos 的 metric=fulfillable、current=0 是**有意义的 0**（0 件），
    # 所以判据是"有没有 metric"，不是"值是不是 0"。
    metric = str(getattr(finding, "metric", "") or "")
    cur = _fmt_metric(metric, getattr(finding, "current", None)) if metric else ""
    base = _fmt_metric(metric, getattr(finding, "baseline", None)) if metric else ""
    fields: list[tuple[str, str]] = []
    if cur:
        fields.append(("当前", cur))
    if base and base != cur:
        # 阈值型规则的 baseline 是"门槛"，趋势型的是"上一期" —— 措辞跟着走，
        # 否则用户会把"基线 3.5 星"读成"上周 3.5 星"
        label = "门槛" if str(getattr(finding, "code", "")).endswith(
            ("_low", "_breach", "_capped")) else "基线"
        fields.append((label, base))
    target_id = str(getattr(finding, "target_id", "") or "")
    if target_id and target_id != str(getattr(finding, "target_name", "")):
        fields.append(("对象", target_id))
    window = str(getattr(finding, "window", "") or "")
    if window:
        fields.append(("窗口", window))
    return fields[:4]


def _footnote(findings: list[Any], *, layer: str = "", skipped: int = 0,
              gaps: int = 0) -> str:
    """页脚：排查时要用、但不该抢注意力的东西。"""
    bits = []
    if layer:
        bits.append(f"巡检层 {layer}")
    codes = sorted({str(getattr(f, "code", "")) for f in findings if getattr(f, "code", "")})
    if codes:
        bits.append("规则 " + "、".join(codes[:4]) + ("…" if len(codes) > 4 else ""))
    # provenance 形如「领星 MCP · 延迟约 10 分钟 · 120 行」。页脚只留源名：
    # 延迟和行数是排查细节，堆在这一行会把它撑成两行小灰字，比不写还乱。
    provs = sorted({str(getattr(f, "provenance", "")).split(" · ")[0]
                    for f in findings if getattr(f, "provenance", "")})
    if provs:
        bits.append("来源 " + "、".join(provs[:3]))
    if skipped:
        bits.append(f"{skipped} 条规则本次跳过")
    if gaps:
        bits.append(f"{gaps} 处数据缺口")
    return " · ".join(bits)


# ── 批量告警卡片 ────────────────────────────────────────────────────────────
def build_alert_card(findings: Iterable[Any], *, sid: Any = "", store_name: str = "",
                     layer: str = "", approval_ids: Optional[dict[str, str]] = None,
                     max_items: int = 10, skipped: int = 0,
                     gaps: int = 0) -> dict[str, Any]:
    """一次巡检的多条异常合并成一张卡（§5.3 批量合并，防刷屏）。

    版式的三条规矩（2026-08-23 按真机截图重排）：

    1. **一条异常一个块**，第一行是"出了什么事"，不是商品名。
       原先是把整句话塞进一行 markdown，手机上折成三行，重点在句尾。
    2. **数字进双列小格**（当前 / 门槛 / 对象 / 窗口），不混在句子里。
    3. **技术信息降级成页脚小灰字**：数据来源、跳过项、规则代码。
       它们必须留着（用户要能核对 agent 有没有瞎说），但不该占据视线。
    """
    # 防御性排序：紧急的必须在最上面。调用方大多已经排过，但"大多"不算数 ——
    # 排错的后果是断货排在评分偏低下面，人一眼看到的是不要紧的那条。
    findings = sorted(list(findings),
                      key=lambda f: _SEV_RANK_ORDER.get(
                          str(getattr(f, "severity", "info")), 9))
    approval_ids = approval_ids or {}
    worst = "info"
    for f in findings:
        sev = str(getattr(f, "severity", "info"))
        if sev == "crit" or (sev == "warn" and worst == "info"):
            worst = sev
    who = store_name or (f"sid {sid}" if sid else "")

    counts: dict[str, int] = {}
    for f in findings:
        s_ = str(getattr(f, "severity", "info"))
        counts[s_] = counts.get(s_, 0) + 1
    # 标题直接把构成写出来：「紧急 2 · 注意 3」比「异常 5 条」有用得多
    parts = [f"{label} {counts[key]}" for key, label in
             (("crit", "紧急"), ("warn", "注意"), ("info", "提示")) if counts.get(key)]
    title = f"{_SEV_ICON.get(worst, 'ℹ️')} " + (" · ".join(parts) or "无异常")
    if who:
        title += f" · {who}"

    elements: list[dict[str, Any]] = []
    buttons: list[dict[str, Any]] = []
    for i, f in enumerate(findings[:max_items]):
        if i:
            elements.append(_hr())
        elements.append(_div(_target_line(f), _finding_fields(f)))
        detail = str(getattr(f, "message", ""))
        # message 里已经包含规则名和目标名，这里只在它明显更长时补一句说明，
        # 避免同一句话在卡片上出现两遍
        if len(detail) > 24 and not _finding_fields(f):
            elements.append(_md(detail))
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

    foot = _footnote(findings, layer=layer, skipped=skipped, gaps=gaps)
    if foot:
        elements.append(_note(foot))

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
        # 与告警卡同一套读法：先"出了什么事"，再"谁" —— 一屏全是商品名开头时
        # 扫不出哪条要紧
        elements.append(_md(f"**异常 {len(alerts)} 条**\n" + "\n".join(
            _target_line(f) for f in alerts[:6])))

    buttons: list[dict[str, Any]] = []
    if actionable:
        elements.append(_hr())
        body = [f"**待你决定 {len(actionable)} 条**"]
        for i, f in enumerate(actionable[:5], 1):
            body.append(f"{i}. {_target_line(f)}")
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
            body.append(_target_line(f, with_store=name))
        if len(alerts) > max_alerts:
            body.append(f"…另有 {len(alerts) - max_alerts} 条")
        elements.append(_md("\n".join(body)))

    buttons: list[dict[str, Any]] = []
    if actionable:
        elements.append(_hr())
        body = [f"**待你决定 {len(actionable)} 条**"]
        for i, (name, f) in enumerate(actionable[:max_actions], 1):
            body.append(f"{i}. {_target_line(f, with_store=name)}")
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
            body.append(_target_line(f, with_store=name if multi else ""))
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
