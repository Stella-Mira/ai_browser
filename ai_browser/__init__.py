"""ai_browser: 将 HTML 页面简化为纯数据 JSON，让 AI 像人一样访问互联网"""
from .core import (
    PROFILES_DIR,
    act_in_session,
    act_on_url,
    browser_executable,
    close_session,
    collect_urls,
    import_cookies,
    list_sessions,
    login_interactive,
    normalize_url,
    open_session,
    parse_html,
    parse_url,
)

__version__ = "0.6.45"
__all__ = [
    "parse_url", "parse_html", "act_on_url",
    "open_session", "act_in_session", "close_session", "list_sessions",
    "login_interactive", "import_cookies",
    "browser_executable", "normalize_url", "collect_urls", "PROFILES_DIR",
]
