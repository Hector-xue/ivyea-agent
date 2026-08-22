"""给子进程准备环境变量：白名单放行，其余显式声明。

为什么不直接继承
----------------
`subprocess.Popen(...)` 不带 `env=` 时，子进程拿到的是父进程的**全部**环境 ——
DEEPSEEK_API_KEY、领星凭据、IVYEA_* 全在里面。自己配一两个 MCP server 时没人
注意；一旦开始装别人写的 server 或 hook 脚本，那就是一条凭据直通道。

所以默认走白名单：只放行"任何程序都得有、且本身不是秘密"的那些
（PATH/HOME/LANG/TZ/TEMP…，Windows 另有一组不给就起不来的）。
判据是"缺了子进程跑不起来"，不是"给了方便"。

逃生舱有三层，从窄到宽：
  1. `env`             —— 直接给值，或 `${VAR}` 从父环境取一个
  2. `env_passthrough` —— 列几个键名，从父环境原样带过去（1 的简写）
  3. `inherit_env`     —— 退回旧行为，整份继承。给升级后发现自己依赖环境变量
                          又一时理不清的人留的，不是推荐用法

**不放行 PYTHONPATH**：它既泄漏本机路径，又是一条代码注入路径（子进程会从
那里 import）。真需要就在 `env` 里显式写。
"""
from __future__ import annotations

import os
import re
from typing import Iterable, Mapping, Optional

#: 类 Unix 下放行的键。判据是"缺了子进程跑不起来或行为不对"。
ALLOWLIST_POSIX = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
    "TZ", "TMPDIR", "TEMP", "TMP",
    # 中文环境下不给这两个，子进程读写中文会按 ASCII 崩 —— 这是踩过的坑。
    "PYTHONIOENCODING", "PYTHONUTF8",
})

#: Windows 下放行的键。前六个缺任何一个，子进程基本起不来。
ALLOWLIST_WINDOWS = frozenset({
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "SYSTEMDRIVE", "PATH",
    "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    # Windows 原生不设 HOME，但 Git for Windows / MSYS 环境会设，而且很多
    # 从 Unix 移植过来的工具只认它。是路径不是密钥，放行。
    "HOME",
    "TEMP", "TMP", "USERNAME", "COMPUTERNAME",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "PROGRAMDATA",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
    "PYTHONIOENCODING", "PYTHONUTF8",
})

#: `${VAR}` —— 只认这一种形式。不支持 `$VAR`：裸 `$` 在真实的值里太常见
#: （密码、正则、Windows 路径），按变量展开会把它们悄悄改掉。
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def allowlist() -> frozenset:
    """当前平台的白名单。"""
    return ALLOWLIST_WINDOWS if os.name == "nt" else ALLOWLIST_POSIX


def expand(value: str) -> str:
    """把 `${VAR}` 换成父进程里的值；父进程没有该变量时换成空串。

    换成空串而不是保留 `${VAR}` 字面量：子进程拿到一个长得像占位符的值，
    多半会当成真值用下去，错得更隐蔽。空串至少会让它在第一步就报"没配"。
    """
    return _PLACEHOLDER.sub(lambda m: os.environ.get(m.group(1), ""), value)


def build_env(
    env: Optional[Mapping[str, object]] = None,
    *,
    passthrough: Optional[Iterable[str]] = None,
    inherit_all: bool = False,
    extra: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """组装子进程的环境。

    优先级从低到高：白名单 → passthrough → env → extra。

    :param env: 配置里写的键值；值支持 `${VAR}` 从父环境取。
    :param passthrough: 从父环境原样带过去的键名；父环境没有的直接跳过
        （不写成空串 —— 那会让子进程以为"配了但是空的"）。
    :param inherit_all: True 时整份继承父环境，等同改动前的行为。
    :param extra: 内核自己注入的（如 IVYEA_HOOK_EVENT），优先级最高，
        配置覆盖不掉。
    """
    if inherit_all:
        out = dict(os.environ)
    else:
        keep = allowlist()
        out = {k: v for k, v in os.environ.items() if k.upper() in keep}

    for key in passthrough or ():
        name = str(key).strip()
        if not name:
            continue
        value = os.environ.get(name)
        if value is not None:
            out[name] = value

    for key, value in (env or {}).items():
        name = str(key).strip()
        if name:
            out[name] = expand(value) if isinstance(value, str) else str(value)

    out.update(extra or {})
    return out
