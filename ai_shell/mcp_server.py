"""ai_shell 的 MCP 服务端：把命令行能力暴露成原生工具。

两种传输（与 ai_browser 的 MCP 服务端一致）：

  1) stdio（默认）—— 由 MCP 客户端拉起子进程：
         python -m ai_shell.mcp_server

  2) streamable-http —— 常驻服务，多客户端共用：
         python -m ai_shell.mcp_server --transport http --host 127.0.0.1 --port 8767

鉴权：设置 AI_SHELL_MCP_TOKEN（或 --token）后 HTTP 请求必须带
`Authorization: Bearer <token>`；未设置时仅允许本机回环。stdio 无需鉴权。
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
from typing import Any

import anyio

try:  # mcp >= 2.0
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

try:
    from mcp_types import CallToolResult, TextContent, ToolAnnotations   # mcp >= 2.0
except ImportError:  # mcp 1.x
    from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .core import (AIShellError, env_info, exec_command, history, job_logs,
                   terminal_close, terminal_open, terminal_send)
from .fs import (glob_paths, list_dir, mkdir_path, move_path, read_file,
                 remove_path, stat_path, write_file)

DEFAULT_HOST = os.environ.get("AI_SHELL_MCP_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("AI_SHELL_MCP_PORT", "8767"))
MCP_PATH = "/mcp"

mcp = _Server("ai-shell")


def _dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _result(obj: Any) -> CallToolResult:
    """把结果包成 CallToolResult：text（JSON 字符串，兼容旧运行时）+ structuredContent。

    结构化数据不再降级成纯文本：支持 structuredContent 的运行时（Claude Code /
    Cline 等）能程序化读取 receipt.exit_code/filter/truncated；旧运行时仍把 text
    原样交给模型，等价于旧的 -> str 行为，零兼容性损失。
    """
    return CallToolResult(
        content=[TextContent(type="text", text=_dumps(obj))],
        structured_content=obj,
    )


def _annot(ro: bool = False, d: bool = False, i: bool = False, ow: bool = False) -> ToolAnnotations:
    """工具的 MCP annotations：readOnly/destructive/idempotent/openWorld 提示。

    供运行时（Claude Code/Cline）做破坏性审批、自动批准只读。exec/terminal_send
    标 openWorld 而非 destructive（通用执行器不是固有破坏性，灾难命令由 _guard 拦截）。
    """
    return ToolAnnotations(read_only_hint=ro, destructive_hint=d,
                           idempotent_hint=i, open_world_hint=ow)


async def _call(fn, *args, **kwargs):
    """运行 fn；AIShellError → 结构化错误结果（isError=false）；其余异常原样抛出。"""
    try:
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
    except AIShellError as e:
        return {"ok": False, "error": e.to_error()}


@mcp.tool(annotations=_annot(ow=True))
async def exec(cmd: str, cwd: str | None = None, timeout: float = 60,
               grep: str | None = None, grep_context: int = 0,
               background: bool = False, allow_outside: bool = False) -> CallToolResult:
    """执行一条命令，返回结构化回执 {receipt:{exit_code,duration_ms,cwd,shell,timed_out},stdout,stderr}。

    成败只看 receipt.exit_code（0=成功、非 0=失败、timed_out 时为 None），不要用 stderr 是否为空判断。
    命令方言必须匹配目标系统：回执里的 shell 字段告诉你实际用的是 bash/cmd/pwsh。
    grep 是正则：命中行 ± grep_context 行在服务端投影，stdout/stderr 各自过滤后再交付，省 token。
    background=True 立即返回 job_id，用 job_output 轮询（长构建/训练任务用）。
    cwd 默认限定在工作区内，越界需 allow_outside=True。
    """
    return _result(await _call(exec_command, cmd, cwd=cwd, timeout=timeout,
                              grep=grep, grep_context=grep_context,
                              background=background, allow_outside=allow_outside))


@mcp.tool(annotations=_annot(ow=True))
async def terminal_open_shell(shell: str | None = None, cwd: str | None = None) -> CallToolResult:
    """开启持久终端：cd/export/激活虚拟环境在后续命令中持续生效。

    首个回执携带 env（OS/shell/python/node/git 版本），先读它再决定命令方言。
    多步操作用终端，单条命令用 exec；只想读环境请用 environment，无需开终端。
    """
    return _result(await _call(terminal_open, shell, cwd))


@mcp.tool(annotations=_annot(ow=True))
async def terminal_send_cmd(terminal_id: str, cmd: str, timeout: float = 60,
                            grep: str | None = None, grep_context: int = 0) -> CallToolResult:
    """在持久终端里执行命令，返回结构化回执（cwd 每次回报，不会迷路）。

    注意：这里的 timed_out=true 只表示「没等到命令结束哨兵」，命令**可能仍在运行**
    （此时 receipt 另带 still_running=true），不像 exec 那样已被杀死；建议
    terminal_close_shell 后重开。全屏程序（vim/top）不可用。
    """
    return _result(await _call(terminal_send, terminal_id, cmd,
                              timeout=timeout, grep=grep, grep_context=grep_context))


@mcp.tool(annotations=_annot(i=True, ow=True))
async def terminal_close_shell(terminal_id: str) -> CallToolResult:
    """关闭持久终端并杀掉其进程树。空闲 900 秒也会自动回收。"""
    return _result({"closed": await _call(terminal_close, terminal_id)})


@mcp.tool(annotations=_annot(ro=True, i=True, ow=True))
async def job_output(job_id: str) -> CallToolResult:
    """查看后台任务的当前输出与状态（running/done/failed/timeout）。"""
    return _result(await _call(job_logs, job_id))


@mcp.tool(annotations=_annot(ro=True, i=True))
async def environment() -> CallToolResult:
    """环境自检：OS/架构/shell/python/node/git/ffmpeg 版本/工作区路径。"""
    return _result(await _call(env_info))


@mcp.tool(annotations=_annot(ro=True, i=True))
async def command_history(n: int = 20) -> CallToolResult:
    """最近 n 条执行记录（含 exit_code 与耗时），跨进程持久。返回 {count,records}。"""
    records = await _call(history, n)
    return _result({"count": len(records), "records": records})


# ============================ 文件系统 ============================


@mcp.tool(annotations=_annot(ro=True, i=True))
async def fs_read(path: str, max_bytes: int | None = None,
                  allow_outside: bool = False) -> CallToolResult:
    """读文本文件，返回 {path,size_bytes,truncated,content}。超长内容 head+tail 双侧截断。"""
    return _result(await _call(read_file, path, max_bytes=max_bytes, allow_outside=allow_outside))


@mcp.tool(annotations=_annot(d=True))
async def fs_write(path: str, content: str, append: bool = False,
                   allow_outside: bool = False) -> CallToolResult:
    """写文本文件（默认覆盖，append=True 追加），自动创建父目录，返回 {path,written_bytes,append}。"""
    return _result(await _call(write_file, path, content, append=append, allow_outside=allow_outside))


@mcp.tool(annotations=_annot(ro=True, i=True))
async def fs_list(path: str = ".", allow_outside: bool = False) -> CallToolResult:
    """列目录，返回 {path,count,entries:[{name,kind,size,mtime}]}。"""
    return _result(await _call(list_dir, path, allow_outside=allow_outside))


@mcp.tool(annotations=_annot(ro=True, i=True))
async def fs_stat(path: str, allow_outside: bool = False) -> CallToolResult:
    """取文件元数据；不存在时返回 {path,exists:false} 而非报错。"""
    return _result(await _call(stat_path, path, allow_outside=allow_outside))


@mcp.tool(annotations=_annot(d=True))
async def fs_move(src: str, dst: str, allow_outside: bool = False) -> CallToolResult:
    """移动/重命名文件或目录，返回 {src,dst,moved}。"""
    return _result(await _call(move_path, src, dst, allow_outside=allow_outside))


@mcp.tool(annotations=_annot(d=True))
async def fs_remove(path: str, allow_outside: bool = False) -> CallToolResult:
    """删除文件或目录（目录递归删除），返回 {path,removed}。"""
    return _result(await _call(remove_path, path, allow_outside=allow_outside))


@mcp.tool(annotations=_annot(i=True))
async def fs_mkdir(path: str, parents: bool = True, allow_outside: bool = False) -> CallToolResult:
    """创建目录（parents=True 连父目录一起建，幂等），返回 {path,created}。"""
    return _result(await _call(mkdir_path, path, parents=parents, allow_outside=allow_outside))


@mcp.tool(annotations=_annot(ro=True, i=True))
async def fs_glob(pattern: str, allow_outside: bool = False) -> CallToolResult:
    """按 glob 模式匹配工作区内路径（相对工作区根），返回 {pattern,count,matches}。"""
    return _result(await _call(glob_paths, pattern, allow_outside=allow_outside))


# ============================ 传输层 ============================


class BearerGuard:
    """ASGI 中间件：有 token 校验 Bearer；无 token 校验 Host 头为回环字面量。

    关键：仅回环绑定(127.0.0.1)挡不住 DNS rebinding——浏览器发起的 rebinding
    请求恰恰来自本机回环。所以无 token 时也必须校验 Host 是回环字面量，
    否则攻击者可用 attacker.com 解析到 127.0.0.1 借本机服务执行命令。
    """
    _LOOPBACK = {"127.0.0.1", "localhost", "::1"}

    def __init__(self, app, token: str | None = None):
        self.app = app
        self.token = token

    @classmethod
    def _loopback(cls, host: str) -> bool:
        h = (host or "").lower()
        for lit in cls._LOOPBACK:
            if h == lit or h.startswith(lit + ":") or h.startswith("[" + lit + "]"):
                return True
        return False

    async def _deny(self, send, detail: str) -> None:
        body = json.dumps({"error": "unauthorized", "detail": detail},
                          ensure_ascii=False).encode("utf-8")
        await send({"type": "http.response.start", "status": 401,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "lifespan":
                return await self.app(scope, receive, send)
            await self._deny(send, "only http/lifespan scopes allowed")
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        if self.token:
            auth = headers.get("authorization", "")
            expected = f"Bearer {self.token}"
            if not hmac.compare_digest(auth.encode("utf-8"), expected.encode("utf-8")):
                await self._deny(send, "Authorization: Bearer <token> required")
                return
        elif not self._loopback(headers.get("host", "")):
            await self._deny(send, "loopback Host header required (no token set)")
            return
        return await self.app(scope, receive, send)


def build_http_app(host: str, port: int, token: str | None = None, stateful: bool = False):
    app = mcp.streamable_http_app(streamable_http_path=MCP_PATH, host=host,
                                  stateless_http=not stateful)
    return BearerGuard(app, token)   # 无条件挂载：有 token 校验 Bearer，无 token 校验 Host 回环


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ai-shell-mcp",
                                 description="ai_shell 的 MCP 服务端（stdio 或 streamable-http）")
    ap.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--token", default=os.environ.get("AI_SHELL_MCP_TOKEN"))
    ap.add_argument("--stateful", action="store_true")
    args = ap.parse_args(argv)

    if args.transport == "stdio":
        mcp.run()
        return

    import uvicorn

    if not args.token and args.host not in ("127.0.0.1", "::1", "localhost"):
        print(f"ai-shell-mcp: 监听 {args.host} 却未设置令牌，拒绝启动（请加 --token）",
              file=sys.stderr)
        raise SystemExit(2)
    if not args.token:
        print("ai-shell-mcp: 未设置令牌，仅本机回环可用、不校验 Authorization",
              file=sys.stderr)
    print(f"ai-shell-mcp: streamable-http 已启动 http://{args.host}:{args.port}{MCP_PATH}",
          file=sys.stderr)
    uvicorn.run(build_http_app(args.host, args.port, args.token, stateful=args.stateful),
                host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
