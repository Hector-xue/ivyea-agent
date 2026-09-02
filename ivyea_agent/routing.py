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
* `quick` —— 知识型提问（"什么是 X"、"了解过 X 吗"、"X 和 Y 有什么区别"）。这类问题
  要查、但只往**读**的方向查：搜网页、翻知识库、想想记得什么。挂全部 54 个工具没有
  意义，其中 40 多个是写文件、跑命令、调板块、动广告的，一个都用不上，却每一步都在
  重发。工具集裁到只读检索那一小撮，知识注入和引证照旧 —— 它和 `chat` 的区别就在
  这里：`chat` 是不查，`quick` 是照查不误，只是不带那一身用不上的家伙。
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

#: 知识型提问的长度上限。再长多半在描述一件具体的事，那要动真格的。
_QUICK_MAX_CHARS = 80

#: 知识提问的形态。这些问的是"事情本身"，不是"我的数据"。
#: 和闲聊白名单一样**必须命中形态**，但不要求整句匹配 —— 知识问句前后常有修饰。
_QUICK_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"(什么是|啥是|是什么意思|是什么|指的是什么|是个什么)",
    r"(了解过|听说过|知道).{0,30}(吗|么|没有|不)",
    r"(介绍一下|介绍下|讲讲|说说|科普).{0,40}",
    r"(是谁|是哪家|哪家公司|什么公司|干什么的|做什么的|是干嘛的)",
    r"(为什么|为啥|怎么理解|原理是|怎么回事)",
    r"(有什么区别|有何区别|区别是什么|和.{1,20}的?区别|跟.{1,20}的?区别|哪个更)",
    r"(有哪些|包括哪些|分为哪几|常见的.{0,10}有)",
))

#: 指向**用户自己的数据**的词。带上任意一个就不是通用知识问题 —— 那要连数据源，
#: 是 work 的活。这道闸比 quick 的形态判定更靠后，但优先级更高。
_SELF_DATA_WORDS = (
    "我的", "我们的", "咱的", "咱们的", "本店", "我店", "自己的",
    "账号", "账户", "店铺", "后台", "报表", "巡检", "数据", "近期", "最近",
    "上周", "本周", "上个月", "这个月", "昨天", "今天的", "多少钱", "花了",
)

#: quick 路线挂的工具：只读检索，一个写/执行/板块工具都没有。
#: 名字必须和 agent_tools.TOOL_SCHEMAS 对得上，写错了就是静默少挂一个。
_QUICK_TOOLS = (
    "web_search", "web_fetch", "web_images",
    "knowledge_search", "recall", "memory_search", "memory_read",
    "skill_search", "skill_view",
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

    lane: str = "work"          # chat | quick | board | work
    board_tool: str = ""        # board：能确定时给出板块工具名
    board_label: str = ""       # board：给人看的板块名
    reason: str = ""            # 为什么这么判，进日志和 stream 事件，便于事后复盘

    @property
    def is_chat(self) -> bool:
        return self.lane == "chat"

    @property
    def is_quick(self) -> bool:
        return self.lane == "quick"

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
    if not said:
        return Route(reason="空，按常规走")

    if len(said) <= _CHAT_MAX_CHARS:
        # ① 必须整句落在寒暄白名单里
        bare = said.strip(_TRIM)
        if any(p.fullmatch(bare) for p in _CHAT_PATTERNS):
            # ② 白名单之外再兜一道：万一某条正则写宽了，业务/工程/动作/实体一律拦下
            blocked = _blocked_by(low, said, (
                (_DOMAIN_WORDS, "业务词"), (_ENG_WORDS, "工程词"), (_ACTION_WORDS, "动作词")))
            if not blocked:
                return Route(lane="chat", reason=f"寒暄/常识类（{len(said)} 字）")
            return Route(reason=f"{blocked}，按常规走")

    return _classify_quick(said, low)


def _blocked_by(low: str, said: str, groups) -> str:
    """命中任一拦截词/实体就返回原因，没命中返回空串。"""
    for group, why in groups:
        hit = next((w for w in group if w in low), "")
        if hit:
            return f"含{why}「{hit}」"
    if any(p.search(said) for p in _ENTITY_PATTERNS):
        return "含具体实体（ASIN/网址/路径/长数字）"
    return ""


def _classify_quick(said: str, low: str) -> Route:
    """知识型提问 → `quick`（只读检索工具集）。判不准一律回 `work`。

    和 `chat` 的取舍方向不同：`chat` 判错的后果是**凭记忆瞎答**，所以卡得极死；
    `quick` 判错的后果只是"这一轮手上没有写工具"，答案照样是查过才给的。所以这里
    可以放宽到业务知识（"ACOS 是什么意思"该走这条，它要查知识库），但凡是可能要
    **动手**或要**看用户自己的数据**的，一律退回 work。
    """
    if len(said) > _QUICK_MAX_CHARS:
        return Route(reason="过长，按常规走")
    if not any(p.search(said) for p in _QUICK_PATTERNS):
        return Route(reason="不是知识型提问，按常规走")
    blocked = _blocked_by(low, said, (
        # 业务词**不拦**：业务知识问题正是 quick 要接的（quick 带着 knowledge_search）。
        (_ENG_WORDS, "工程词"), (_ACTION_WORDS, "动作词"), (_SELF_DATA_WORDS, "自有数据词")))
    if blocked:
        return Route(reason=f"{blocked}，按常规走")
    return Route(lane="quick", reason=f"知识型提问（{len(said)} 字），只挂只读检索工具")


def tools_for(route: Route, all_tools: list | None = None) -> list | None:
    """这一轮挂哪些工具。返回 `[]` = 一个都不挂；`None` = 全量（由调用方决定）。

    `chat` 一个不挂，`quick` 只挂只读检索那一小撮。板块任务照挂全量 —— 裁工具省下
    的是 token，缺能力赔上的是整件事做不成，那笔账不划算；而 quick 用不上的恰恰是
    写/执行/板块这些，裁掉不赔任何能力。
    """
    if route.is_chat:
        return []
    if route.is_quick:
        return quick_tool_schemas(all_tools)
    return all_tools


def quick_tool_schemas(all_tools: list | None = None) -> list:
    """quick 路线的工具 schema。调用方没给全量就自己去取（CLI 就是这么调的）。"""
    if all_tools is None:
        from .agent_tools import TOOL_SCHEMAS
        all_tools = TOOL_SCHEMAS
    return [t for t in all_tools
            if (t.get("function") or {}).get("name") in _QUICK_TOOLS]


def quick_hint(route: Route) -> str:
    """知识型提问的直达提示 —— 治的是"同一件事反复搜"。

    实测：挂上只读工具集之后，模型仍然跑了 `web_search → web_images → web_search →
    web_images` 四步，后两步和前两步查的是同一件事。裁工具省下的是每步的 token，
    省不掉步数；步数得靠说清楚"查一次就够了"来省，而每一步都是一次完整的模型往返。
    """
    if not route.is_quick:
        return ""
    return (
        "\n\n[本轮直达] 这是一个知识型问题，答完就结束，没有后续动作。"
        "**检索最多一轮**：需要外部资料就一次性把要查的都查了"
        "（`web_images` 会连来源页摘要一起给你，要配图的话它一个工具就够，不用再 `web_search`），"
        "拿到就直接作答。**不要为了确认再搜第二遍同样的内容**；"
        "已经知道答案的就直接答，一个工具都不用调。"
    )


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
