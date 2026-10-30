"""REST API 服务 v0.4

启动：
  uvicorn ai_browser.server:app --host 127.0.0.1 --port 8000

鉴权（v0.4 新增）：
  设置 AI_BROWSER_TOKEN 后，所有请求必须带 `X-Auth-Token: <token>`（或 `?token=<token>`）。
  未设置该环境变量时，服务只接受来自本机（127.0.0.1/::1）的请求——远程访问会被 403 拒绝。
  这台服务能驱动你**已登录的浏览器**，请不要在无鉴权的情况下暴露到公网。

端点：
  GET  /health                                  → 存活探测（不鉴权）
  GET  /parse?url=...&static=false&profile=...   → 页面纯数据 JSON
  POST /act        {"url": "...", "action": {...}, "profile": "..."}
  POST /collect    {"session_id": "...", "from": "a7", "fields": ["text","href"], "limit": 100}
  POST /session/open   {"url": "...", "profile": "bilibili"}  → {"session_id": ..., 页面 JSON}
  POST /session/act    {"session_id": "...", "action": {...}} → 动作后的页面 JSON
  POST /session/close  {"session_id": "..."}
  GET  /sessions                                → 当前会话列表

action 两种形态：
  元素级：{"id": "a4", "text": "关键词"} / {"id": "a5", "option": "行星"} / {"id": "a0"}
  页面级：{"action": "scroll", "pages": 2} / {"action": "back"}
          {"action": "wait", "ms": 1000} / {"action": "goto", "url": "..."}
          {"action": "collect", "from": "a7", "limit": 100}     ← 记录级抓取（会翻页）

tree=true（/parse、/session/open、/act）：每个块附带 id/p/i/tag 与列表的 item_ids，
让调用方能自己复核 collect 的分组结果；代价约 +17% 体积，默认关闭。

并发：v0.3 用单线程执行器把**所有**请求串行化（一次慢解析阻塞全部客户端）。v0.4 由 core
按会话/无状态分别持有执行器，本层不再做串行化。
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import __version__
from .core import (BlockedAction, act_in_session, act_on_url, close_session,
                   list_sessions, open_session, parse_url)

TOKEN = os.environ.get("AI_BROWSER_TOKEN") or None
DEFAULT_HOST = os.environ.get("AI_BROWSER_HOST", "127.0.0.1")   # 不 import mcp_server，避免 REST 依赖 mcp 包
DEFAULT_REST_PORT = int(os.environ.get("AI_BROWSER_REST_PORT", "8000"))
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def require_auth(
    request: Request,
    x_auth_token: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> None:
    if request.url.path == "/health":   # 存活探测不鉴权，也不暴露内容
        return
    if TOKEN:
        provided = x_auth_token or token or ""
        if not secrets.compare_digest(provided, TOKEN):
            raise HTTPException(status_code=401, detail="X-Auth-Token 缺失或不正确")
        return
    host = request.client.host if request.client else ""
    if host not in LOOPBACK:
        raise HTTPException(
            status_code=403,
            detail="未配置 AI_BROWSER_TOKEN，仅允许本机访问；远程使用请设置该环境变量",
        )


app = FastAPI(title="ai-browser", version=__version__,
              description="HTML → 纯数据 JSON：让 AI 像人一样访问互联网",
              dependencies=[Depends(require_auth)])

if not TOKEN:
    print("ai-browser: 未设置 AI_BROWSER_TOKEN，服务仅接受本机请求", file=sys.stderr)


class ActRequest(BaseModel):
    url: str
    action: dict[str, Any]
    static: Optional[bool] = False
    profile: Optional[str] = None
    exclude: Optional[list[str]] = None
    include_urls: Optional[bool] = True
    tree: Optional[bool] = False
    max_chars: Optional[int] = Field(
        default=None, description='交付前体积上限（字符）：超限丢文档序末尾的块并如实报告')


class OpenRequest(BaseModel):
    url: Optional[str] = None
    profile: Optional[str] = None
    static: Optional[bool] = False
    exclude: Optional[list[str]] = None
    include_urls: Optional[bool] = True
    tree: Optional[bool] = False
    max_chars: Optional[int] = Field(
        default=None, description='交付前体积上限（字符）：超限丢文档序末尾的块并如实报告')


class CollectRequest(BaseModel):
    """记录级抓取。session_id 优先；只给 url 时开一次临时浏览器（无状态）。"""
    session_id: Optional[str] = None
    url: Optional[str] = None
    profile: Optional[str] = None
    from_: Optional[str] = Field(default=None, alias="from")
    fields: Optional[Any] = None
    limit: Optional[int] = None
    max_pages: Optional[int] = None
    advance: Optional[Any] = None
    next: Optional[dict[str, Any]] = None
    multi_policy: Optional[str] = None
    deviants: Optional[str] = None
    dedupe: Optional[bool] = None
    wait_ms: Optional[int] = None
    settle_ms: Optional[int] = Field(
        default=None,
        description="空列表「是不是真的空了」的确认预算（毫秒，默认 max(wait_ms,1200)，上限 15000）")
    exclude: Optional[list[str]] = None


class SessionActRequest(BaseModel):
    session_id: str
    action: dict[str, Any]


class SessionCloseRequest(BaseModel):
    session_id: str


@app.get("/health")
def health():
    return {"ok": True, "version": app.version, "auth": "token" if TOKEN else "loopback-only"}


@app.get("/parse")
def parse(url: str = Query(..., description="目标页面 URL"),
          static: bool = Query(False, description="禁用 JS 的静态模式"),
          profile: Optional[str] = Query(None, description="持久化档案名，携带登录态"),
          exclude: Optional[list[str]] = Query(None, description="忽略这些 CSS 选择器命中的子树，可重复"),
          include_urls: bool = Query(True, description="false 时不输出元素/媒体/表单的 URL，省 token"),
          tree: bool = Query(False, description="true 时每块附带 id/p/i/tag（供复核分组，+约 17% 体积）"),
          max_chars: Optional[int] = Query(None, description="交付前体积上限（字符）：超限丢文档序末尾的块，coverage.truncated 如实报告")):
    try:
        return parse_url(url, static=static, profile=profile, exclude=exclude,
                         include_urls=include_urls, tree=tree, max_chars=max_chars)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


def _collect_action(req: "CollectRequest") -> dict[str, Any]:
    """把 CollectRequest 收敛成 core 认的动作字典（只带显式给过的字段）。"""
    act: dict[str, Any] = {"action": "collect"}
    for key, val in (("from", req.from_), ("fields", req.fields), ("limit", req.limit),
                     ("max_pages", req.max_pages), ("advance", req.advance), ("next", req.next),
                     ("multi_policy", req.multi_policy), ("deviants", req.deviants),
                     ("dedupe", req.dedupe), ("wait_ms", req.wait_ms),
                     ("settle_ms", req.settle_ms)):
        if val is not None:
            act[key] = val
    return act


@app.post("/collect")
def collect(req: CollectRequest):
    """记录级抓取：一次调用把「点下一页 + 每页重读整页」压成一列记录。

    返回的是**采集结果**（records/stop_reason/next…），不是一整页 blocks。
    """
    try:
        act = _collect_action(req)
        if req.session_id:
            return act_in_session(req.session_id, act)
        if not req.url:
            raise ValueError("collect 需要 session_id 或 url")
        return act_on_url(req.url, act, profile=req.profile, exclude=req.exclude)
    except BlockedAction as e:
        raise HTTPException(status_code=409, detail=e.detail | {"message": str(e)})
    except ValueError as e:
        # 会话不存在 → 404；动作参数错（例如 collect limit<=0）→ 400，别把两者混成一个码
        missing = "不存在或已过期" in str(e)
        raise HTTPException(status_code=404 if missing else 400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/act")
def act(req: ActRequest):
    try:
        return act_on_url(req.url, req.action, static=bool(req.static),
                          profile=req.profile, exclude=req.exclude,
                          include_urls=bool(req.include_urls), tree=bool(req.tree),
                          max_chars=req.max_chars)
    except BlockedAction as e:
        raise HTTPException(status_code=409, detail=e.detail | {"message": str(e)})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/session/open")
def session_open(req: OpenRequest):
    try:
        return open_session(req.url, profile=req.profile, static=bool(req.static),
                            exclude=req.exclude, include_urls=bool(req.include_urls),
                            tree=bool(req.tree))
    except ValueError as e:
        # 会话数达上限 → 429（限流语义）；URL 非法等参数错误 → 400
        if "会话数已达上限" in str(e):
            raise HTTPException(status_code=429, detail=str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/session/act")
def session_act(req: SessionActRequest):
    try:
        return act_in_session(req.session_id, req.action)
    except BlockedAction as e:      # 结构化：{"detail": {"code": "blocked_by", "kind": ...}}
        raise HTTPException(status_code=409, detail=e.detail | {"message": str(e)})
    except ValueError as e:
        # 会话不存在 → 404；动作参数错（例如 collect limit<=0）→ 400，别把两者混成一个码
        missing = "不存在或已过期" in str(e)
        raise HTTPException(status_code=404 if missing else 400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/session/close")
def session_close(req: SessionCloseRequest):
    return {"closed": close_session(req.session_id)}


@app.get("/sessions")
def sessions():
    return {"sessions": list_sessions()}


def main(argv: list[str] | None = None) -> None:
    """命令行启动 REST 服务，与 `python -m ai_browser.cli` / `mcp_server` 对称。

        python -m ai_browser.server --port 8000 --token <令牌>
    """
    global TOKEN
    ap = argparse.ArgumentParser(prog="ai-browser-server",
                                 description="ai_browser 的 REST 服务（FastAPI）")
    ap.add_argument("--host", default=DEFAULT_HOST, help=f"监听地址（默认 {DEFAULT_HOST}）")
    ap.add_argument("--port", type=int, default=DEFAULT_REST_PORT,
                    help=f"监听端口（默认 {DEFAULT_REST_PORT}）")
    ap.add_argument("--token", default=TOKEN,
                    help="X-Auth-Token 令牌；缺省读 AI_BROWSER_TOKEN，都没有则只允许本机访问")
    ap.add_argument("--reload", action="store_true", help="开发模式：代码变更自动重载")
    args = ap.parse_args(argv)

    if args.token:
        TOKEN = args.token          # 直接传 app 对象，uvicorn 不会重新导入本模块
    elif args.host not in ("127.0.0.1", "::1", "localhost"):
        print(f"ai-browser-server: 监听 {args.host} 却未设置令牌，拒绝启动（请加 --token）", file=sys.stderr)
        raise SystemExit(2)

    import uvicorn

    print(f"ai-browser-server: REST 已启动 http://{args.host}:{args.port}"
          f"（鉴权：{'令牌' if TOKEN else '仅本机'}）", file=sys.stderr)
    if args.reload:
        # reload 由子进程重新导入模块，令牌只能经环境变量传递
        if TOKEN:
            os.environ["AI_BROWSER_TOKEN"] = TOKEN
        uvicorn.run("ai_browser.server:app", host=args.host, port=args.port,
                    log_level="info", reload=True)
    else:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
