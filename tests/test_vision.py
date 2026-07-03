from __future__ import annotations

from tests.test_image_audit import _png


def test_build_vision_packages(tmp_path):
    from ivyea_agent import vision

    img = tmp_path / "main.png"
    _png(img, 1200, 1200)
    for provider in ("openai", "anthropic", "gemini"):
        pkg = vision.build(provider, [str(tmp_path)], product_context="karaoke machine", max_images=1)
        assert pkg["provider"] == provider
        assert pkg["images"]
        text = vision.render_package(pkg, include_payload=True)
        assert "多模态视觉请求包" in text
        assert "karaoke machine" in text
        assert "<base64 truncated>" in text


def test_image_vision_cli(tmp_path, capsys):
    from ivyea_agent.cli import main

    img = tmp_path / "main.png"
    _png(img, 1200, 1200)
    out = tmp_path / "vision.json"
    assert main([
        "image", "vision", str(tmp_path),
        "--provider", "openai",
        "--payload",
        "--output", str(out),
        "--context", "karaoke machine",
    ]) == 0
    stdout = capsys.readouterr().out
    assert "多模态视觉请求包" in stdout
    assert out.exists()
    assert "base64 truncated" in out.read_text(encoding="utf-8")


def test_build_general_vision_package(tmp_path):
    from ivyea_agent import vision

    img = tmp_path / "screen.png"
    _png(img, 800, 1200)
    pkg = vision.build_general(
        "openai",
        [str(tmp_path)],
        task="检查手机端是否需要横向滚动",
        context="agent.ivyea.com mobile screenshot",
        max_images=1,
    )
    assert pkg["mode"] == "general"
    assert "通用多模态视觉分析器" in pkg["prompt"]
    assert "横向滚动" in pkg["prompt"]
    out = vision.render_package(pkg, include_payload=True)
    assert "agent.ivyea.com" in out
    assert "<base64 truncated>" in out


def test_general_vision_cli(tmp_path, capsys):
    from ivyea_agent.cli import main

    img = tmp_path / "screen.png"
    _png(img, 800, 1200)
    out = tmp_path / "generic-vision.json"
    assert main([
        "vision", str(tmp_path),
        "--task", "检查 UI 是否重叠",
        "--context", "mobile",
        "--payload",
        "--output", str(out),
    ]) == 0
    stdout = capsys.readouterr().out
    assert "通用多模态视觉分析器" in stdout
    assert out.exists()


def test_call_openai_vision(monkeypatch, tmp_path):
    from ivyea_agent import vision

    img = tmp_path / "main.png"
    _png(img, 1200, 1200)
    pkg = vision.build("openai", [str(tmp_path)], max_images=1)
    seen = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"output_text": "主图文字可读，建议增强卖点。"}

    def fake_post(url, headers, params, json, timeout):
        seen["url"] = url
        seen["headers"] = headers
        seen["params"] = params
        return _Resp()

    monkeypatch.setattr(vision.httpx, "post", fake_post)
    result = vision.call(pkg, api_key="sk-test1234567890abcdef")
    assert result["ok"] is True
    assert "api.openai.com/v1/responses" in seen["url"]
    assert seen["headers"]["Authorization"].startswith("Bearer ")
    assert "主图文字" in result["text"]


def test_call_gemini_vision(monkeypatch, tmp_path):
    from ivyea_agent import vision

    img = tmp_path / "main.png"
    _png(img, 1200, 1200)
    pkg = vision.build("gemini", [str(tmp_path)], max_images=1)
    seen = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "图片合规。"}]}}]}

    def fake_post(url, headers, params, json, timeout):
        seen["url"] = url
        seen["params"] = params
        return _Resp()

    monkeypatch.setattr(vision.httpx, "post", fake_post)
    result = vision.call(pkg, api_key="gemini-key")
    assert result["ok"] is True
    assert ":generateContent" in seen["url"]
    assert seen["params"] == {"key": "gemini-key"}
    assert result["text"] == "图片合规。"


