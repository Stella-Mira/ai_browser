"""ai_shell 核心 v0.1：跨系统命令行执行（为 agent 消费设计）

设计原则（与 ai_browser 同一语言：回执 / 会话 / 结构化错误 / 诚实统计）：

1. 输出有界：head+tail 双侧截断并带元数据；支持服务端正则投影（grep）。
2. 回执结构化：exit_code / duration_ms / cwd / shell / timed_out 嵌套在 receipt 下
   （顶层另留 exit_code 冗余键兼容旧调用方，已废弃），stdout 与 stderr 分离。
   判断成败靠 receipt.exit_code，不靠读文本。
3. 双模式：exec_command 一次性执行；terminal_open/send/close 持久终端
   （cd/export 跨命令生效，cwd 每次从哨兵行解析回报）。
4. 跨系统：Windows 探测 pwsh → powershell → cmd，POSIX 探测 bash → sh；
   回执始终报告实际 shell，命令方言由调用方按 receipt.shell 选择。
   零第三方依赖：持久终端用管道实现，放弃全 PTY 以换取全平台一致行为。
5. 防护：灾难模式拦截（明确报错，绝不静默改写命令）+ 工作区 cwd 限定 +
   stdin 默认关闭防挂死 + 全量审计日志。字符串级防护是建议性的，
   真正的隔离请把本工具放进容器/虚拟机里跑。
"""
from __future__ import annotations

import atexit
import json
import locale
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__version__ = "0.1.0"

WORKSPACE = Path(os.environ.get("AI_SHELL_WORKSPACE", os.getcwd())).resolve()
HISTORY_FILE = WORKSPACE / ".ai_shell" / "history.jsonl"
MAX_OUTPUT = int(os.environ.get("AI_SHELL_MAX_OUTPUT", "8000"))       # 字节
DEFAULT_TIMEOUT = float(os.environ.get("AI_SHELL_TIMEOUT", "60"))     # 秒
TERMINAL_TTL = float(os.environ.get("AI_SHELL_TERMINAL_TTL", "900"))  # 秒
# 持久终端「没等到结束哨兵」时的交付告警。它属于 lifecycle 通道而非「命令输出」通道，
# 因此必须在 grep 投影**之后**追加（见 Terminal._send_locked 的超时分支）。
TIMEOUT_NOTICE = "\n[超时：命令可能仍在运行；建议 terminal_close 后重开]"


