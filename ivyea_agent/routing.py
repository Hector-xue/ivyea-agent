"""按用户这句话选执行路线：闲聊 / 板块直达 / 常规。

为什么要有这一层
----------------
慢的从来不是模型（DeepSeek 官方实测单次 1.3–2.5s，挂上全部 54 个工具也只多 0.5s），
慢的是**步数**：每一步都是一次完整往返，而且上下文越滚越大。线上量到的两个典型：

* 一句「测试」跑了 18 步，其中 17 步是 `progress_update`/`todo_write`，工具自身耗时
  0.0s，用户等了 2 分 16 秒。
* 最近 300 次工具调用里 112 次（37%）是这类纯记账调用。

所以这一层的目标只有一个：**该几步就几步**。

* `chat` —— 问候、道谢、问身份、简单常识。这类问题挂着 54 个工具（≈6.9K token）
  只会诱导模型"顺手查一下"，白白多走一两步。直接不挂工具、单次调用返回。
* `board` —— IvyeaOps 的板块任务（市场调研 / 打法 / 关键词竞争…）。工具集**不裁**
  （裁了就可能缺能力），但给一条点名到具体工具的直达提示，并关掉汇报状态机 ——
  板块工具本身就是一次长任务，它自己会回报进度，再套一层 todo 只是挡路。
* `work` —— 其余全部按原样：全量工具 + 原有纪律。**默认落在这里**，判不准就走这条。

判据只看用户真正打的那句话（`task_scope._user_said` 切掉系统注入的知识/技能/记忆
块）—— 那些块动辄上千字、满篇动作词，拿它们判复杂度正是上一次事故的成因。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import task_scope

#: 闲聊路线的长度上限（清洗后的原话）。超过这个长度的多半在描述一件事，不是打招呼。
_CHAT_MAX_CHARS = 40

#: 业务词：出现任意一个就不是闲聊 —— 它多半要查真实数据。
_DOMAIN_WORDS = (
    "亚马逊", "amazon", "广告", "投放", "acos", "tacos", "roas", "listing", "asin", "sku",
    "关键词", "词根", "否词", "竞价", "bid", "campaign", "活动", "预算", "竞品", "店铺",
    "销量", "订单", "库存", "退货", "退款", "评论", "review", "评分", "差评", "价格",
    "利润", "毛利", "转化", "曝光", "点击", "流量", "排名", "类目", "站点", "fba",
    "巡检", "报表", "报告", "调研", "打法", "选品", "领星", "sorftime", "卖家精灵",
    "客户", "对手", "市场",
)

#: 工程词：同上，这类要读文件/跑命令。
_ENG_WORDS = (
    "代码", "仓库", "文件", "目录", "路径", "函数", "接口", "日志", "报错", "异常",
    "部署", "发版", "构建", "编译", "脚本", "服务", "进程", "端口", "数据库", "测试",
    "前端", "后端", "界面", "页面", "样式", "git", "bug", "deploy", "server", "api",
    "commit", "分支", "配置",
)

#: 动作词：要动手的信号。
_ACTION_WORDS = (
    "改", "删", "建", "生成", "执行", "运行", "跑", "查", "搜", "找", "看一下", "看下",
    "看看", "分析", "诊断", "优化", "修复", "检查", "导出", "导入", "上传", "下载",
    "统计", "对比", "整理", "汇总", "帮我", "给我", "做一下", "做个", "写一个", "写个",
    "启动", "停止", "重启", "安装", "更新", "升级",
)

#: 闲聊白名单。**必须整句匹配**，不是"包含即可" —— "你好，帮我查一下广告"里也有"你好"。
#:
#: 为什么是白名单而不是"不含业务词就算闲聊"：黑名单永远列不全。实测反例
#: 「新卖家注册身份验证失败怎么办」—— 一个字都不在业务词表里，却是标准的知识库
#: 问题，走了快车道就等于没查知识库、没挂工具、凭记忆瞎答。**判不准要往"多做一点"
#: 那边倒，不是往"快"那边倒。**
_CHAT_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"(你好|您好|哈喽|哈啰|嗨|hi|hello|hey|在吗|在不在|早上好|中午好|下午好|晚上好|晚安)+",
    r"(谢谢|多谢|感谢|辛苦了|辛苦|好的|好嘞|收到|ok|okay|嗯+|知道了|明白了|明白|懂了)+",
    r"你(是谁|叫什么名?字?|是什么|能做什么|会做什么|能干什么|能干嘛|有什么功能|有什么能力)",
    r"(介绍一下你自己|自我介绍|你怎么用|怎么用你)",
    r"(今天是?(几号|星期几|周几)|现在几点|讲个笑话|说个笑话|你在吗)",
    r"\d+\s*[+\-*/×÷]\s*\d+\s*(=|等于)?\s*(几|多少)?",
))

#: 整句匹配前先剥掉的标点/语气字符。
_TRIM = "".join((" \t\r\n", "，。！？、,.!?~～:：;；\"'“”‘’()（）[]【】"))


#: 具体实体：带上这些就一定不是闲聊（ASIN / 网址 / 路径 / 文件名 / 长数字）。
_ENTITY_PATTERNS = (
    re.compile(r"\bB0[A-Z0-9]{8}\b", re.I),          # ASIN
    re.compile(r"https?://|www\."),                   # 网址
    re.compile(r"[/\\][\w.-]+[/\\]"),                 # 路径
    re.compile(r"\.\w{2,4}\b"),                       # 文件名后缀
    re.compile(r"\d{4,}"),                            # 长数字（ID / 金额 / 日期串）
)

#: 板块意图 → 板块工具名。名字对着 IvyeaOps 的 ivyea_ops_tools.TOOLS，别凭印象写。
#: 顺序有意义：先命中的先用，所以更具体的排前面。
_BOARD_INTENTS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("市场调研", "市场报告", "市场分析", "看看这个市场", "市场怎么样"),
     "market_generate_report", "市场调研"),
    (("打法", "launch", "上新方案", "推广方案", "起量方案"),
     "playbook_generate_report", "打法推荐"),
    (("关键词竞争", "竞品反查", "流量结构", "流量诊断", "词层竞争"),
     "deep_generate_report", "关键词竞争"),
    (("asin 审计", "asin审计", "深度审计", "listing 诊断", "listing诊断"),
     "asin_audit_start", "ASIN / Listing 审计"),
    (("广告审计", "广告巡检", "广告浪费", "浪费诊断"),
     "ad_audit_start", "广告审计"),
)


@dataclass(frozen=True)
class Route:
    """一轮的执行路线。`lane` 之外的字段只有对应 lane 才有意义。"""

    lane: str = "work"          # chat | board | work
    board_tool: str = ""        # board：能确定时给出板块工具名
    board_label: str = ""       # board：给人看的板块名
    reason: str = ""            # 为什么这么判，进日志和 stream 事件，便于事后复盘

    @property
    def is_chat(self) -> bool:
        return self.lane == "chat"

    @property
    def is_board(self) -> bool:
        return self.lane == "board"


def _said(message: str) -> str:
    """用户真正打的那句话（切掉系统注入块）。"""
    return task_scope._user_said(message or "")


def _board_intent(text: str) -> tuple[str, str]:
    for words, tool, label in _BOARD_INTENTS:
        if any(w in text for w in words):
            return tool, label
    return "", ""


def classify(message: str, *, ops_bridge: bool = False,
             has_attachments: bool = False) -> Route:
    """判这一轮走哪条路线。**判不准一律回 work**（全量工具 + 原有纪律）。"""
    said = _said(message)
    low = said.lower()

    if ops_bridge:
        tool, label = _board_intent(low)
        if tool:
            return Route(lane="board", board_tool=tool, board_label=label,
                         reason=f"命中板块意图：{label}")

    if has_attachments:
        return Route(reason="带了图片/引用，按常规走")
    if not said or len(said) > _CHAT_MAX_CHARS:
        return Route(reason="空或过长，按常规走")

    # ① 必须整句落在寒暄白名单里
    bare = said.strip(_TRIM)
    if not any(p.fullmatch(bare) for p in _CHAT_PATTERNS):
        return Route(reason="不是寒暄/身份/常识类，按常规走")

    # ② 白名单之外再兜一道：万一某条正则写宽了，业务/工程/动作/实体一律拦下
    for group, why in ((_DOMAIN_WORDS, "业务词"), (_ENG_WORDS, "工程词"),
                       (_ACTION_WORDS, "动作词")):
        hit = next((w for w in group if w in low), "")
        if hit:
            return Route(reason=f"含{why}「{hit}」，按常规走")
    if any(p.search(said) for p in _ENTITY_PATTERNS):
        return Route(reason="含具体实体（ASIN/网址/路径/长数字），按常规走")
    return Route(lane="chat", reason=f"寒暄/常识类（{len(said)} 字）")


def tools_for(route: Route, all_tools: list | None = None) -> list | None:
    """这一轮挂哪些工具。返回 `[]` = 一个都不挂；`None` = 全量（由调用方决定）。

    只有 `chat` 会被裁。板块任务照挂全量 —— 裁工具省下的是 token，缺能力赔上的是
    整件事做不成，这笔账不划算。
    """
    if route.is_chat:
        return []
    return all_tools


def board_hint(route: Route) -> str:
    """板块直达提示。点名到具体工具，省掉"先分析一轮再想起来该调工具"那几步。"""
    if not route.is_board or not route.board_tool:
        return ""
    return (
        f"\n\n[本轮直达] 这是「{route.board_label}」板块任务。"
        f"**第一步就调用** `ivyea_ops_call_tool`，name=`{route.board_tool}`，"
        "参数按用户这句话里的 query / mode / marketplace 填；"
        "不要先长篇分析、不要先列待办、不要自己手写报告。"
    )
