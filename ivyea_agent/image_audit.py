"""Local image asset audit and multimodal prompt package."""
from __future__ import annotations

import base64
import struct
from pathlib import Path
from typing import Any

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def _png_size(data: bytes) -> tuple[int, int] | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    return None


def _gif_size(data: bytes) -> tuple[int, int] | None:
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    if data[12:16] == b"VP8X" and len(data) >= 30:
        w = 1 + int.from_bytes(data[24:27], "little")
        h = 1 + int.from_bytes(data[27:30], "little")
        return w, h
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    if not data.startswith(b"\xff\xd8"):
        return None
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        i += 2
        if marker in (0xD8, 0xD9):
            continue
        if i + 2 > len(data):
            return None
        length = int.from_bytes(data[i:i + 2], "big")
        if length < 2 or i + length > len(data):
            return None
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h = int.from_bytes(data[i + 3:i + 5], "big")
            w = int.from_bytes(data[i + 5:i + 7], "big")
            return w, h
        i += length
    return None


def image_size(path: str | Path) -> tuple[int, int] | None:
    data = Path(path).read_bytes()[:2_000_000]
    for fn in (_png_size, _jpeg_size, _gif_size, _webp_size):
        size = fn(data)
        if size:
            return size
    return None


def _role_from_name(name: str) -> str:
    low = name.lower()
    if any(k in low for k in ("main", "hero", "主图", "1")):
        return "main"
    if any(k in low for k in ("size", "dimension", "尺寸")):
        return "size"
    if any(k in low for k in ("scene", "lifestyle", "场景")):
        return "lifestyle"
    if any(k in low for k in ("compare", "comparison", "对比")):
        return "comparison"
    if any(k in low for k in ("feature", "卖点", "功能")):
        return "feature"
    return "unknown"


def scan(paths: list[str], *, recursive: bool = True) -> list[dict[str, Any]]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            pattern = "**/*" if recursive else "*"
            files.extend([c for c in p.glob(pattern) if c.suffix.lower() in IMAGE_EXTS and c.is_file()])
        elif p.is_file() and p.suffix.lower() in IMAGE_EXTS:
            files.append(p)
    out = []
    seen = set()
    for p in sorted(files):
        rp = str(p.resolve())
        if rp in seen:
            continue
        seen.add(rp)
        size = image_size(p)
        st = p.stat()
        w, h = size or (0, 0)
        out.append({
            "path": rp,
            "name": p.name,
            "ext": p.suffix.lower(),
            "bytes": st.st_size,
            "width": w,
            "height": h,
            "aspect": round(w / h, 3) if w and h else None,
            "role": _role_from_name(p.name),
        })
    return out


def audit(paths: list[str], *, recursive: bool = True) -> dict[str, Any]:
    images = scan(paths, recursive=recursive)
    risks: list[dict[str, str]] = []
    if not images:
        risks.append({"level": "high", "area": "assets", "reason": "未找到图片资产。"})
    if images and not any(i["role"] == "main" for i in images):
        risks.append({"level": "medium", "area": "main_image", "reason": "未识别到主图命名，建议明确 main/hero/主图。"})
    if len(images) < 6:
        risks.append({"level": "medium", "area": "image_count", "reason": f"仅 {len(images)} 张图，可能不足以覆盖卖点/尺寸/场景/对比。"})
    for img in images:
        if not img["width"] or not img["height"]:
            risks.append({"level": "medium", "area": "metadata", "reason": f"{img['name']} 无法读取尺寸。"})
            continue
        if img["width"] < 1000 or img["height"] < 1000:
            risks.append({"level": "medium", "area": "resolution", "reason": f"{img['name']} 分辨率 {img['width']}x{img['height']} 偏低。"})
        if img["role"] == "main" and abs((img["aspect"] or 1) - 1) > 0.08:
            risks.append({"level": "medium", "area": "main_ratio", "reason": f"{img['name']} 主图比例 {img['aspect']} 非接近 1:1。"})
        if img["bytes"] > 8 * 1024 * 1024:
            risks.append({"level": "info", "area": "file_size", "reason": f"{img['name']} 文件超过 8MB，上传/分析成本较高。"})

    roles = {i["role"] for i in images}
    missing_roles = [r for r in ("size", "lifestyle", "feature") if r not in roles]
    tasks = [{"type": "missing_role", "action": f"补充或重命名 {r} 类型图片，便于模型和人工审核。"} for r in missing_roles]
    return {"images": images, "risks": risks, "tasks": tasks}


def multimodal_prompt(result: dict[str, Any], *, product_context: str = "") -> str:
    lines = [
        "你是亚马逊 Listing 图片审核专家。请基于上传图片和下面资产清单做诊断。",
        "",
        "要求：",
        "- 区分事实观察和推断，不要编造图片中不存在的文字/功能。",
        "- 检查主图合规感、卖点表达、尺寸/配件/场景/对比图是否完整。",
        "- 找出会影响 CTR/CVR 的视觉问题，并给出可执行修改任务。",
        "- 如果图片中文字看不清，明确说需要 OCR/高清图，不要猜。",
        "",
    ]
    if product_context:
        lines.extend(["产品/广告上下文：", product_context, ""])
    lines.append("本地资产清单：")
    for img in result.get("images", []):
        lines.append(f"- {img['name']} role={img['role']} size={img['width']}x{img['height']} bytes={img['bytes']}")
    if result.get("risks"):
        lines.append("")
        lines.append("本地预检查风险：")
        for r in result["risks"]:
            lines.append(f"- [{r['level']}] {r['area']}: {r['reason']}")
    return "\n".join(lines)


def render(result: dict[str, Any]) -> str:
    lines = ["# 图片资产诊断", ""]
    lines.append("## 图片清单")
    if not result["images"]:
        lines.append("（无）")
    else:
        for img in result["images"]:
            lines.append(f"- {img['name']} · {img['width']}x{img['height']} · {img['role']} · {img['bytes']} bytes")
    lines.append("")
    lines.append("## 本地风险")
    if not result["risks"]:
        lines.append("（未发现明显本地资产风险）")
    else:
        for r in result["risks"]:
            lines.append(f"- [{r['level']}] {r['area']}: {r['reason']}")
    lines.append("")
    lines.append("## 任务")
    if not result["tasks"]:
        lines.append("（暂无明确任务）")
    else:
        for t in result["tasks"]:
            lines.append(f"- {t['type']}: {t['action']}")
    return "\n".join(lines) + "\n"


def data_url(path: str | Path, max_bytes: int = 4_000_000) -> str:
    p = Path(path)
    data = p.read_bytes()
    if len(data) > max_bytes:
        raise ValueError(f"图片过大：{p} {len(data)} bytes > {max_bytes}")
    mime = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }.get(p.suffix.lower(), "application/octet-stream")
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