class AIShellError(ValueError):
    """可预测的业务错误：带稳定 code/kind/retryable，供 agent 程序化处理。

    继承 ValueError 是为了向后兼容（旧的 except ValueError 仍能捕获）；
    mcp_server 层单独 catch AIShellError 转成结构化 error 结果，其余异常才变 isError=true。
    """
    _KIND = {
        "INVALID_ARGUMENT": "validation", "INVALID_TIMEOUT": "validation",
        "INVALID_REGEX": "validation", "UNSUPPORTED_SHELL": "validation",
        "OUTSIDE_WORKSPACE": "validation", "INVALID_CWD": "validation",
        "NOT_A_FILE": "validation", "NOT_A_DIR": "validation",
        "JOB_NOT_FOUND": "not_found", "TERMINAL_NOT_FOUND": "not_found",
        "NOT_FOUND": "not_found", "DANGER_BLOCKED": "refused",
        "PERMISSION_DENIED": "permission", "NO_SHELL_AVAILABLE": "unavailable",
        "TERMINAL_WRITE_FAILED": "unavailable", "INTERNAL_ERROR": "internal",
    }
    _RETRYABLE = {"validation": True, "not_found": False, "refused": False,
                  "permission": True, "unavailable": False, "internal": True}

    def __init__(self, code: str, message: str, *, detail: dict[str, Any] | None = None,
                 kind: str | None = None, retryable: bool | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.kind = kind or self._KIND.get(code, "internal")
        self.retryable = self._RETRYABLE.get(self.kind, False) if retryable is None else retryable
        self.detail = detail or {}

    def to_error(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "kind": self.kind,
                               "retryable": self.retryable, "message": self.message}
        if self.detail:
            out["detail"] = self.detail
        return out


# ============================ Shell 抽象层 ============================


@dataclass
class ShellSpec:
    name: str
    argv: list[str]        # 持久终端启动命令（从 stdin 读命令）
    exec_argv: list[str]   # 一次性执行前缀
    sentinel_tpl: str      # stdout 哨兵 echo 模板（携带 exit_code + cwd），{s} 为本次唯一串
    sentinel_err_tpl: str  # stderr 哨兵 echo 模板（仅作 stderr 边界标记）
    boot: list[str] = field(default_factory=list)  # 终端启动初始化命令
    newline: str = "\n"


_SHELLS: dict[str, ShellSpec] = {}


def _register_shells() -> None:
    ps_boot = ["[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
               "$OutputEncoding=[System.Text.Encoding]::UTF8"]
    ps_sentinel = 'echo "@@{s}:$($LASTEXITCODE):$($PWD.Path)@@"'
    ps_sentinel_err = '[Console]::Error.WriteLine("@@{s}:ERR@@")'
    _SHELLS["pwsh"] = ShellSpec(
        "pwsh", ["pwsh", "-NoProfile", "-NoLogo", "-"],
        ["pwsh", "-NoProfile", "-Command"], ps_sentinel, ps_sentinel_err, ps_boot)
    _SHELLS["powershell"] = ShellSpec(
        "powershell", ["powershell", "-NoProfile", "-NoLogo", "-"],
        ["powershell", "-NoProfile", "-Command"], ps_sentinel, ps_sentinel_err, ps_boot)
    _SHELLS["cmd"] = ShellSpec(
        "cmd", ["cmd", "/q"], ["cmd", "/c"],
        "echo @@{s}:%ERRORLEVEL%:%CD%@@", "echo @@{s}:ERR@@ 1>&2",
        ["chcp 65001 >nul"], "\r\n")
    sh_sentinel = 'echo "@@{s}:$?:$PWD@@"'
    sh_sentinel_err = 'echo "@@{s}:ERR@@" 1>&2'
    _SHELLS["bash"] = ShellSpec("bash", ["bash"], ["bash", "-c"],
                                sh_sentinel, sh_sentinel_err)
    _SHELLS["sh"] = ShellSpec("sh", ["sh"], ["sh", "-c"],
                              sh_sentinel, sh_sentinel_err)


_register_shells()


def detect_shell() -> ShellSpec:
    """选择当前系统可用的 shell；AI_SHELL_SHELL 可强制指定（bash/sh/cmd/pwsh/powershell）"""
    env = os.environ.get("AI_SHELL_SHELL")
    if env:
        if env not in _SHELLS:
            raise AIShellError("UNSUPPORTED_SHELL", f"AI_SHELL_SHELL={env!r} 不受支持，可选：{sorted(_SHELLS)}")
        return _SHELLS[env]
    # Windows 默认选 cmd：cmd 按“原生命令行”解释（双引号即字符串，与 bash/sh
    # 一致），而 powershell -Command 会把整条命令当 PowerShell 脚本解析，
    # cmd 风格的 `"C:\...python.exe" -c "..."` 会报 ParserError。需要
    # PowerShell 方言时用 AI_SHELL_SHELL=pwsh/powershell 显式指定。
    order = ("cmd", "pwsh", "powershell") if os.name == "nt" else ("bash", "sh")
    for name in order:
        if shutil.which(_SHELLS[name].argv[0]):
            return _SHELLS[name]
    raise AIShellError("NO_SHELL_AVAILABLE", "未找到可用 shell（Windows 需要 pwsh/powershell/cmd，POSIX 需要 bash/sh）")


def get_shell(name: str) -> ShellSpec:
    """按名字取 shell，并**当场确认它的二进制真的在 PATH 上**。

    `detect_shell()` 之所以安全，是因为它逐个 `shutil.which`；而按名字直取（`shell="pwsh"`）
    在 r186 前**只查表不查盘**，于是"这台机器没装 pwsh"要在 `subprocess.Popen` 里才炸成
    **裸 `FileNotFoundError`**——一次调用里三条路径（`exec_command` / 后台 `_start_job` /
    `Terminal.__init__`）全漏，而 README 承诺错误一律 `AIShellError`。
    修在**选 shell 这一处**而不是给三个 Popen 加 try：漏的根因是"没验可用性"，
    Popen 只是它迟到的表现；在这里拒绝还能在消息里带上候选 shell 名。
    """
    if name not in _SHELLS:
        raise AIShellError("UNSUPPORTED_SHELL", f"未知 shell {name!r}，可选：{sorted(_SHELLS)}")
    spec = _SHELLS[name]
    if shutil.which(spec.argv[0]) is None:
        usable = [n for n in _SHELLS if shutil.which(_SHELLS[n].argv[0])]
        raise AIShellError("NO_SHELL_AVAILABLE",
                           f"shell {name!r} 的二进制 {spec.argv[0]!r} 不在 PATH 上；"
                           f"本机可用：{usable or '（无）'}",
                           detail={"shell": name, "binary": spec.argv[0], "available": usable})
    return spec


# ============================ 编码与输出处理 ============================


def _console_encoding() -> str:
    """Windows 命令写进重定向管道时用的实际码页：优先 OEM 码页（cp437/850/936…）。

    cmd.exe 把输出写进管道用的是 OEM 码页，而非 locale.getpreferredencoding
    返回的 ANSI 码页（西文 Windows ANSI=cp1252、OEM=cp437/850，两者不同）；
    中文 Windows 两者同为 cp936，所以旧实现只在中文机上「碰巧对」。
    """
    if os.name == "nt":
        try:
            import ctypes
            cp = ctypes.windll.kernel32.GetOEMCP()
            if cp:
                return f"cp{cp}"
        except Exception:
            pass
    return locale.getpreferredencoding(False)


def _decode(data: bytes) -> str:
    """UTF-8 优先；只有「坏字节段」按回退码页解，不整段连坐。绝不抛异常。

    用 errors="surrogateescape" 先解：合法 UTF-8 段原样保留，坏字节映射到
    U+DC80..U+DCFF，再把这些坏字节单独按回退码页重解。避免旧实现「一个坏字节
    导致整段（含合法中文）按 GBK/cp1252 重解」的全篇乱码。
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    fb = _console_encoding()
    s = data.decode("utf-8", errors="surrogateescape")
    out: list[str] = []
    bad: list[int] = []

    def flush() -> None:
        if bad:
            raw = bytes(cp & 0xFF for cp in bad)
            out.append(raw.decode(fb, errors="replace"))
            bad.clear()

    for ch in s:
        cp = ord(ch)
        if 0xDC80 <= cp <= 0xDCFF:   # 来自 surrogateescape 的坏字节
            bad.append(cp)
        else:
            flush()
            out.append(ch)
    flush()
    return "".join(out)


def _bound(text: str, limit: int = MAX_OUTPUT) -> tuple[str, dict[str, Any] | None]:
    """head+tail 双侧截断：开头留上下文，结尾留错误信息"""
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return text, None
    # 奇数 limit 也要用满（head 多分 1 字节，tail 取余下部分），避免「给了 N 只截 N-1」。
    head_n = (limit + 1) // 2
    tail_n = limit - head_n
    head = data[:head_n].decode("utf-8", errors="ignore")
    tail = data[-tail_n:].decode("utf-8", errors="ignore")
    cut = len(data) - limit
    return (f"{head}\n…[中间截断 {cut} 字节，共 {len(data)} 字节]…\n{tail}",
            {"total_bytes": len(data), "shown_bytes": limit, "cut_bytes": cut})


def _grep(text: str, pattern: str, context: int = 0) -> tuple[str, dict[str, Any]]:
    """服务端正则投影：匹配行 ± context 行，过滤发生在交付前"""
    lines = text.splitlines()
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"[filter 正则非法：{e}]", {"matched": 0, "error": str(e),
                                           "total_lines": len(lines)}
    hits = [i for i, l in enumerate(lines) if rx.search(l)]
    if not hits:
        return f"[filter 无匹配，输出共 {len(lines)} 行]", {"matched": 0, "total_lines": len(lines)}
    keep: set[int] = set()
    for i in hits:
        keep.update(range(max(0, i - context), min(len(lines), i + context + 1)))
    out = "\n".join(lines[i] for i in sorted(keep))
    return out, {"matched": len(hits), "total_lines": len(lines)}


def _validate_grep(grep: str | None) -> None:
    """前置校验 grep 正则：spawn 之前就发现非法 regex，避免命令已跑（有副作用）才发现。"""
    if grep is not None:
        try:
            re.compile(grep)
        except re.error as e:
            raise AIShellError("INVALID_REGEX", f"grep 正则非法：{e}",
                               detail={"grep": grep, "re_error": str(e)})


def _merge_legacy_filter(grep: str | None, grep_context: int,
                         filter: Any) -> tuple[str | None, int]:
    """兼容旧版 filter 参数（dict {"regex","context"} 或直接正则串）→ grep/grep_context。

    已废弃：请改用 grep= / grep_context=。仅库路径兼容；MCP 层不暴露 filter。
    """
    if filter is None:
        return grep, grep_context
    warnings.warn("filter 参数已废弃，请改用 grep= 和 grep_context=",
                  DeprecationWarning, stacklevel=3)
    if grep is not None:          # 新参数优先
        return grep, grep_context
    if isinstance(filter, dict):
        return filter.get("regex"), filter.get("context", 0)
    return filter, grep_context   # 兼容直接传正则串


def _finish(stdout: str, stderr: str, receipt: dict[str, Any],
            grep: str | None = None, grep_context: int = 0) -> dict[str, Any]:
    if grep:
        stdout, fout = _grep(stdout, grep, grep_context)
        stderr, ferr = _grep(stderr, grep, grep_context)
        receipt_result_filter = {
            "grep": grep,
            "stdout_matched": fout.get("matched", 0),
            "stderr_matched": ferr.get("matched", 0),
            "stdout_lines": fout.get("total_lines", 0),
            "stderr_lines": ferr.get("total_lines", 0),
        }
        if "error" in fout:
            receipt_result_filter["stdout_error"] = fout["error"]
        if "error" in ferr:
            receipt_result_filter["stderr_error"] = ferr["error"]
    else:
        receipt_result_filter = None
    stdout, tr1 = _bound(stdout)
    stderr, tr2 = _bound(stderr)
    out: dict[str, Any] = {"receipt": receipt, "stdout": stdout, "stderr": stderr,
                           "exit_code": receipt.get("exit_code")}   # 兼容旧版顶层 exit_code（废弃，请用 receipt.exit_code）
    trunc: dict[str, Any] = {}
    if tr1:
        trunc["stdout"] = tr1
    if tr2:
        trunc["stderr"] = tr2
    if trunc:
        out["truncated"] = trunc
    if receipt_result_filter:
        out["filter"] = receipt_result_filter
    return out


# ============================ 防护与审计 ============================

_DANGER: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\brm\s+(-[\w-]+\s+)*-[\w-]*[rf][\w-]*\s+(/|/\*|~|\$HOME)\s*$", re.I), "递归删除根/家目录"),
    (re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;:"), "fork 炸弹"),
    (re.compile(r"\b(mkfs(\.\w+)?|fdisk|diskpart)\b", re.I), "磁盘分区/格式化"),
    (re.compile(r"\bformat\s+[a-zA-Z]:", re.I), "格式化磁盘"),
    (re.compile(r"\bdd\b[^|]*\bof=/dev/", re.I), "dd 直写块设备"),
    (re.compile(r"\b(shutdown|reboot|poweroff|halt)\b", re.I), "关机/重启"),
    (re.compile(r"\b(rd|rmdir|del|erase)\s+(/[sqSQ]\s+)*[a-zA-Z]:\\?\s*$", re.I), "整盘删除"),
]


def _guard(cmd: str) -> None:
    for pat, desc in _DANGER:
        if pat.search(cmd):
            raise AIShellError(
                "DANGER_BLOCKED",
                f"拒绝执行：{desc}（命中灾难模式）。本工具不做静默改写；如确需执行，请人工在终端运行",
                detail={"matched": desc})


def _check_cwd(cwd: str | None, allow_outside: bool) -> Path:
    if cwd:
        p = Path(cwd).expanduser()
        if not p.is_absolute():          # 相对路径以工作区为基准，而非进程 cwd
            p = WORKSPACE / p
        d = p.resolve()
    else:
        d = WORKSPACE
    if not allow_outside and d != WORKSPACE and WORKSPACE not in d.parents:
        raise AIShellError(
            "OUTSIDE_WORKSPACE",
            f"cwd {d} 在工作区 {WORKSPACE} 之外；确认后传 allow_outside=True",
            detail={"cwd": str(d), "workspace": str(WORKSPACE)})
    if not d.is_dir():
        raise AIShellError("INVALID_CWD", f"cwd 不存在或不是目录：{d}",
                           detail={"cwd": str(d)})
    return d


def _audit(kind: str, cmd: str, extra: dict[str, Any]) -> None:
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": round(time.time(), 3), "kind": kind, "cmd": cmd[:500],
               "shell": extra.get("shell"), "cwd": extra.get("cwd"),
               "exit_code": extra.get("exit_code"),
               "duration_ms": extra.get("duration_ms")}
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 审计失败不阻塞执行


def history(n: int = 20) -> list[dict[str, Any]]:
    """最近 n 条执行记录（跨进程持久，agent 失忆时重建上下文用）"""
    if not HISTORY_FILE.exists():
        return []
    n = max(1, min(n, 1000))   # 上限防一次性读入超大历史
    with open(HISTORY_FILE, encoding="utf-8") as f:
        lines = f.readlines()[-n:]
    recs: list[dict[str, Any]] = []
    for l in lines:
        l = l.strip()
        if not l:
            continue
        try:
            recs.append(json.loads(l))
        except ValueError:      # JSONDecodeError 是 ValueError 子类：跳过损坏半行
            continue
    return recs


# ============================ 进程工具 ============================


def _popen_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _exec_argv(spec: ShellSpec, cmd: str) -> str | list[str]:
    """一次性执行的 spawn 参数（str 或 list，都可交给 subprocess.Popen）。

    Windows 下 subprocess 会把 list 交给 list2cmdline：嵌套引号被反斜杠转义，
    而 cmd.exe / PowerShell 都不认反斜杠转义 → `"C:\\...python.exe" -c "..."`
    被误解析成 `\\"...python.exe\\"` 这个不存在的命令。因此 Windows 全走整行
    字符串原样传递：cmd 用 `cmd /d /s /c "<cmd>"`，pwsh/powershell 用
    `<前缀> <cmd>`。注意 PowerShell -Command 把命令当脚本解析，需要
    PowerShell 方言（原生命令请用默认的 cmd）。POSIX 用 list（原生 argv 传递）。
    """
    if os.name == "nt":
        if spec.name == "cmd":
            return 'cmd /d /s /c "' + cmd + '"'
        return subprocess.list2cmdline(spec.exec_argv) + " " + cmd
    return spec.exec_argv + [cmd]


def _kill_tree(proc: subprocess.Popen) -> None:
    """杀整个进程组/进程树，不留孤儿。

    Windows 优先 taskkill /F /T（连孙进程一起杀）。taskkill 在沙箱/受限令牌下
    可能被拒（返回非 0，或抛 TimeoutExpired），此时退化为 proc.kill()
    （TerminateProcess，只杀直接子进程）。绝不能静默失败后留下活进程，
    让调用方后续的 proc.wait() 永久阻塞。
    """
    try:
        if os.name == "nt":
            killed = False
            try:
                r = subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                   capture_output=True, timeout=10)
                killed = r.returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                killed = False
            if not killed:
                proc.kill()
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _read_pump(stream, buf: bytearray) -> None:
    """把流逐行读进 buf。readline 每 flush 一行就返回，不会像 read(4096)
    那样在 Windows 管道上等满缓冲/EOF。"""
    for line in iter(stream.readline, b""):
        buf.extend(line)


def _communicate(proc: subprocess.Popen, timeout: float) -> tuple[bytes, bytes, bool]:
    """带超时地收集 stdout/stderr，并保留「已 flush 的部分输出」。

    subprocess.communicate(timeout) 内部用 read() 读管道：Windows 上 read()
    要等满缓冲或 EOF 才返回，超时杀进程后 e.output 是 None——「超时返回部分
    输出」拿不到任何东西。这里改用 readline 泵：进程 flush 过的一行立刻能被
    读到，超时后仍能拿到这部分。
    """
    out_buf, err_buf = bytearray(), bytearray()
    threads = [
        threading.Thread(target=_read_pump, args=(proc.stdout, out_buf), daemon=True),
        threading.Thread(target=_read_pump, args=(proc.stderr, err_buf), daemon=True),
    ]
    for t in threads:
        t.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            proc.wait(timeout=5)   # 兜底：进程拒不退出也不阻塞主线程
        except subprocess.TimeoutExpired:
            pass
    for t in threads:
        t.join(timeout=2)
    return bytes(out_buf), bytes(err_buf), timed_out


# ============================ 一次性执行 ============================


def exec_command(cmd: str, cwd: str | None = None, timeout: float = DEFAULT_TIMEOUT,
                 grep: str | None = None, grep_context: int = 0,
                 background: bool = False, allow_outside: bool = False,
                 shell: str | None = None, filter: Any = None) -> dict[str, Any]:
    """执行一条命令并返回结构化回执。

    grep 是正则：命中行 ± grep_context 行在服务端投影（stdout/stderr 各自过滤）。
    background=True 立即返回 job_id，用 job_output 轮询。
    filter 是旧版参数（已废弃，用 grep/grep_context 替代），保留仅作库路径兼容。
    """
    grep, grep_context = _merge_legacy_filter(grep, grep_context, filter)
    _guard(cmd)
    if timeout is not None and timeout <= 0:
        raise AIShellError("INVALID_TIMEOUT", "timeout 必须为正数（秒）")
    _validate_grep(grep)
    d = _check_cwd(cwd, allow_outside)
    spec = detect_shell() if shell is None else get_shell(shell)
    if background:
        return _start_job(cmd, d, timeout, spec, grep, grep_context)

    t0 = time.time()
    proc = subprocess.Popen(_exec_argv(spec, cmd), cwd=str(d),
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            **_popen_kwargs())
    out_b, err_b, timed_out = _communicate(proc, timeout)
    receipt = {"exit_code": None if timed_out else proc.returncode,
               "duration_ms": int((time.time() - t0) * 1000),
               "cwd": str(d), "shell": spec.name, "timed_out": timed_out,
               # exec 超时会 _kill_tree（连孙进程），所以不论超时与否进程都已终止。
               # 这个键在三条路径（exec / 终端 / 后台）上**都出现**（r160 统一）：
               # 修前只有终端超时路径带它，而 README 读起来像它恒在（Muse 报的 A 档）。
               "still_running": False}
    result = _finish(_decode(out_b), _decode(err_b), receipt, grep, grep_context)
    _audit("exec", cmd, {**receipt})
    return result


# ============================ 后台任务 ============================

JOB_TTL = float(os.environ.get("AI_SHELL_JOB_TTL", "3600"))  # 秒：已完成任务保留时长

JOBS: dict[str, dict[str, Any]] = {}


def _gc_jobs() -> None:
    """惰性回收已结束且超 TTL 的任务；running 任务不动。"""
    now = time.time()
    for jid in [k for k, j in JOBS.items()
                if j.get("finished") and now - j["finished"] > JOB_TTL]:
        JOBS.pop(jid, None)


def _start_job(cmd: str, cwd: Path, timeout: float, spec: ShellSpec,
               grep: str | None = None, grep_context: int = 0) -> dict[str, Any]:
    _gc_jobs()
    jid = uuid.uuid4().hex[:8]
    lock = threading.Lock()
    job: dict[str, Any] = {"job_id": jid, "cmd": cmd, "status": "running",
                           "stdout": bytearray(), "stderr": bytearray(),
                           "lock": lock, "finished": None,
                           "exit_code": None, "timed_out": False,
                           "duration_ms": 0, "started": time.time(),
                           "shell": spec.name, "cwd": str(cwd),
                           "grep": grep, "grep_context": grep_context}
    JOBS[jid] = job

    def pump(stream, key: str) -> None:
        """把子进程输出逐行实时 append 进 job（加锁，供 job_logs 流式读取）"""
        for line in iter(stream.readline, b""):
            with lock:
                job[key].extend(line)

    def run() -> None:
        proc = subprocess.Popen(_exec_argv(spec, cmd), cwd=str(cwd),
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                **_popen_kwargs())
        threads = [
            threading.Thread(target=pump, args=(proc.stdout, "stdout"), daemon=True),
            threading.Thread(target=pump, args=(proc.stderr, "stderr"), daemon=True),
        ]
        for t in threads:
            t.start()
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for t in threads:
            t.join(timeout=2)
        with lock:
            if timed_out:
                job["status"] = "timeout"
                job["timed_out"] = True
            else:
                job["exit_code"] = proc.returncode
                job["status"] = "done" if proc.returncode == 0 else "failed"
            job["duration_ms"] = int((time.time() - job["started"]) * 1000)
            job["finished"] = time.time()

    threading.Thread(target=run, daemon=True, name=f"ai-shell-job-{jid}").start()
    _audit("exec-bg", cmd, {"shell": spec.name, "cwd": str(cwd)})
    return {"job_id": jid, "status": "running",
            # 刚提交的后台任务当然还在跑（见 README 的 still_running 语义表）
            "receipt": {"exit_code": None, "duration_ms": 0, "cwd": str(cwd),
                        "shell": spec.name, "timed_out": False,
                        "still_running": True},
            "stdout": "", "stderr": ""}


def job_logs(job_id: str) -> dict[str, Any]:
    _gc_jobs()
    job = JOBS.get(job_id)
    if not job:
        raise AIShellError("JOB_NOT_FOUND",
                           f"任务 {job_id} 不存在（进程重启后任务不保留）；请用 background=True 重新执行",
                           detail={"job_id": job_id})
    with job["lock"]:
        out_b = bytes(job["stdout"])
        err_b = bytes(job["stderr"])
        status = job["status"]
        exit_code = job["exit_code"]
        timed_out = job["timed_out"]
        duration = job.get("duration_ms", 0)
        started = job["started"]
    receipt = {"exit_code": exit_code, "shell": job["shell"],
               "cwd": job["cwd"], "timed_out": timed_out,
               "duration_ms": duration or int((time.time() - started) * 1000),
               # running / timeout 都还在跑吗？timeout 路径已 _kill_tree ⇒ False（见 README 语义表）
               "still_running": status == "running"}
    result = _finish(_decode(out_b), _decode(err_b),
                     receipt, job.get("grep"), job.get("grep_context", 0))
    result["job_id"] = job_id
    result["status"] = status
    return result


# ============================ 持久终端 ============================


class Terminal:
    """管道实现的持久 shell：cd/export 跨命令生效，全平台行为一致。

    已知限制：不是真 PTY，全屏程序（vim/top）与从 stdin 读密码的程序不可用；
    超时无法可靠中断运行中的命令（Windows 管道无法送 Ctrl+C），
    超时后建议 terminal_close 重开。
    """

    def __init__(self, shell: str | None = None, cwd: str | None = None):
        self.id = uuid.uuid4().hex[:8]
        self.spec = detect_shell() if shell is None else get_shell(shell)
        d = _check_cwd(cwd, allow_outside=False)
        self.cwd = str(d)
        self.proc = subprocess.Popen(
            self.spec.argv, cwd=self.cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, **_popen_kwargs())
        self._out = bytearray()
        self._err = bytearray()
        self._cond = threading.Condition()
        self._send_lock = threading.Lock()
        self._closed = False
        self.last = time.time()
        for stream, buf in ((self.proc.stdout, self._out), (self.proc.stderr, self._err)):
            threading.Thread(target=self._pump, args=(stream, buf),
                             daemon=True, name=f"ai-shell-pump-{self.id}").start()
        for b in self.spec.boot:  # 编码初始化，回执丢弃
            self.send(b, timeout=10)

    def _pump(self, stream, buf: bytearray) -> None:
        while True:
            try:
                chunk = stream.read(4096)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            with self._cond:
                buf.extend(chunk)
                self.last = time.time()   # 有输出即续期，长命令不被误回收
                self._cond.notify_all()

    def send(self, cmd: str, timeout: float = DEFAULT_TIMEOUT,
             grep: str | None = None, grep_context: int = 0) -> dict[str, Any]:
        with self._send_lock:
            return self._send_locked(cmd, timeout, grep, grep_context)

    def _send_locked(self, cmd: str, timeout: float,
                     grep: str | None, grep_context: int) -> dict[str, Any]:
        if self._closed:
            raise AIShellError("TERMINAL_NOT_FOUND", f"终端 {self.id} 已关闭",
                               detail={"terminal_id": self.id, "status": "closed"})
        self.last = time.time()   # 活动即续期：任何 send（含随后超时的）都续期
        _guard(cmd)
        if timeout is not None and timeout <= 0:
            raise AIShellError("INVALID_TIMEOUT", "timeout 必须为正数（秒）")
        _validate_grep(grep)
        s = uuid.uuid4().hex[:12]
        with self._cond:
            mark_out, mark_err = len(self._out), len(self._err)
        nl = self.spec.newline
        payload = (cmd + nl + self.spec.sentinel_tpl.format(s=s) + nl
                   + self.spec.sentinel_err_tpl.format(s=s) + nl)
        try:
            self.proc.stdin.write(payload.encode("utf-8"))
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as e:
            raise AIShellError("TERMINAL_WRITE_FAILED",
                               f"终端 {self.id} 已关闭或进程退出（写入失败）",
                               detail={"terminal_id": self.id}) from e
        rx = re.compile(re.escape(f"@@{s}:").encode() + rb"(-?\d*):(.*?)@@")
        rx_err = re.compile(re.escape(f"@@{s}:ERR@@").encode())
        t0 = time.time()
        m = None
        m_err = None
        with self._cond:
            while True:
                if m is None:
                    m = rx.search(bytes(self._out[mark_out:]))
                if m_err is None:
                    m_err = rx_err.search(bytes(self._err[mark_err:]))
                if m and m_err:
                    break
                remaining = timeout - (time.time() - t0)
                if remaining <= 0:
                    break
                self._cond.wait(min(remaining, 0.2))
        duration = int((time.time() - t0) * 1000)
        if not m:
            with self._cond:
                partial = bytes(self._out[mark_out:])
            receipt = {"exit_code": None, "duration_ms": duration, "cwd": None,
                       "shell": self.spec.name, "timed_out": True,
                       "still_running": True, "terminal_id": self.id,
                       # 这次调用**实际捕获到**的 stdout 字节数（截断前）。给它是为了让调用方
                       # 能区分「程序一个字都还没吐出来」与「已经吐了但命令没结束」——
                       # 前者多半是：①子进程块缓冲还没 flush（管道不是 tty），②在等 stdin。
                       # 这两种 ai_shell 从外面**分辨不了**（没有 PTY，见 README 已知代价）：
                       # 所以只报可得的事实，不猜一个 waiting_for_stdin 出来。
                       "captured_bytes": len(partial)}
            # 告警必须在 grep 投影**之后**追加：否则调用方只要传了 grep，唯一那句
            # 「命令可能仍在运行」就被当普通输出过滤掉——命中的话交付只剩命令输出，
            # 不命中的话交付 "[filter 无匹配，输出共 N 行]"，把 agent 引去改自己的正则，
            # 而真实情况是命令还在跑（B 档：响但方向误导）。r155 修，回归见 S16。
            # 代价：告警不计入 _bound 的预算，交付量可超 MAX_OUTPUT 约 40 字节——
            # 与「截断标记不计入上限」同一笔账（README「已知代价」），且告警必须能到。
            result = _finish(_decode(partial), "", receipt, grep, grep_context)
            result["stdout"] += TIMEOUT_NOTICE
            _audit("terminal-send", cmd, {**receipt})
            return result
        with self._cond:
            out_b = bytes(self._out[mark_out:mark_out + m.start()])
            err_b = (bytes(self._err[mark_err:mark_err + m_err.start()])
                     if m_err else bytes(self._err[mark_err:]))
        ec_raw = m.group(1).strip()
        exit_code = int(ec_raw) if ec_raw else 0  # PowerShell 纯 cmdlet 时 $LASTEXITCODE 为空
        cwd = _decode(m.group(2)).strip()
        self.last = time.time()
        receipt = {"exit_code": exit_code, "duration_ms": duration, "cwd": cwd,
                   "shell": self.spec.name, "timed_out": False,
                   "still_running": False, "terminal_id": self.id,
                   "captured_bytes": len(out_b)}
        result = _finish(_decode(out_b), _decode(err_b), receipt, grep, grep_context)
        _audit("terminal-send", cmd, {**receipt})
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True   # 先置标志：后续 send 立即拒绝
        try:
            _kill_tree(self.proc)
        finally:
            try:
                self.proc.stdin.close()   # 让阻塞中的 write 尽快失败
            except OSError:
                pass


TERMINALS: dict[str, Terminal] = {}


def _gc() -> None:
    now = time.time()
    for tid in [k for k, t in TERMINALS.items() if now - t.last > TERMINAL_TTL]:
        TERMINALS.pop(tid).close()


@atexit.register
def _shutdown() -> None:
    for t in list(TERMINALS.values()):
        t.close()


def terminal_open(shell: str | None = None, cwd: str | None = None) -> dict[str, Any]:
    """开启持久终端；首个回执携带 env（OS/shell/工具版本），省去探针往返"""
    _gc()
    t = Terminal(shell, cwd)
    TERMINALS[t.id] = t
    return {"terminal_id": t.id, "env": env_info(),
            "receipt": {"shell": t.spec.name, "cwd": t.cwd}}


def terminal_send(terminal_id: str, cmd: str, timeout: float = DEFAULT_TIMEOUT,
                  grep: str | None = None, grep_context: int = 0,
                  filter: Any = None) -> dict[str, Any]:
    grep, grep_context = _merge_legacy_filter(grep, grep_context, filter)
    _gc()
    t = TERMINALS.get(terminal_id)
    if not t:
        raise AIShellError("TERMINAL_NOT_FOUND",
                           f"终端 {terminal_id} 不存在或已过期；请用 terminal_open_shell 重开",
                           detail={"terminal_id": terminal_id, "status": "expired"})
    return t.send(cmd, timeout=timeout, grep=grep, grep_context=grep_context)


def terminal_close(terminal_id: str) -> bool:
    t = TERMINALS.pop(terminal_id, None)
    if t:
        t.close()
        return True
    return False


# ============================ 环境自检 ============================


def env_info() -> dict[str, Any]:
    """OS / 架构 / shell / 常用工具版本 / 工作区，一份回执省掉全部探针命令"""
    spec = detect_shell()
    info: dict[str, Any] = {
        "os": platform.system(), "release": platform.release(),
        "arch": platform.machine(), "python": platform.python_version(),
        "shell": spec.name, "workspace": str(WORKSPACE),
    }
    for tool in ("git", "node", "npm", "ffmpeg"):
        p = shutil.which(tool)
        if not p:
            continue
        try:
            r = subprocess.run([p, "--version"], capture_output=True, timeout=5)
            info[tool] = _decode(r.stdout).strip().splitlines()[0][:80]
        except Exception:
            info[tool] = p
    return info
