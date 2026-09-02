"""准备阶段的 stage 事件：让「正在准备」不再是黑盒。

用户原话："接到指令的正在准备有点黑盒的感觉，有时候准备几十秒甚至更久，
不知道在准备什么。"

在第一个 token 之前，serve 要载入会话历史、召回记忆、注入知识证据、组工具清单
（可能要连 MCP）、语义匹配技能 —— 这些加起来动辄几十秒，而此前**一个字节都不发**：
连 start 都排在 _chat_messages 之后。前端只能干等。

这里锁住三件事：
  1. stage 事件确实发得出来；
  2. **第一条 stage 早于 start** —— 这是整个修复的意义所在，晚于 start 就等于
     "慢活干完了才告诉你要开始干活"；
  3. 每条 stage 都带人话 label 和已耗时，前端不用自己猜该显示什么。
"""
from __future__ import annotations


class _EchoProvider:
    def stream_chat(self, messages, tools=None):
        yield {"type": "text", "text": "好的。"}
        yield {"type": "final", "content": "好的。", "tool_calls": [], "usage": {}}

    def chat(self, messages, tools=None):
        return {"content": "好的。", "tool_calls": []}


def _events(message: str = "你好", **payload):
    from ivyea_agent import service

    seen: list[tuple[str, dict]] = []
    body = {"message": message, "persist": False, "max_steps": 2, **payload}
    service.chat_stream(body, lambda e, d: seen.append((e, d)), provider=_EchoProvider())
    return seen


def test_准备阶段会发出_stage_事件():
    names = [d.get("stage") for e, d in _events() if e == "stage"]
    assert names, "一条 stage 都没有，前端仍然只能显示一句干巴巴的「正在准备」"
    # intake 是入口那条，必然有；其余几条随路线可能被跳过（比如闲聊不匹配技能）
    assert "intake" in names
    assert "model" in names, "缺少「等待模型响应」——用户无从判断是本地卡住还是在等模型"


def test_第一条_stage_必须早于_start():
    """晚于 start 就失去意义：_chat_messages 那段慢活正好发生在 start 之前。"""
    seq = [e for e, _ in _events()]
    assert "stage" in seq and "start" in seq
    assert seq.index("stage") < seq.index("start"), (
        "第一条 stage 排在了 start 后面 —— 准备阶段最慢的那段仍然是黑的")


def test_每条_stage_都带人话标签和已耗时():
    for e, d in _events():
        if e != "stage":
            continue
        assert d.get("label"), f"stage {d.get('stage')} 没有 label，前端只能显示英文枚举"
        assert isinstance(d.get("elapsed_ms"), int), "缺 elapsed_ms，界面没法显示已经等了多久"
        assert d["elapsed_ms"] >= 0


def test_stage_顺序与真实执行顺序一致():
    names = [d.get("stage") for e, d in _events() if e == "stage"]
    order = {n: i for i, n in enumerate(["intake", "context", "tools", "skills", "model"])}
    ranks = [order[n] for n in names if n in order]
    assert ranks == sorted(ranks), f"stage 顺序乱了：{names}"
