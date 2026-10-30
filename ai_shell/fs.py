"""ai_shell 文件系统工具：直接读写/列举/移动文件，不经过 shell 命令。

与 exec_command 互补：文件操作走这里更可移植、更安全（工作区限定 + 结构化错误），
shell 只留给「必须在 shell 里做」的事。设计语言与 core.py 一致：结构化回执、
诚实统计、路径以工作区为基准（相对路径相对 WORKSPACE，越界需 allow_outside）。
"""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Any

from .core import AIShellError, WORKSPACE

__version__ = "0.1.0"

MAX_READ = int(os.environ.get("AI_SHELL_MAX_READ", "200000"))  # 读文件字节上限
MAX_LIST = int(os.environ.get("AI_SHELL_MAX_LIST", "1000"))    # 列目录/glob 条目上限


def _resolve(path: str | None, allow_outside: bool = False) -> Path:
    """把路径解析到工作区内；相对路径以 WORKSPACE 为基准，越界需 allow_outside。

    ⚠️ 它**会跟随最后一段符号链接**（`.resolve()`）——读文件时这是对的（读穿链接是常识），
    但**删除/移动这类「动对象本身」的操作必须用 `_lexical()`**，否则动的是链接的目标。
    """
    p = Path(path or ".").expanduser()
    if not p.is_absolute():
        p = WORKSPACE / p
    p = p.resolve()
    if not allow_outside and p != WORKSPACE and WORKSPACE not in p.parents:
        raise AIShellError("OUTSIDE_WORKSPACE",
                           f"path {p} 在工作区 {WORKSPACE} 之外；确认后传 allow_outside=True",
                           detail={"path": str(p), "workspace": str(WORKSPACE)})
    return p


def _lexical(path: str | None, allow_outside: bool = False) -> Path:
    """绝对路径，但**不解开最后一段链接**（父目录照常 resolve）。

    为什么单独有一个（r160 的 S 级修复）：`_resolve()` 里的 `.resolve()` 会把符号链接/
    junction 解开，于是 `remove_path("link")` 删掉的是**链接指向的目标**、回执也报目标路径，
    而调用方点的是链接——同一条链上 `move_path` 也会**搬走目标**、把链接留成悬空。
    删除/移动要操作的是"那个对象本身"，所以用词法路径。

    父目录仍然 resolve：否则 macOS 上 `/tmp` → `/private/tmp` 这类会让
    「路径在工作区之内」的判定**假红**（WORKSPACE 在 import 时已 resolve）。
    """
    raw = Path(path or ".")
    if not raw.is_absolute():
        raw = WORKSPACE / raw
    lex = Path(os.path.normpath(str(raw)))     # 只做词法折叠（. 与 ..），不碰链接
    parent = lex.parent.resolve()              # 容器照常解链接
    if not allow_outside and parent != WORKSPACE and WORKSPACE not in parent.parents:
        raise AIShellError("OUTSIDE_WORKSPACE",
                           f"path {parent / lex.name} 在工作区 {WORKSPACE} 之外；"
                           f"确认后传 allow_outside=True",
                           detail={"path": str(parent / lex.name), "workspace": str(WORKSPACE)})
    if not lex.name:
        raise AIShellError("INVALID_ARGUMENT", f"path {lex} 折叠后没有可操作的叶子名",
                           detail={"path": str(lex)})
    return parent / lex.name


def _is_junction(p: Path) -> bool:
    """Windows 的 junction：`lstat` **不**报 S_ISLNK，但语义上仍是"链接"。

    没有这一步，P0 在 Windows 上会漏（符号链接要管理员权限才建得出，junction 不要，
    所以实际更常见）。Python 3.11 没有 `os.path.isjunction`，退回读 `st_reparse_tag`。
    """
    if os.name != "nt":
        return False
    f = getattr(os.path, "isjunction", None)
    if f is not None:
        return bool(f(p))
    try:
        return os.lstat(p).st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
    except (OSError, AttributeError):
        return False


def _end_boundary(data: bytes, n: int) -> int:
    """≤ n 的最大位置 i，使 `data[:i]` 是**完整** UTF-8（切点落在字符末尾）。

    用增量解码器问「喂进去的字节里有多少没解出来」，比自己数前导位稳。
    收拢**不会额外丢内容**：被切在中间的那个字符本来就在「中间截断」的缺口里。
    """
    import codecs

    n = max(0, min(n, len(data)))
    dec = codecs.getincrementaldecoder("utf-8")()
    dec.decode(data[:n], final=False)
    leftover, _ = dec.getstate()
    return n - len(leftover)


