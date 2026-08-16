"""视觉能力判定矩阵 + serve 侧三档接线。

这里盯的是两个具体的历史故障：
  1. 视觉能力按**厂商白名单**判，国内 provider 上的 Qwen-VL/GLM-4V 配了 key 也选不中；
  2. serve 自己 `raise main_brain_no_vision`，导致 IvyeaOps 在主脑无视觉时整块死掉，
     而同一台机器上的 CLI 却有旁路可走。
"""
from __future__ import annotations

import pytest

from tests.test_image_audit import _png


# ── 判定矩阵 ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model,expected", [
    ("gpt-4o", True),
    ("claude-sonnet-4-6", True),
    ("gemini-3-pro-preview", True),
    ("qwen3-vl-235b", True),
    ("Qwen/Qwen2.5-VL-72B-Instruct", True),      # 硅基流动的大小写混写
    ("glm-4v-plus", True),
    ("minicpm-v-2.6", True),
    ("llava:13b", True),                          # ollama 本地 VLM
    ("anthropic/claude-sonnet-4.6", True),        # openrouter 带前缀
    ("deepseek-chat", False),
    ("deepseek-reasoner", False),
    ("qwen3-coder", False),
    ("text-embedding-3-large", False),
    ("some-unknown-model", None),                 # 看不出来 ≠ 确定没有
])
def test_model_name_vision_matrix(model, expected):
    from ivyea_agent import models
    assert models.model_name_has_vision(model) is expected


def test_model_has_vision_requires_a_capable_transport(ivyea_home):
    from ivyea_agent import models
    # 模型名像视觉模型，但传输格式装不下图 → 判否
    assert models.model_has_vision({"model": "gpt-4o", "api_mode": "some_text_only_mode"}) is False
    assert models.model_has_vision({"model": "gpt-4o", "api_mode": "chat_completions"}) is True
    # "看不出来"保守判否（真能看图的话用户可以用 override 打开）
    assert models.model_has_vision({"model": "vlm-prod-v3", "api_mode": "chat_completions"}) is False


def test_vision_capable_override_wins(ivyea_home):
    """自建网关的模型名不带任何视觉特征词，必须有一个用户说了算的开关。"""
    from ivyea_agent import config, models
    mcfg = {"model": "vlm-prod-v3", "api_mode": "chat_completions"}
    config.set_setting("vision_capable_override", "true")
    assert models.model_has_vision(mcfg) is True
    config.set_setting("vision_capable_override", "false")
    assert models.model_has_vision({"model": "gpt-4o", "api_mode": "chat_completions"}) is False


def test_provider_level_vision_is_broader_than_the_old_whitelist():
    """provider 级 vision 是 UI 徽标语义："这家有视觉可选模型"。

    老实现硬编码 {openai, anthropic, gemini, openai-codex} 四家，国内 provider
    和聚合网关全被挡在外面。
    """
    from ivyea_agent import models
    caps = {p["id"]: models.provider_capabilities(p).get("vision") for p in models.providers()}
    assert caps.get("openrouter") is True        # 聚合网关：开放目录
    assert caps.get("ollama") is True            # 本地 VLM：模型随用户拉
    assert caps.get("deepseek") is False         # 目录里确实一个视觉模型都没有


# ── serve 接线 ────────────────────────────────────────────────────────────

def _data_url(path) -> str:
    from ivyea_agent import image_audit
    return image_audit.data_url(path)


def test_serve_no_longer_raises_on_images_without_main_vision(tmp_path, monkeypatch, ivyea_home):
    """核心回归：serve 收到带图请求且主脑无视觉，必须降级而不是抛异常。"""
    from ivyea_agent import service, vision
    from ivyea_agent.agent_tools import ToolContext

    img = tmp_path / "main.png"
    _png(img, 1200, 1200)
    monkeypatch.setattr(vision, "pick_vision_model", lambda: None)   # 无 T2
    monkeypatch.setattr("ivyea_agent.config.get_model_config",
                        lambda: {"provider_id": "deepseek", "model": "deepseek-chat",
                                 "api_mode": "chat_completions"})

    ctx = ToolContext()
    out = service._with_payload_images("这套图有问题吗？", {"images": [_data_url(img)]}, ctx)

    assert isinstance(out, str)                        # T3 回纯文本，不是多模态 list
    assert "这套图有问题吗？" in out
    assert "本地视觉度量" in out
    assert ctx.vision_tier["tier"] == vision.TIER_LOCAL_CV
    assert ctx.vision_notes                            # 降级说明要留给调用方去 narrate


def test_serve_passes_images_through_when_main_has_vision(tmp_path, monkeypatch, ivyea_home):
    from ivyea_agent import service, vision
    from ivyea_agent.agent_tools import ToolContext

    img = tmp_path / "a.png"
    _png(img)
    monkeypatch.setattr("ivyea_agent.config.get_model_config",
                        lambda: {"provider_id": "openai", "model": "gpt-4o",
                                 "api_mode": "chat_completions"})
    ctx = ToolContext()
    out = service._with_payload_images("看图", {"images": [_data_url(img)]}, ctx)

    assert isinstance(out, list)
    assert out[0]["text"] == "看图"
    assert any(part.get("type") == "image_url" for part in out)
    assert ctx.vision_tier["tier"] == vision.TIER_MAIN


