"""命令行入口

解析页面：
  python -m ai_browser.cli <url> [-o out.json] [--static] [--compact] [--profile 名称]
  python -m ai_browser.cli --html page.html

执行动作（无状态）：
  python -m ai_browser.cli <url> --act '{"id":"a4","text":"关键词"}'
  python -m ai_browser.cli <url> --act '{"action":"scroll","pages":2}'

记录级抓取（自动翻页，一步拿一列记录）：
  python -m ai_browser.cli <url> --collect --fields text,href --limit 100
  python -m ai_browser.cli <url> --collect --from a7 --fields '{"title":"h3 a@text","url":"h3 a@href"}'

登录态管理：
  python -m ai_browser.cli --login "https://passport.bilibili.com/login" --profile bilibili
  python -m ai_browser.cli --import-cookies cookies.json --profile bilibili
"""
from __future__ import annotations

import argparse
import json
import sys

from .core import (PROFILES_DIR, act_on_url, import_cookies, list_sessions,
                   login_interactive, parse_html, parse_url)


def _fields_arg(raw: str | None):
    """--fields 既收 "text,href" 也收一个 JSON 对象（精确选择器）。"""
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("{"):
        return json.loads(raw)
    return [s.strip() for s in raw.split(",") if s.strip()]


def main() -> None:
    ap = argparse.ArgumentParser(prog="ai-browser",
                                 description="HTML → 纯数据 JSON，让 AI 像人一样访问互联网")
    ap.add_argument("url", nargs="?", help="目标页面 URL")
    ap.add_argument("--html", help="直接解析本地 HTML 文件")
    ap.add_argument("-o", "--output", help="输出到文件（默认打印到 stdout）")
    ap.add_argument("--static", action="store_true", help="静态模式：禁用 JS，速度更快")
    ap.add_argument("--compact", action="store_true", help="压缩输出（无缩进，token 最少）")
    ap.add_argument("--profile", help="持久化档案名称：携带/保存登录态")
    ap.add_argument("--exclude", help="忽略这些 CSS 选择器命中的子树（逗号分隔），例如站点自身的 hover 浮层")
    ap.add_argument("--no-urls", action="store_true",
                    help="不输出元素/媒体/表单的 URL（省 token）；需要时用 --act '{\"action\":\"url\",\"id\":\"a3\"}' 单独取回")
    ap.add_argument("--tree", action="store_true",
                    help="每个块附带 id/p/i/tag（供复核 collect 的分组；代价约 +17%% 体积）")
    ap.add_argument("--max-chars", type=int,
                    help="交付前的体积上限（字符）：超限时丢掉**文档序末尾**的块，"
                         "并在 coverage.truncated 里如实报告丢了什么（绝不静默截断）")
    ap.add_argument("--act", help="执行动作后返回新页面 JSON，值为 JSON 字符串")
    ap.add_argument("--collect", action="store_true",
                    help="记录级抓取：自动翻页，只回一列记录（records/stop_reason/next）")
    ap.add_argument("--from", dest="anchor", metavar="ID",
                    help="--collect 的锚点：记录组里任一元素的 id（不给则整页自动找最像列表的那一组）")
    ap.add_argument("--fields", help='--collect 的字段：逗号分隔的名字（text,href,title,image）'
                                     ' 或 JSON 对象（{"title":"h3 a@text","url":"h3 a@href"}）')
    ap.add_argument("--limit", type=int, default=50, help="--collect 最多取多少条（默认 50）")
    ap.add_argument("--max-pages", type=int, default=5, help="--collect 最多翻几页（默认 5）")
    ap.add_argument("--advance", default="auto",
                    help="--collect 怎么推进：auto | scroll | none | JSON(如 '{\"id\":\"a9\"}')")
    ap.add_argument("--deviants", choices=["stop", "skip", "keep"], default="stop",
                    help="--collect 遇到内部结构不同的条目：stop 先问（默认）/ skip 跳过并计数 / keep 全收并计数")
    ap.add_argument("--multi-policy", choices=["heading", "first", "longest_text", "nth"],
                    default="heading",
                    help="--collect 一个字段匹配到多个元素时怎么取（默认 heading：优先取标题里的链接）")
    ap.add_argument("--stalled-after", type=int,
                    help="--collect 连续几次「推进没生效」就报 stalled（默认：点击翻页 1、滚动 2）")
    ap.add_argument("--wait-ms", type=int,
                    help="--collect 每次推进后等多久再读页面（默认 900）")
    ap.add_argument("--settle-ms", type=int,
                    help="--collect 空列表时的确认预算（毫秒，默认 max(wait_ms,1200)，上限 15000）："
                         "预算内每 250ms 轮询，一见数据就继续")
    ap.add_argument("--login", metavar="URL", help="弹出浏览器人工登录，登录态存入 --profile")
    ap.add_argument("--login-cookie", metavar="NAME",
                    help="登录成功的判定 cookie（如 B 站 SESSDATA）；不指定则按「出现新 cookie 且已离开登录页」判定")
    ap.add_argument("--login-timeout", type=float, default=300, help="登录等待秒数（默认 300）")
    ap.add_argument("--login-enter", action="store_true", help="退回旧行为：在终端按回车结束登录")
    ap.add_argument("--import-cookies", metavar="FILE",
                    help="导入浏览器扩展导出的 cookie JSON 到 --profile")
    ap.add_argument("--sessions", action="store_true", help="列出当前进程内的会话")
    args = ap.parse_args()

    if args.sessions:
        for s in list_sessions():
            print(f"{s['session_id']}  profile={s['profile']}  空闲 {s['idle_seconds']}s")
        return

    if args.login:
        if not args.profile:
            ap.error("--login 必须搭配 --profile")
        r = login_interactive(args.login, args.profile,
                              wait_cookie=args.login_cookie,
                              timeout=args.login_timeout,
                              use_enter=args.login_enter)
        print(f"登录态已保存到 {PROFILES_DIR / args.profile}"
              f"（{r['reason']}，共 {r.get('cookies', 0)} 条 cookie，ok={r['ok']}）")
        return

    if args.import_cookies:
        if not args.profile:
            ap.error("--import-cookies 必须搭配 --profile")
        n = import_cookies(args.profile, args.import_cookies)
        print(f"已导入 {n} 条 cookie 到 {PROFILES_DIR / args.profile}")
        return

    exclude = [s for s in (args.exclude or "").split(",") if s.strip()]
    include_urls = not args.no_urls
    advance = args.advance
    if advance.startswith("{"):
        advance = json.loads(advance)

    if args.collect:
        if not args.url:
            ap.error("--collect 需要提供 url")
        action = {"action": "collect", "limit": args.limit, "max_pages": args.max_pages,
                  "advance": advance, "deviants": args.deviants,
                  "multi_policy": args.multi_policy}
        if args.anchor:
            action["from"] = args.anchor
        if args.stalled_after:
            action["stalled_after"] = args.stalled_after
        if args.wait_ms is not None:
            action["wait_ms"] = args.wait_ms
        if args.settle_ms is not None:
            action["settle_ms"] = args.settle_ms
        fields = _fields_arg(args.fields)
        if fields:
            action["fields"] = fields
        result = act_on_url(args.url, action, static=args.static, profile=args.profile,
                            exclude=exclude, include_urls=include_urls)
    elif args.act:
        if not args.url:
            ap.error("--act 需要提供 url")
        result = act_on_url(args.url, json.loads(args.act),
                            static=args.static, profile=args.profile, exclude=exclude,
                            include_urls=include_urls, tree=args.tree,
                            max_chars=args.max_chars)
    elif args.html:
        with open(args.html, encoding="utf-8") as f:
            result = parse_html(f.read(), exclude=exclude, include_urls=include_urls,
                                tree=args.tree, max_chars=args.max_chars)
    elif args.url:
        result = parse_url(args.url, static=args.static, profile=args.profile,
                           exclude=exclude, include_urls=include_urls, tree=args.tree,
                           max_chars=args.max_chars)
    else:
        ap.error("需要提供 url 或 --html")

    indent = None if args.compact else 2
    out = json.dumps(result, ensure_ascii=False, indent=indent)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out)
        if "records" in result:            # collect：本来就没有「整页 HTML」，别报压缩率
            print(f"已写入 {args.output}  采集 {result.get('count')} 条 / {result.get('pages')} 页，"
                  f"stop_reason={result.get('stop_reason')}，data={result.get('data')}，"
                  f"resume={result.get('resume')}")
        else:
            stats = result.get("stats", {})
            print(f"已写入 {args.output}  压缩率 {stats.get('reduction')} "
                  f"({stats.get('html_chars')} → {stats.get('json_chars')} 字符)")
    else:
        sys.stdout.write(out + "\n")


if __name__ == "__main__":
    main()
