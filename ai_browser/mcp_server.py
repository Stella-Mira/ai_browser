"""ai_browser 的 MCP（Model Context Protocol）服务端：把网页浏览能力暴露成
`mcp__<serverName>__<tool>` 形式的原生工具。

两种传输方式，工具定义完全相同：

  1) stdio（默认）—— 由 MCP 客户端拉起子进程，即用即起、随客户端退出：
         python -m ai_browser.mcp_server

  2) streamable-http —— 常驻 HTTP 服务，多个客户端共用一个进程与登录态；
     进程必须先于客户端启动，客户端断开不影响服务（v0.4 新增）：
         python -m ai_browser.mcp_server --transport http --host 127.0.0.1 --port 8766
     或直接双击 serve_mcp_http.cmd（会从 .mcp_token 读令牌，缺省自动生成）。

鉴权：设置了 AI_BROWSER_MCP_TOKEN（或 --token）时，HTTP 请求必须带
`Authorization: Bearer <token>`；未设置时只允许本机回环访问。stdio 传输无需鉴权
（管道天然只有父进程能读写）。

DSH 注册示例（profile 的 cordis.patch.yml，注意必须用无 id 的 insert 形式）：

    - insert:
        - id: mcp-ai-browser
          name: '@deepseek-ai/dsh-mcp-client'
          config:
            serverName: aibrowser
            transport: streamable-http
            url: http://127.0.0.1:8766/mcp
            headers:
              Authorization: 'Bearer <token>'
            toolCallTimeoutMs: 120000
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
from typing import Any

import anyio

try:  # mcp >= 2.0 把 FastMCP 改名为 MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server

from . import __version__
from .core import (BlockedAction, act_in_session, act_on_url, close_session,
                   import_cookies, list_sessions, open_session, parse_url)

DEFAULT_HOST = os.environ.get("AI_BROWSER_MCP_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("AI_BROWSER_MCP_PORT", "8766"))
MCP_PATH = "/mcp"

mcp = _Server("ai-browser", version=__version__)


def _dumps(data: Any) -> str:
    """紧凑 JSON：同样的信息量占用更少 token。"""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


async def _call(fn, *args, **kwargs):
    return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))


@mcp.tool()
async def browse(url: str, profile: str | None = None, static: bool = False,
                 exclude: list[str] | None = None, include_urls: bool = False,
                 tree: bool = False, max_chars: int | None = None) -> str:
    """渲染一个网页并返回纯数据 JSON（blocks 线性阅读流；每个可交互元素带 id）。

    profile 非空时携带该档案的持久登录态（cookie/localStorage）。
    static=True 禁用 JavaScript，更快但看不到动态内容。
    exclude 是 CSS 选择器列表，命中的子树整个忽略——真实站点常有「已渲染、只靠 hover
    才收起」的气泡/浮层（如 B 站的 .v-popover），它们有真实布局，只能由调用方指定排除。
    **默认不输出 URL**（include_urls=False，省 token：URL 常占三到五成体积）——
    只留页面自身的 url，元素/媒体/表单的 URL 摘掉，顶层标记 "urls_omitted": true；
    id/label/text/items 全部照旧。需要 URL 时二选一：
      · 本次就带上：browse(..., include_urls=True)；
      · 之后按需取回：对同一 URL 用 session_open + session_act 的
        {"action":"url","id":"a3"} / {"action":"url","ids":[...]} / {"action":"urls"}
    （在会话里取回最可靠，因为 DOM 还活着。）
    tree=True：每个块附带 id/p/i/tag、列表块附带 item_ids（条目边界），
    用来自己复核 collect 的分组结果；代价约 +17% 体积，默认关闭。
    **max_chars：交付前的体积上限**（字符）。超限时丢掉**文档序末尾**的块，并在
    `coverage.truncated` 里如实报告 `{blocks_dropped, blocks_total, blocks_delivered, cap}`——
    绝不静默截断（要按区域裁剪用 exclude）。这就是「省 token 优先」时该给的抓手：
    过滤发生在**交付前**，否则 token 已经花掉了。
    """
    return _dumps(await _call(parse_url, url, static=static, profile=profile,
                              exclude=exclude, include_urls=include_urls, tree=tree,
                              max_chars=max_chars))


@mcp.tool()
async def collect(session_id: str | None = None, url: str | None = None,
                  profile: str | None = None, from_id: str | None = None,
                  fields: Any = None, limit: int = 50, max_pages: int = 5,
                  advance: Any = "auto", next: dict[str, Any] | None = None,
                  multi_policy: str = "heading", deviants: str = "stop",
                  stalled_after: int | None = None, wait_ms: int | None = None,
                  settle_ms: int | None = None) -> str:
    """记录级抓取：一次调用把「点下一页 + 每页重读整页」压成**一列记录**。

    会话里跑（给 session_id）最省 token：它返回采集结果，**不返回整页 blocks**。
    也给 url（无状态）——会临时开一次浏览器，采完即关。

    参数：
      from_id  记录组里任一元素的 id（强烈建议给：先 browse/session_open 看一眼，
               挑一条记录里的某个 id 传进来）。不给则整页自动找「最像列表」的那一组，
               并回 containers 让你核对找到了哪一组。
      fields   要抓的字段。两种写法：
                 ["text","href","title","image"]   名字走启发式（text=整条文本，
                                                     href=第一个链接，title=标题，
                                                     image=第一张图）
                 {"title":"h3 a@text","url":"h3 a@href","price":".price@text"}
                                                    迷你选择器：CSS@属性（@text=文本，
                                                     @href/@src/@value=属性）
               不给则默认 {"text","href"}。字段缺就回 null，**不省 key**。
      limit    最多取多少条（默认 50）；max_pages 最多翻几页（默认 5）。
      advance  auto（**缺省**：优先点「下一页」，没有就滚动）/ scroll / none / {"id":"a9"}。
               **显式 null ≡ none**（不推进）；回包有 advance_used 让调用方核实到底翻没翻页
      next     上一次返回的游标，用来接着抓（按内容锚定位，换会话也不失效）
      multi_policy 一个字段在同一记录里匹配到多个元素时怎么取值：
               heading（默认：优先取 h1–h6 标题子树里的那个）/ longest_text / first / nth。
               实测理由：B 站卡片的「文档序第一个 <a>」恒定是封面链接，按 first 取等于每条都取错。
               无论用哪条规则，候选值样本都会回在 multi_samples 里——真正能救人的是样本，不是停止。
      deviants 记录组里内部结构不同的条目（搜索结果页很常见）："stop" 先问（默认）/
               "skip" 只留同构的（并回被跳过的样本）/ "keep" 全收并照样计数
      stalled_after 连续几次「推进没生效」就报 stalled（默认：点翻页 1、滚动 2）
      settle_ms 「这页是不是真的空了」的**确认预算**（毫秒，默认 max(wait_ms,1200)，上限 15000）：
               空列表时不睡死一个固定时长，而是在预算内每 250ms 轮询，一见数据就继续。
               默认预算只求安全；站点补数据明显慢（>1.2s）时显式给大一点，就能一趟到底、
               少一轮续跑。它是**预算**不是保证：页面上没有正向宣告「结束」时，无论预算多大
               都只回可重试的 stalled（疑罪从无）。回包顶层 settle_used 给出这次的预算与实测
               等待：waited_ms == budget_ms 说明预算用尽还没等到数据（加预算有用），
               waited_ms < budget_ms 说明一见数据就停了。

    返回：
      records / count      记录数组（每条带 _src.page/_src.block；合并式记录还带 _src.extra）
      stop_reason          闭集：limit | exhausted | repeat | stalled | wall | blocked |
                           timeout | error | needs_confirm
                           **`stop_reason="stalled"` 表示工具无法证实列表已到底**（相关证据已原样暴露
                           在响应里），是否到底由你判断；只有 `exhausted` 才是工具主张的到底。
                           （这条来自「已知代价」的共同前提：歧义证据只报可重试的 stalled，
                           详见 README；放在这里是因为它正是调用方最容易误读的一格——把 stalled
                           当失败去重试，或把 exhausted 当唯一可信终态。）
      stop_detail.reason   为什么停（闭集：limit/max_pages/total_reached/pager_disabled/
                           pager_disabled_no_declaration/no_advance_control/no_advance/empty_group/
                           empty_group_once/empty_group_loading/empty_group_pager_disabled_ambiguous/
                           empty_group_but_pager_live/cycle/cycle_key_collision/cross_document_cycle/
                           heterogeneous/second_family/ambiguous_identity/advance_blocked/…）。
                           `cycle*` 另带 rebuilt_records / possible_key_collision / colliding_fields：
                           「记录组被重建但零新键」在**真循环**与**去重键取粗了**之间不可区分，
                           这时只报可重试的 stalled（cycle_key_collision），别当成确定结论
      resume / data        还能不能接着跑（True/False/"after_confirm"）/ 数据可不可信
                           （ok/partial/suspect）
      record_shape         记录组签名 + 匹配数 + 偏离主签名的条目 + 各族大小（可复核）
      field_coverage       每个字段 present/missing/multi/truncated + 实际用的取值规则
      multi_samples        多值字段的前 3 条候选值样本
      skeleton             needs_confirm 时回一条代表性记录的结构骨架（≤14 项），
                           照着它就能写出精确的 fields 选择器
      end_evidence         「到底」的证据（`signals` 是封闭枚举 + 声明的总数）。
                           `end` = **有主**的强宣告（引擎会据此下终态）、`end_raw` = 见过强信号，
                           `ignored_declarations` = 被丢掉的宣告（`{signal,source,why,hint}`，
                           如「没有更多评论了」这种属于别的列表的账）——`end` 与 `stop_reason`
                           永远互证（stalled→False、exhausted→True）
      settle_used          空列表确认的账：{"budget_ms":…, "elapsed_ms":…, "polls":…, "settled":bool}；
                           elapsed_ms 是**墙钟**（含轮询里探针的开销），settled=false ＝ 撞到预算顶
                           还没等到稳定（这次的「空」不足采信）；settle_ms 可调（默认 max(wait_ms,1200)）
      advance_used         到底翻没翻页：{"kind":"click","control":"下一页","times":2} 或
                           {"kind":"none","times":0,"reason":"user_none|limit|max_pages|…"}（封闭枚举）
      next                 下次续跑用的游标

    三条硬规矩：
      · **exhausted 必须有页面自己的正证据**（列表附近「没有更多」/区间计数器到底/aria-setsize
        取满；空列表还要先被判到底）。「分页器禁用/消失」**不算**正证据——异步站点加载时
        正是把分页器禁用，把它当到底会让同一 URL 只改 wait_ms 就换答案；
        只有「推不动」时一律回 stalled——把 20/500 说成「抓完了」是这里最贵的错。
      · 停止的**种类**由推进的回执决定：推进没生效 → stalled（可续跑、可重试）；
        推进生效了但内容全是已见 → repeat（站点在循环喂同一页）。页内重复只计数，永不停止。
      · 取到第一条记录之前出错会**抛错**（什么都没丢）；之后出错则回已收部分 + stop_reason。
    """
    act: dict[str, Any] = {"action": "collect", "limit": limit, "max_pages": max_pages,
                           "advance": advance, "deviants": deviants,
                           "multi_policy": multi_policy}
    for key, val in (("from", from_id), ("fields", fields), ("next", next),
                     ("stalled_after", stalled_after), ("wait_ms", wait_ms),
                     ("settle_ms", settle_ms)):
        if val is not None:
            act[key] = val
    try:
        if session_id:
            return _dumps(await _call(act_in_session, session_id, act))
        if not url:
            return _dumps({"error": {"code": "bad_arguments",
                                     "message": "collect 需要 session_id 或 url"}})
        return _dumps(await _call(act_on_url, url, act, profile=profile))
    except BlockedAction as e:
        return _dumps({"error": e.detail | {"message": str(e)}})
    except ValueError as e:
        return _dumps({"error": {"code": "bad_arguments", "message": str(e)}})


@mcp.tool()
async def session_open(url: str | None = None, profile: str | None = None,
                       exclude: list[str] | None = None,
                       include_urls: bool = False, tree: bool = False,
                       max_chars: int | None = None) -> str:
    """开启有状态浏览器会话（页面常驻、不重载），返回 session_id 与页面 JSON。

    多步操作（登录、翻页、滚动加载）都应该用会话，而不是反复 browse。
    exclude 同 browse。include_urls 默认也是 False（省 token）；因为会话里 DOM 一直活着，
    随时可以用 session_act 的 {"action":"url","id":...} / {"action":"urls"} 取回任意 URL，
    不必为了 URL 重新抓页面。tree 同 browse（默认关闭）。
    max_chars 同 browse：**交付前体积上限**，超限丢文档序末尾的块并如实报告；
    会话级默认值，可在单个动作里用 {"max_chars": N} 覆盖。
    """
    try:
        return _dumps(await _call(open_session, url, profile=profile,
                                  exclude=exclude, include_urls=include_urls, tree=tree,
                                  max_chars=max_chars))
    except ValueError as e:
        return _dumps({"error": {"code": "bad_arguments", "message": str(e)}})


@mcp.tool()
async def session_act(session_id: str, action: dict[str, Any]) -> str:
    """在会话中执行一个动作，返回动作之后的页面 JSON。

    **每次都带 receipt（结构化回执）**，用来判断动作到底有没有生效：
      {"navigated": {"from","to","kind": "full|spa|newtab"},  // URL 变没变、怎么变的
       "dom_delta": 12,          // 节点数变化（AJAX 增删/弹层出现）
       "appeared": ["a31"], "removed": ["a7"]}   // 相对上一次提取的 id 集合差（精确，不是猜测）
    回执为空 {} 也表示「这次动作既没换页、也没动 DOM」——不要把它当成成功。

    元素级动作（用上一次返回里的 id）：
      {"id": "a4", "text": "关键词"}      输入文本（会先清空原内容）
      {"id": "a5", "option": "行星"}      选择下拉项（按 label/value/序号/模糊匹配）
      {"id": "a0"}                        点击；点是 <form> 时必须显式加 "submit": true
      {"id": "a0", "submit": true}        提交表单（隐式提交已取消，避免误引用触发副作用）
    页面级动作：
      {"action": "scroll", "pages": 2} / {"action": "back"}
      {"action": "wait", "ms": 1000}   / {"action": "goto", "url": "https://..."}
      {"action": "press", "key": "Enter"}   在当前焦点处按键（提交搜索框的通用办法：
                                            很多站点的提交按钮是 div + JS，提取不到可点元素）
      {"action": "click_text", "text": "新建会话"}   按可见文本点击「普通 div + JS」控件
    取 URL（不改页面，返回紧凑的 {"urls": {id: url}}，适合 browse 时用了 include_urls=False）：
      {"action": "url", "id": "a3"}       取一个元素的 URL
      {"action": "url", "ids": ["a3","a7"]}  取多个
      {"action": "urls"}                  取当前页所有 URL
    可选 {"doc": "<上一份 JSON 里的 doc>"}：页面已变化时会明确报错，而不是点错元素。

    目标被浮层盖住/禁用/尺寸为 0 时，会**立刻**返回结构化错误（不是等 30 秒超时）：
      {"error": {"code": "blocked_by", "kind": "overlay", "cover": "div.mask", "message": "..."}}

    本工具也会转交 `collect` 动作的结果，因此同样适用那条契约事实：
    **`stop_reason="stalled"` 表示工具无法证实列表已到底（证据已原样暴露在响应里），
    是否到底由你判断；只有 `exhausted` 才是工具主张的到底。**

    体积控制：动作字典里可以带 `{"max_chars": N}`（交付前体积上限，超限丢**文档序末尾**的块，
    并在 `coverage.truncated` 里如实报告丢了什么）。
    """
    try:
        return _dumps(await _call(act_in_session, session_id, action))
    except BlockedAction as e:      # 结构化错误：让 agent 拿到字段，而不是从中文里正则抠
        return _dumps({"error": e.detail | {"message": str(e)}})
    except ValueError as e:         # 会话不存在/已过期：也回结构化错误，别让 MCP 层报工具崩溃
        return _dumps({"error": {"code": "bad_arguments", "message": str(e)}})


@mcp.tool()
async def session_close(session_id: str) -> str:
    """关闭会话并释放浏览器进程。会话空闲 600 秒也会被自动回收。"""
    return _dumps({"closed": await _call(close_session, session_id)})


@mcp.tool()
async def session_list() -> str:
    """列出当前存活的会话（session_id、档案名、空闲秒数）。"""
    return _dumps(await _call(list_sessions))


@mcp.tool()
async def cookies_import(profile: str, cookie_file: str) -> str:
    """把浏览器扩展导出的 cookie JSON 导入某个档案，用于免人工登录。

    适用于无显示环境：在能登录的浏览器里导出 cookie，导入后该 profile 即带登录态。
    """
    try:
        n = await _call(import_cookies, profile, cookie_file)
    except (ValueError, OSError) as e:
        return _dumps({"error": {"code": "bad_arguments", "message": str(e)}})
    return _dumps({"imported": n, "profile": profile})


# ============================ 传输层 ============================


class BearerGuard:
    """ASGI 中间件：有 token 时校验 Bearer；无 token 时校验 Host 为回环字面量。

    关键：仅回环绑定(127.0.0.1)挡不住 DNS rebinding——浏览器发起的 rebinding
    请求恰恰来自本机回环。所以无 token 时也必须校验 Host 是回环字面量，
    否则攻击者可用 attacker.com 解析到 127.0.0.1 借本机服务驱动浏览器。
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
    """返回可交给 uvicorn 的 ASGI 应用；无条件挂载 BearerGuard（见该类的注释）。

    默认 **无状态**（stateless_http=True）：传输层不为每个客户端维护 Mcp-Session-Id。
    有状态模式下服务端一旦重启，客户端仍会带着旧 session id 请求，得到
    `-32600 Session not found`——工具"在"但调不通，必须重载客户端配置才恢复。
    浏览器的会话状态由本模块自己管（SESSIONS），不依赖传输层会话，因此无状态更稳。
    需要服务端主动推送通知时才用 stateful=True。
    """
    app = mcp.streamable_http_app(streamable_http_path=MCP_PATH, host=host,
                                  stateless_http=not stateful)
    return BearerGuard(app, token)   # 无条件挂载：有 token 校验 Bearer，无 token 校验 Host 回环


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="ai-browser-mcp",
                                 description="ai_browser 的 MCP 服务端（stdio 或 streamable-http）")
    ap.add_argument("--transport", choices=("stdio", "http"), default="stdio",
                    help="stdio：由客户端拉起（默认）；http：常驻 streamable-http 服务")
    ap.add_argument("--host", default=DEFAULT_HOST, help=f"HTTP 监听地址（默认 {DEFAULT_HOST}）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"HTTP 监听端口（默认 {DEFAULT_PORT}）")
    ap.add_argument("--token", default=os.environ.get("AI_BROWSER_MCP_TOKEN"),
                    help="HTTP Bearer 令牌；缺省读 AI_BROWSER_MCP_TOKEN，都没有则只允许回环且不校验")
    ap.add_argument("--stateful", action="store_true",
                    help="启用有状态传输会话（默认无状态：服务重启不会让客户端卡在 Session not found）")
    args = ap.parse_args(argv)

    if args.transport == "stdio":
        mcp.run()
        return

    import uvicorn

    if not args.token and args.host not in ("127.0.0.1", "::1", "localhost"):
        print(f"ai-browser-mcp: 监听 {args.host} 却未设置令牌，拒绝启动（请加 --token）", file=sys.stderr)
        raise SystemExit(2)
    if not args.token:
        print("ai-browser-mcp: 未设置令牌，仅本机回环可用、不校验 Authorization", file=sys.stderr)
    mode = "有状态" if args.stateful else "无状态"
    print(f"ai-browser-mcp: streamable-http（{mode}）已启动 http://{args.host}:{args.port}{MCP_PATH}",
          file=sys.stderr)
    uvicorn.run(build_http_app(args.host, args.port, args.token, stateful=args.stateful),
                host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
