"""本地 OCR：RapidOCR（ONNX）优先，tesseract 兜底，都没有就明说没有。

**为什么换掉纯 tesseract**：tesseract 对电商图（艺术字、低对比描边、中英混排）
识别率一般，而 T3 本地视觉恰恰最需要读出图上的字。RapidOCR 是 PP-OCR 系模型的
ONNX 封装，中英电商图明显更强，模型内置在 wheel 里（14.9MB），且本仓已经依赖
`onnxruntime`（见 onnx_embedding.py 那条"自带小模型"的路子）。

**为什么 tesseract 不删**：RapidOCR 拖 opencv-python，精简 Linux 镜像常缺
`libGL.so.1`，`import cv2` 会直接炸。那种环境下还有 tesseract 可用就不至于
整条 T3 断掉——所以引擎探测必须是运行时的，不能在 import 阶段决定。

对外契约保持不变：`run()` 仍返回 {available, detail, images, results}，
只是多了 `engine` 字段，且 RapidOCR 路径下每个 result 多带 `boxes`
（文本块坐标，local_vision 用它推版面骨架）。
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Any

from . import image_audit, security

_RAPID_CACHE: Any = None
_RAPID_FAILED = ""


def _rapidocr():
    """惰性加载 RapidOCR，失败原因记下来只报一次。

    必须惰性：模型初始化要几百毫秒，而绝大多数请求根本不看图；更要紧的是
    `import cv2` 在缺 libGL 的机器上会抛，放在模块顶层会让整个 ocr 模块不可导入。
    """
    global _RAPID_CACHE, _RAPID_FAILED
    if _RAPID_CACHE is not None or _RAPID_FAILED:
        return _RAPID_CACHE
    try:
        from rapidocr_onnxruntime import RapidOCR
        _RAPID_CACHE = RapidOCR()
    except Exception as e:  # noqa: BLE001 — 缺包/缺 libGL/模型损坏都走同一条降级路
        _RAPID_FAILED = str(e)
        return None
    return _RAPID_CACHE


def _tesseract() -> str:
    return shutil.which("tesseract") or ""


def available() -> tuple[bool, str]:
    """(能否 OCR, 引擎描述)。描述里带引擎名，调用方直接透传给用户看。"""
    if _rapidocr() is not None:
        # 版本从包元数据取：rapidocr_onnxruntime 并不导出 __version__，
        # 按属性拿只会一直显示 "?"。
        try:
            from importlib.metadata import version as _pkg_version
            ver = _pkg_version("rapidocr-onnxruntime")
        except Exception:  # noqa: BLE001
            ver = "?"
        return True, f"RapidOCR (ONNX) {ver}"
    exe = _tesseract()
    if exe:
        try:
            proc = subprocess.run([exe, "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, encoding="utf-8", errors="replace", timeout=5)
            first = (proc.stdout or "").splitlines()[0] if proc.stdout else exe
            return True, first
        except Exception as e:  # noqa: BLE001
            return False, f"tesseract 不可用：{e}"
    detail = "未找到 OCR 引擎：建议 pip install rapidocr-onnxruntime，或安装系统包 tesseract。"
    if _RAPID_FAILED:
        detail += f"（RapidOCR 加载失败：{_RAPID_FAILED}）"
    return False, detail


def engine_name() -> str:
    ok, detail = available()
    return detail if ok else ""


def _run_rapidocr(engine, images: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results = []
    for img in images:
        try:
            out, _elapse = engine(img["path"])
            boxes: list[dict[str, Any]] = []
            texts: list[str] = []
            for item in out or []:
                # RapidOCR 单条结果是 [四点坐标, 文本, 置信度]
                pts, text, score = item[0], str(item[1]), float(item[2])
                xs = [float(p[0]) for p in pts]
                ys = [float(p[1]) for p in pts]
                texts.append(text)
                boxes.append({"text": security.redact_text(text),
                              "score": round(score, 3),
                              "bbox": [min(xs), min(ys), max(xs), max(ys)]})
            results.append({
                "path": img["path"], "name": img["name"], "ok": True,
                "text": security.redact_text("\n".join(texts)).strip(),
                "boxes": boxes,
                "image_width": img.get("width") or 0,
                "image_height": img.get("height") or 0,
                "error": "",
            })
        except Exception as e:  # noqa: BLE001
            results.append({"path": img["path"], "name": img["name"], "ok": False,
                            "text": "", "boxes": [], "error": str(e)})
    return results


def _run_tesseract(exe: str, images: list[dict[str, Any]], lang: str, timeout: int) -> list[dict[str, Any]]:
    results = []
    for img in images:
        try:
            proc = subprocess.run(
                [exe, img["path"], "stdout", "-l", lang],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            text = security.redact_text(proc.stdout or "").strip()
            results.append({
                "path": img["path"],
                "name": img["name"],
                "ok": proc.returncode == 0,
                "text": text,
                # tesseract 的纯文本模式没有坐标。这里给空表而不是编造坐标——
                # local_vision 看到空 boxes 就不推版面骨架。
                "boxes": [],
                "error": "" if proc.returncode == 0 else text[:500],
            })
        except subprocess.TimeoutExpired:
            results.append({"path": img["path"], "name": img["name"], "ok": False,
                            "text": "", "boxes": [], "error": "OCR 超时"})
        except Exception as e:  # noqa: BLE001
            results.append({"path": img["path"], "name": img["name"], "ok": False,
                            "text": "", "boxes": [], "error": str(e)})
    return results


def run(paths: list[str], *, lang: str = "eng", recursive: bool = True, timeout: int = 30) -> dict[str, Any]:
    ok, detail = available()
    images = image_audit.scan(paths, recursive=recursive)
    if not ok:
        return {"available": False, "engine": "", "detail": detail, "images": images, "results": []}

    engine = _rapidocr()
    if engine is not None:
        # RapidOCR 自带中英双语模型，不吃 lang 参数——这里刻意忽略它而不是报错，
        # 因为调用方传 lang 是为了 tesseract，换引擎不该让老调用失败。
        results = _run_rapidocr(engine, images)
    else:
        results = _run_tesseract(_tesseract(), images, lang, timeout)
    return {"available": True, "engine": detail, "detail": detail, "images": images, "results": results}


def render(result: dict[str, Any]) -> str:
    lines = ["# 图片 OCR", "", f"- OCR 引擎：{result['detail']}", f"- 图片数：{len(result.get('images') or [])}", ""]
    if not result.get("available"):
        lines.append("## 状态")
        lines.append("OCR 不可用。可先用 `ivyea image audit` 做本地资产诊断，"
                     "或 `pip install rapidocr-onnxruntime` / 安装 tesseract 后重试。")
        return "\n".join(lines) + "\n"
    lines.append("## 识别结果")
    rows = result.get("results") or []
    if not rows:
        lines.append("（无图片或无结果）")
    else:
        for row in rows:
            lines.append(f"### {row['name']}")
            if row["ok"]:
                lines.append(row["text"] or "（未识别到文字）")
            else:
                lines.append("识别失败：" + (row.get("error") or "-"))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"
