"""T3 本地视觉度量的测试。

合成图而不是抓真实素材：CV 指标要断言**具体数值区间**，只有自己画的图才知道
正确答案是多少。真实图只能断言"没炸"，那种测试防不住算错。
"""
from __future__ import annotations

import pytest

pytest.importorskip("PIL")


def _canvas(path, w, h, bg=(255, 255, 255), box=None, box_color=(20, 60, 160)):
    """画一张白底 + 居中色块的图。box 是 (x0,y0,x1,y1) 像素坐标。"""
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (w, h), bg)
    if box:
        ImageDraw.Draw(im).rectangle(box, fill=box_color)
    im.save(path)
    return path


def test_white_background_and_coverage(tmp_path):
    from ivyea_agent import local_vision

    # 1000×1000 白底，正中 500×500 色块 → 主体占比应为 25%
    p = _canvas(tmp_path / "main.png", 1000, 1000, box=(250, 250, 749, 749))
    res = local_vision.analyze([str(p)], with_ocr=False)

    assert res["available"] is True
    img = res["images"][0]
    assert img["is_white_background"] is True
    assert img["white_border_ratio"] == pytest.approx(1.0, abs=0.01)

    fg = img["foreground"]
    assert fg["coverage_ratio"] == pytest.approx(0.25, abs=0.02)
    assert fg["touches_edge"] is False
    # 正中放置 → 偏移应接近 0
    assert abs(fg["center_offset_x"]) < 0.02
    assert abs(fg["center_offset_y"]) < 0.02


def test_non_white_background_is_flagged(tmp_path):
    from ivyea_agent import local_vision

    p = _canvas(tmp_path / "main.png", 800, 800, bg=(240, 220, 200), box=(100, 100, 699, 699))
    res = local_vision.analyze([str(p)], with_ocr=False)
    img = res["images"][0]
    assert img["is_white_background"] is False
    # 命名带 main → 角色判定为主图 → 白底不合规必须报 high
    assert any(r["area"] == "main_white_bg" and r["level"] == "high" for r in res["risks"])


def test_off_center_and_edge_bleed(tmp_path):
    from ivyea_agent import local_vision

    # 色块顶到左上角 → 触边 + 明显偏心
    p = _canvas(tmp_path / "main.png", 1000, 1000, box=(0, 0, 399, 399))
    res = local_vision.analyze([str(p)], with_ocr=False)
    fg = res["images"][0]["foreground"]
    assert fg["touches_edge"] is True
    assert fg["center_offset_x"] < -0.4 and fg["center_offset_y"] < -0.4
    assert any(r["area"] == "main_bleed" for r in res["risks"])


def test_palette_finds_the_two_real_colours(tmp_path):
    from ivyea_agent import local_vision

    p = _canvas(tmp_path / "a.png", 600, 600, box=(0, 0, 299, 599), box_color=(200, 0, 0))
    res = local_vision.analyze([str(p)], with_ocr=False)
    palette = res["images"][0]["palette"]
    hexes = [c["hex"] for c in palette]
    # 半红半白：两个主色都得在色板里（k-means 会给出近似值，比对通道倾向）
    assert any(c["rgb"][0] > 150 and c["rgb"][1] < 80 for c in palette), hexes
    assert any(min(c["rgb"]) > 200 for c in palette), hexes
    assert sum(c["share"] for c in palette) == pytest.approx(1.0, abs=0.02)


def test_palette_survives_a_white_dominated_image(tmp_path):
    """白底主图（占比 ~88% 白）不许把色板塌成 "#FFFFFF 100%"。

    这是实打实踩过的坑：按亮度分位数播种 k-means，在极度偏斜的白底分布上会让
    全部质心落进白色簇，产品配色——色板唯一有价值的输出——直接消失。
    """
    from ivyea_agent import local_vision

    p = _canvas(tmp_path / "main.png", 1200, 1200, box=(450, 450, 749, 749),
                box_color=(28, 74, 140))
    res = local_vision.analyze([str(p)], with_ocr=False)
    palette = res["images"][0]["palette"]

    assert len(palette) >= 2, palette
    assert palette[0]["hex"] == "#FFFFFF"                       # 白底仍是第一主色
    # 产品蓝必须被单独识别出来，且色值贴近真值
    blue = [c for c in palette if c["rgb"][2] > c["rgb"][0] + 60]
    assert blue, palette
    assert abs(blue[0]["rgb"][0] - 28) < 12 and abs(blue[0]["rgb"][2] - 140) < 12


