"""命令行入口

用法：
  python -m ai_shell.cli "ls -la" [--cwd 目录] [--timeout 30]
  python -m ai_shell.cli "构建命令" --grep "ERROR" --grep-context 3
  python -m ai_shell.cli "长任务" --background
  python -m ai_shell.cli --job-logs <job_id>
  python -m ai_shell.cli --env-info
  python -m ai_shell.cli --history 20
"""
from __future__ import annotations

import argparse
import json
import sys

from .core import AIShellError, env_info, exec_command, history, job_logs

# 超时用 124（GNU `timeout(1)` 的约定），不是 1：调用方常把 1 当"命令失败"，
# 而"我没等到它结束"是另一回事——沿用同一个数字能让 `timeout` 包装过的脚本不改判据。
EXIT_TIMEOUT = 124


def _exit_code_of(result: object) -> int:
    """从回执里取**进程退出码**作为 CLI 自己的退出码（r186 修）。

    修前：`main()` 永远以 0 结束（`SystemExit(1)` 只在 AIShellError 分支），于是
    `python -m ai_shell.cli "..."` 跑一条 `sys.exit(7)` 的命令时**回执里 exit_code=7、
    进程退出码却是 0**——脚本/CI 看 `$?`/`%ERRORLEVEL%` 会以为成功（子智能体 B 报的，我复现过）。
    规则：回执里有多少就说多少；**没得说才回 0**：
      · `timed_out=True`         → 124（此时 `exit_code` 是 None，因为进程是被杀的）
      · `exit_code` 是 int       → 原样透传（含负数/非零）
      · 其余（后台刚提交、env_info、history、job_logs）→ 0（调用本身成功了）
    绝不把"取不到退出码"编成 1：那会把"提交成功"报成"失败"，是反方向的谎。
    """
    if not isinstance(result, dict):
        return 0
    receipt = result.get("receipt")
    if not isinstance(receipt, dict):
        return 0
    if receipt.get("timed_out"):
        return EXIT_TIMEOUT
    code = receipt.get("exit_code")
    return code if isinstance(code, int) and not isinstance(code, bool) else 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="ai-shell",
                                 description="跨系统命令行执行，结构化回执（为 agent 设计）")
    ap.add_argument("cmd", nargs="?", help="要执行的命令")
    ap.add_argument("--cwd", help="工作目录（默认工作区根目录）")
    ap.add_argument("--timeout", type=float, default=None, help="超时秒数（默认 60）")
    ap.add_argument("--grep", help="服务端正则过滤模式")
    ap.add_argument("--grep-context", type=int, default=0, help="过滤时保留的上下文行数")
    ap.add_argument("--background", action="store_true", help="后台执行，返回 job_id")
    ap.add_argument("--allow-outside", action="store_true", help="允许 cwd 在工作区之外")
    ap.add_argument("--shell", help="强制 shell：bash/sh/cmd/pwsh/powershell")
    ap.add_argument("--job-logs", metavar="JOB_ID", help="查看后台任务输出")
    ap.add_argument("--env-info", action="store_true", help="输出环境自检信息")
    ap.add_argument("--history", type=int, metavar="N", nargs="?", const=20,
                    help="最近 N 条执行记录")
    args = ap.parse_args()

    try:
        if args.env_info:
            result = env_info()
        elif args.job_logs:
            result = job_logs(args.job_logs)
        elif args.history is not None:
            result = history(args.history)
        elif args.cmd:
            kwargs: dict = {}
            if args.timeout is not None:
                kwargs["timeout"] = args.timeout
            result = exec_command(args.cmd, cwd=args.cwd, grep=args.grep,
                                  grep_context=args.grep_context,
                                  background=args.background,
                                  allow_outside=args.allow_outside, shell=args.shell, **kwargs)
        else:
            ap.error("需要提供 cmd 或 --env-info / --job-logs / --history")
    except AIShellError as e:
        sys.stdout.write(json.dumps({"ok": False, "error": e.to_error()},
                                    ensure_ascii=False, indent=2) + "\n")
        raise SystemExit(1)

    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    raise SystemExit(_exit_code_of(result))


if __name__ == "__main__":
    main()
