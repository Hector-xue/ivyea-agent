from __future__ import annotations

import struct
import zlib


def _png(path, w=800, h=600):
    raw = b"\x00" + b"\xff\xff\xff" * w
    comp = zlib.compress(raw * h)

    def chunk(kind, data):
        return (
            struct.pack(">I", len(data)) + kind + data +
            struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
        )

    data = (
        b"\x89PNG\r\n\x1a\n" +
        chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
        chunk(b"IDAT", comp) +
        chunk(b"IEND", b"")
    )
    path.write_bytes(data)


def test_image_audit_scans_dimensions_and_prompt(tmp_path):
    from ivyea_agent import image_audit

    img = tmp_path / "main-hero.png"
    _png(img, 800, 600)
    res = image_audit.audit([str(tmp_path)])
    assert res["images"][0]["width"] == 800
    assert res["images"][0]["height"] == 600
    assert any(r["area"] == "resolution" for r in res["risks"])
    prompt = image_audit.multimodal_prompt(res, product_context="karaoke machine")
    assert "亚马逊 Listing 图片审核专家" in prompt
    assert "karaoke machine" in prompt


def test_image_cli_and_agent_tool(tmp_path, capsys):
    from ivyea_agent.agent_tools import TOOL_SCHEMAS, _DISPATCH, ToolContext
    from ivyea_agent.cli import main

    img = tmp_path / "feature.png"
    _png(img, 1200, 1200)
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert "run_image_audit" in names
    out = _DISPATCH["run_image_audit"]({"paths": [str(tmp_path)], "include_prompt": True}, ToolContext())
    assert "图片资产诊断" in out
    assert "多模态审核 Prompt" in out

    prompt_out = tmp_path / "prompt.md"
    assert main(["image", "audit", str(tmp_path), "--prompt", "--prompt-out", str(prompt_out)]) == 0
    cli_out = capsys.readouterr().out
    assert "已导出多模态审核 Prompt" in cli_out
    assert "feature.png" in prompt_out.read_text(encoding="utf-8")