def test_image_vision_cli_call(monkeypatch, tmp_path, capsys):
    from ivyea_agent import vision
    from ivyea_agent.cli import main

    img = tmp_path / "main.png"
    _png(img, 1200, 1200)

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"output_text": "审核完成。"}

    monkeypatch.setattr(vision.httpx, "post", lambda *a, **k: _Resp())
    assert main([
        "image", "vision", str(tmp_path),
        "--provider", "openai",
        "--call",
        "--api-key", "sk-test1234567890abcdef",
    ]) == 0
    stdout = capsys.readouterr().out
    assert "多模态视觉审核结果" in stdout
    assert "审核完成" in stdout


# ── 视觉旁路（route_images）──

def _fake_pick():
    return {"cfg": {"kind": "openai", "provider_id": "openai", "model": "gpt-5-mini"},
            "key": "sk-x", "label": "OpenAI · gpt-5-mini"}


class _SeenProvider:
    """记录收到的 messages，返回固定分析文本。"""
    def __init__(self, reply="图里是一张销量趋势图，3月见顶。"):
        self.reply = reply
        self.seen = None

    def chat(self, messages, tools=None, **kw):
        self.seen = messages
        return {"content": self.reply, "tool_calls": []}


def test_route_images_passthrough_when_main_has_vision(tmp_path, monkeypatch):
    from ivyea_agent import vision
    img = tmp_path / "a.png"
    _png(img)
    mcfg = {"provider_id": "openai", "provider": "openai"}
    content, imgs = vision.route_images("看图", [str(img)], mcfg, lambda s: None)
    assert content == "看图" and imgs == [str(img)]     # 有视觉：原样透传，不剥图


def test_route_images_sidecar_injects_and_strips(tmp_path, monkeypatch, ivyea_home):
    from ivyea_agent import vision, providers
    img = tmp_path / "a.png"
    _png(img)
    prov = _SeenProvider()
    monkeypatch.setattr(vision, "pick_vision_model", _fake_pick)
    monkeypatch.setattr(providers, "from_settings", lambda cfg, key: prov)
    notes = []
    mcfg = {"provider_id": "deepseek", "provider": "deepseek"}   # 主脑无视觉
    content, imgs = vision.route_images("这图说明什么？", [str(img)], mcfg, notes.append)
    assert imgs == []                                   # 图已剥掉，主脑只见文本
    assert "销量趋势图" in content and "视觉模型" in content and "这图说明什么？" in content
    assert any("视觉旁路代读" in n for n in notes)
    # sidecar 收到的是多模态 content（含 image_url）且带用户问题原文
    user = prov.seen[-1]
    assert isinstance(user["content"], list)
    assert any(b.get("type") == "image_url" for b in user["content"])
    assert "这图说明什么" in user["content"][0]["text"]


def test_route_images_no_vision_provider_available(tmp_path, monkeypatch, ivyea_home):
    from ivyea_agent import vision
    img = tmp_path / "a.png"
    _png(img)
    monkeypatch.setattr(vision, "pick_vision_model", lambda: None)
    notes = []
    content, imgs = vision.route_images("看图", [str(img)], {"provider_id": "deepseek"}, notes.append)
    assert imgs == [] and content == "看图"             # 忽略图片继续文本
    assert any("没有可用的视觉模型" in n for n in notes)


def test_route_images_sidecar_error_fail_open(tmp_path, monkeypatch, ivyea_home):
    from ivyea_agent import vision
    img = tmp_path / "a.png"
    _png(img)
    monkeypatch.setattr(vision, "pick_vision_model", _fake_pick)
    monkeypatch.setattr(vision, "sidecar_describe", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api down")))
    notes = []
    content, imgs = vision.route_images("看图", [str(img)], {"provider_id": "deepseek"}, notes.append)
    assert imgs == [] and content == "看图"
    assert any("视觉旁路调用失败" in n for n in notes)


def test_pick_vision_model_prefers_config_key(monkeypatch, ivyea_home):
    from ivyea_agent import config, vision
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
    config.set_setting("vision_model", "gemini")
    got = vision.pick_vision_model()
    assert got and got["cfg"]["provider_id"] == "gemini" and got["key"] == "g-key"


def test_pick_vision_model_auto_detects_first_configured(monkeypatch, ivyea_home):
    from ivyea_agent import vision
    for env in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(env, raising=False)
    assert vision.pick_vision_model() is None           # 全未配 → 无可用
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
    got = vision.pick_vision_model()
    assert got and got["cfg"]["provider_id"] == "anthropic"
