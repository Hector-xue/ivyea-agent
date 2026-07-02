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
