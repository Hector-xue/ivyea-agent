"""@文件引用：把用户输入里的 @path 展开成内联文件内容（文本）或图片附件（多模态）。

对标 Claude Code / Codex 的 @文件：`@src/app.py 解释一下` 会把文件内容内联给模型，
省去模型再调 read_file。返回 (处理后的文本, 图片路径列表)——图片走多模态附件（Phase 3）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# 图片后缀走多模态附件（不内联文本）；其余按文本文件内联
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_MAX_LINES = 2000                                  # 单文件内联行数上限，防塞爆上下文
_MENTION_RE = re.compile(r"(?<!\S)@([^\s@]+)")     # 行首或空白后的 @路径


def _resolve(cwd: str, raw: str) -> Path:
    p = Path(os.path.expanduser(raw))
    return p if p.is_absolute() else Path(cwd) / p


def expand(line: str, cwd: str, *, with_images: bool = False) -> tuple[str, list[str]]:
    """展开 @path 引用：
    - 存在的文本文件 → 正文保留 `@path`，文件内容附到末尾（超 2000 行截断）。
    - 图片文件 → with_images 时收进 images 列表（多模态）；否则原样保留。
    - 路径不存在 → 原样保留 @path（可能只是普通 @提及）。
    返回 (处理后的文本, 图片路径列表)。
    """
    images: list[str] = []
    inlined: list[str] = []

    def _sub(m: re.Match) -> str:
        raw = m.group(1).rstrip(".,;:!?)），。；：！？")   # 容忍句末标点
        if not raw:
            return m.group(0)
        p = _resolve(cwd, raw)
        if not p.is_file():
            return m.group(0)                              # 不存在 → 原样
        if p.suffix.lower() in IMAGE_EXTS:
            if with_images:
                images.append(str(p))
                return f"[图片 {raw}]"
            return m.group(0)                              # 未接多模态 → 原样
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return m.group(0)
        lines = text.splitlines()
        if len(lines) > _MAX_LINES:
            text = "\n".join(lines[:_MAX_LINES]) + f"\n…（已截断，文件共 {len(lines)} 行）"
        inlined.append(f"[文件 {raw}]\n{text}")
        return f"@{raw}"                                   # 正文保留引用，内容附末尾

    processed = _MENTION_RE.sub(_sub, line)
    if inlined:
        processed = processed + "\n\n" + "\n\n".join(inlined)
    return processed, images


def build_user_content(text: str, image_paths: list[str]):
    """构造一条 user 消息的 content：无图返回纯文本 str；有图返回 OpenAI 多模态
    list-content（[{type:text},{type:image_url,image_url:{url:data:...}}]）。
    各 provider 适配器把 image_url 转成自身格式（codex/anthropic/gemini），
    不支持多模态的 provider 会安全拍平只取文本。"""
    if not image_paths:
        return text
    from . import image_audit
    parts: list = [{"type": "text", "text": text}]
    for p in image_paths:
        try:
            parts.append({"type": "image_url", "image_url": {"url": image_audit.data_url(p)}})
        except Exception:
            pass   # 单张编码失败(过大/损坏)跳过，不阻塞整轮
    return parts
