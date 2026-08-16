"""T3 本地视觉：没有任何视觉模型时，把"看图"降级成"量图"。

**为什么要有它**：主脑常年是 DeepSeek 这类纯文本模型，用户又不一定配得起第二个
视觉模型。此前这种情况下带图请求直接 `main_brain_no_vision` 报错，Listing 的
图片分析整块跳过——功能不是降级，是死掉。

**思路**（方案 D）：视觉识别的语义部分（"这是个什么产品"）确实非多模态模型不可，
但 Listing 真正在用的视觉判断里有很大一块是**可测量的**：主图白底合规、产品占比、
比例、分辨率、配色、文字覆盖率、图上写了什么字。这些用确定性 CV + OCR 就能拿到
硬数字，渲染成结构化描述文本后，纯文本主脑照样能做复核、判合规、写修改建议。

于是这里只做两件事：`analyze()` 出指标，`render_for_text_model()` 把指标写成给
文本模型读的段落。**不做**语义推断——不猜产品品类、不编版式意图，那是 T1/T2 的活。

依赖只有 Pillow + numpy（numpy 本来就是硬依赖）。OCR 走 `ocr.py` 的引擎调度，
拿不到 OCR 也能出全部几何/色彩指标，绝不因为缺 OCR 就整体失败。
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from . import image_audit

# 判"接近纯白"的阈值。亚马逊主图要求纯白 RGB(255,255,255)，但 JPEG 压缩和
# 相机白平衡会让实际像素落在 250 上下，卡死 255 会把合规图全判成不合规。
WHITE_MIN = 246
# 主体掩膜：与背景色的通道差超过这个值才算前景。太小会把 JPEG 噪点当主体
# （实测纯白底图会"检出"占比 3% 的假主体），太大会吃掉浅色产品的边缘。
FOREGROUND_DELTA = 18
# 分析前统一缩到这个长边。指标全是比例量，缩图不影响结论，但能把 4000px 大图的
# 耗时从秒级压到毫秒级——套图动辄七八张，这个差别是用户能感知到的。
WORK_SIZE = 512
# 文字热区网格。8×8 在 512px 工作图上每格 64px，正好是电商图一行标题的量级。
TEXT_GRID = 8


def available() -> tuple[bool, str]:
    """本地 CV 能不能跑。缺 Pillow 时明确说，不要在调用点炸出 ImportError。"""
    try:
        from PIL import Image  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, f"缺少 Pillow，本地视觉不可用：{e}"
    try:
        import numpy  # noqa: F401
    except Exception as e:  # noqa: BLE001
        return False, f"缺少 numpy，本地视觉不可用：{e}"
    return True, "Pillow + numpy"


def _load_rgb(path: str):
    """读成 RGB 工作图 + alpha 通道（没有 alpha 回 None）。

    统一转 RGB 是必须的：调色板 PNG（mode="P"）和灰度图直接取 array 会是 2 维，
    后面所有按 (h, w, 3) 写的运算会静默错位。
    """
    from PIL import Image
    import numpy as np

    with Image.open(path) as im:
        im.load()
        alpha = None
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            rgba = im.convert("RGBA")
            rgba.thumbnail((WORK_SIZE, WORK_SIZE), Image.LANCZOS)
            arr = np.asarray(rgba, dtype=np.uint8)
            return arr[:, :, :3].copy(), arr[:, :, 3].copy()
        rgb = im.convert("RGB")
        rgb.thumbnail((WORK_SIZE, WORK_SIZE), Image.LANCZOS)
        return np.asarray(rgb, dtype=np.uint8).copy(), alpha


def _border_stats(rgb) -> dict[str, Any]:
    """边框采样：判背景色和白底合规。

    只采**边框带**而不是四个角：角点会被圆角/水印/角标骗过去，而边框带对
    "产品顶到边"这种真问题反而更敏感。
    """
    import numpy as np

    h, w = rgb.shape[:2]
    band = max(2, min(h, w) // 25)
    strips = [rgb[:band, :, :], rgb[-band:, :, :], rgb[:, :band, :], rgb[:, -band:, :]]
    border = np.concatenate([s.reshape(-1, 3) for s in strips], axis=0)
    white_mask = np.all(border >= WHITE_MIN, axis=1)
    return {
        "border_mean_rgb": [int(v) for v in border.mean(axis=0).round()],
        "border_median_rgb": [int(v) for v in np.median(border, axis=0).round()],
        "white_border_ratio": round(float(white_mask.mean()), 4),
    }


def _foreground(rgb, alpha, bg_rgb) -> dict[str, Any]:
    """主体掩膜 → bbox / 占比 / 居中偏移。

    有 alpha 通道时直接用它当掩膜——那是设计稿导出的**真**主体边界，比任何颜色
    阈值都准。没有才退回"与背景色的差异"。
    """
    import numpy as np

    h, w = rgb.shape[:2]
    if alpha is not None and alpha.min() < 250:
        mask = alpha > 32
        source = "alpha"
    else:
        diff = np.abs(rgb.astype(np.int16) - np.asarray(bg_rgb, dtype=np.int16))
        mask = diff.max(axis=2) > FOREGROUND_DELTA
        source = "color_delta"

    coverage = float(mask.mean())
    if not mask.any():
        return {"mask_source": source, "coverage_ratio": 0.0, "bbox": None,
                "center_offset_x": None, "center_offset_y": None, "touches_edge": False}

    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    top, bottom = int(rows[0]), int(rows[-1])
    left, right = int(cols[0]), int(cols[-1])
    cx = (left + right) / 2.0
    cy = (top + bottom) / 2.0
    return {
        "mask_source": source,
        # 占比按**掩膜像素数**算，不是 bbox 面积——L 形/镂空产品的 bbox 会
        # 严重高估占比，而卖家真正关心的是"画面被产品填了多少"。
        "coverage_ratio": round(coverage, 4),
        "bbox_ratio": [round(left / w, 4), round(top / h, 4),
                       round((right + 1) / w, 4), round((bottom + 1) / h, 4)],
        "bbox_area_ratio": round(((right - left + 1) * (bottom - top + 1)) / float(w * h), 4),
        "center_offset_x": round((cx - w / 2.0) / (w / 2.0), 4),
        "center_offset_y": round((cy - h / 2.0) / (h / 2.0), 4),
        "touches_edge": bool(top == 0 or left == 0 or bottom == h - 1 or right == w - 1),
    }


def _palette(rgb, k: int = 5, iters: int = 12) -> list[dict[str, Any]]:
    """主色板：numpy 手写 k-means。

    不引 sklearn/scipy——为了 5 个色号拖一个几十 MB 的科学计算栈不划算，而
    k-means 在 512px 缩图上就是十几行矩阵运算。

    **播种用确定性最远点（k-means++ 的去随机版），不能用亮度分位数**：电商图
    绝大多数是白底，像素分布极度偏斜，按亮度分位取初始质心会让 5 个质心全落在
    白色附近，最后塌成 "#FFFFFF 100%" 一个色——实测就是这么塌的，而产品配色
    恰恰是这个色板唯一有价值的输出。最远点播种保证每个质心落在不同色簇上，
    且从全局均值起步，同一张图每次跑给同一组色号（prompt 不能每次都抖）。
    """
    import numpy as np

    pixels = rgb.reshape(-1, 3).astype(np.float32)
    if len(pixels) > 20000:
        pixels = pixels[:: max(1, len(pixels) // 20000)]
    if len(pixels) < k:
        k = max(1, len(pixels))

    first = pixels[np.argmin(((pixels - pixels.mean(axis=0)) ** 2).sum(axis=1))]
    centers = [first]
    dist = ((pixels - first) ** 2).sum(axis=1)
    for _ in range(k - 1):
        nxt = pixels[int(dist.argmax())]
        centers.append(nxt)
        dist = np.minimum(dist, ((pixels - nxt) ** 2).sum(axis=1))
    centers = np.stack(centers)

    labels = np.zeros(len(pixels), dtype=np.int64)
    for _ in range(iters):
        d = ((pixels[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        new_labels = d.argmin(axis=1)
        if np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for i in range(k):
            hit = pixels[labels == i]
            if len(hit):
                centers[i] = hit.mean(axis=0)

    out = []
    for i in range(k):
        share = float((labels == i).mean())
        if share <= 0:
            continue
        r, g, b = (int(round(v)) for v in centers[i])
        out.append({"hex": f"#{r:02X}{g:02X}{b:02X}", "rgb": [r, g, b], "share": round(share, 4)})
    return sorted(out, key=lambda c: c["share"], reverse=True)


def _text_density(rgb) -> dict[str, Any]:
    """文字/图形密集区估计：Sobel 梯度能量分块统计。

    这是**估计**不是识别——高频区可能是文字，也可能是产品纹理，所以对外的字段名
    和渲染文案都要说"高频区/疑似文字"，不能说"文字覆盖 X%"。真要读字看 OCR 那段。
    """
    import numpy as np

    gray = rgb.astype(np.float32) @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    gx = np.abs(np.diff(gray, axis=1, prepend=gray[:, :1]))
    gy = np.abs(np.diff(gray, axis=0, prepend=gray[:1, :]))
    energy = gx + gy
    thresh = max(24.0, float(np.percentile(energy, 92)))
    busy = energy > thresh

    h, w = busy.shape
    grid = []
    for r in range(TEXT_GRID):
        row = []
        for c in range(TEXT_GRID):
            cell = busy[r * h // TEXT_GRID:(r + 1) * h // TEXT_GRID,
                        c * w // TEXT_GRID:(c + 1) * w // TEXT_GRID]
            row.append(round(float(cell.mean()), 3) if cell.size else 0.0)
        grid.append(row)
    return {"busy_ratio": round(float(busy.mean()), 4), "grid": grid}


def _dhash(rgb) -> str:
    """感知哈希（dHash 64bit），用于整套图查重 / 与竞品图比相似度。"""
    from PIL import Image
    import numpy as np

    img = Image.fromarray(rgb).convert("L").resize((9, 8), Image.LANCZOS)
    arr = np.asarray(img, dtype=np.int16)
    bits = (arr[:, 1:] > arr[:, :-1]).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def hamming(a: str, b: str) -> int:
    """两个 dhash 的汉明距离。<=5 基本可以认为是同一张图的不同压缩/缩放版本。"""
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1")
    except (TypeError, ValueError):
        return 64


def _layout_from_ocr(boxes: list[dict[str, Any]], w: int, h: int) -> dict[str, Any]:
    """OCR 文本块坐标 → 版面骨架（上/中/下 × 左/中/右 的文字分布）。

    只有 OCR 给出**带坐标**的结果时才有这一段；tesseract 的纯文本模式没有坐标，
    这时返回空，渲染时就不提版面，别拿没有的东西编。
    """
    if not boxes or not w or not h:
        return {}
    bands = {"top": 0.0, "middle": 0.0, "bottom": 0.0}
    cols = {"left": 0.0, "center": 0.0, "right": 0.0}
    for box in boxes:
        x0, y0, x1, y1 = box.get("bbox") or (0, 0, 0, 0)
        area = max(0.0, (x1 - x0)) * max(0.0, (y1 - y0)) / float(w * h)
        cy = (y0 + y1) / 2.0 / h
        cx = (x0 + x1) / 2.0 / w
        bands["top" if cy < 1 / 3 else "middle" if cy < 2 / 3 else "bottom"] += area
        cols["left" if cx < 1 / 3 else "center" if cx < 2 / 3 else "right"] += area
    total = sum(bands.values())
    return {
        "text_area_ratio": round(total, 4),
        "vertical": {k: round(v, 4) for k, v in bands.items()},
        "horizontal": {k: round(v, 4) for k, v in cols.items()},
        "dominant_band": max(bands, key=lambda k: bands[k]) if total > 0 else "",
    }


def analyze_one(path: str, meta: dict[str, Any], ocr_row: dict[str, Any] | None = None) -> dict[str, Any]:
    """单张图的全部本地指标。任何一步失败都只影响该图，不炸整批。"""
    out: dict[str, Any] = {
        "path": path,
        "name": meta.get("name") or Path(path).name,
        "width": meta.get("width") or 0,
        "height": meta.get("height") or 0,
        "aspect": meta.get("aspect"),
        "bytes": meta.get("bytes") or 0,
        "role": meta.get("role") or "unknown",
    }
    try:
        rgb, alpha = _load_rgb(path)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"读取失败：{e}"
        return out

    border = _border_stats(rgb)
    out.update(border)
    out["has_alpha"] = alpha is not None
    out["is_white_background"] = border["white_border_ratio"] >= 0.9
    out["foreground"] = _foreground(rgb, alpha, border["border_median_rgb"])
    out["palette"] = _palette(rgb)
    out["texture"] = _text_density(rgb)
    out["dhash"] = _dhash(rgb)

    if ocr_row and ocr_row.get("ok"):
        out["ocr_text"] = ocr_row.get("text") or ""
        boxes = ocr_row.get("boxes") or []
        if boxes:
            layout = _layout_from_ocr(boxes, ocr_row.get("image_width") or 0,
                                      ocr_row.get("image_height") or 0)
            if layout:
                out["layout"] = layout
    return out


def analyze(paths: list[str], *, recursive: bool = True, with_ocr: bool = True,
            max_images: int = 12) -> dict[str, Any]:
    """整批分析。返回 {available, engine, ocr_engine, images, duplicates, risks}。"""
    ok, detail = available()
    metas = image_audit.scan(paths, recursive=recursive)[:max_images]
    if not ok:
        return {"available": False, "engine": detail, "ocr_engine": "",
                "images": [], "duplicates": [], "risks": []}

    ocr_rows: dict[str, dict[str, Any]] = {}
    ocr_engine = ""
    if with_ocr and metas:
        from . import ocr as ocr_mod
        try:
            got = ocr_mod.run([m["path"] for m in metas], recursive=False)
            ocr_engine = str(got.get("engine") or "")
            for row in got.get("results") or []:
                ocr_rows[str(row.get("path"))] = row
        except Exception:  # noqa: BLE001 — OCR 是增强项，坏了不许拖垮几何指标
            ocr_engine = ""

    images = [analyze_one(m["path"], m, ocr_rows.get(m["path"])) for m in metas]

    # 套图查重：卖家常把同一张图重复上传或只改了尺寸，这在本地就能查出来。
    duplicates = []
    for i in range(len(images)):
        for j in range(i + 1, len(images)):
            a, b = images[i].get("dhash"), images[j].get("dhash")
            if not a or not b:
                continue
            dist = hamming(a, b)
            if dist <= 5:
                duplicates.append({"a": images[i]["name"], "b": images[j]["name"], "distance": dist})

    return {
        "available": True,
        "engine": detail,
        "ocr_engine": ocr_engine,
        "images": images,
        "duplicates": duplicates,
        "risks": _risks(images, duplicates),
    }


def _risks(images: list[dict[str, Any]], duplicates: list[dict[str, Any]]) -> list[dict[str, str]]:
    """只报**度量能证实**的问题。审美、卖点、版式意图一概不碰。"""
    risks: list[dict[str, str]] = []
    for img in images:
        name = img.get("name", "?")
        if img.get("error"):
            risks.append({"level": "medium", "area": "read", "reason": f"{name} {img['error']}"})
            continue
        w, h = img.get("width") or 0, img.get("height") or 0
        if w and h and (w < 1000 or h < 1000):
            risks.append({"level": "medium", "area": "resolution",
                          "reason": f"{name} {w}x{h} 低于亚马逊建议的 1000px 缩放门槛。"})
        fg = img.get("foreground") or {}
        cov = fg.get("coverage_ratio")
        if img.get("role") == "main":
            if not img.get("is_white_background"):
                risks.append({"level": "high", "area": "main_white_bg",
                              "reason": f"{name} 边框白底占比仅 {img.get('white_border_ratio')}，主图白底可能不合规。"})
            if isinstance(cov, float) and cov < 0.6:
                risks.append({"level": "medium", "area": "main_fill",
                              "reason": f"{name} 主体占比 {cov:.0%}，低于主图建议的 85%，产品在缩略图里会偏小。"})
        if fg.get("touches_edge") and img.get("role") == "main":
            risks.append({"level": "medium", "area": "main_bleed",
                          "reason": f"{name} 主体触到画布边缘，主图要求四周留白。"})
        offx, offy = fg.get("center_offset_x"), fg.get("center_offset_y")
        if isinstance(offx, float) and isinstance(offy, float) and math.hypot(offx, offy) > 0.25:
            risks.append({"level": "info", "area": "centering",
                          "reason": f"{name} 主体明显偏心（x={offx:+.2f} y={offy:+.2f}）。"})
        if (img.get("bytes") or 0) > 8 * 1024 * 1024:
            risks.append({"level": "info", "area": "file_size", "reason": f"{name} 超过 8MB。"})
    for dup in duplicates:
        risks.append({"level": "medium", "area": "duplicate",
                      "reason": f"{dup['a']} 与 {dup['b']} 视觉上几乎相同（汉明距离 {dup['distance']}），套图存在重复位。"})
    return risks


def render_for_text_model(result: dict[str, Any], *, question: str = "") -> str:
    """把指标写成给**纯文本主脑**读的段落——方案 D 的落点。

    两条硬约束写进正文，因为这是本地视觉最容易被误用的地方：
      1. 明说这些是仪器读数不是"看到的画面"，禁止据此编造画面内容；
      2. 明说哪些问题它答不了，让模型主动说"需要视觉模型"而不是硬答。
    """
    images = result.get("images") or []
    lines = [
        "[本地视觉度量：当前没有可用的视觉模型，图片由本地 CV + OCR 量化后以文本提供]",
        "",
        "重要约束：以下是对图片的**客观测量读数**，不是对画面内容的描述。",
        "- 只能依据这些读数和 OCR 文本作判断，**禁止**据此想象或编造画面里有什么物体、场景、人物。",
        "- 读数答不了的问题（产品是什么、版式意图、审美好坏、有没有穿帮），"
        "必须直说「这需要视觉模型才能判断」，不要猜。",
        "- 「高频区占比」是纹理/文字的粗略估计，不等于文字面积；要引用文字请用 OCR 文本。",
    ]
    if question:
        lines += ["", f"用户的问题：{question}"]

    lines += ["", f"图片数：{len(images)}　OCR 引擎：{result.get('ocr_engine') or '无（本次未做文字识别）'}"]

    for idx, img in enumerate(images, 1):
        lines += ["", f"## 图 {idx}：{img.get('name')}（角色推断：{img.get('role')}）"]
        if img.get("error"):
            lines.append(f"- 读取失败：{img['error']}")
            continue
        lines.append(f"- 尺寸 {img.get('width')}×{img.get('height')}，比例 {img.get('aspect')}，"
                     f"{round((img.get('bytes') or 0) / 1024)}KB"
                     + ("，含透明通道" if img.get("has_alpha") else ""))
        lines.append(f"- 边框白底占比 {img.get('white_border_ratio')}"
                     f"（判定：{'接近纯白底' if img.get('is_white_background') else '非白底'}），"
                     f"背景中位色 RGB{img.get('border_median_rgb')}")
        fg = img.get("foreground") or {}
        if fg.get("coverage_ratio") is not None:
            lines.append(
                f"- 主体占画面 {fg.get('coverage_ratio')}，包围盒占比 {fg.get('bbox_area_ratio')}，"
                f"中心偏移 x={fg.get('center_offset_x')} y={fg.get('center_offset_y')}，"
                f"{'触边' if fg.get('touches_edge') else '四周有留白'}"
                f"（掩膜来源：{fg.get('mask_source')}）"
            )
        pal = img.get("palette") or []
        if pal:
            lines.append("- 主色：" + "，".join(f"{c['hex']} {c['share']:.0%}" for c in pal))
        tex = img.get("texture") or {}
        if tex:
            lines.append(f"- 高频区（疑似文字/密集纹理）占比 {tex.get('busy_ratio')}")
        layout = img.get("layout") or {}
        if layout:
            lines.append(f"- 文字分布：主要在{ {'top': '上', 'middle': '中', 'bottom': '下'}.get(layout.get('dominant_band'), '?') }部，"
                         f"文字面积占比 {layout.get('text_area_ratio')}，"
                         f"纵向 {layout.get('vertical')}，横向 {layout.get('horizontal')}")
        text = (img.get("ocr_text") or "").strip()
        if text:
            lines.append(f"- OCR 文本：{text[:600]}")
        elif result.get("ocr_engine"):
            lines.append("- OCR 未识别到文字")

    if result.get("duplicates"):
        lines += ["", "## 套图查重"]
        for d in result["duplicates"]:
            lines.append(f"- {d['a']} ≈ {d['b']}（汉明距离 {d['distance']}）")

    if result.get("risks"):
        lines += ["", "## 本地度量已证实的问题"]
        for r in result["risks"]:
            lines.append(f"- [{r['level']}] {r['area']}：{r['reason']}")

    return "\n".join(lines)


def render(result: dict[str, Any]) -> str:
    """给人看的 CLI 报告（`ivyea image local` 用）。"""
    if not result.get("available"):
        return f"# 本地视觉\n\n不可用：{result.get('engine')}\n"
    lines = ["# 本地视觉度量", "",
             f"- 引擎：{result.get('engine')}",
             f"- OCR：{result.get('ocr_engine') or '无'}",
             f"- 图片数：{len(result.get('images') or [])}", ""]
    for img in result.get("images") or []:
        if img.get("error"):
            lines.append(f"## {img.get('name')}\n- 读取失败：{img['error']}\n")
            continue
        fg = img.get("foreground") or {}
        lines += [
            f"## {img.get('name')}",
            f"- {img.get('width')}×{img.get('height')} · {img.get('role')} · "
            f"{'白底' if img.get('is_white_background') else '非白底'}",
            f"- 主体占比 {fg.get('coverage_ratio')} · 偏心 x={fg.get('center_offset_x')} y={fg.get('center_offset_y')}",
            "- 主色 " + "，".join(c["hex"] for c in (img.get("palette") or [])[:5]),
            "",
        ]
    if result.get("risks"):
        lines.append("## 风险")
        for r in result["risks"]:
            lines.append(f"- [{r['level']}] {r['area']}：{r['reason']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
