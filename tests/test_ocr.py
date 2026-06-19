from __future__ import annotations

import subprocess

from tests.test_image_audit import _png


def test_ocr_unavailable(tmp_path, monkeypatch):
    from ivyea_agent import ocr

    img = tmp_path / "main.png"
    _png(img, 1200, 1200)
    monkeypatch.setattr("shutil.which", lambda name: None)
    res = ocr.run([str(tmp_path)])
    assert res["available"] is False
    assert res["images"]
    text = ocr.render(res)
    assert "OCR 不可用" in text


def test_ocr_available_and_agent_cli(tmp_path, monkeypatch, capsys):
    from ivyea_agent.agent_tools import TOOL_SCHEMAS, _DISPATCH, ToolContext
    from ivyea_agent.cli import main

    img = tmp_path / "feature.png"
    _png(img, 1200, 1200)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/tesseract")

    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="tesseract 5.0\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="Waterproof karaoke machine\n")

    monkeypatch.setattr("subprocess.run", fake_run)
    names = {t["function"]["name"] for t in TOOL_SCHEMAS}
    assert "run_image_ocr" in names
    out = _DISPATCH["run_image_ocr"]({"paths": [str(tmp_path)], "lang": "eng"}, ToolContext())
    assert "Waterproof karaoke machine" in out

    assert main(["image", "ocr", str(tmp_path), "--lang", "eng"]) == 0
    cli_out = capsys.readouterr().out
    assert "图片 OCR" in cli_out
    assert "Waterproof karaoke machine" in cli_out