def test_sidecar_accepts_data_uris_not_just_file_paths(tmp_path, monkeypatch, ivyea_home):
    """serve（网页上传）传的是 data URI，CLI 传的是文件路径，两种都得收。

    实测故障：只按文件路径处理时，data URI 在 read_bytes 上抛异常并被静默吞掉，
    视觉模型一张图没收到却照常作答——编出过一整张不存在的柱状图，数字有名有姓。
    """
    from ivyea_agent import mentions, vision, providers

    img = tmp_path / "a.png"
    _png(img, 800, 800)
    uri = _data_url(img)

    seen = {}

    class _P:
        def chat(self, messages, tools=None, **kw):
            seen["content"] = messages[-1]["content"]
            return {"content": "图里是一个蓝色方块。", "tool_calls": []}

    monkeypatch.setattr(providers, "from_settings", lambda cfg, key: _P())
    picked = {"cfg": {"model": "qwen-vl", "provider_id": "custom"}, "key": "k", "label": "vl"}

    # data URI 输入
    assert vision.sidecar_describe([uri], "这是什么", picked)
    parts = seen["content"]
    assert isinstance(parts, list)
    assert [p for p in parts if p.get("type") == "image_url"], parts
    assert parts[-1]["image_url"]["url"].startswith("data:image/")

    # 文件路径输入（CLI 那条路）同样成立
    assert vision.sidecar_describe([str(img)], "这是什么", picked)
    assert [p for p in seen["content"] if p.get("type") == "image_url"]

    # build_user_content 本身也要两种都吃
    both = mentions.build_user_content("t", [uri, str(img)])
    assert len([p for p in both if p.get("type") == "image_url"]) == 2


def test_sidecar_refuses_to_answer_with_zero_images(monkeypatch, ivyea_home):
    """全部图片都挂不上时必须抛错降 T3，绝不能对着纯文字让视觉模型硬答。"""
    from ivyea_agent import vision, providers

    called = []
    monkeypatch.setattr(providers, "from_settings",
                        lambda cfg, key: called.append(1) or (_ for _ in ()).throw(AssertionError("不该发出请求")))
    picked = {"cfg": {"model": "qwen-vl"}, "key": "k", "label": "vl"}

    with pytest.raises(ValueError, match="未能附上任何图片"):
        vision.sidecar_describe(["/does/not/exist.png"], "这是什么", picked)
    assert not called


def test_route_images_degrades_when_sidecar_cannot_attach(tmp_path, monkeypatch, ivyea_home):
    """接上上一条：这种失败要走到 T3，而不是把编造的描述当成结果。"""
    from ivyea_agent import vision, providers

    img = tmp_path / "main.png"
    _png(img, 1000, 1000)
    monkeypatch.setattr(vision, "pick_vision_model",
                        lambda: {"cfg": {"model": "qwen-vl"}, "key": "k", "label": "vl"})
    monkeypatch.setattr(providers, "from_settings",
                        lambda cfg, key: (_ for _ in ()).throw(AssertionError("不该发出请求")))
    # 传一个既不是路径也不是 data URI 的东西 → 一张都挂不上
    notes = []
    content, kept, tier = vision.route_images(
        "看图", ["https://example.com/not-a-local-image.png"],
        {"provider_id": "deepseek", "model": "deepseek-chat", "api_mode": "chat_completions"},
        notes.append)
    assert tier["tier"] == vision.TIER_LOCAL_CV or tier["tier"] == 0
    assert kept == []


def test_health_exposes_vision_chain(monkeypatch, ivyea_home):
    """IvyeaOps 判 agent 能不能接带图任务，读的就是这个字段。"""
    from ivyea_agent import service
    chain = service.health()["vision_chain"]
    assert "tier" in chain and "effective" in chain
    assert "local_cv" in chain and "sidecar" in chain


def test_vision_configure_roundtrip(ivyea_home):
    """ops 下推视觉槽 → 立刻变成 T2，且响应里不回显 key。"""
    from ivyea_agent import service, vision

    res = service.vision_configure({"provider": "custom", "model": "qwen2.5-vl-72b",
                                    "base_url": "https://gw.internal/v1", "api_key": "sk-secret"})
    assert res["ok"] is True
    assert res["configured"]["model"] == "qwen2.5-vl-72b"
    assert "api_key" not in res["configured"]
    assert "sk-secret" not in str(res)

    picked = vision.pick_vision_model()
    assert picked and picked["cfg"]["model"] == "qwen2.5-vl-72b"

    cleared = service.vision_configure({"model": ""})
    assert cleared["cleared"] is True