def _start_boundary(data: bytes, m: int) -> int:
    """≥ m 的第一个字符边界：跳过切开处残留的续字节（最多 3 个，UTF-8 上限）。"""
    m = max(0, min(m, len(data)))
    skipped = 0
    while m < len(data) and skipped < 3 and (data[m] & 0xC0) == 0x80:
        m += 1
        skipped += 1
    return m


def _head_tail(data: bytes, limit: int) -> tuple[str, bool, int]:
    """超长内容双侧截断（head+tail），保留开头上下文与结尾信息。

    切点**收拢到 UTF-8 字符边界**（r160 修）：修前切在多字节中间会交付替换符 `�`，
    而调用方无法区分「文件里本来就有替换符」与「工具切坏的」。缺口按**实际**字节数报。
    """
    total = len(data)
    if total <= limit:
        return data.decode("utf-8", errors="replace"), False, total
    half = limit // 2
    head_b = data[: _end_boundary(data, half)]
    tail_b = data[_start_boundary(data, total - half):]
    gap = total - len(head_b) - len(tail_b)
    return (f"{head_b.decode('utf-8', errors='replace')}\n"
            f"…[中间截断 {gap} 字节，共 {total} 字节]…\n"
            f"{tail_b.decode('utf-8', errors='replace')}",
            True, total)


def _check_max_bytes(max_bytes: Any) -> None:
    """`max_bytes` 必须是**正整数**（None = 用默认上限），否则**响亮拒绝**。

    与 exec_command 的 `timeout <= 0 → INVALID_TIMEOUT` **同款**：非法的上限不许静默降级。
    实测过的三种坏输入（各自都在证伪不同的承诺）：
      · `0`/`-1`（r185 前）：falsy ⇒ 落到 MAX_READ ⇒ **交付整个文件还报 truncated=False**；
        `-1` 更怪：`f.read(-1)` 是"读到底" ⇒ 交付字节**比文件本身还大**。
      · `"500"`：`"500" < 1` 直接 `TypeError` **漏出信封**（README 承诺错误一律 AIShellError）。
      · `True`：是 int 且 >0 ⇒ `limit=True` ⇒ `half = 0` ⇒ **交付空内容却报 truncated=True**
        （读到的字节全被"截断"掉，比报错更难查）；`100.5` 则被当成上限**静默接受**。
    bool 单独挡：`isinstance(True, int)` 为真，但调用方说"正整数"时没人想要 True 当 1。
    """
    if max_bytes is None:
        return
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise AIShellError("INVALID_ARGUMENT",
                           f"max_bytes 必须是正整数（收到 {max_bytes!r}）；不给就用默认上限 {MAX_READ}",
                           detail={"max_bytes": max_bytes})


def read_file(path: str, max_bytes: int | None = None, allow_outside: bool = False) -> dict[str, Any]:
    """读文本文件，返回 {path, size_bytes, truncated, delivered_bytes, content}。

    先 stat 取大小再按需读：文件超过 limit 时只读头尾各半（不整读进内存），
    避免超大文件把内存打爆。切点收拢到字符边界（不产替换符），
    `delivered_bytes` 是**含标记、含换行**的实际交付字节数。
    """
    p = _resolve(path, allow_outside)
    if not p.is_file():
        raise AIShellError("NOT_A_FILE", f"不是文件：{p}", detail={"path": str(p)})
    _check_max_bytes(max_bytes)
    limit = max_bytes or MAX_READ
    total = p.stat().st_size
    if total <= limit:
        content, truncated, _ = _head_tail(p.read_bytes(), limit)
    else:
        half = limit // 2
        with open(p, "rb") as f:
            head = f.read(half)
            f.seek(total - half)
            tail = f.read(half)
        head_b = head[: _end_boundary(head, len(head))]
        tail_b = tail[_start_boundary(tail, 0):]
        gap = total - len(head_b) - len(tail_b)
        content = (head_b.decode("utf-8", errors="replace") + "\n"
                   f"…[中间截断 {gap} 字节，共 {total} 字节]…\n"
                   + tail_b.decode("utf-8", errors="replace"))
        truncated = True
    return {"path": str(p), "size_bytes": total, "truncated": truncated,
            "delivered_bytes": len(content.encode("utf-8")), "content": content}


