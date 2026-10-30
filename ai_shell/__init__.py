"""ai_shell：跨系统命令行工具 + 文件系统工具（为 agent 设计）"""
from .core import (
    __version__,
    env_info,
    exec_command,
    history,
    job_logs,
    terminal_close,
    terminal_open,
    terminal_send,
)
from .fs import (
    glob_paths,
    list_dir,
    mkdir_path,
    move_path,
    read_file,
    remove_path,
    stat_path,
    write_file,
)

__all__ = [
    "exec_command", "terminal_open", "terminal_send", "terminal_close",
    "job_logs", "env_info", "history", "__version__",
    "read_file", "write_file", "list_dir", "stat_path",
    "move_path", "remove_path", "mkdir_path", "glob_paths",
]