def test_alpha_channel_is_used_as_mask(tmp_path):
    """有 alpha 时用 alpha 当主体掩膜——那是设计稿导出的真边界，比颜色阈值准。"""
    from PIL import Image
    from ivyea_agent import local_vision

    im = Image.new("RGBA", (400, 400), (255, 255, 255, 0))
    im.paste((255, 255, 255, 255), (100, 100, 300, 300))   # 纯白但不透明的主体
    p = tmp_path / "cut.png"
    im.save(p)

    res = local_vision.analyze([str(p)], with_ocr=False)
    fg = res["images"][0]["foreground"]
    assert res["images"][0]["has_alpha"] is True
    assert fg["mask_source"] == "alpha"
    # 纯白主体在纯白背景上，颜色阈值会完全看不见它；alpha 掩膜必须给出 25%
    assert fg["coverage_ratio"] == pytest.approx(0.25, abs=0.03)


def test_duplicate_detection(tmp_path):
    from ivyea_agent import local_vision

    _canvas(tmp_path / "a.png", 800, 800, box=(200, 200, 599, 599))
    _canvas(tmp_path / "b.png", 800, 800, box=(200, 200, 599, 599))
    _canvas(tmp_path / "c.png", 800, 800, box=(0, 0, 199, 199))
    res = local_vision.analyze([str(tmp_path)], with_ocr=False)
    pairs = {tuple(sorted((d["a"], d["b"]))) for d in res["duplicates"]}
    assert ("a.png", "b.png") in pairs
    assert not any("c.png" in p for p in pairs)


def test_render_for_text_model_states_its_limits(tmp_path):
    """喂给纯文本主脑的那段话必须自带约束，否则模型会照着读数编画面。"""
    from ivyea_agent import local_vision

    p = _canvas(tmp_path / "main.png", 1500, 1500, box=(300, 300, 1199, 1199))
    res = local_vision.analyze([str(p)], with_ocr=False)
    text = local_vision.render_for_text_model(res, question="主图合规吗？")

    assert "主图合规吗？" in text
    assert "禁止" in text and "编造" in text
    assert "需要视觉模型才能判断" in text
    assert "1500×1500" in text
    assert "主体占画面" in text


def test_layout_from_ocr_boxes():
    from ivyea_agent import local_vision

    boxes = [
        {"bbox": [10, 10, 500, 80]},      # 顶部横幅
        {"bbox": [20, 20, 400, 60]},
    ]
    layout = local_vision._layout_from_ocr(boxes, 1000, 1000)
    assert layout["dominant_band"] == "top"
    assert layout["text_area_ratio"] > 0

    # 没有坐标就不许推版面——tesseract 分支给的就是空 boxes
    assert local_vision._layout_from_ocr([], 1000, 1000) == {}


def test_image_local_cli(tmp_path, capsys):
    """`ivyea image local` —— 让用户能直接核 T3 到底读到了什么。

    没有这个出口时本地视觉是个只在带图对话里间接生效的黑盒，降级结果对不对
    用户没法自己验。
    """
    from ivyea_agent.cli import main

    _canvas(tmp_path / "main.png", 1200, 1200, box=(300, 300, 899, 899))
    assert main(["image", "local", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "本地视觉度量" in out
    assert "1200×1200" in out
    assert "主体占比" in out

    assert main(["image", "local", str(tmp_path), "--prompt", "--context", "主图合规吗"]) == 0
    out2 = capsys.readouterr().out
    assert "注入主脑的文本" in out2
    assert "主图合规吗" in out2
    assert "禁止" in out2                      # 约束段必须跟着一起出去


def test_broken_file_does_not_kill_the_batch(tmp_path):
    from ivyea_agent import local_vision

    _canvas(tmp_path / "good.png", 600, 600, box=(100, 100, 499, 499))
    (tmp_path / "bad.png").write_bytes(b"\x89PNG\r\n\x1a\nnot-a-real-png")
    res = local_vision.analyze([str(tmp_path)], with_ocr=False)
    names = {i["name"]: i for i in res["images"]}
    assert "error" in names["bad.png"]
    assert names["good.png"]["foreground"]["coverage_ratio"] > 0
