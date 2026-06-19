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