def write_file(path: str, content: str, append: bool = False,
               allow_outside: bool = False) -> dict[str, Any]:
    """写文本文件（默认覆盖，append=True 追加），自动创建父目录。"""
    p = _resolve(path, allow_outside)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    with open(p, "ab" if append else "wb") as f:
        f.write(data)
    return {"path": str(p), "written_bytes": len(data), "append": append}


def list_dir(path: str = ".", allow_outside: bool = False) -> dict[str, Any]:
    """列目录，返回 {path, count, entries:[{name, kind, size, mtime}]}；超 MAX_LIST 截断。"""
    p = _resolve(path, allow_outside)
    if not p.is_dir():
        raise AIShellError("NOT_A_DIR", f"不是目录：{p}", detail={"path": str(p)})
    names = sorted(p.iterdir(), key=lambda x: x.name.lower())
    total = len(names)
    entries = []
    for e in names[:MAX_LIST]:
        try:
            st = e.stat()
        except OSError:
            st = None
        entries.append({
            "name": e.name,
            "kind": "dir" if e.is_dir() else "file",
            "size": st.st_size if st is not None and e.is_file() else None,
            "mtime": round(st.st_mtime, 3) if st is not None else None,
        })
    result = {"path": str(p), "count": total, "entries": entries}
    if total > MAX_LIST:
        result["truncated"] = True
        result["shown"] = len(entries)
    return result


def stat_path(path: str, allow_outside: bool = False) -> dict[str, Any]:
    """取文件元数据；不存在时返回 {path, exists:false} 而非抛错。"""
    p = _resolve(path, allow_outside)
    if not p.exists():
        return {"path": str(p), "exists": False}
    st = p.stat()
    return {"path": str(p), "exists": True, "is_dir": p.is_dir(), "is_file": p.is_file(),
            "size": st.st_size, "mtime": round(st.st_mtime, 3)}


def move_path(src: str, dst: str, allow_outside: bool = False) -> dict[str, Any]:
    """移动/重命名文件或目录。

    **源是符号链接 / junction 时搬的是链接本身，不是它指向的目标**（r160 修，与
    `remove_path` 同一类：`_resolve()` 会把链接解开，于是目标被搬走、链接留成悬空）。
    """
    s = _lexical(src, allow_outside)
    d = _lexical(dst, allow_outside)
    try:
        shutil.move(str(s), str(d))
    except FileNotFoundError:
        raise AIShellError("NOT_FOUND", f"源路径不存在：{s}", detail={"path": str(s)}) from None
    except PermissionError:
        raise AIShellError("PERMISSION_DENIED", f"无权限移动：{s} -> {d}",
                           detail={"src": str(s), "dst": str(d)}) from None
    return {"src": str(s), "dst": str(d), "moved": True}


def remove_path(path: str, allow_outside: bool = False) -> dict[str, Any]:
    """删除文件或目录（目录递归删除）。

    **符号链接 / junction 只删链接本身，不删目标**（与 `os.remove` 的语义对齐）。
    回执把「引擎做的那个选择」记全：
      · `path`          实际被删的**绝对路径**（叶子不跟随链接 ⇒ 链接就是链接）
      · `requested`     调用方原样给的字符串
      · `removed_kind`  `"link" | "file" | "dir"`
      · `link_target`   叶子是链接时，**删除前**它指向哪里（否则 None）
    """
    target = _lexical(path, allow_outside)
    try:
        st = os.lstat(target)          # 不跟随：要判「叶子是不是链接 / junction」
    except FileNotFoundError:
        raise AIShellError("NOT_FOUND", f"路径不存在：{target}",
                           detail={"path": str(target)}) from None
    except PermissionError:
        raise AIShellError("PERMISSION_DENIED", f"无权限访问：{target}",
                           detail={"path": str(target)}) from None

    junction = _is_junction(target)
    is_link = stat.S_ISLNK(st.st_mode) or junction
    link_target: str | None = None
    if is_link:
        try:
            link_target = str(target.resolve())      # 它指向哪儿（只读，不跟随去删）
        except OSError:
            link_target = None

    try:
        if is_link:
            if os.name == "nt" and (junction or stat.S_ISDIR(st.st_mode)):
                os.rmdir(target)       # Windows：目录链接/junction 要用 rmdir 才只摘链接
            else:
                os.unlink(target)      # POSIX：unlink 对符号链接就是只删链接
            kind = "link"
        elif stat.S_ISDIR(st.st_mode):
            shutil.rmtree(str(target))
            kind = "dir"
        else:
            os.unlink(target)
            kind = "file"
    except FileNotFoundError:
        raise AIShellError("NOT_FOUND", f"路径不存在：{target}",
                           detail={"path": str(target)}) from None
    except PermissionError:
        raise AIShellError("PERMISSION_DENIED", f"无权限删除：{target}",
                           detail={"path": str(target)}) from None
    return {"path": str(target), "requested": path, "removed": True,
            "removed_kind": kind, "link_target": link_target}


