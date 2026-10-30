"""ai_shell 回归测试（跨平台：命令全部用 python -c 构造，Windows/Linux/macOS 均可跑）

  S1  exec 基本执行与 exit_code
  S2  非零退出码传播
  S3  stdout/stderr 分离
  S4  超长输出截断（带元数据）
  S5  服务端正则过滤
  S6  超时返回部分输出并杀进程树
  S7  后台任务 + job_logs 轮询
  S8  持久终端：cwd 与变量跨命令生效（S8a 首个回执带 env；S8b cd 跨命令生效）
  S9  灾难命令拦截
  S10 工作区外 cwd 拦截 / allow_outside 放行
  S11 env_info 字段完整
  S12 审计历史落盘
  S13 fs 写读 + 截断
  S14 fs 列目录 / 元数据 / 越界
  S15 fs glob / 移动 / 删除
  S16 终端超时告警必须穿透 grep 投影（回归：r155 修前这条红）
  S17 绊线：截断标记必须完整 + 超出量有界（**未采纳判据的代价面**，见下）
  S18 remove_path 对链接只删链接本身（P0，S 级；r160 修前删的是目标）
  S19 move_path 对链接搬链接本身（与 S18 同族）
  S20 read_file 截断点落在字符边界、不产替换符 + delivered_bytes 属实（P1，A 级）
  S21 still_running 在 exec/后台/终端三条路径上都有 + 终端回执带 captured_bytes（P2.2，A 级）
  S22 两个**输入边界**：glob 不许逃出工作区 + 非正 max_bytes 必须响亮拒绝（r185）
  S23 「让失败说得清楚」：CLI 退出码透传 + 漏出信封的异常收进 AIShellError（r186）

用法：python run_suite.py --suite ai_shell --min-pass 25      ← 推荐：fail-closed 验收（UTF-8）
      python run_suite.py --suite ai_shell --gbk --min-pass 25 # 同上，GBK 控制台
      python test_ai_shell.py          ← 只跑一遍（不是 UTF-8 控制台时请设 PYTHONIOENCODING=utf-8）

**结论看输出里的 PASS/FAIL 行数，不要只看退出码**——退出码 0 只证明「进程正常结束」，
不证明「测试跑过」（仓库里为这条吃过亏）。正式验收走 `run_suite.py --suite ai_shell`：它调
`check_log()`（判据只有一处实现），不存在 / 空 / 无结论 / 有 FAIL / 低于下限**一律判红**。

S17 是**绊线**，不是普通回归：它钉的是「交付量 = 上限 + 标记长度」这条**尚未采纳**判据的
**不可接受代价面**——为了守住「交付 ≤ 上限」而把截断标记裁掉（＝静默截断）。
将来谁要落地「上限就是上限」，**先让 S17 红**：红即代价超界，不红才配落地
（见 README 的「已知代价」第 1 条与「绊线」规约）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
os.environ["AI_SHELL_WORKSPACE"] = str(HERE)   # 必须在 import 前设置
sys.path.insert(0, str(HERE))

from ai_shell import (env_info, exec_command, glob_paths, history,  # noqa: E402
                      job_logs, list_dir, mkdir_path, move_path, read_file,
                      remove_path, stat_path, terminal_close, terminal_open,
                      terminal_send, write_file)

PY = sys.executable
results: dict[str, bool] = {}
# 本次运行的起点。S12 用它区分「**这次真的写了审计**」与「前几次留下的旧记录」——
# 审计日志是**跨运行累积**的 `$WORKSPACE/.ai_shell/history.jsonl`，
# 只断言 `len(history) >= 5` 的话，前几次的记录就能让它恒真（那是装饰，见 S12 注释）。
T0 = time.time()

# S1 基本执行
r = exec_command(f'"{PY}" -c "print(\'hello ai_shell\')"')
results["S1 基本执行"] = r["receipt"]["exit_code"] == 0 and "hello ai_shell" in r["stdout"]

# S2 非零退出码
r = exec_command(f'"{PY}" -c "import sys; sys.exit(3)"')
results["S2 退出码传播"] = r["receipt"]["exit_code"] == 3

# S3 stderr 分离
r = exec_command(f'"{PY}" -c "import sys; print(\'out\'); print(\'err\', file=sys.stderr)"')
results["S3 stderr分离"] = "out" in r["stdout"] and "err" in r["stderr"] \
    and "err" not in r["stdout"]

# S4 截断
r = exec_command(f'"{PY}" -c "print(\'x\' * 20000)"')
results["S4 截断带元数据"] = "truncated" in r and \
    r["truncated"]["stdout"]["total_bytes"] > 20000 and len(r["stdout"]) < 10000

# S5 正则过滤
r = exec_command(f'"{PY}" -c "print(\'INFO ok\'); print(\'ERROR boom\'); print(\'INFO done\')"',
                 grep="ERROR")
results["S5 服务端过滤"] = "ERROR boom" in r["stdout"] and "INFO ok" not in r["stdout"] \
    and r["filter"]["stdout_matched"] == 1

# S6 超时
t0 = time.time()
r = exec_command(f'"{PY}" -c "import time; print(\'start\', flush=True); time.sleep(30)"',
                 timeout=2)
results["S6 超时部分输出"] = r["receipt"]["timed_out"] is True and "start" in r["stdout"] \
    and time.time() - t0 < 15

# S7 后台任务
# ⚠️ **本项原来把"2.5 秒内到 done"当判据（固定 sleep），那是一条墙钟判据**——
# 与 T119/T121 那两条是同一类（win-node 的裁定："墙钟判据不许进断言"）。
# 实测账：① 第一次红（未归因，`0/20 超`的余量测量据此做过：1.13–1.26s vs 2.5s）；
#         ② 第二次红：在**并发跑 ai_browser 套件**（浏览器很重）时，固定预算被挤爆。
# ⇒ 改法**只动"等到什么时候"，不动"断言什么"**：轮询到死线（而不是睡固定值），
#   断言内容原样保留（任务要能到 done、回执要带输出、timed_out 要为 False）。
r = exec_command(f'"{PY}" -c "import time; time.sleep(1); print(\'bg done\')"',
                 background=True)
jid = r["job_id"]
_deadline = time.time() + 15          # 死线给足：它不再是"性能判据"，只是防挂死
logs = job_logs(jid)
while logs["status"] == "running" and time.time() < _deadline:
    time.sleep(0.25)
    logs = job_logs(jid)
results["S7 后台任务"] = logs["status"] == "done" and "bg done" in logs["stdout"] \
    and logs["receipt"]["timed_out"] is False

# S8 持久终端：cd 与变量跨命令生效
t = terminal_open()
tid = t["terminal_id"]
# S8a：不只查「键在不在」——`workspace` 必须**就是**这次设的工作区（值级断言，键级会放走错值）
results["S8a 首个回执带env"] = ("os" in t["env"] and "shell" in t["env"]
                                and t["env"].get("workspace") == str(HERE))
# 用 shell 原生命令改目录（python 子进程改不了父 shell 的 cwd）
r1 = terminal_send(tid, "cd ..")
r2 = terminal_send(tid, "AI_VAR=42" if r1["receipt"]["shell"] in ("bash", "sh")
                   else "set AI_VAR=42")
r3 = terminal_send(tid, f'"{PY}" -c "import os; print(os.getcwd())"')
results["S8b cd跨命令生效"] = r3["receipt"]["cwd"] == str(HERE.parent) or \
    str(HERE.parent) in r3["stdout"]
terminal_close(tid)

# S9 灾难拦截
# 断言**错误码**而不只是文案：文案可能被改写，而 `code` 是机器可读的契约。
# （弱形状 `"拒绝执行" in str(e)` 只证明"抛了、且文案里有那四个字"；代码是这次的 8 条之一，见提交信息。）
try:
    exec_command("rm -rf /")
    results["S9 灾难拦截"] = False
except ValueError as e:
    results["S9 灾难拦截"] = (getattr(e, "code", None) == "DANGER_BLOCKED"
                              and "拒绝执行" in str(e))

# S10 工作区限定
try:
    exec_command(f'"{PY}" -c "print(1)"', cwd=str(HERE.parent.parent))
    results["S10a 越界拦截"] = False
except ValueError as e:
    # 断言**拦住它的那一个**错误码。只写 `except ValueError: True` 会放过"因别的原因抛错"——
    # 本次审计里 S10a **没有**成为装饰（实测：旁路掉检查后它**不抛**了 ⇒ 它会红 ⇒ 有牙齿），
    # 但"不查原因"仍然是弱形状，故一并收紧。
    results["S10a 越界拦截"] = getattr(e, "code", None) == "OUTSIDE_WORKSPACE"
r = exec_command(f'"{PY}" -c "print(1)"', cwd=str(HERE.parent.parent), allow_outside=True)
results["S10b allow_outside放行"] = r["receipt"]["exit_code"] == 0

# S11 env_info：**键 + 值**。只查键的话，"workspace 报成别的目录"这种错会被放走。
info = env_info()
results["S11 env_info"] = (all(k in info for k in ("os", "arch", "shell", "python", "workspace"))
                           and info.get("workspace") == str(HERE)
                           and bool(info.get("shell")))

# S12 审计历史
# ⚠️ **这条原来是一个装饰**（本次审计抓到）：日志是**跨运行累积**的，
# 审计**这次一条都没写**时，前几次留下的记录**照样**满足 `len >= 5`。
# 实测：跑之前日志里已有 316 条，其中紧邻本次的那批距今约 35453 秒（≈9.8 小时）——
# 也就是说旧写法在任何一次运行里都恒真。⇒ 必须再要求**本次运行写过至少一条**（ts >= T0）。
h = history(50)
results["S12 审计历史"] = (len(h) >= 5 and any("exit_code" in rec for rec in h)
                          and any(rec.get("ts", 0) >= T0 for rec in h))

# S13 文件系统：写 + 读 + 截断
_w = write_file("_fs_test.txt", "hello ai_shell fs")
_r = read_file("_fs_test.txt")
results["S13 fs写读"] = _w["written_bytes"] == 17 and _r["content"] == "hello ai_shell fs"
_rb = read_file("_fs_test.txt", max_bytes=4)
results["S13b fs截断"] = _rb["truncated"] is True and "截断" in _rb["content"]

# S14 文件系统：列目录 / 元数据 / 越界
_l = list_dir(".")
results["S14a fs列目录"] = _l["count"] >= 1 and any(e["name"] == "_fs_test.txt" for e in _l["entries"])
_st = stat_path("_fs_test.txt")
results["S14b fs元数据"] = _st["exists"] is True and _st["is_file"] is True and _st["size"] == 17
# S14c fs 越界拦截
# ⚠️ **这条原来是一个装饰**（本次审计抓到，已实测）：它读的 `HERE.parent / "x.txt"` **本来就不存在**，
# 所以工作区检查**真坏掉**时，`read_file` 会以 `NOT_A_FILE` 抛出——**同样是 `ValueError`**，
# 于是老写法 `except ValueError: True` **照样绿**。实测（旁路掉检查，即 allow_outside=True）：
#   read_file(工作区外, allow_outside=False) → OUTSIDE_WORKSPACE
#   read_file(工作区外, allow_outside=True ) → NOT_A_FILE     ← 两者都是 ValueError
# ⇒ 必须断言**是哪一个**错误码。
try:
    read_file(str(HERE.parent / "x.txt"))          # 工作区之外
    results["S14c fs越界拦截"] = False
except ValueError as e:
    results["S14c fs越界拦截"] = getattr(e, "code", None) == "OUTSIDE_WORKSPACE"

# S15 文件系统：glob / 移动 / 删除
# `count >= 1` 是弱形状：它放走"匹配到了**别的**文件"。实测此刻匹配集恰好是 `['_fs_test.txt']`，
# 所以直接钉这个**集合**（可与 S14a 交叉对账：那里也要求 `_fs_test.txt` 在场）。
_g = glob_paths("_fs_test.*")
results["S15a fs_glob"] = _g["matches"] == ["_fs_test.txt"]
move_path("_fs_test.txt", "_fs_test2.txt")
results["S15b fs_move"] = stat_path("_fs_test2.txt")["exists"] is True \
    and stat_path("_fs_test.txt")["exists"] is False
remove_path("_fs_test2.txt")
results["S15c fs_remove"] = stat_path("_fs_test2.txt")["exists"] is False

# S16 终端超时告警必须穿透 grep 投影（回归）
# 修前实测：grep 命中时交付的 stdout 只有 "NOISE"，那句「命令可能仍在运行」被 _grep 吃掉；
# grep 不命中时更糟——交付 "[filter 无匹配，输出共 N 行]"，把调用方引去改自己的正则，
# 而真实情况是命令还在跑（receipt.timed_out / still_running 仍是 true）。
_t2 = terminal_open()
_tid2 = _t2["terminal_id"]
try:
    _r = terminal_send(_tid2, f'"{PY}" -c "print(\'NOISE\', flush=True); import time; time.sleep(30)"',
                       timeout=2, grep="NOISE")
    results["S16 超时告警穿透grep"] = (_r["receipt"]["timed_out"] is True
                                       and _r["receipt"]["still_running"] is True
                                       and "NOISE" in _r["stdout"]
                                       and "超时" in _r["stdout"])
finally:
    terminal_close(_tid2)

# S17 绊线（见文件头）：钉住「交付 ≤ 上限」这条**未采纳**判据的不可接受代价面。
# 刻意用【极小上限】跑，因为两种朴素落地都在这里翻车（`.tmp_aishell_tripwire_teeth.py` 实测）：
#   ② 硬裁到上限：不变量守住了，但**截断标记被裁断**（静默截断）→ S17a 红；
#   ③ 预算 = 上限 − 标记长度且不设下限：limit < 标记长度时预算为负，而**切片**的负数语义相反
#      （`data[:-25]` ＝「除尾部 25 字节以外全部」）→ 交付量从 50 涨到 1000 字节 → S17b 红。
#      （附注：`f.read(负数)` 除 -1 外会响亮地抛 ValueError，所以危险的是**切片**那条路。）
write_file("_fs_trip.txt", "z" * 500)
try:
    _r = read_file("_fs_trip.txt", max_bytes=1)
    _d = len(_r["content"].encode("utf-8"))
    results["S17a 绊线:标记完整(极小上限)"] = ("中间截断" in _r["content"]
                                               and "共 500 字节" in _r["content"])
    # 上界 = 上限 + 64：标记长度随 cut/total 位数浮动（r155 实测 47–54 字节），
    # 这里钉的是「超出量有界、且由标记解释」。**收紧到上限以内不会让它红；让它红的是
    # 无界膨胀**（负预算那类）。会因「裁掉标记」而红的只有 S17a——两条成对。
    results["S17b 绊线:超出量有界(极小上限)"] = _d <= 1 + 64
finally:
    remove_path("_fs_trip.txt")


# ---------- S18–S21：r160 修的三条（P0 删链接、P1 截断产替换符、P2.2 still_running 统一）----------

def _make_dir_link(target: Path, link: Path):
    """造一个指向目录的链接：优先真符号链接；Windows 无创建权限时退回 junction。

    junction 在 Windows 上**不需要管理员**（`mklink /J`），而 `os.symlink` 需要
    SeCreateSymbolicLinkPrivilege（实测 WinError 1314）——所以没有这个退路，
    「删链接只删链接」这条在 Windows 上就永远验证不了。
    """
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError):
        if os.name != "nt":
            return None
        # 只用到 returncode ⇒ **不进文本模式**：`text=True` 的解码走 locale，
        # 而 mklink 的输出在中文 Windows 上是 GBK、在英文 Windows 上是 OEM 码页
        # ⇒ 没有"钉一个就对"的编码，最好的处置是**别解码**。
        # （这条是 r183 新增的 check_subproc_encoding.py 抓出来的：本文件当时是
        #  "文本模式却没钉 encoding=" 的两个真实调用点之一。）
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           capture_output=True)
        return "junction" if r.returncode == 0 else None


def _drop_link(link: Path) -> None:
    """摘掉链接本身（`shutil.rmtree` 对链接会抛，junction 必须 `rmdir`）。

    也容忍「它不是链接、而是一个真目录」：**旧代码会把目标搬过来**，
    那时清理路径与修后不同——清理代码不该把红路验证变成一个崩溃。
    """
    if not os.path.lexists(link):
        return
    try:
        if os.path.islink(link):
            os.unlink(link)
        else:
            os.rmdir(link)
    except OSError:
        shutil.rmtree(link, ignore_errors=True)


def _try_remove(path: str) -> None:
    """清理用：目标可能已被别的路径删掉（旧代码的语义不同），失败不算测试失败。"""
    try:
        remove_path(path)
    except Exception:  # noqa: BLE001
        pass


# S18 remove_path 对链接：**只删链接本身**（r160 修的 S 级：修前删的是链接指向的目标，
# 回执还报目标路径 ⇒ 调用方点的是链接、被删的是另一个文件，且从回执里看不出来）
_fs_link = HERE / "_fs_link"
_fs_link_target = HERE / "_fs_link_target"
mkdir_path("_fs_link_target")
write_file("_fs_link_target/inside.txt", "payload")
_link_how = _make_dir_link(_fs_link_target, _fs_link)
if _link_how is None:
    # 无法验证 ⇒ 红（fail-closed：不许「没跑」被当成「跑绿了」）
    results["S18a 删链接只删链接"] = False
    results["S18b 回执记录没跟随链接"] = False
else:
    try:
        _rr = remove_path("_fs_link")
        results["S18a 删链接只删链接"] = (not os.path.lexists(_fs_link)
                                          and _fs_link_target.is_dir()
                                          and (_fs_link_target / "inside.txt").is_file())
        # 用 .get()：修前这些键根本不存在——那时要**判红**，不是让套件崩掉
        results["S18b 回执记录没跟随链接"] = (
            _rr.get("removed_kind") == "link"
            and _rr.get("requested") == "_fs_link"
            and str(_rr.get("path", "")).replace("\\", "/").endswith("_fs_link")
            and os.path.normcase(_rr.get("link_target") or "")
            == os.path.normcase(os.path.realpath(_fs_link_target)))
    finally:
        _drop_link(_fs_link)
_try_remove("_fs_link_target")

# S19 move_path 同类：源是链接时搬**链接本身**，不是它指向的目标
_fs_mv_target = HERE / "_fs_mv_target"
_fs_mv_link = HERE / "_fs_mv_link"
_fs_mv_moved = HERE / "_fs_mv_moved"
mkdir_path("_fs_mv_target")
write_file("_fs_mv_target/inside.txt", "payload")
if _make_dir_link(_fs_mv_target, _fs_mv_link) is None:
    results["S19 move链接不搬目标"] = False
else:
    try:
        move_path("_fs_mv_link", "_fs_mv_moved")
        results["S19 move链接不搬目标"] = (not os.path.lexists(_fs_mv_link)
                                           and _fs_mv_target.is_dir()
                                           and (_fs_mv_target / "inside.txt").is_file()
                                           and os.path.lexists(_fs_mv_moved))
    finally:
        _drop_link(_fs_mv_moved)
        _drop_link(_fs_mv_link)
_try_remove("_fs_mv_target")

# S20 read_file 截断点落在字符边界（r160 修的 A 级：修前 `read_file(max_bytes=8)` 交付
# `'第�…'` —— 那个 `�` 是切出来的，调用方无法与「文件里本来就有替换符」区分）
write_file("_fs_cn.txt", "第一行中文\n第二行中文\n")
_r = read_file("_fs_cn.txt", max_bytes=8)
results["S20a 截断不产替换符"] = (_r["truncated"] is True
                                  and "\ufffd" not in _r["content"]
                                  and "中间截断" in _r["content"])
results["S20b delivered_bytes 属实"] = (_r.get("delivered_bytes")
                                        == len(_r["content"].encode("utf-8")))
_try_remove("_fs_cn.txt")

# S21 still_running 在三条路径上**都出现**（r160 统一；Muse 报的 A 档：README 读起来像它恒在，
# 而修前只有终端超时路径带它）+ 终端回执带 captured_bytes
_r = exec_command(f'"{PY}" -c "print(1)"')
results["S21a exec前台带still_running"] = _r["receipt"].get("still_running") is False
_r = exec_command(f'"{PY}" -c "import time; time.sleep(1)"', background=True)
results["S21b 后台即时回执带still_running"] = _r["receipt"].get("still_running") is True
_j = job_logs(_r["job_id"])
results["S21c job_logs回执带still_running"] = "still_running" in _j["receipt"]
_r = exec_command(f'"{PY}" -c "import time; time.sleep(30)"', timeout=2)
results["S21d exec超时仍报still_running=False"] = (_r["receipt"].get("timed_out") is True
                                                   and _r["receipt"].get("still_running") is False)
_t3 = terminal_open()
_tid3 = _t3["terminal_id"]
try:
    _r = terminal_send(_tid3, "echo hi", timeout=15)
    results["S21e 终端回执带captured_bytes"] = ("captured_bytes" in _r["receipt"]
                                                and _r["receipt"]["captured_bytes"] >= 0)
finally:
    terminal_close(_tid3)

# S22 fs 的两个**输入边界**（r185，一次只做一件；两条都先在这份套件上跑红）
# 来源：`.tmp_aishell_edge_probe.py` 的秒级自检（不是猜的，是实测出来的两个真缺陷）。
#
# S22a glob 逃逸：`glob_paths("../*")` 曾经**列出了工作区之外**的路径（实测 count=63，
#       含 `..\.svn_msg_v3.txt`、`..\.tools`），而它不传 allow_outside。
#       ⇒ 修法是**响亮拒绝**（OUTSIDE_WORKSPACE），不是静默过滤（本模块的教义：绝不静默）。
#       断言钉的是"**不许把区外路径交出去**"，不是"必须抛" —— 两种实现都可通过，
#       但"交出去"一定不合格（这样它不会因为实现风格而误红）。
try:
    _g_esc = glob_paths("../*")
    results["S22a glob不许逃出工作区"] = all(
        not m.replace("\\", "/").startswith("..") for m in _g_esc["matches"])
except ValueError as e:
    results["S22a glob不许逃出工作区"] = getattr(e, "code", None) == "OUTSIDE_WORKSPACE"

# S22b 非正的 max_bytes：`read_file(max_bytes=0)` 曾经**交付整个文件且 truncated=False**
#       （0 是 falsy ⇒ 落到 MAX_READ）；`max_bytes=-1` 更怪：**交付 1049 字节 > 文件本身**。
#       ⇒ 与 exec_command 的 `timeout <= 0 → INVALID_TIMEOUT` 同款处理：**响亮拒绝**。
write_file("_edge22.txt", "x" * 1000)
try:
    _bad = []
    for _mb in (0, -1):
        try:
            read_file("_edge22.txt", max_bytes=_mb)
            _bad.append(_mb)          # 没抛就记一笔
        except ValueError as e:
            if getattr(e, "code", None) != "INVALID_ARGUMENT":
                _bad.append(_mb)
    results["S22b 非正max_bytes必须响"] = not _bad
finally:
    _try_remove("_edge22.txt")

# S23 「让失败说得清楚」——**两位独立消费者（子智能体 A/B）实测出来的**，我各自复现过：
#   · CLI 跑一个失败的命令，**进程退出码仍是 0**（回执里 exit_code=7）⇒ 脚本/CI 看 $? 会以为成功；
#   · 多个失败**漏出信封**（README 承诺"错误一律 AIShellError"）：
#     read_file(max_bytes="500") ⇒ 裸 TypeError；glob_paths("") ⇒ 裸 ValueError；
#     glob_paths(r"C:\\Users\\*\\*") ⇒ 裸 NotImplementedError；shell 二进制不存在 ⇒ 裸 FileNotFoundError。
# 这四条断言都**先跑红**（现状见提交信息里的实测），再实现让它转绿。
_cli = [sys.executable, "-m", "ai_shell.cli"]
_cli_env = dict(os.environ, PYTHONIOENCODING="utf-8")


def _cli_rc(*args):
    p = subprocess.run(_cli + list(args), cwd=str(HERE), env=_cli_env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    return p.returncode


results["S23a CLI退出码透传成败"] = _cli_rc(f'"{PY}" -c "import sys; sys.exit(7)"') == 7
results["S23b CLI成功仍回0"] = _cli_rc(f'"{PY}" -c "print(1)"') == 0
results["S23c CLI超时用124"] = _cli_rc(f'"{PY}" -c "import time; time.sleep(30)"', "--timeout", "2") == 124


def _is_envelope(fn, want_code):
    try:
        fn()
        return False
    except Exception as e:                       # noqa: BLE001
        return getattr(e, "code", None) == want_code


# S23d 的判据要的是"**参数非法**被拒"，所以这里必须有一个**真实存在的文件**，
# 否则先撞上 NOT_A_FILE（那也是 AIShellError，但 code 不对）——那就变成"红得不是地方"。
write_file("_fs_cn2.txt", "第一行中文\n第二行中文\n")
results["S23d 非法max_bytes收进信封"] = (
    _is_envelope(lambda: read_file("_fs_cn2.txt", max_bytes="500"), "INVALID_ARGUMENT")
    and _is_envelope(lambda: read_file("_fs_cn2.txt", max_bytes=100.5), "INVALID_ARGUMENT")
    # bool 那个最阴：`True` 是 int 且 >0，于是 `limit=True` ⇒ `half=0` ⇒
    # **交付空内容却报 truncated=True**（"读到了、只是全被截断掉"），比抛错更难查。
    and _is_envelope(lambda: read_file("_fs_cn2.txt", max_bytes=True), "INVALID_ARGUMENT"))
results["S23e 非法glob模式收进信封"] = (
    _is_envelope(lambda: glob_paths(""), "INVALID_ARGUMENT")
    and _is_envelope(lambda: glob_paths(r"C:\Users\*\*"), "INVALID_ARGUMENT")
    and _is_envelope(lambda: glob_paths(None), "INVALID_ARGUMENT"))
# shell 二进制不存在时也必须进信封。本机没装 pwsh（shutil.which 为 None）⇒ 这条**真的会跑**；
# 装了 pwsh 的机器上它退化成"必须成功"——两种情况下断的都是同一条契约（失败不许漏出信封）。
if shutil.which("pwsh") is None:
    results["S23f 缺shell二进制收进信封"] = _is_envelope(
        lambda: exec_command("echo hi", shell="pwsh"), "NO_SHELL_AVAILABLE")
else:
    results["S23f 缺shell二进制收进信封"] = exec_command("echo hi", shell="pwsh")["receipt"]["exit_code"] == 0
_try_remove("_fs_cn2.txt")

print()
for k, v in results.items():
    print(f"{'PASS' if v else 'FAIL'}  {k}")
print()
sys.exit(0 if all(results.values()) else 1)
