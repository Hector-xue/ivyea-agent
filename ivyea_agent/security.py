"""Small security helpers for local logs and terminal output."""
from __future__ import annotations

import re
from typing import Any

_SECRET_KEYS = ("api_key", "apikey", "token", "secret", "password", "authorization", "access_key")
_PATTERNS = [
    # `Bearer` / `Basic` / `Token` 这类**认证方案前缀**要一起吃掉：不吃的话
    # `authorization: Bearer eyJhbGci…` 只会把 "Bearer" 抹掉，真正的 JWT 原样留在日志里。
    re.compile(r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*"
               r"['\"]?(?:(?:Bearer|Basic|Token|JWT)\s+)?([^'\"\s,}]+)"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]

#: **占位符不是密钥。** 这条是实测打出来的：`read_file` 读一个 shell 脚本时，
#: `-H "X-Token: $REPORT_TOKEN"` 被抹成了 `X-Token=***REDACTED***`，模型据此得出
#: "这个脚本硬编码了 token"的**错误结论**并写进了它生成的技能文档。
#:
#: 危害不止于看错：脱敏是加在 `_truncate` 上的，也就是**模型读到的每一份源码**都可能
#: 被悄悄改写。模型照抄读到的那一行去 `edit_file`，`old` 永远匹配不上文件真实内容。
#:
#: 所以这里只放过**明显是占位符**的取值，真实密钥一个都不放过。
_PLACEHOLDER_PATTERNS = (
    re.compile(r"^\$"),                       # $TOKEN / ${TOKEN}
    re.compile(r"^%[A-Za-z_][\w]*%$"),        # %TOKEN%（Windows）
    re.compile(r"^[<{]"),                     # <your-token> / {{token}} / {token}
    re.compile(r"^[xX*.\-_]+$"),              # xxxx / **** / ------
    re.compile(r"(?i)(environ|getenv|process\.env|secrets?\.|config\.|self\.)"),
    re.compile(r"(?i)^(none|null|nil|true|false|undefined|required|optional|str|string|"
               r"your[_-]?\w*|placeholder|example|changeme|todo|redacted)$"),
    re.compile(r"\*\*\*REDACTED\*\*\*"),     # 已经脱敏过的，别再套一层
)


def _is_placeholder(value: str) -> bool:
    v = (value or "").strip()
    if not v:
        return True
    return any(p.search(v) for p in _PLACEHOLDER_PATTERNS)


def redact_text(text: str) -> str:
    out = str(text or "")
    for pat in _PATTERNS:
        if pat.groups >= 2:
            out = pat.sub(
                lambda m: m.group(0) if _is_placeholder(m.group(m.lastindex))
                else f"{m.group(1)}=***REDACTED***",
                out,
            )
        else:
            out = pat.sub("***REDACTED***", out)
    return out


def redact_obj(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if any(s in str(k).lower() for s in _SECRET_KEYS):
                out[k] = "***REDACTED***"
            else:
                out[k] = redact_obj(v)
        return out
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
