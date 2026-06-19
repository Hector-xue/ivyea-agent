"""Optional local OCR support via the Tesseract CLI."""
from __future__ import annotations

import shutil
import subprocess
from typing import Any

from . import image_audit, security


def available() -> tuple[bool, str]:
    exe = shutil.which("tesseract")
    if not exe:
        return False, "未找到 tesseract，可安装系统包后重试。"
    try:
        proc = subprocess.run([exe, "--version"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding="utf-8", errors="replace", timeout=5)
    except Exception as e:  # noqa: BLE001
        return False, f"tesseract 不可用：{e}"
    first = (proc.stdout or "").splitlines()[0] if proc.stdout else exe
    return True, first


def run(paths: list[str], *, lang: str = "eng", recursive: bool = True, timeout: int = 30) -> dict[str, Any]:
    ok, detail = available()
    images = image_audit.scan(paths, recursive=recursive)
    if not ok:
        return {"available": False, "detail": detail, "images": images, "results": []}
    exe = shutil.which("tesseract") or "tesseract"
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
                "error": "" if proc.returncode == 0 else text[:500],
            })
        except subprocess.TimeoutExpired:
            results.append({"path": img["path"], "name": img["name"], "ok": False, "text": "", "error": "OCR 超时"})
        except Exception as e:  # noqa: BLE001
            results.append({"path": img["path"], "name": img["name"], "ok": False, "text": "", "error": str(e)})
    return {"available": True, "detail": detail, "images": images, "results": results}


def render(result: dict[str, Any]) -> str:
    lines = ["# 图片 OCR", "", f"- OCR 引擎：{result['detail']}", f"- 图片数：{len(result.get('images') or [])}", ""]
    if not result.get("available"):
        lines.append("## 状态")
        lines.append("OCR 不可用。可先用 `ivyea image audit` 做本地资产诊断，或安装 tesseract 后重试。")
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
