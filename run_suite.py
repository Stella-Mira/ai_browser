# -*- coding: utf-8 -*-
"""套件运行器：**fail-closed** —— 不能验证 ⇒ 红，不许「没跑」被当成「跑绿了」。

为什么需要它（判级 agent 第二十五轮的裁定）：
> **退出码 0 只证明「进程正常结束」，不证明「测试跑过」。**

实测踩过的那一次：两个后台作业写同一个 `.tmp_suite_utf8.txt`，后一个重定向失败，
PowerShell 的 `Out-File` 报错、而 `$LASTEXITCODE` 仍然打印 0 ——**「没跑」看起来和「跑绿了」一样**。
唯一输出文件名只是**治标**；治本是：**套件的结论不能只看退出码，要看输出存在、非空、且有完整结论**。
这条与「未验证会红的检查 = 待实测的检查」是同一枚硬币：**绿必须是真绿，不是「没红」。**

用法：
    python run_suite.py                 # 跑一轮（UTF-8 控制台），日志名带时间戳，跑完即校验
    python run_suite.py --gbk           # 用默认 GBK 控制台跑（中文 Windows 的真环境）
    python run_suite.py --min-pass 123  # 断言至少这么多条 PASS（防止「跑了一半」也算绿）
    python run_suite.py --suite ai_shell        # 跑另一个套件（test_ai_shell.py，下限 25）
    python run_suite.py --suite ai_shell --gbk  # 同上，GBK 控制台
    python run_suite.py --check-log X   # 只校验已有日志（T114 用它验证 fail-closed 真的会红）

`--suite` 缺省仍是 `ai_browser`（跑 `test_v03.py`），日志名、日志体、输出行都**与加 `--suite` 之前一致**；
`check_log()` 依旧是**唯一**判据实现，没有为任何套件放宽一行。

判据（全部满足才算绿，任何一条不满足即退出码 1）：
    ① 日志文件**存在**且**非空**；
    ② 至少一条 `PASS ` 行（证明测试真的跑过、而不是空文件/半截文件）；
    ③ **零**条 `FAIL ` 行；
    ④ PASS 条数 ≥ `--min-pass`（缺省取该套件的下限；调用方给真实下限才有意义）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
PASS_RE = re.compile(r"^PASS\s", re.M)
FAIL_RE = re.compile(r"^FAIL\s", re.M)

DEFAULT_SUITE = "ai_browser"
# 每个套件：跑哪个文件 + 它自己的【下限】。下限是判据④那道闸（防「跑了一半也算绿」），
# 所以它是**每套件的事实**，不是全局一个数；`--min-pass` 显式给出时以显式值为准。
# ai_browser 的下限刻意保持 1：**`--suite` 缺省时必须与加 `--suite` 之前逐字节一致**。
# ai_shell 的下限要跟着断言数走：**加断言后要把它抬上去**，否则「跑了一半」也能绿。
SUITES: dict[str, tuple[str, int]] = {
    "ai_browser": ("test_v03.py", 1),
    "ai_shell": ("test_ai_shell.py", 43),
}


def check_log(path: pathlib.Path, min_pass: int) -> tuple[bool, str]:
    """**fail-closed 的核心判据**（唯一实现：跑完校验与 `--check-log` 都调它）。

    返回 (是否绿, 说明)。任何「无法验证」的情况（文件不存在、空、没有结论行）**一律判红**——
    这正是「不能验证 ⇒ 红」那条：检查的结果域必须是封闭的 {通过 / 不通过 / 无法验证}，
    而「无法验证」归「不通过」，绝不归「通过」。
    """
    if not path.exists():
        return False, f"日志不存在：{path}（**无法验证 ⇒ 红**）"
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return False, f"日志是空的：{path}（**无法验证 ⇒ 红**）"
    n_pass, n_fail = len(PASS_RE.findall(text)), len(FAIL_RE.findall(text))
    if n_pass == 0:
        return False, f"日志里一条 PASS 都没有（跑了半截 / 结论被吞）：{path}"
    if n_fail:
        names = FAIL_RE.findall(text)
        return False, f"有 {n_fail} 条 FAIL（{names[:3]}）"
    if n_pass < min_pass:
        return False, f"PASS 只有 {n_pass} 条，低于下限 {min_pass}（可能跑了一半）"
    return True, f"PASS {n_pass} 条 / FAIL 0 条"


def main() -> int:
    ap = argparse.ArgumentParser(description="fail-closed 的套件运行器")
    ap.add_argument("--suite", choices=sorted(SUITES), default=DEFAULT_SUITE,
                    help=f"跑哪个套件（默认 {DEFAULT_SUITE}）；每个套件有各自的下限")
    ap.add_argument("--gbk", action="store_true", help="用默认 GBK 控制台跑（中文 Windows 真环境）")
    ap.add_argument("--min-pass", type=int, default=None,
                    help="断言至少这么多条 PASS（缺省取该套件的下限）")
    ap.add_argument("--check-log", default=None, help="只校验已有日志，不跑套件")
    ap.add_argument("--log", default=None, help="日志路径（默认带时间戳，避免并发互相覆盖）")
    args = ap.parse_args()

    suite_file, suite_min = SUITES[args.suite]
    min_pass = suite_min if args.min_pass is None else args.min_pass

    if args.check_log:
        ok, why = check_log(pathlib.Path(args.check_log), min_pass)
        print(("[OK] " if ok else "[X] ") + why)
        return 0 if ok else 1

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.log:
        log = pathlib.Path(args.log)
    elif args.suite == DEFAULT_SUITE:
        log = HERE / f".tmp_suite_{stamp}.txt"          # 与加 --suite 之前**同一个命名**
    else:
        log = HERE / f".tmp_suite_{args.suite}_{stamp}.txt"
    env = dict(os.environ)
    if args.gbk:
        env.pop("PYTHONIOENCODING", None)          # 用控制台自己的编码（GBK 真环境）
    else:
        env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run([sys.executable, str(HERE / suite_file)],
                          cwd=str(HERE), env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    # **stderr 也要落盘**（第二十六轮实测教训）：只记 stdout 时，套件在打印任何结论之前就崩掉，
    # 日志会是**空的**——fail-closed 会把它判红（对的），但**没人知道为什么红**。
    # 报警必须可解释，所以把 stderr 一起写进同一份日志（并明确分隔）。
    # **先把「环境」写进日志**（对抗评审第 28 轮的建议）：解释器版本/平台落盘之后，
    # 「**导入期**就崩（如 Python < 3.12 的 f-string 不许带反斜杠）」与「**真 FAIL**」在日志里
    # 一眼可区分——两者的表象都是「一条 PASS 都没有」，但处置方向相反（换解释器 vs 改代码）。
    # 这条与 T114 同源：**报警必须可解释**。
    # （注意这里用 chr(10) 而不是 \n：本文件本身就要能在 3.11 上跑，别自己踩自己那条。）
    body = ("=== 环境 ===\n"
            f"解释器: {sys.executable}\n"
            f"版本: {sys.version.replace(chr(10), ' ')}\n"
            f"平台: {sys.platform}\n"
            f"控制台模式: {'gbk（默认控制台，中文 Windows 真环境）' if args.gbk else 'utf-8'}\n")
    if args.suite != DEFAULT_SUITE:      # 缺省套件的日志体保持与加 --suite 之前逐字节一致
        body += f"套件: {args.suite}（{suite_file}）\n"
    body += "=== 套件输出 ===\n"
    body += (proc.stdout or "")
    if (proc.stderr or "").strip():
        body += "\n=== stderr ===\n" + (proc.stderr or "")
    log.write_text(body, encoding="utf-8")
    ok, why = check_log(log, min_pass)
    # 退出码只是**第 0 道**证据：它证明进程正常结束，不证明测试跑过（判级 agent 的裁定）。
    tag = "" if args.suite == DEFAULT_SUITE else f"套件={args.suite} "
    print(f"{tag}退出码={proc.returncode} 日志={log.name} → " + ("[OK] " if ok else "[X] ") + why)
    if not ok and not (proc.stdout or "").strip() and (proc.stderr or "").strip():
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        print("  套件在出结论之前就退出了，stderr 末几行：")
        for line in tail:
            print("   | " + line[:160])
    if proc.returncode != 0 and ok:
        print("[X] 退出码非 0 而日志说绿：两者矛盾，按红处理（fail-closed）")
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
