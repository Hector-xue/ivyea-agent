"""通用 MCP 客户端（极简 JSON-RPC over Streamable HTTP / SSE）。

只依赖 httpx，兼容 Python 3.9，不引官方 mcp SDK（其要求 3.10+ 且体积大）。
支持 http / sse / stdio 三种传输（远程 MCP 如领星网关，或本地 stdio server）。
协议覆盖 tools/resources/prompts；sampling/roots 暂不支持（运营场景用不到）。

通用设计：不写死任何厂商的工具名/字段——连上后 list_tools 自动发现，调用由
mcp.json 里的 dataSource 映射驱动（见 mcp_source.py）。
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Optional

import httpx

from . import subproc_env


class MCPError(Exception):
    pass


class MCPClient:
    def __init__(self, spec: dict[str, Any], timeout: float = 60.0):
        transport = (spec.get("transport") or "http").lower()
        if transport not in ("http", "sse", "stdio"):
            raise MCPError(f"不支持的 MCP 传输：{transport}")
        if transport in ("http", "sse") and not spec.get("url"):
            raise MCPError("MCP 服务器缺少 url")
        if transport == "stdio" and not spec.get("command"):
            raise MCPError("stdio MCP 服务器缺少 command")
        self.transport = transport
        self.command = spec.get("command", "")
        self.args = list(spec.get("args") or [])
        self.url: str = spec.get("url", "")
        self.headers: dict[str, str] = dict(spec.get("headers") or {})
        self.query: dict[str, str] = dict(spec.get("query") or {})
        # stdio 子进程的环境。这三个字段此前一个都没读 —— mcp.json 里早就写着
        # env 的服务器（如 sellersprite）其实一直没拿到密钥，只是因为多数
        # server 到真正调用工具时才校验，list_tools 能过，所以没人发现。
        self.env: dict[str, Any] = dict(spec.get("env") or {})
        self.env_passthrough: list[str] = [str(k) for k in (spec.get("env_passthrough") or [])]
        # **只有 `inherit_env: false` 才收紧。** 没写就继承全部环境，也就是
        # 这些字段存在之前的行为；`env` / `env_passthrough` 一律是**叠加**。
        #
        # 为什么不能拿"写过 env"当收紧信号（v1.15.8 的错）：`env` 这个字段在
        # v1.15.7 之前是**被静默忽略**的。用户在 mcp.json 里写它的时候，
        # 既不知道有"收紧"这回事，也没打算收紧 —— 他只是想给服务器加一个变量，
        # 而那个服务器同时还在靠 shell / systemd 里的其它变量工作。
        # 把"写过 env"读成"要收紧"，等于替他做了一个他从没做过的决定。
        #
        # 所以：升级完什么都不用改，原来能跑的照样跑，而且 env 从此真的生效
        # （以前写了也没用）。收紧走两条主动路径 —— `ivyea mcp add` 向导给新
        # 服务器写 `inherit_env: false`，以及 `ivyea mcp env <名> --secure`。
        self.inherit_env: bool = bool(spec.get("inherit_env", True))
        self.env_policy_declared: bool = "inherit_env" in spec
        self.timeout = timeout
        self.session_id: Optional[str] = None
        self._id = 0
        self._proc: subprocess.Popen | None = None

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream", **self.headers}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    @staticmethod
    def _parse_response(resp: httpx.Response) -> Optional[dict[str, Any]]:
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            payload = None
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    try:
                        payload = json.loads(line[5:].strip())
                    except Exception:
                        pass
            return payload
        try:
            return resp.json()
        except Exception:
            return None

    def _rpc(self, method: str, params: Optional[dict] = None) -> Any:
        self._id += 1
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            body["params"] = params
        if self.transport == "stdio":
            return self._rpc_stdio(body)
        try:
            resp = httpx.post(self.url, params=self.query or None,
                              headers=self._headers(), json=body, timeout=self.timeout)
        except Exception as exc:  # noqa: BLE001
            raise MCPError(f"连接失败：{exc}") from exc
        sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if resp.status_code >= 400:
            raise MCPError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        data = self._parse_response(resp)
        if data is None:
            raise MCPError("空/不可解析的响应")
        if data.get("error"):
            raise MCPError(f"MCP 错误：{data['error']}")
        return data.get("result")

    def child_env(self) -> dict[str, str]:
        """stdio server 子进程的环境：白名单 + 本服务器显式声明的那些。

        独立成方法而不是内联进 Popen，是为了能单独测 —— 直接断言"密钥不在里面"
        比起一个进程再去读它自己的环境要可靠得多。
        """
        return subproc_env.build_env(
            self.env,
            passthrough=self.env_passthrough,
            inherit_all=self.inherit_env,
        )

    def _ensure_stdio(self) -> subprocess.Popen:
        if self._proc and self._proc.poll() is None:
            return self._proc
        try:
            self._proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                # 不带 env= 的话子进程会拿到父进程全部环境（含各家 API key）。
                env=self.child_env(),
            )
        except OSError as exc:
            raise MCPError(f"stdio 启动失败：{exc}") from exc
        return self._proc

    def _rpc_stdio(self, body: dict[str, Any]) -> Any:
        proc = self._ensure_stdio()
        if not proc.stdin or not proc.stdout:
            raise MCPError("stdio 管道不可用")
        proc.stdin.write(json.dumps(body, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        target_id = body.get("id")
        while True:
            line = proc.stdout.readline()
            if not line:
                err = proc.stderr.read() if proc.stderr else ""
                raise MCPError(f"stdio 连接已关闭：{err[:300]}")
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("id") != target_id:
                continue
            if data.get("error"):
                raise MCPError(f"MCP 错误：{data['error']}")
            return data.get("result")

    def _notify(self, method: str, params: Optional[dict] = None) -> None:
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if self.transport == "stdio":
            try:
                proc = self._ensure_stdio()
                if proc.stdin:
                    proc.stdin.write(json.dumps(body, ensure_ascii=False) + "\n")
                    proc.stdin.flush()
            except MCPError:
                pass
            return
        try:
            httpx.post(self.url, params=self.query or None,
                       headers=self._headers(), json=body, timeout=self.timeout)
        except Exception:
            pass  # 通知失败不致命

    def initialize(self) -> dict[str, Any]:
        res = self._rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "ivyea-agent", "version": "0.1"},
        })
        self._notify("notifications/initialized")
        return res or {}

    def list_tools(self) -> list[dict[str, Any]]:
        res = self._rpc("tools/list")
        return (res or {}).get("tools", []) if isinstance(res, dict) else []

    def call_tool(self, name: str, arguments: Optional[dict] = None) -> dict[str, Any]:
        return self._rpc("tools/call", {"name": name, "arguments": arguments or {}})

    def list_resources(self) -> list[dict[str, Any]]:
        res = self._rpc("resources/list")
        return (res or {}).get("resources", []) if isinstance(res, dict) else []

    def read_resource(self, uri: str) -> list[dict[str, Any]]:
        res = self._rpc("resources/read", {"uri": uri})
        return (res or {}).get("contents", []) if isinstance(res, dict) else []

    def list_prompts(self) -> list[dict[str, Any]]:
        res = self._rpc("prompts/list")
        return (res or {}).get("prompts", []) if isinstance(res, dict) else []

    def get_prompt(self, name: str, arguments: Optional[dict] = None) -> dict[str, Any]:
        return self._rpc("prompts/get", {"name": name, "arguments": arguments or {}}) or {}

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