def mkdir_path(path: str, parents: bool = True, allow_outside: bool = False) -> dict[str, Any]:
    """创建目录（parents=True 连父目录一起建，exist_ok 幂等）。"""
    p = _resolve(path, allow_outside)
    p.mkdir(parents=parents, exist_ok=True)
    return {"path": str(p), "created": True}


def glob_paths(pattern: str, allow_outside: bool = False) -> dict[str, Any]:
    """按 glob 模式匹配工作区内的路径，返回相对工作区根的路径；超 MAX_LIST 截断。

    ⚠️ **pattern 本身也在工作区限定之内**（r185 修）：`glob_paths("../*")` 曾经**列出工作区之外**
    的路径（实测 count=63，含 `..\\.svn_msg_v3.txt`、`..\\.tools`），而调用方**没有**传 allow_outside
    —— 而 README 写的正是"fs 模块的路径参数受工作区限定" ⇒ 代码在证伪自家文档。
    修法是**响亮拒绝**（与 `_resolve` 同一个错误码），不是静默过滤：本模块的教义是**绝不静默**。

    ⚠️ **pattern 本身非法时也必须进信封**（r186 修）：修前三种输入各自漏一个裸异常——
    `""` → `ValueError: Unacceptable pattern`、绝对模式（`C:\\Users\\*\\*`）→
    `NotImplementedError: Non-relative patterns are unsupported`、非字符串（`None`）→ `TypeError`。
    调用方按 README 写 `except AIShellError` 时这三条都**穿过去**（其中 `NotImplementedError`
    还是 `RuntimeError` 的子类，连 `except ValueError` 都接不住）。
    """
    if not isinstance(pattern, str):
        raise AIShellError("INVALID_ARGUMENT",
                           f"glob 模式必须是字符串（收到 {pattern!r}）",
                           detail={"pattern": pattern})
    base = _resolve(".", allow_outside)
    try:
        hits = list(base.glob(pattern))
    except (ValueError, NotImplementedError) as e:
        # 只支持工作区内的相对模式：`Path.glob` 的这两种拒绝是**可预期的调用方错误**，
        # 而它自带的异常类型在文档里没有承诺过 ⇒ 收进同一套信封（与上面同一个错误码）。
        raise AIShellError("INVALID_ARGUMENT",
                           f"glob 模式 {pattern!r} 不可解析：{e}；"
                           f"只支持工作区内的相对模式（相对 {base}）",
                           detail={"pattern": pattern, "reason": str(e)}) from None
    if not allow_outside:
        for x in hits:
            # **词法**折叠（`normpath`，不 `resolve`）：读穿符号链接是常识，
            # 只有 `..` 这类词法逃逸才算越界——否则"glob 到一个指向区外的链接"会被误红。
            lex = Path(os.path.normpath(str(x)))
            if lex != base and base not in lex.parents:
                raise AIShellError("OUTSIDE_WORKSPACE",
                                   f"glob 模式 {pattern!r} 会匹配到工作区 {base} 之外的 {lex}；"
                                   f"确认后传 allow_outside=True",
                                   detail={"pattern": pattern, "escaped": str(lex),
                                           "workspace": str(base)})
    matches = sorted(str(x.relative_to(base)) for x in hits)
    total = len(matches)
    result = {"pattern": pattern, "count": total, "matches": matches[:MAX_LIST]}
    if total > MAX_LIST:
        result["truncated"] = True
        result["shown"] = len(result["matches"])
    return result
