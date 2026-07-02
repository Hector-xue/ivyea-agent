"""@文件引用展开（mentions.expand）+ @路径补全。"""
from __future__ import annotations

from ivyea_agent import mentions


def test_text_file_inlined(tmp_path):
    (tmp_path / "foo.py").write_text("print('hi')\n# 关键实现\n", encoding="utf-8")
    txt, imgs = mentions.expand("解释 @foo.py 这个文件", str(tmp_path))
    assert "@foo.py" in txt                       # 正文保留引用
    assert "[文件 foo.py]" in txt and "关键实现" in txt  # 内容内联到末尾
    assert imgs == []


def test_missing_path_left_literal(tmp_path):
    txt, imgs = mentions.expand("看看 @nope.py 和 @user 提及", str(tmp_path))
    assert "@nope.py" in txt and "[文件" not in txt
    assert imgs == []


def test_image_default_literal_but_collected_with_flag(tmp_path):
    (tmp_path / "a.png").write_bytes(b"\x89PNG\r\n")
    t1, i1 = mentions.expand("@a.png 这是啥", str(tmp_path))
    assert i1 == [] and "@a.png" in t1            # 默认不接多模态 → 原样
    t2, i2 = mentions.expand("@a.png 这是啥", str(tmp_path), with_images=True)
    assert len(i2) == 1 and i2[0].endswith("a.png") and "[图片 a.png]" in t2


def test_truncates_huge_file(tmp_path):
    big = "\n".join(f"line{i}" for i in range(5000))
    (tmp_path / "big.txt").write_text(big, encoding="utf-8")
    txt, _ = mentions.expand("@big.txt", str(tmp_path))
    assert "已截断" in txt and "共 5000 行" in txt


def _tiny_png(p):
    import base64
    p.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="))


def test_build_user_content_multimodal(tmp_path):
    png = tmp_path / "x.png"
    _tiny_png(png)
    assert mentions.build_user_content("hi", []) == "hi"          # 无图=纯文本
    c = mentions.build_user_content("这是什么", [str(png)])
    assert isinstance(c, list) and c[0]["type"] == "text"
    assert any(p.get("type") == "image_url" and p["image_url"]["url"].startswith("data:image/png;base64,")
               for p in c)


def test_providers_accept_multimodal_content(tmp_path):
    png = tmp_path / "x.png"
    _tiny_png(png)
    content = mentions.build_user_content("这是什么", [str(png)])
    msgs = [{"role": "user", "content": content}]
    # codex → input_image
    from ivyea_agent.providers.codex_provider import CodexProvider
    _, items = CodexProvider.__new__(CodexProvider)._input(msgs)
    assert any(c.get("type") == "input_image" for it in items for c in it.get("content", []))
    # anthropic → image 块
    from ivyea_agent.providers.anthropic_provider import _split_messages
    _, out = _split_messages(msgs)
    assert any(b.get("type") == "image" for m in out for b in (m.get("content") or []) if isinstance(b, dict))
    # gemini → inlineData
    from ivyea_agent.providers.gemini_provider import _messages_to_gemini
    _, contents = _messages_to_gemini(msgs)
    assert any("inlineData" in p for c in contents for p in c.get("parts", []))


def test_at_path_completion(tmp_path):
    (tmp_path / "alpha.py").write_text("x", encoding="utf-8")
    (tmp_path / "beta.txt").write_text("y", encoding="utf-8")
    import os
    from ivyea_agent import chat_input
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        got = sorted(c.text for c in chat_input._at_completions("al"))
    finally:
        os.chdir(cwd)
    assert "alpha.py" in got
