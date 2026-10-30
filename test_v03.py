"""ai_browser 回归测试（v0.4）

覆盖：
  T1  profile 持久化 / 无 profile 隔离
  T2  会话内状态连续
  T3  scroll 触发无限加载
  T4  cookie 导入即登录
  T5  元素 id 不错位：点段落里的链接，不能点到它前面的按钮（v0.3 的 P0）
  T6  裸文本兜底：无语义标签的文本不再丢失
  T7  懒加载图片取 data-src 而不是占位符
  T8  表格表头与 caption
  T9  stats 诚实：json_chars 等于实际交付的 JSON 长度
  T10 输入动作先清空预填内容
  T11 select 按选项文本选择
  T12 doc 令牌不匹配时明确报错（而不是点错元素）
  T13 REST 令牌鉴权

用法：
    python test_v03.py            # 直接跑
全部通过退出码 0，任一失败退出码 1。
"""
from __future__ import annotations

import functools
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# 这套断言会在中文 Windows（stdout 默认 GBK）上跑：任何**不在 GBK 里**的字符（✓ ✗ ⚠ ∈ …）
# 一旦被 print，测试自己就会 UnicodeEncodeError 崩掉——那是最冤的红。
# 降级成可替换字符而不是崩：报告难看一点，但结论一定出得来。
try:
    sys.stdout.reconfigure(errors="replace")        # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

# 每次运行都用全新的 profile 目录：避免上一轮残留让 T1 假通过
PROFILES = Path(tempfile.mkdtemp(prefix="ai-browser-profiles-"))
os.environ["AI_BROWSER_PROFILES"] = str(PROFILES)

from ai_browser import (act_in_session, act_on_url, close_session,  # noqa: E402
                        import_cookies, normalize_url, open_session, parse_url)

# ---- 测试站点：用多线程服务器，浏览器会并发连接（v0.3 用单线程 TCPServer） ----
class _SiteHandler(http.server.SimpleHTTPRequestHandler):
    """测试站点处理器：在静态文件之外加一条**永不返回**的 `/hold`。

    为什么需要它（第三十一轮·T122）：`parse_url` 默认 `wait_until="networkidle"`，
    而"有连接永不关闭"的页面上静置**永不成立** ⇒ 修前会白等满整个 `timeout`。
    要钉住"挂起连接不许拖满超时"，就得让服务器**真的挂住**一个请求——
    静态文件服务器做不到（每个请求都立刻完成）。
    挂住的线程随进程结束消失（daemon），不影响后续用例。
    """
    def do_GET(self):                                   # noqa: N802（沿用父类命名）
        if self.path.split("?")[0] == "/hold":
            time.sleep(120)                             # 永不返回：按住连接不放
            return
        super().do_GET()


Handler = functools.partial(_SiteHandler, directory=str(HERE / "test_site"))
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"

results: dict[str, bool] = {}
detail: list[str] = []


def check(name: str, ok: bool, info: str = "") -> None:
    results[name] = bool(ok)
    if info:
        detail.append(f"    {name}: {info}")


def paragraphs(page: dict) -> list[str]:
    return [b.get("text", "") for b in page["blocks"] if b.get("type") == "paragraph"]


def action_by(page: dict, **match) -> dict:
    """在 blocks（含表单子元素）里找第一个匹配的 action。"""
    flat = []
    for b in page["blocks"]:
        if b.get("type") == "action":
            flat.append(b)
        for c in b.get("children", []):
            flat.append(c)
    for a in flat:
        if all(a.get(k) == v for k, v in match.items()):
            return a
    raise AssertionError(f"找不到匹配 {match} 的元素，实际有 {[ (a.get('id'), a.get('label')) for a in flat ]}")


# ---------------- T1 profile 持久化 ----------------
s = open_session(f"{BASE}/index.html", profile="t1")
btn = next(b for b in s["blocks"] if b["type"] == "action" and b.get("label") == "模拟登录")
act_in_session(s["session_id"], {"id": btn["id"]})
close_session(s["session_id"])
d = parse_url(f"{BASE}/index.html", profile="t1")
check("T1a 登录态跨会话存活", any("已登录" in t for t in paragraphs(d)))
d2 = parse_url(f"{BASE}/index.html")
check("T1b 无profile时未登录", not any("已登录" in t for t in paragraphs(d2)))

# ---------------- T2 会话有状态 ----------------
s = open_session(f"{BASE}/counter.html")
btn = next(b for b in s["blocks"] if b["type"] == "action")
act_in_session(s["session_id"], {"id": btn["id"]})
r2 = act_in_session(s["session_id"], {"id": btn["id"]})
check("T2 会话内状态连续", any("点击数 2" in t for t in paragraphs(r2)))
close_session(s["session_id"])

# ---------------- T3 无限滚动 ----------------
s = open_session(f"{BASE}/feed.html")
before = next(b for b in s["blocks"] if b["type"] == "list")["items"]
r = act_in_session(s["session_id"], {"action": "scroll", "pages": 2})
after = next(b for b in r["blocks"] if b["type"] == "list")["items"]
check("T3 scroll触发加载", len(after) > len(before), f"{len(before)} → {len(after)} 条")
close_session(s["session_id"])

# ---------------- T4 cookie 导入 ----------------
ck = Path(PROFILES) / "ck.json"
ck.write_text(json.dumps([{"name": "ai_user", "value": "stella",
                           "domain": "127.0.0.1", "path": "/"}]), encoding="utf-8")
n = import_cookies("t4", str(ck))
d = parse_url(f"{BASE}/index.html", profile="t4")
check(f"T4 cookie导入即登录({n}条)", any("已登录" in t for t in paragraphs(d)))

# ---------------- T5~T12 widgets 页面 ----------------
s = open_session(f"{BASE}/widgets.html")
sid = s["session_id"]

# T5：段落里的链接 id 必须指向链接本身（v0.3 会指向前面的按钮）
para = next(b for b in s["blocks"] if b["type"] == "paragraph" and b.get("links"))
link_id = para["links"][0]["id"]
after = act_in_session(sid, {"id": link_id, "doc": s["doc"]})
check("T5 段内链接不再错位", any("点了返回" in t for t in paragraphs(after)),
      f"点 {link_id}({para['links'][0]['label']}) → {paragraphs(after)[-1:]}")

# T6：裸文本兜底
texts = " ".join(paragraphs(s))
check("T6a div裸文本不丢失", "没有任何语义标签包裹" in texts)
check("T6b span裸文本不丢失", "嵌套在 span 里的裸文本" in texts)

# T7：懒加载图片
imgs = [b for b in s["blocks"] if b["type"] == "image"]
check("T7 懒加载图片取 data-src", any(str(b.get("url", "")).endswith("lazy-real.png") for b in imgs),
      f"{[b.get('url') for b in imgs]}")

# T8：表格表头与标题
tables = [b for b in s["blocks"] if b["type"] == "table"]
check("T8a 表头标记", bool(tables) and tables[0].get("header_rows") == 1)
check("T8b 表格 caption", bool(tables) and tables[0].get("caption") == "观测参数")

# T9：stats 口径诚实
real_len = len(json.dumps(s, ensure_ascii=False))
check("T9 stats.json_chars 等于实际 JSON 长度", s["stats"]["json_chars"] == real_len,
      f"自报 {s['stats']['json_chars']} / 实际 {real_len}")

# T10：输入先清空
inp = action_by(s, kind="type", name="q")
after = act_in_session(sid, {"id": inp["id"], "text": "新关键词"})
now = action_by(after, kind="type", name="q")
check("T10 输入前清空预填内容", now.get("value") == "新关键词", f"value={now.get('value')!r}")

# T11：select 按文本选择
sel = action_by(s, kind="select", name="cat")
after = act_in_session(sid, {"id": sel["id"], "option": "行星"})
now = action_by(after, kind="select", name="cat")
check("T11 select 按选项文本选择", now.get("value") == "行星", f"value={now.get('value')!r}")

# T12：doc 令牌不匹配
try:
    act_in_session(sid, {"id": link_id, "doc": "deadbeef"})
    check("T12 doc不匹配时报错", False, "居然没报错")
except ValueError as e:
    check("T12 doc不匹配时报错", "doc" in str(e), str(e))
close_session(sid)

# ---------------- T14 隐藏内容不得进入输出（visibility 与裸文本兜底不冲突） ----------------
from pathlib import Path as _P  # noqa: E402

demo_file = _P(__file__).resolve().parent / "demo_page.html"
hide = parse_url(demo_file.as_uri())
all_text = " ".join(json.dumps(b, ensure_ascii=False) for b in hide["blocks"])
check("T14 隐藏广告未被提取", "隐藏广告" not in all_text)
check("T14b 可见正文仍在", any("系外行星" in t for t in paragraphs(hide)))

# ---------------- T15 URL 规范化（模型/CLI 常直接给本机路径或省协议） ----------------
bare = str((_P(__file__).resolve().parent / "test_site" / "counter.html"))  # Windows: D:\..\counter.html
check("T15a 裸路径补 file://", normalize_url(bare).startswith("file:///"), normalize_url(bare)[:40])
check("T15b 省协议补 https", normalize_url("example.com/x") == "https://example.com/x")
check("T15c 本机补 http", normalize_url("localhost:8000/x") == "http://localhost:8000/x")
check("T15d 已有协议不动", normalize_url("https://a.b/c") == "https://a.b/c")
try:
    normalize_url("javascript:alert(1)")
    check("T15e 拒绝 javascript:", False, "居然放过了")
except ValueError as e:
    check("T15e 拒绝 javascript:", "javascript" in str(e))

# 裸路径走完整会话链路（这正是 REST /session/open 曾经的报错来源）
try:
    s_bare = open_session(bare)
    ok_bare = any("点击数 0" in t for t in paragraphs(s_bare))
    close_session(s_bare["session_id"])
    check("T16 裸路径可直接开会话", ok_bare)
except Exception as e:  # noqa: BLE001
    check("T16 裸路径可直接开会话", False, f"{type(e).__name__}: {e}")

# ---------------- T17 无状态单次动作（REST /act、act_on_url 走的路径） ----------------
# 该路径的页面上没人跑过提取，元素还没有 data-ai-id；v0.4 改 id 方案时曾在这里回归成 400
try:
    d = parse_url(f"{BASE}/counter.html")
    btn = next(b for b in d["blocks"] if b["type"] == "action")
    r = act_on_url(f"{BASE}/counter.html", {"id": btn["id"]})
    check("T17 无状态动作可用", any("点击数 1" in t for t in paragraphs(r)),
          f"点 {btn['id']} → {paragraphs(r)}")
except Exception as e:  # noqa: BLE001
    check("T17 无状态动作可用", False, f"{type(e).__name__}: {e}")

# ---------------- T18 press 动作（真实站点常把提交做成 div + JS，只能靠回车） ----------------
try:
    s = open_session(f"{BASE}/widgets.html")
    sid2 = s["session_id"]
    inp = action_by(s, kind="type", name="q")
    before_url = s["url"]
    act_in_session(sid2, {"id": inp["id"], "text": "回车测试"})
    after = act_in_session(sid2, {"action": "press", "key": "Enter"})
    moved = after["url"] != before_url and "q" in after["url"]
    check("T18 press 回车提交表单", moved, f"{before_url.split('/')[-1]} → {after['url'].split('/')[-1]}")
    close_session(sid2)
except Exception as e:  # noqa: BLE001
    check("T18 press 回车提交表单", False, f"{type(e).__name__}: {e}")

# ---------------- T19 exclude：按站点排除已渲染但只靠 hover 收起的浮层 ----------------
try:
    full = parse_url(f"{BASE}/widgets.html")
    lean = parse_url(f"{BASE}/widgets.html", exclude=[".soup", ".soup2"])
    hit_full = any("没有任何语义标签包裹" in t for t in paragraphs(full))
    hit_lean = any("没有任何语义标签包裹" in t for t in paragraphs(lean))
    check("T19 exclude 排除指定子树", hit_full and not hit_lean and lean["stats"]["json_chars"] < full["stats"]["json_chars"],
          f"排除前 {full['stats']['json_chars']} 字符 → 排除后 {lean['stats']['json_chars']} 字符")
except Exception as e:  # noqa: BLE001
    check("T19 exclude 排除指定子树", False, f"{type(e).__name__}: {e}")

# ---------------- T20 跟随新标签页（target=_blank / window.open，B 站搜索就是这样） ----------------
try:
    s = open_session(f"{BASE}/popup.html")
    sid3 = s["session_id"]
    btn = next(b for b in s["blocks"] if b["type"] == "action" and b.get("tag") == "button")
    r = act_in_session(sid3, {"id": btn["id"], "doc": s["doc"]})
    followed = bool(r.get("followed_new_tab")) and r["url"].endswith("/feed.html")
    check("T20 动作开新标签页后自动跟随", followed,
          f"{s['url'].split('/')[-1]} → {r['url'].split('/')[-1]} followed={r.get('followed_new_tab')}")
    close_session(sid3)
except Exception as e:  # noqa: BLE001
    check("T20 动作开新标签页后自动跟随", False, f"{type(e).__name__}: {e}")

# ---------------- T21 内联 base64 图不得把 JSON 挤爆（字节跳动官网实测：单张图 URL 1 万字符） ----------------
try:
    from ai_browser import parse_html  # noqa: E402

    big = "data:image/png;base64," + ("iVBORw0KGgoAAAANSUhEUg" * 80)      # ≈2KB 内联图
    small = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw=="
    probe = f'<html><body><p>正文</p><img src="{big}" alt="大内联图">' \
            f'<img src="{small}" alt="小内联图"></body></html>'
    ph = parse_html(probe)
    imgs2 = [b for b in ph["blocks"] if b["type"] == "image"]
    big_blk = next((b for b in imgs2 if b.get("alt") == "大内联图"), None)
    small_blk = next((b for b in imgs2 if b.get("alt") == "小内联图"), None)
    ok21 = (big_blk is not None and big_blk.get("inline") is True and not big_blk.get("url")
            and big_blk.get("inline_bytes", 0) > 1000
            and small_blk is not None and str(small_blk.get("url", "")).startswith("data:"))
    check("T21 大内联图只记元数据、小内联图保留", ok21,
          f"大图 url={big_blk.get('url') if big_blk else None} inline={big_blk.get('inline') if big_blk else None} "
          f"bytes={big_blk.get('inline_bytes') if big_blk else None}；"
          f"json_chars={ph['stats']['json_chars']}（载荷若被内联会是 2000+）")
except Exception as e:  # noqa: BLE001
    check("T21 大内联图只记元数据、小内联图保留", False, f"{type(e).__name__}: {e}")

# ---------------- T22 见文件后段（v0.4.3 起改为「只省媒体、保留链接 href」） ----------------

# ---------------- T23 按 id 单独取回 URL（含媒体，且返回不带整页 blocks） ----------------
try:
    s = open_session(f"{BASE}/widgets.html", include_urls=False)
    sid4 = s["session_id"]
    para = next(b for b in s["blocks"] if b["type"] == "paragraph" and b.get("links"))
    lid = para["links"][0]["id"]
    img = next(b for b in s["blocks"] if b["type"] == "image")
    one = act_in_session(sid4, {"action": "url", "id": lid})
    img_u = act_in_session(sid4, {"action": "url", "id": img["id"]})
    many = act_in_session(sid4, {"action": "url", "ids": [lid, img["id"]]})
    allu = act_in_session(sid4, {"action": "urls"})
    ok23 = (str(one["urls"].get(lid, "")).endswith("?go=back")
            and one["count"] == 1 and "blocks" not in one          # 紧凑返回
            and str(img_u["urls"].get(img["id"], "")).endswith("lazy-real.png")   # 媒体也可寻址
            and many["count"] == 2
            and allu["count"] >= many["count"])                    # urls 取全量
    check("T23 按 id 取回 URL（含媒体）", ok23,
          f"链接 → {one['urls'].get(lid)}；图片({img['id']}) → {img_u['urls'].get(img['id'])}；"
          f"全量 {allu['count']} 条")
    close_session(sid4)
except Exception as e:  # noqa: BLE001
    check("T23 按 id 取回 URL（含媒体）", False, f"{type(e).__name__}: {e}")

# ---------------- T24 反爬/风控拦截要显式报出，而不是默默回空 blocks ----------------
try:
    from ai_browser.core import _block_info  # noqa: E402

    normal = _block_info(200, "<html>" + "x" * 2000 + "</html>", ["sessionid"],
                         [{"type": "paragraph", "text": "正文"}])
    challenge = _block_info(202, '<script src="/C2WF946J0/probe.js"></script>',
                            ["x-waf-captcha-referer"], [])
    empty = _block_info(200, "<html>" + "y" * 2000 + "</html>", [], [])
    # 关键回归：渲染成功的页面里同样残留 WAF 标记，不能因此误报（起点实测踩到过）
    rich = _block_info(200, "<html>probe.js __TENCENT_CHAOS_VM captcha</html>",
                       ["x-waf-captcha-referer"],
                       [{"type": "paragraph", "text": f"正文{i}"} for i in range(5)])
    ok24 = (normal is None                                             # 正常页不误报
            and rich is None                                           # 有正文就不采信 WAF 标记
            and challenge is not None and challenge["status"] == 202
            and "http-202" in challenge["signals"] and "waf-challenge" in challenge["signals"]
            and empty is not None and empty["reason"] == "empty-render")
    check("T24 拦截被显式报出", ok24,
          f"正常页={normal} 有正文含WAF标记={rich} "
          f"挑战页信号={challenge['signals'] if challenge else None} "
          f"空渲染={empty['reason'] if empty else None}")
    plain = parse_url(f"{BASE}/widgets.html")
    check("T24b 正常页面不误报 blocked", "blocked" not in plain)
except Exception as e:  # noqa: BLE001
    check("T24 拦截被显式报出", False, f"{type(e).__name__}: {e}")

# ---------------- T25 stealth 开关：AI_BROWSER_STEALTH=1 时 navigator.webdriver 消失 ----------------
try:
    probe_html = ("<html><body><p id='x'>init</p><script>"
                  "document.getElementById('x').textContent="
                  "'wd='+String(navigator.webdriver)</script></body></html>")
    os.environ.pop("AI_BROWSER_STEALTH", None)
    off = paragraphs(parse_html(probe_html))[0]
    os.environ["AI_BROWSER_STEALTH"] = "1"
    on = paragraphs(parse_html(probe_html))[0]
    os.environ.pop("AI_BROWSER_STEALTH", None)
    check("T25 stealth 开关生效", "wd=true" in off and "wd=undefined" in on,
          f"关={off!r} 开={on!r}")
except Exception as e:  # noqa: BLE001
    os.environ.pop("AI_BROWSER_STEALTH", None)
    check("T25 stealth 开关生效", False, f"{type(e).__name__}: {e}")

# ---------------- T26/T27 contenteditable 富文本输入框（聊天类 SPA 的通用形态） ----------------
try:
    ce_page = parse_html('<html><body><div contenteditable="true" aria-label="写点什么"></div>'
                         '</body></html>')
    ce = next((b for b in ce_page["blocks"] if b["type"] == "action"), None)
    check("T26 contenteditable 识别为 type", bool(ce) and ce.get("kind") == "type",
          f"kind={ce.get('kind') if ce else None} editable={ce.get('editable') if ce else None}")
except Exception as e:  # noqa: BLE001
    check("T26 contenteditable 识别为 type", False, f"{type(e).__name__}: {e}")

try:
    s = open_session(f"{BASE}/widgets.html")
    sid5 = s["session_id"]
    box = next(b for b in s["blocks"] if b["type"] == "action" and b.get("editable"))
    after = act_in_session(sid5, {"id": box["id"], "text": "你好富文本", "doc": s["doc"]})
    ok27 = any("已输入：你好富文本" in t for t in paragraphs(after))
    check("T27 往 contenteditable 输入生效", ok27,
          f"{box['id']} → {[t for t in paragraphs(after) if t.startswith('已输入')]}")
    close_session(sid5)
except Exception as e:  # noqa: BLE001
    check("T27 往 contenteditable 输入生效", False, f"{type(e).__name__}: {e}")

# ---------------- T28 长文本输入不得超时（实测：413 字提问撞上 Playwright 30s 默认超时） ----------------
try:
    from ai_browser.core import _typing_params  # noqa: E402

    short_delay, _ = _typing_params("短")
    _, long_timeout = _typing_params("字" * 500)
    s = open_session(f"{BASE}/widgets.html")
    sid6 = s["session_id"]
    # 真实场景就是往 contenteditable（聊天框/Lexical 编辑器）里发长文本
    box = next(b for b in s["blocks"] if b["type"] == "action" and b.get("editable"))
    long_text = "长文本输入测试" * 45          # 315 字
    t0 = time.time()
    after = act_in_session(sid6, {"id": box["id"], "text": long_text, "doc": s["doc"]})
    spent = time.time() - t0
    echoed = [t for t in paragraphs(after) if t.startswith("已输入")]
    # 粘贴式输入应当是瞬时的：315 字若按逐键至少要 15s，这里要求 < 8s
    ok28 = bool(echoed) and long_text in echoed[0] and spent < 8
    check("T28 长文本用粘贴式瞬时输入", ok28,
          f"{len(long_text)} 字用时 {spent:.1f}s（逐键会 ≥15s）；回显 {len(echoed[0]) if echoed else 0} 字")
    close_session(sid6)
except Exception as e:  # noqa: BLE001
    check("T28 长文本输入不超时", False, f"{type(e).__name__}: {str(e)[:80]}")

# ---------------- T29 按文本点击「普通 div + JS」控件（菜单/会话列表/卡片的通用形态） ----------------
try:
    s = open_session(f"{BASE}/widgets.html")
    sid7 = s["session_id"]
    ghost = [b for b in s["blocks"] if b["type"] == "action"]      # div 控件不该出现在动作里
    after = act_in_session(sid7, {"action": "click_text", "text": "纯 div 按钮", "doc": s["doc"]})
    hit = any("点到了纯 div 按钮" in t for t in paragraphs(after))
    clicked = (after.get("clicked") or {})
    ok29 = hit and clicked.get("tag") == "div"
    check("T29 按文本点击 div 控件", ok29,
          f"动作块 {len(ghost)} 个里没有它；clicked={clicked}")
    close_session(sid7)
except Exception as e:  # noqa: BLE001
    check("T29 按文本点击 div 控件", False, f"{type(e).__name__}: {str(e)[:80]}")

# ---------------- T22 include_urls=False 现在只省「媒体与表单端点」，链接 href 必须保留 ----------------
try:
    def has_media_url(obj) -> bool:
        """媒体/表单类 URL（url/poster/endpoint），这些才是该省的。"""
        if isinstance(obj, dict):
            if any(obj.get(k) for k in ("url", "poster", "endpoint")):
                return True
            return any(has_media_url(v) for v in obj.values())
        if isinstance(obj, list):
            return any(has_media_url(v) for v in obj)
        return False

    def link_hrefs(obj) -> int:
        n = 0
        if isinstance(obj, dict):
            if obj.get("href"):
                n += 1
            for l in obj.get("links", []) or []:
                if l.get("href"):
                    n += 1
            for v in obj.values():
                n += link_hrefs(v)
        elif isinstance(obj, list):
            n += sum(link_hrefs(v) for v in obj)
        return n

    full = parse_url(f"{BASE}/widgets.html")
    lean = parse_url(f"{BASE}/widgets.html", include_urls=False)
    ok22 = (lean.get("urls_omitted") is True
            and lean["url"] == full["url"]                       # 页面自身 url 保留
            and not has_media_url(lean["blocks"])                # 媒体/端点省掉
            and has_media_url(full["blocks"])                    # 对照：默认模式有
            and link_hrefs(lean["blocks"]) > 0                   # 但链接 href 保留（决策依据）
            and link_hrefs(lean["blocks"]) == link_hrefs(full["blocks"])
            and lean.get("coverage", {}).get("capped", {}).get("media_urls", 0) > 0   # 丢了多少必须可见
            and len(lean["blocks"]) == len(full["blocks"]))
    check("T22 --no-urls 只省媒体、保留链接 href", ok22,
          f"{full['stats']['json_chars']} → {lean['stats']['json_chars']} 字符；"
          f"链接 href {link_hrefs(full['blocks'])} → {link_hrefs(lean['blocks'])}；"
          f"capped={lean.get('coverage', {}).get('capped')}")
except Exception as e:  # noqa: BLE001
    check("T22 --no-urls 只省媒体、保留链接 href", False, f"{type(e).__name__}: {e}")

# ---------------- T30 结构化回执：navigated 必须显式报出（URL 变了没有） ----------------
try:
    s = open_session(f"{BASE}/widgets.html")
    sid8 = s["session_id"]
    no_nav = act_in_session(sid8, {"action": "click_text", "text": "纯 div 按钮", "doc": s["doc"]})
    nav = act_in_session(sid8, {"action": "goto", "url": f"{BASE}/counter.html"})
    rec = nav.get("receipt") or {}
    ok30 = ("navigated" not in (no_nav.get("receipt") or {})      # 只改 DOM，不该报 navigated
            and rec.get("navigated", {}).get("kind") == "full"
            and rec["navigated"]["to"].endswith("/counter.html"))
    check("T30 回执 navigated 显式化", ok30,
          f"未跳转回执={no_nav.get('receipt')}；跳转回执={rec.get('navigated')}")
    close_session(sid8)
except Exception as e:  # noqa: BLE001
    check("T30 回执 navigated 显式化", False, f"{type(e).__name__}: {str(e)[:80]}")

# ---------------- T31 被浮层盖住：立刻报 blocked_by，而不是等 30s 超时 ----------------
try:
    s = open_session(f"{BASE}/widgets.html")
    sid9 = s["session_id"]
    covered = next((b for b in s["blocks"] if b["type"] == "action"
                    and (b.get("label") or "").strip() == "被盖住的按钮"), None)
    t0 = time.time()
    try:
        act_in_session(sid9, {"id": covered["id"], "doc": s["doc"]})
        ok31, msg = False, "居然点成功了"
    except ValueError as e:
        spent = time.time() - t0
        err_detail = getattr(e, "detail", {}) or {}
        ok31 = ("blocked_by=overlay" in str(e) and spent < 12
                and err_detail.get("kind") == "overlay" and err_detail.get("cover"))
        msg = f"{spent:.1f}s 后报错：{str(e)[:56]}；结构 detail={err_detail}"
    check("T31 被遮挡立刻报 blocked_by", ok31, msg)
    close_session(sid9)
except Exception as e:  # noqa: BLE001
    check("T31 被遮挡立刻报 blocked_by", False, f"{type(e).__name__}: {str(e)[:80]}")

# ---------------- T32 无跳转动作也必须有回执（另一个 agent 评审点出的洞：AJAX 点击旧实现连回执都不回） ----------------
try:
    s = open_session(f"{BASE}/widgets.html")
    sidA = s["session_id"]
    btn = next(b for b in s["blocks"] if b["type"] == "action"
               and (b.get("label") or "").strip() == "加一个节点")
    after = act_in_session(sidA, {"id": btn["id"], "doc": s["doc"]})
    rec = after.get("receipt")
    ok32 = (rec is not None                                   # 回执一定存在（哪怕是 {}）
            and rec.get("dom_delta", 0) > 0                   # 节点确实变多了
            and "navigated" not in rec                        # 但没换页
            and bool(rec.get("appeared")))                    # id 集合差给出新节点
    check("T32 无跳转动作也有回执", ok32,
          f"receipt 存在={rec is not None} dom_delta={rec.get('dom_delta')} "
          f"appeared={rec.get('appeared')} removed={rec.get('removed')}")
    close_session(sidA)
except Exception as e:  # noqa: BLE001
    check("T32 无跳转动作也有回执", False, f"{type(e).__name__}: {str(e)[:80]}")

# ---------------- T34 新标签页是第三种结局：必须写成 navigated.kind="newtab" ----------------
try:
    s = open_session(f"{BASE}/popup.html")
    sidB = s["session_id"]
    btn = next(b for b in s["blocks"] if b["type"] == "action" and b.get("tag") == "button")
    r = act_in_session(sidB, {"id": btn["id"], "doc": s["doc"]})
    nav = (r.get("receipt") or {}).get("navigated") or {}
    ok34 = r.get("followed_new_tab") is True and nav.get("kind") == "newtab" \
        and str(nav.get("to", "")).endswith("/feed.html")
    check("T34 新标签页写进回执 newtab", ok34, f"navigated={nav}")
    close_session(sidB)
except Exception as e:  # noqa: BLE001
    check("T34 新标签页写进回执 newtab", False, f"{type(e).__name__}: {str(e)[:80]}")

# ---------------- T35 提交按钮要标出来（封住"点是 form"是不够的） ----------------
try:
    p = parse_url(f"{BASE}/widgets.html")
    kids = [c for b in p["blocks"] if b["type"] == "form" for c in b["children"]]
    submit_btn = next((c for c in kids if (c.get("label") or "") == "搜索"), None)
    other_btn = next((c for c in kids if c.get("kind") == "select"), None)
    ok35 = bool(submit_btn) and submit_btn.get("submits") is True \
        and not (other_btn or {}).get("submits") and not (other_btn or {}).get("kind") == "click"
    check("T35 提交按钮标记 submits", ok35,
          f"搜索按钮 submits={submit_btn.get('submits') if submit_btn else None}")
except Exception as e:  # noqa: BLE001
    check("T35 提交按钮标记 submits", False, f"{type(e).__name__}: {str(e)[:80]}")

# ---------------- T36 coverage：必须能解释「文档里有、但没进 blocks」的量 ----------------
try:
    page36 = parse_url(f"{BASE}/widgets.html")
    cov = page36["coverage"]
    ok36 = (cov["candidates"] >= cov["blocks"]
            and cov["blocks"] == len(page36["blocks"])      # 口径与 stats 一致
            and set(cov["dropped"]) == {"invisible", "excluded", "empty"}
            and "media_urls" in cov["capped"] and "inline_images" in cov["capped"]
            and (cov["unrendered"] is None or "known" in cov["unrendered"])
            and cov["undecidable"] >= 0)
    check("T36 coverage 解释省略量", ok36,
          f"candidates={cov['candidates']} blocks={cov['blocks']} dropped={cov['dropped']} "
          f"capped={cov['capped']} unrendered={cov['unrendered']} undecidable={cov['undecidable']}")
except Exception as e:  # noqa: BLE001
    check("T36 coverage 解释省略量", False, f"{type(e).__name__}: {str(e)[:80]}")

# ---------------- T37 提交失败/成功都要有正证据（另一个 agent：submits:true 只解决「点之前」） ----------------
try:
    s = open_session(f"{BASE}/widgets.html")
    sidC = s["session_id"]
    # 1) 必填项为空 → 浏览器拦下 → 无 submit 事件、无跳转 → submitted=false
    blocked_btn = next(c for b in s["blocks"] if b["type"] == "form"
                       for c in b["children"] if (c.get("label") or "").startswith("提交(必填校验)"))
    r1 = act_in_session(sidC, {"id": blocked_btn["id"], "submit": True, "doc": s["doc"]})
    rec1 = r1.get("receipt") or {}
    # 2) 预填了内容 → 校验通过 → 真的提交（跳转）→ submitted=true
    ok_btn = next(c for b in r1["blocks"] if b["type"] == "form"
                  for c in b["children"] if c.get("submits") and c.get("name") is None
                  and (c.get("label") or "") == "搜索")
    r2 = act_in_session(sidC, {"id": ok_btn["id"], "submit": True, "doc": r1["doc"]})
    rec2 = r2.get("receipt") or {}
    ok37 = rec1.get("submitted") is False and rec2.get("submitted") is True
    check("T37 submitted 正证据（失败 false / 成功 true）", ok37,
          f"校验失败: submitted={rec1.get('submitted')} navigated={'navigated' in rec1}；"
          f"成功: submitted={rec2.get('submitted')} navigated={'navigated' in rec2}")
    close_session(sidC)
except Exception as e:  # noqa: BLE001
    check("T37 submitted 正证据（失败 false / 成功 true）", False, f"{type(e).__name__}: {str(e)[:80]}")

# ---------------- T38 collect：一次调用拿一列记录（含游标） ----------------
LIST = f"{BASE}/list.html"
COLLECT_FIELDS = ["text", "href"]
try:
    s = open_session(LIST)
    sidD = s["session_id"]
    r = act_in_session(sidD, {"action": "collect", "limit": 3, "max_pages": 1,
                              "fields": COLLECT_FIELDS})
    recs = r.get("records") or []
    ok38 = (r.get("stop_reason") == "limit" and r.get("count") == 3 == len(recs)
            and r.get("data") == "ok" and r.get("resume") is True
            and recs[0]["text"].startswith("第 0 条记录")
            and recs[0]["href"].endswith("?item=0")
            and recs[0]["_src"]["block"] and recs[0]["_src"]["i"] == 0
            and (r.get("next") or {}).get("collected_this_call") == 3
            and (r.get("next") or {}).get("collected_total") == 3
            and r["field_coverage"]["href"]["present"] == 5          # 覆盖率按「本页记录组」算
            and r["record_shape"]["matched"] >= 3)
    check("T38 collect 一步取一列 + 游标", ok38,
          f"stop={r.get('stop_reason')} count={r.get('count')} pages={r.get('pages')} "
          f"rec0={recs[0] if recs else None} next={bool(r.get('next'))}")
    close_session(sidD)
except Exception as e:  # noqa: BLE001
    check("T38 collect 一步取一列 + 游标", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T39 collect：翻页直到「到底」，而且是**有正证据**的 exhausted ----------------
try:
    s = open_session(LIST)
    sidE = s["session_id"]
    r = act_in_session(sidE, {"action": "collect", "limit": 100, "max_pages": 6,
                              "fields": COLLECT_FIELDS})
    ok39 = (r.get("count") == 15 and r.get("stop_reason") == "exhausted"
            and r.get("pages") == 3 and r.get("resume") is False
            and r.get("dedupe", {}).get("dropped_duplicates") == 0
            and r.get("end_evidence", {}).get("total") == 15
            and r["end_evidence"]["signals"])                     # 到底必须有证据，不许空口
    check("T39 collect 翻到 exhausted（有证据）", ok39,
          f"count={r.get('count')} stop={r.get('stop_reason')} pages={r.get('pages')} "
          f"end={r.get('end_evidence')}")
    close_session(sidE)
except Exception as e:  # noqa: BLE001
    check("T39 collect 翻到 exhausted（有证据）", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T40 collect：多值字段默认用 heading 取值 + 回候选样本；只有「身份不可信」才停 ----------------
try:
    s = open_session(f"{LIST}?mode=multi")
    sidF = s["session_id"]
    r1 = act_in_session(sidF, {"action": "collect", "limit": 20, "max_pages": 1,
                               "fields": COLLECT_FIELDS})
    r2 = act_in_session(sidF, {"action": "collect", "limit": 20, "max_pages": 1,
                               "fields": {"href": "a@href", "link": "a@href"}})
    r3 = act_in_session(sidF, {"action": "collect", "limit": 20, "max_pages": 1,
                               "fields": COLLECT_FIELDS, "multi_policy": "first"})
    d2 = r2.get("stop_detail") or {}
    # 第 3 条的「次选」链接排在标题前面：first 会取错，heading/longest_text 取对
    got_heading = next((x["href"] for x in r1.get("records", []) if "第 2 条" in (x.get("text") or "")), None)
    got_first = next((x["href"] for x in r3.get("records", []) if "第 2 条" in (x.get("text") or "")), None)
    ok40 = (r1.get("count") == 5 and r1.get("stop_reason") == "limit"
            and r1["field_coverage"]["href"]["multi"] == 1
            and r1["field_coverage"]["href"].get("rule") == "heading"
            and len(r1.get("multi_samples") or []) >= 1
            and (got_heading or "").endswith("?item=2")          # 默认规则取到标题链接
            and (got_first or "").endswith("?x=2")               # first 取到「次选」= 错的那个
            and r2.get("stop_reason") == "needs_confirm"
            and d2.get("reason") == "ambiguous_identity" and r2.get("skeleton"))
    check("T40 collect 多值：默认 heading + 样本 + 身份歧义才停", ok40,
          f"heading={got_heading} first={got_first} multi={r1['field_coverage']['href']} "
          f"样本={len(r1.get('multi_samples') or [])} 歧义={r2.get('stop_reason')}/{d2.get('reason')}")
    close_session(sidF)
except Exception as e:  # noqa: BLE001
    check("T40 collect 多值：默认 heading + 样本 + 身份歧义才停", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T41 collect：异构记录组（deviant > 20%）→ needs_confirm，skip 可跳过并给样本 ----------------
try:
    s = open_session(f"{LIST}?mode=hetero")
    sidG = s["session_id"]
    r1 = act_in_session(sidG, {"action": "collect", "limit": 20, "max_pages": 1,
                               "fields": COLLECT_FIELDS})
    r2 = act_in_session(sidG, {"action": "collect", "limit": 20, "max_pages": 1,
                               "fields": COLLECT_FIELDS, "deviants": "skip"})
    d1 = r1.get("stop_detail") or {}
    samples = (r2.get("dedupe") or {}).get("skipped_samples") or []
    ok41 = (r1.get("stop_reason") == "needs_confirm" and d1.get("reason") == "heterogeneous"
            and d1.get("deviant") == 2 and len(d1.get("deviants") or []) == 2
            and len(d1.get("skeleton") or []) >= 3        # 骨架：照着写精确选择器
            and r2.get("count") == 3 and r2["dedupe"]["skipped_deviants"] == 2
            and len(samples) == 2 and samples[0].get("text"))   # 跳过的是广告还是半页结果，必须能看见
    check("T41 collect 异构组先问再跳（带被跳样本）", ok41,
          f"未声明: {r1.get('stop_reason')}/{d1.get('reason')} skeleton={len(d1.get('skeleton') or [])}；"
          f"skip 后 count={r2.get('count')} 样本={[s.get('text') for s in samples]}")
    close_session(sidG)
except Exception as e:  # noqa: BLE001
    check("T41 collect 异构组先问再跳（带被跳样本）", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T42 collect：停止的**种类**由推进回执决定，而不是去重计数 ----------------
try:
    s = open_session(f"{LIST}?mode=stuck")
    sidH = s["session_id"]
    stuck = act_in_session(sidH, {"action": "collect", "limit": 100, "max_pages": 6,
                                  "fields": COLLECT_FIELDS})
    close_session(sidH)
    s2 = open_session(f"{LIST}?mode=cycle")
    sidH2 = s2["session_id"]
    cyc = act_in_session(sidH2, {"action": "collect", "limit": 100, "max_pages": 6,
                                 "fields": COLLECT_FIELDS})
    close_session(sidH2)
    ds = stuck.get("stop_detail") or {}
    dc = cyc.get("stop_detail") or {}
    ok42 = (stuck.get("stop_reason") == "stalled" and ds.get("reason") == "no_advance"
            and stuck.get("resume") is True and stuck.get("count") == 5
            # 跨文档「内容一样」不可区分（可能循环、可能只是复阅同一页）→ 按终态疑罪从无落 stalled
            and cyc.get("stop_reason") == "stalled" and dc.get("reason") == "cross_document_cycle"
            and cyc.get("resume") is True and cyc.get("count") == 5
            and len(dc.get("pages") or []) >= 2)         # 「凭什么说它循环」必须可审计
    check("T42 collect 推进回执定性 stalled/跨文档循环", ok42,
          f"stuck(推进没生效)={stuck.get('stop_reason')}/{ds.get('reason')} resume={stuck.get('resume')}；"
          f"cycle(跨文档复阅)={cyc.get('stop_reason')}/{dc.get('reason')} pages={len(dc.get('pages') or [])}")
except Exception as e:  # noqa: BLE001
    check("T42 collect 推进回执定性 stalled/repeat", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T43 tree=True：块带 id/p/i/tag，列表带 item_ids，采集分组可被复核 ----------------
try:
    s = open_session(LIST, tree=True)
    sidI = s["session_id"]
    blocks = s["blocks"]
    lst = next((b for b in blocks if b.get("type") == "list"), None)
    shaped = all(b.get("id") and "p" in b and "i" in b and b.get("tag") for b in blocks)
    r = act_in_session(sidI, {"action": "collect", "limit": 3, "max_pages": 1,
                              "fields": COLLECT_FIELDS})
    rec_ids = [x["_src"]["block"] for x in (r.get("records") or [])]
    ok43 = (shaped and lst is not None and lst.get("item_ids")
            and all(i in lst["item_ids"] for i in rec_ids)
            and lst.get("p") and lst.get("tag") == "ul")
    check("T43 tree=True 暴露结构（可复核分组）", ok43,
          f"块字段齐备={shaped} item_ids={len(lst.get('item_ids') or []) if lst else None} "
          f"记录 id 全在 item_ids 内={all(i in (lst or {}).get('item_ids', []) for i in rec_ids)}")
    # 体积代价要如实报出来（这是本项目的头号指标）
    lean = parse_url(LIST)
    fat = parse_url(LIST, tree=True)
    detail.append(f"    T43 代价：{lean['stats']['json_chars']} → {fat['stats']['json_chars']} "
                  f"字符（+{(fat['stats']['json_chars'] / max(lean['stats']['json_chars'], 1) - 1) * 100:.1f}%）")
    close_session(sidI)
except Exception as e:  # noqa: BLE001
    check("T43 tree=True 暴露结构（可复核分组）", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T44 collect：参数错误与「第一条之前出错」按契约抛 ----------------
try:
    s = open_session(LIST)
    sidJ = s["session_id"]

    def raises(action) -> str:
        try:
            act_in_session(sidJ, action)
            return ""
        except ValueError as e:
            return str(e)

    e1 = raises({"action": "collect", "limit": 0})
    e2 = raises({"action": "collect", "advance": "teleport"})
    e3 = raises({"action": "collect", "from": "a99999"})      # 锚点不存在 -> 第一条之前 -> 抛
    ok44 = ("limit" in e1 and "advance" in e2 and "无法开始" in e3)
    check("T44 collect 参数/锚点错误显式抛", ok44, f"{e1[:46]} | {e2[:46]} | {e3[:56]}")
    close_session(sidJ)
except Exception as e:  # noqa: BLE001
    check("T44 collect 参数/锚点错误显式抛", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T45 collect：按游标续跑，既不重复也不丢 ----------------
try:
    s = open_session(LIST)
    sidK = s["session_id"]
    r1 = act_in_session(sidK, {"action": "collect", "limit": 5, "max_pages": 6,
                               "fields": COLLECT_FIELDS})
    r2 = act_in_session(sidK, {"action": "collect", "limit": 5, "max_pages": 6,
                               "fields": COLLECT_FIELDS, "next": r1.get("next")})
    got1 = [x["text"] for x in r1.get("records", [])]
    got2 = [x["text"] for x in r2.get("records", [])]
    ok45 = (len(got1) == 5 and len(got2) == 5 and not (set(got1) & set(got2))
            and got1[0].startswith("第 0 条") and got2[0].startswith("第 5 条")
            and r2.get("dedupe", {}).get("dropped_duplicates") == 0)
    check("T45 collect 游标续跑不重复", ok45,
          f"第一批={got1[:1]}… 第二批={got2[:1]}… 交集={len(set(got1) & set(got2))} "
          f"second_collected_total={(r2.get('next') or {}).get('collected_total')}")
    close_session(sidK)
except Exception as e:  # noqa: BLE001
    check("T45 collect 游标续跑不重复", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T46 REST：/collect 端点 + /parse?tree=true + 参数错回 400 ----------------
try:
    from fastapi.testclient import TestClient

    from ai_browser import server as srv_mod

    c = TestClient(srv_mod.app)
    r = c.post("/collect", json={"url": LIST, "fields": ["text", "href"],
                                 "limit": 4, "max_pages": 1}).json()
    bad = c.post("/collect", json={"url": LIST, "limit": 0})
    tree = c.get("/parse", params={"url": f"{BASE}/widgets.html", "tree": "true"}).json()
    ok46 = (r.get("count") == 4 and r.get("stop_reason") == "limit"
            and bad.status_code == 400 and "limit" in str(bad.json().get("detail"))
            and all("tag" in b and "p" in b for b in tree["blocks"]))
    check("T46 REST /collect + tree 参数", ok46,
          f"collect count={r.get('count')}/{r.get('stop_reason')}；limit=0 -> {bad.status_code} "
          f"{str(bad.json().get('detail'))[:30]}；tree 块带 tag/p={all('tag' in b for b in tree['blocks'])}")
except ImportError as e:  # noqa: BLE001
    # 缺依赖时记「跳过」而不是 FAIL（交流板 SylviaMuse 的发现：T46/T98 在无 fastapi/anyio 的
    # 系统 Python 上会红成「契约坏了」，实际是「这条断言没跑」；T13 早就这么处理了）。
    # 跳过**不是**绿：README 写明「全绿」的前提是装齐 requirements.txt。
    detail.append(f"    T46 跳过（缺依赖 {e}）：这一格本轮没有证据")
except Exception as e:  # noqa: BLE001
    check("T46 REST /collect + tree 参数", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T47 CLI：--collect 走通、--help 能渲染（% 号曾把 argparse 打挂） ----------------
try:
    import contextlib
    import io

    from ai_browser import cli as cli_mod

    argv_bak = sys.argv
    try:
        sys.argv = ["ai-browser", f"{LIST}", "--collect", "--fields", "text,href",
                    "--limit", "3", "--max-pages", "1", "--compact"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli_mod.main()
        cli_out = json.loads(buf.getvalue())
        sys.argv = ["ai-browser", "--help"]
        code = 0
        with contextlib.redirect_stdout(io.StringIO()):
            try:
                cli_mod.main()
            except SystemExit as e:
                code = e.code
        ok47 = cli_out.get("count") == 3 and cli_out.get("stop_reason") == "limit" and code == 0
        check("T47 CLI --collect 与 --help", ok47,
              f"collect count={cli_out.get('count')}/{cli_out.get('stop_reason')}；--help 退出码={code}")
    finally:
        sys.argv = argv_bak
except Exception as e:  # noqa: BLE001
    check("T47 CLI --collect 与 --help", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T48 collect：结果条目内部结构可以完全不同（搜索页常态），按主标签定组 ----------------
try:
    s = open_session(f"{LIST}?mode=serp")
    sidL = s["session_id"]
    r1 = act_in_session(sidL, {"action": "collect", "limit": 10, "max_pages": 1,
                               "fields": COLLECT_FIELDS})
    r2 = act_in_session(sidL, {"action": "collect", "limit": 10, "max_pages": 1,
                               "fields": COLLECT_FIELDS, "deviants": "keep"})
    d1 = r1.get("stop_detail") or {}
    cont = (r1.get("containers") or [{}])[0]
    ok48 = (r1.get("stop_reason") == "needs_confirm" and d1.get("reason") == "heterogeneous"
            and cont.get("tag") == "div" and cont.get("count") == 4      # 只认同构会只剩 2 条
            and d1.get("matched") == 2 and d1.get("deviant") == 2
            and r2.get("count") == 4)                                    # keep：全收
    check("T48 collect 异构结果组按主标签定组", ok48,
          f"未声明: stop={r1.get('stop_reason')} tag={cont.get('tag')} count={cont.get('count')} "
          f"matched={d1.get('matched')} deviant={d1.get('deviant')}；keep 后 count={r2.get('count')}")
    close_session(sidL)
except Exception as e:  # noqa: BLE001
    check("T48 collect 异构结果组按主标签定组", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T49 collect：用 --from 锚点指哪打哪（与自动探测找到同一组） ----------------
try:
    s = open_session(LIST, tree=True)
    sidM = s["session_id"]
    lst = next(b for b in s["blocks"] if b.get("type") == "list")
    anchor_id = lst["item_ids"][2]
    by_anchor = act_in_session(sidM, {"action": "collect", "from": anchor_id, "limit": 10,
                                      "max_pages": 1, "fields": COLLECT_FIELDS})
    ok49 = (by_anchor.get("count") == 5
            and (by_anchor.get("containers") or [{}])[0].get("id") == lst["id"]
            and [x["_src"]["block"] for x in by_anchor["records"]] == lst["item_ids"])
    check("T49 collect 锚点定位记录组", ok49,
          f"anchor={anchor_id} count={by_anchor.get('count')} "
          f"container={((by_anchor.get('containers') or [{}])[0]).get('id')} list={lst['id']}")
    close_session(sidM)
except Exception as e:  # noqa: BLE001
    check("T49 collect 锚点定位记录组", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T50 collect：锚定式合并（标题行 + 副信息行 = 一条记录），不用周期法 ----------------
try:
    s = open_session(f"{LIST}?mode=pair")
    sidN = s["session_id"]
    r = act_in_session(sidN, {"action": "collect", "limit": 20, "max_pages": 1,
                              "fields": COLLECT_FIELDS})
    recs = r.get("records") or []
    cont = (r.get("containers") or [{}])[0]
    merge = cont.get("merge") or {}
    ok50 = (r.get("count") == 6 and len(recs) == 6
            and all(len(x["_src"].get("extra") or []) == 1 for x in recs)   # 副信息行被并进来了
            and "分 ·" in (recs[0].get("text") or "")                       # 副信息没丢
            and (recs[0].get("href") or "").endswith("?item=0")             # 链接仍取标题行
            and merge.get("merged") == 6 and merge.get("unmerged_heads") == 0
            and cont.get("count") == 6)
    check("T50 collect 交替式记录锚定合并", ok50,
          f"count={r.get('count')} merge={merge} text0={(recs[0].get('text') if recs else '')[:36]!r}")
    close_session(sidN)
except Exception as e:  # noqa: BLE001
    check("T50 collect 交替式记录锚定合并", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T51 collect：同页重复**保留**，去重只跨页生效（第三方评审 F5） ----------------
try:
    s = open_session(f"{LIST}?mode=dup")
    sidO = s["session_id"]
    r = act_in_session(sidO, {"action": "collect", "limit": 20, "max_pages": 1,
                              "fields": COLLECT_FIELDS})
    d = r.get("stop_detail") or {}
    # dup 夹具里第 3、4 条与第 1 条字段完全相同：它们是**同页的不同元素**，必须都保留
    # （跨页去重是另一回事，由 cycle 模式覆盖：那 5 条会被计入 dropped_duplicates）
    ok51 = (r.get("count") == 5 and r["dedupe"]["dropped_duplicates"] == 0
            and r["dedupe"].get("scope") == "cross_page"
            and r.get("stop_reason") == "limit" and d.get("reason") == "max_pages")
    check("T51 同页重复保留、去重只跨页", ok51,
          f"count={r.get('count')} dropped={r['dedupe']['dropped_duplicates']} "
          f"stop={r.get('stop_reason')}/{d.get('reason')}")
    close_session(sidO)
except Exception as e:  # noqa: BLE001
    check("T51 同页重复保留、去重只跨页", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T52 回执补 text_delta：只展开文本、不增删元素的点击不能是静默的 ----------------
try:
    s = open_session(f"{BASE}/widgets.html")
    sidP = s["session_id"]
    btn = next(b for b in s["blocks"] if b["type"] == "action"
               and (b.get("label") or "").strip() == "展开纯文本")
    after = act_in_session(sidP, {"id": btn["id"], "doc": s["doc"]})
    rec = after.get("receipt") or {}
    ok52 = rec.get("text_delta", 0) > 0 and "appeared" not in rec
    check("T52 回执 text_delta（只展开文本的点击）", ok52,
          f"receipt={rec}")
    close_session(sidP)
except Exception as e:  # noqa: BLE001
    check("T52 回执 text_delta（只展开文本的点击）", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T53 探针用法：collect(limit=1, max_pages=1, advance=null) 一次看清一条记录 ----------------
# 这条路径是「不加 shape 动作」的理由本身：探针必须真的好用，否则那个反驳就不成立。
try:
    s = open_session(LIST)
    sidQ = s["session_id"]
    probe = act_in_session(sidQ, {"action": "collect", "limit": 1, "max_pages": 1,
                                  "advance": None, "fields": COLLECT_FIELDS})
    rec = (probe.get("records") or [{}])[0]
    cont = (probe.get("containers") or [{}])[0]
    ok53 = (probe.get("count") == 1 and probe.get("stop_reason") == "limit"
            and rec.get("text") and rec.get("href")            # 值取到了什么（shape 给不了）
            and cont.get("path") and cont.get("family_sizes")  # 组路径与族大小
            and probe.get("record_shape", {}).get("signature")
            and probe.get("pages") == 1)                       # advance=null：真的没翻页
    check("T53 探针用法 collect(limit=1, advance=null)", ok53,
          f"count={probe.get('count')} pages={probe.get('pages')} "
          f"path={cont.get('path')} text={(rec.get('text') or '')[:20]!r}")
    close_session(sidQ)
except Exception as e:  # noqa: BLE001
    check("T53 探针用法 collect(limit=1, advance=null)", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T54 advance 的语义必须钉死（探针不能靠「偶尔没翻页」） ----------------
try:
    s = open_session(LIST)
    sidR = s["session_id"]
    # (a) 缺省（不写 advance）＝ auto：会翻页（max_pages=2 → 真的点到第 2 页）
    default_paging = act_in_session(sidR, {"action": "collect", "limit": 20, "max_pages": 2,
                                           "fields": COLLECT_FIELDS})
    # (b) 显式 null ＝ none：不翻页
    explicit_null = act_in_session(sidR, {"action": "collect", "limit": 20, "max_pages": 2,
                                          "advance": None, "fields": COLLECT_FIELDS})
    # (c) 探针写法：靠 advance=null 保证不翻页，而**不是**靠 limit 恰好挡在前面
    probe_only = act_in_session(sidR, {"action": "collect", "limit": 1, "max_pages": 5,
                                       "advance": None, "fields": COLLECT_FIELDS})
    leaf = ((default_paging.get("containers") or [{}])[0] or {}).get("path")
    # 只到 limit、从没推进过的情形：reason 也必须是封闭枚举，不许是中文 prose
    limit_only = act_in_session(sidR, {"action": "collect", "limit": 5, "max_pages": 5,
                                       "fields": COLLECT_FIELDS})
    ADVANCE_REASONS = {"user_none", "limit", "max_pages", "total_reached", "pager_disabled",
                       "no_advance_control", "no_advance", "cycle", "heterogeneous",
                       "second_family", "ambiguous_identity", "first_page_instances<2",
                       "advance_blocked", "advance_timeout", "advance_error", "wall", "stalled"}
    reasons = {str((x.get("advance_used") or {}).get("reason")) for x in
               (explicit_null, probe_only, limit_only)}
    ok54 = (default_paging.get("pages") == 2
            and (default_paging.get("advance_used") or {}).get("kind") == "click"
            and (default_paging.get("advance_used") or {}).get("control")
            and explicit_null.get("pages") == 1
            and (explicit_null.get("advance_used") or {}).get("kind") == "none"
            and probe_only.get("pages") == 1 and probe_only.get("count") == 1
            and (probe_only.get("advance_used") or {}).get("kind") == "none"
            and (probe_only.get("advance_used") or {}).get("reason") == "user_none"
            and limit_only.get("count") == 5
            and (limit_only.get("advance_used") or {}).get("reason") == "limit"
            and reasons <= ADVANCE_REASONS)          # 只用封闭枚举，调用方不必抠中文
    check("T54 advance 缺省=auto / null=none 钉死", ok54,
          f"缺省 pages={default_paging.get('pages')} used={default_paging.get('advance_used')}；"
          f"null reason={(explicit_null.get('advance_used') or {}).get('reason')}；"
          f"探针 reason={(probe_only.get('advance_used') or {}).get('reason')}；"
          f"只到 limit reason={(limit_only.get('advance_used') or {}).get('reason')} path={leaf}")
    close_session(sidR)
except Exception as e:  # noqa: BLE001
    check("T54 advance 缺省=auto / null=none 钉死", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T55-T58 第三方对抗性评审的四条反例（夹具 test_site/collect_edge.html） ----------------
# 这四条当初都是**静默错答**：数据看起来完整正确、其实是错的。修完之后必须由测试钉住。
EDGE = f"{BASE}/collect_edge.html"


def edge(mode: str, q: str = "", **act):
    """在对抗夹具上跑一次 collect。q 是附加查询串（如 "&nomore=1"）。"""
    s_ = open_session(f"{EDGE}?mode={mode}{q}")
    try:
        r_ = act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS, **act})
    finally:
        close_session(s_["session_id"])
    return r_


# T55 容器第一个兄弟是分组标题（且与续行同签名）：
#     曾经整组错位一格、最后一条被吃掉，而 deviant=0 / data=ok 全静默
try:
    r_stop = edge("lead", limit=50, max_pages=1, advance=None)
    r_skip = edge("lead", limit=50, max_pages=1, advance=None, deviants="skip")
    texts = [x.get("text") or "" for x in r_stop.get("records") or []]
    hrefs = [x.get("href") or "" for x in r_stop.get("records") or []]
    ok55 = (r_stop.get("count") == 5 and len(texts) == 5
            and all(t.startswith(f"第 {i} 条") and "标签" in t for i, t in enumerate(texts))
            and all(h.endswith(f"?item={i}") for i, h in enumerate(hrefs))
            and (r_stop.get("record_shape") or {}).get("deviant") == 0
            and r_skip.get("count") == 5)          # skip 不许把 5 条真记录删掉
    check("T55 分组标题不污染记录组（不错位/不丢条）", ok55,
          f"count={r_stop.get('count')} texts={texts[:2]}… hrefs={[h[-9:] for h in hrefs[:2]]}… "
          f"deviant={(r_stop.get('record_shape') or {}).get('deviant')} skip_count={r_skip.get('count')}")
except Exception as e:  # noqa: BLE001
    check("T55 分组标题不污染记录组（不错位/不丢条）", False, f"{type(e).__name__}: {str(e)[:90]}")

# T56 正文里一句「共 N 条」+ 真分页器：曾经把 15 条说成 5 条、exhausted、resume=false
try:
    r = edge("bogus", limit=100, max_pages=5)
    ok56 = (r.get("count") == 15 and r.get("pages") == 3
            and (r.get("end_evidence") or {}).get("total_claimed") == 2     # 弱证据只报告
            and (r.get("end_evidence") or {}).get("total_source") == "range"  # 强证据来自区间文本
            and (r.get("end_evidence") or {}).get("total") == 15
            and (r.get("stop_detail") or {}).get("total") != 2)             # 绝不许拿弱证据停
    check("T56 正文「共 N 条」不许当停止证据", ok56,
          f"count={r.get('count')} pages={r.get('pages')} end={r.get('end_evidence')} "
          f"detail={str(r.get('stop_detail'))[:70]}")
except Exception as e:  # noqa: BLE001
    check("T56 正文「共 N 条」不许当停止证据", False, f"{type(e).__name__}: {str(e)[:90]}")

# T57 作者链接在 <h4> 里、标题在外面：heading 规则曾经 5/5 稳定取错
try:
    r = edge("byline", limit=50, max_pages=1, advance=None)
    hrefs = [x.get("href") or "" for x in r.get("records") or []]
    ok57 = (r.get("count") == 5 and all("?doc=" in h for h in hrefs)
            and not any("?author=" in h for h in hrefs))
    check("T57 heading 不取作者链接", ok57, f"hrefs={[h[-10:] for h in hrefs]}")
except Exception as e:  # noqa: BLE001
    check("T57 heading 不取作者链接", False, f"{type(e).__name__}: {str(e)[:90]}")

# T58 字段不足以区分同页记录：曾经第 2 条被当重复静默丢掉（count=5、dropped=1）
try:
    r = edge("price", limit=50, max_pages=1, advance=None, fields={"price": "@data-price"})
    prices = [x.get("price") for x in r.get("records") or []]
    ok58 = (r.get("count") == 6 == len(prices)
            and prices.count("19.90") == 2                       # 两条同价必须在
            and r["dedupe"]["dropped_duplicates"] == 0
            and r["dedupe"].get("scope") == "cross_page")        # 去重只跨页生效
    check("T58 同页同值不许当重复吞掉", ok58,
          f"count={r.get('count')} prices={prices} dropped={r['dedupe']['dropped_duplicates']}")
except Exception as e:  # noqa: BLE001
    check("T58 同页同值不许当重复吞掉", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T59-T61 三张「结束证据不标域」的脸 + 抖动假阳性（判级 agent 要求补的夹具） ----------------
# 这三条在修前都是**静默错答**（我原先按「没实测」压到 C，实测证明该是 S）：
#   setsizeonly 修前 exhausted/count=5(共15)/resume=false，total=1 来自无关 role=tab 的 aria-setsize
#   footerend   修前 exhausted/count=5/resume=false，证据来自页脚一句解释文案
#   jitter      修前 repeat/resume=false，只是点了一下让无关文案变长（第三方标为「未实锤风险」）

# T59 无关 set 的 aria-setsize 不许当总数
try:
    r = edge("setsizeonly", limit=100, max_pages=5)
    end = r.get("end_evidence") or {}
    ok59 = (r.get("count") == 15 and r.get("pages") == 3
            # 判级 agent 的裁定：pager_disabled 移出 STRONG（加载态下站点正是这么禁用的），
            # 所以「翻到底 + 控件禁用 + 页面无正向宣告」现在落 stalled（可重试），不再谎称 exhausted
            and r.get("stop_reason") == "stalled" and r.get("resume") is True
            and end.get("total") is None                      # 无关 tab 的 setsize=1 被忽略
            and end.get("signals") == ["pager_disabled"]       # 只作为「没有下一个控件」上报
            and (r.get("stop_detail") or {}).get("reason") == "pager_disabled_no_declaration")
    check("T59 无关 aria-setsize 不许当总数", ok59,
          f"count={r.get('count')} pages={r.get('pages')} stop={r.get('stop_reason')} end={end}")
except Exception as e:  # noqa: BLE001
    check("T59 无关 aria-setsize 不许当总数", False, f"{type(e).__name__}: {str(e)[:90]}")

# T60 页脚文案不许当「到底」证据
try:
    r = edge("footerend", limit=100, max_pages=5)
    end = r.get("end_evidence") or {}
    ok60 = (r.get("stop_reason") == "stalled" and r.get("resume") is True
            and end.get("end") is False and "no_more_text" not in (end.get("signals") or []))
    check("T60 页脚文案不许当到底证据", ok60,
          f"stop={r.get('stop_reason')} resume={r.get('resume')} signals={end.get('signals')} "
          f"src={(end.get('signal_sources') or {})}")
except Exception as e:  # noqa: BLE001
    check("T60 页脚文案不许当到底证据", False, f"{type(e).__name__}: {str(e)[:90]}")

# T61 与推进无关的抖动不许被当成「推进生效」（否则误报 repeat / resume=false）
try:
    r = edge("jitter", limit=100, max_pages=5)
    d = r.get("stop_detail") or {}
    ok61 = (r.get("stop_reason") == "stalled" and r.get("resume") is True
            and d.get("reason") == "no_advance"
            and (r.get("advance_used") or {}).get("kind") == "click")
    check("T61 无关抖动不算推进生效", ok61,
          f"stop={r.get('stop_reason')}/{d.get('reason')} resume={r.get('resume')} "
          f"adv={r.get('advance_used')}")
except Exception as e:  # noqa: BLE001
    check("T61 无关抖动不算推进生效", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T62-T64 判级规约闭环：弱证据不许单独判 exhausted；两条「已知限制」必须有夹具 ----------------

# T62 弱证据（列表区里的文案，但不在容器/不在其下一个兄弟）只许报告
try:
    r = edge("weakonly", limit=100, max_pages=5)
    end = r.get("end_evidence") or {}
    ok62 = ("no_more_text_weak" in (end.get("signals") or [])      # 弱证据确实被看到了
            and end.get("end") is False                            # 但它不构成「到底」
            and r.get("stop_reason") == "stalled" and r.get("resume") is True
            and (end.get("signal_sources") or {}).get("no_more_text_weak") == "scope_text")
    check("T62 弱证据不许单独判 exhausted", ok62,
          f"signals={end.get('signals')} end={end.get('end')} stop={r.get('stop_reason')} "
          f"src={end.get('signal_sources')}")
except Exception as e:  # noqa: BLE001
    check("T62 弱证据不许单独判 exhausted", False, f"{type(e).__name__}: {str(e)[:90]}")

# T63 三行以上一条记录（A B B A B B）：不合并，但必须**响**（不许静默错位）
try:
    r = edge("abbA", limit=100, max_pages=1, advance=None)
    d = r.get("stop_detail") or {}
    merge = ((r.get("containers") or [{}])[0] or {}).get("merge") or {}
    ok63 = (merge.get("merged", 0) == 0                            # 没有硬合并
            and r.get("stop_reason") == "needs_confirm"            # 而且响了，不是静默
            and d.get("reason") in ("heterogeneous", "second_family"))
    check("T63 三行以上记录不合并但要响", ok63,
          f"merged={merge.get('merged')} stop={r.get('stop_reason')}/{d.get('reason')} "
          f"count={r.get('count')} merge={merge}")
except Exception as e:  # noqa: BLE001
    check("T63 三行以上记录不合并但要响", False, f"{type(e).__name__}: {str(e)[:90]}")

# T64 纯图形箭头的分页器：识别不到是**被钉住的已知限制**，但必须响（stalled/resume=true），不许假 exhausted
try:
    r = edge("svgnext", limit=100, max_pages=5)
    d = r.get("stop_detail") or {}
    ok64 = (r.get("stop_reason") == "stalled" and r.get("resume") is True
            and d.get("reason") == "no_advance_control"
            and (r.get("advance_used") or {}).get("pager") == "absent")
    check("T64 图形箭头分页器：识别不到但不静默", ok64,
          f"stop={r.get('stop_reason')}/{d.get('reason')} adv={r.get('advance_used')}")
except Exception as e:  # noqa: BLE001
    check("T64 图形箭头分页器：识别不到但不静默", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T65-T66 判级 agent 指定的两条证伪用例 ----------------

# T65 pushState：URL 变了但列表没重渲染 —— 旧规则（只看 navigated）会误判 repeat(resume=false)
try:
    r = edge("pushstate", limit=100, max_pages=5)
    d = r.get("stop_detail") or {}
    ok65 = (r.get("stop_reason") == "stalled" and r.get("resume") is True
            and d.get("reason") == "no_advance"
            and (r.get("advance_used") or {}).get("kind") == "click")
    check("T65 URL 变了但列表没动 ≠ 循环", ok65,
          f"stop={r.get('stop_reason')}/{d.get('reason')} resume={r.get('resume')} "
          f"count={r.get('count')} adv={r.get('advance_used')}")
except Exception as e:  # noqa: BLE001
    check("T65 URL 变了但列表没动 ≠ 循环", False, f"{type(e).__name__}: {str(e)[:90]}")

# T66 空记录组：**属于这份列表**的正向宣告才判到底。
#     第七轮政策变更（实测 S 逼出来的，见 T89）：`emptynomore` 的宣告在容器的**兄弟**里，
#     而兄弟与合法页脚在结构上不可区分 → 它只能是空组的**弱**依据（照报、不判到底）；
#     `emptynomoreinner` 的宣告在**容器自身内部** → 这才是「这份列表到底了」的正证据。
try:
    amb = edge("emptypage", limit=100, max_pages=5)
    pos = edge("emptynomoreinner", limit=100, max_pages=5)
    sib = edge("emptynomore", limit=100, max_pages=5)
    da = amb.get("stop_detail") or {}
    ds = sib.get("stop_detail") or {}
    endp = pos.get("end_evidence") or {}
    ok66 = (amb.get("count") == 5 and amb.get("stop_reason") == "stalled"
            and amb.get("resume") is True
            and da.get("reason") == "empty_group_pager_disabled_ambiguous"
            and (amb.get("end_evidence") or {}).get("end") is False      # 空组不构成终态
            and pos.get("count") == 5 and pos.get("stop_reason") == "exhausted"
            and (pos.get("stop_detail") or {}).get("reason") == "empty_group"
            and (endp.get("signal_sources") or {}).get("no_more_text") == "container"
            and "empty_group" in (endp.get("signals") or [])
            # 兄弟里的宣告：照报，但只作为弱依据 → stalled + 看得见的 ignored_declarations
            and sib.get("stop_reason") == "stalled"
            and ds.get("reason") == "empty_group_pager_disabled_ambiguous"
            and ((sib.get("end_evidence") or {}).get("ignored_declarations") or [{}])[0].get("source") == "next_sibling"
            and ((sib.get("end_evidence") or {}).get("ignored_declarations") or [{}])[0].get("why") == "out_of_container"
            # `end` 与 `stop_reason` 必须互证：不再出现「end:true 却 stalled」（第七轮评审 E）
            and (sib.get("end_evidence") or {}).get("end") is False
            and (sib.get("end_evidence") or {}).get("end_raw") is True)
    check("T66 空组要**这份列表的**正向宣告才判到底", ok66,
          f"无宣告: {amb.get('stop_reason')}/{da.get('reason')}；"
          f"有「没有更多了」: {pos.get('stop_reason')}/{(pos.get('stop_detail') or {}).get('reason')} "
          f"signals={endp.get('signals')}")
except Exception as e:  # noqa: BLE001
    check("T66 空组要页面正向宣告才判到底", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T67-T68 终态疑罪从无：空组+控件活着不许判到底；repeat 必须暴露键碰撞的二义性 ----------------

# T67 空列表但分页器还活着：只许 stalled，且 empty_group 观测与 pager 状态分开可见
try:
    r = edge("emptyalive", limit=100, max_pages=5)
    end = r.get("end_evidence") or {}
    d = r.get("stop_detail") or {}
    ok67 = (r.get("count") == 5 and r.get("stop_reason") == "stalled" and r.get("resume") is True
            and d.get("reason") == "empty_group_but_pager_live"
            and d.get("pager_at_stop") == "present"
            and "empty_group" in (end.get("signals") or [])
            and (end.get("signal_sources") or {}).get("empty_group") == "record_group"
            and end.get("end") is False)          # 观测在、但不构成终态
    check("T67 空组+控件活着：只报 stalled（终态疑罪从无）", ok67,
          f"stop={r.get('stop_reason')}/{d.get('reason')} pager={d.get('pager')} "
          f"signals={end.get('signals')} end={end.get('end')}")
except Exception as e:  # noqa: BLE001
    check("T67 空组+控件活着：只报 stalled（终态疑罪从无）", False, f"{type(e).__name__}: {str(e)[:90]}")

# T68 键碰撞 vs 真循环不可区分：repeat 必须带重建条数与二义性标记，不许静默折成「站点循环」
try:
    r = edge("keycollide", limit=100, max_pages=5, fields={"kind": "@data-kind"})
    d = r.get("stop_detail") or {}
    ok68 = (r.get("stop_reason") == "stalled" and r.get("resume") is True
            and d.get("reason") == "cross_document_cycle"    # 跨文档复阅：不谎称循环
            and d.get("possible_key_collision") is True
            and d.get("rebuilt_records", 0) >= 5 and d.get("new_keys") == 0
            and d.get("colliding_fields") == ["kind"]      # 点名撞源，调用方才好加细
            and d.get("distinct_keys") == 1
            and r["dedupe"]["dropped_duplicates"] >= 5)    # 5 条真记录被键撞掉了，看得见
    check("T68 repeat 暴露「循环 vs 键碰撞」并点名撞源", ok68,
          f"stop={r.get('stop_reason')} rebuilt={d.get('rebuilt_records')} "
          f"colliding={d.get('colliding_fields')} distinct_keys={d.get('distinct_keys')} "
          f"dropped={r['dedupe']['dropped_duplicates']}")
except Exception as e:  # noqa: BLE001
    check("T68 repeat 暴露「循环 vs 键碰撞」的二义性", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T69-T74 第二轮对抗性评审（S1/S2/M1/M2/M3/A3） ----------------

# T69 空壳 + 分页器「加载中临时禁用」不许被当成到底（同一页面，只改 wait_ms）
try:
    slow = edge("asyncfill", limit=100, max_pages=5, wait_ms=300)
    fast = edge("asyncfill", limit=100, max_pages=5, wait_ms=3000)
    d = slow.get("stop_detail") or {}
    ok69 = (slow.get("stop_reason") == "stalled" and slow.get("resume") is True
            and (slow.get("count") or 0) >= 5                 # 拿到多少取决于时序，但不许「宣告到底」
            and (slow.get("end_evidence") or {}).get("end") is False   # 关键：没有终态主张
            and d.get("reason") in ("empty_group_once", "empty_group_loading",
                                    "empty_group_pager_disabled_ambiguous",
                                    "pager_disabled_no_declaration", "no_advance_control")
            and fast.get("count") == 15)                     # 正例不能一起坏
    check("T69 加载中的空壳不许判到底", ok69,
          f"wait300: {slow.get('stop_reason')}/{d.get('reason')} count={slow.get('count')} "
          f"seen={d.get('consecutive_empty_probes')}；wait3000 count={fast.get('count')}")
except Exception as e:  # noqa: BLE001
    check("T69 加载中的空壳不许判到底", False, f"{type(e).__name__}: {str(e)[:90]}")

# T70 结构相同、类名不同、各自连续的两块（5 推广 + 5 结果）必须被 sub_families 兜住并提问
try:
    r = edge("tie2", limit=20, max_pages=1, advance=None)
    d = r.get("stop_detail") or {}
    cont = (r.get("containers") or [{}])[0]
    ok70 = (r.get("stop_reason") == "needs_confirm" and d.get("reason") == "second_family"
            and (d.get("sub_families") or [])[:2] == [5, 5]
            and cont.get("sub_families") == [5, 5])
    check("T70 同签名桶里的等大两族要提问", ok70,
          f"stop={r.get('stop_reason')}/{d.get('reason')} sub={d.get('sub_families')} "
          f"family_sizes={cont.get('family_sizes')}")
except Exception as e:  # noqa: BLE001
    check("T70 同签名桶里的等大两族要提问", False, f"{type(e).__name__}: {str(e)[:90]}")

# T71 跨文档「内容一样」不可区分：按终态疑罪从无落 stalled 并交出证据（不再谎称循环）
try:
    r = edge("cycle_placeholder", limit=100, max_pages=5) if False else None
    s_ = open_session(f"{LIST}?mode=cycle")
    try:
        r = act_in_session(s_["session_id"], {"action": "collect", "limit": 100, "max_pages": 5,
                                              "fields": COLLECT_FIELDS})
    finally:
        close_session(s_["session_id"])
    d = r.get("stop_detail") or {}
    ok71 = (r.get("stop_reason") == "stalled" and r.get("resume") is True
            and d.get("reason") == "cross_document_cycle"
            and d.get("cross_document") is True and len(d.get("pages") or []) >= 2)
    check("T71 跨文档复阅落 stalled（不谎称循环）", ok71,
          f"stop={r.get('stop_reason')}/{d.get('reason')} cross_doc={d.get('cross_document')} "
          f"pages={len(d.get('pages') or [])}")
except Exception as e:  # noqa: BLE001
    check("T71 跨文档复阅落 stalled（不谎称循环）", False, f"{type(e).__name__}: {str(e)[:90]}")

# T72 同文档重渲染 + 零新键 = 循环的**真**签名 → repeat
try:
    r = edge("ajcycle", limit=100, max_pages=5)
    d = r.get("stop_detail") or {}
    ok72 = (r.get("stop_reason") == "repeat" and r.get("resume") is False
            and d.get("reason") == "cycle" and d.get("cross_document") is False)
    check("T72 同文档循环才判 repeat", ok72,
          f"stop={r.get('stop_reason')}/{d.get('reason')} cross_doc={d.get('cross_document')}")
except Exception as e:  # noqa: BLE001
    check("T72 同文档循环才判 repeat", False, f"{type(e).__name__}: {str(e)[:90]}")

# T73 日期区间 + 无关「共 N」不许写进 total/total_source
#     第十四轮起这条夹具的语义更准了：页面上**同时**有一句不自洽的日期区间（weak）与一个
#     **真的**分页计数器「1-5 of 15」。旧实现只看文档序第一个匹配 → 被日期文案遮蔽，
#     于是 `total=None`（T73 当时就是在断言这个**漏读**）。现在两个候选都在：
#     真计数器给出 `total=15`，日期文案仍只作弱证据（`range_prose` + `total_claimed=3`）。
try:
    r = edge("scopedate", limit=100, max_pages=5)
    end = r.get("end_evidence") or {}
    cands = end.get("range_candidates") or []
    ok73 = (r.get("count") == 15                              # 别被假 total 截断
            and end.get("total") == 15                        # 真计数器（1-5 of 15）在场
            and end.get("total_source") == "range"
            # 日期文案仍然**可见且被标为不自洽**（现在走 `range_candidates` 这条更结构化的路，
            # 而不是旧的 `range_prose` 散文信号：候选表里 `sane:false` 就是它）
            and any(c.get("tot") == 3 and c.get("sane") is False and not c.get("chosen") for c in cands)
            and end.get("total_claimed") == 3)                # 「共 3 个批次」如实上报
    check("T73 日期区间不许当区间计数器", ok73,
          f"count={r.get('count')} total={end.get('total')} source={end.get('total_source')} "
          f"signals={end.get('signals')} claimed={end.get('total_claimed')} "
          f"cands={[(c.get('tot'), c.get('chosen')) for c in cands]}")
except Exception as e:  # noqa: BLE001
    check("T73 日期区间不许当区间计数器", False, f"{type(e).__name__}: {str(e)[:90]}")

# T74 lead_siblings 必须真的存在（README 曾写了它，代码里没有 = 说了没做），且标题要能被发现
try:
    r1 = edge("lead", limit=50, max_pages=1, advance=None)
    r2 = edge("leadlink", limit=20, max_pages=1, advance=None)
    c1 = (r1.get("containers") or [{}])[0]
    c2 = (r2.get("containers") or [{}])[0]
    ok74 = (c1.get("lead_siblings") == 1                       # lead 夹具切掉了 1 个标题
            and r2.get("stop_reason") == "needs_confirm")       # 同签名的标题行至少会「响」
    check("T74 lead_siblings 存在且标题行会响", ok74,
          f"lead.lead_siblings={c1.get('lead_siblings')}；leadlink stop={r2.get('stop_reason')} "
          f"matched={c2.get('matched')} sub={c2.get('sub_families')}")
except Exception as e:  # noqa: BLE001
    check("T74 lead_siblings 存在且标题行会响", False, f"{type(e).__name__}: {str(e)[:90]}")

# T75 连续多次慢加载：连续空计数必须归零（否则「再确认一次」整次调用只能用一次）
try:
    slow = edge("slowtwice", limit=100, max_pages=8, wait_ms=300)
    fast = edge("slowtwice", limit=100, max_pages=8, wait_ms=2500)
    d = slow.get("stop_detail") or {}
    ok75 = (slow.get("count") == 25          # 5 页每页慢加载 1s，wait_ms=300 也必须全拿到
            and slow.get("resume") is True    # 无正向宣告 → 不发终态（可重试，代价便宜）
            and d.get("reason") != "exhausted"
            and fast.get("count") == 25)      # 正例不能坏
    check("T75 连续多次慢加载都能恢复", ok75,
          f"wait300: count={slow.get('count')} pages={slow.get('pages')} "
          f"stop={slow.get('stop_reason')}/{d.get('reason')}；wait2500 count={fast.get('count')}")
except Exception as e:  # noqa: BLE001
    check("T75 连续多次慢加载都能恢复", False, f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T76-T78 第四轮：容器提示跨文档 + settle 预算（夹具 test_site/decoy.html） ----------------
# 这一条修的是**我自己引入的 S**：容器提示只带「末段选择器」时，跨文档后会命中页面更前面的
# **同选择器诱饵**，而诱饵紧挨着一句「没有更多了」→ 假 exhausted（同一 URL 只改 wait_ms：
# 5 条 / 15 条）。修法是结构路径（body 起、逐层下钻）+ 选择器必须全文档唯一。
DECOY = f"{BASE}/decoy.html"


def decoy(**act):
    s_ = open_session(DECOY)
    try:
        return act_in_session(s_["session_id"],
                              {"action": "collect", "fields": COLLECT_FIELDS, **act})
    finally:
        close_session(s_["session_id"])


# T76 跨文档同选择器诱饵：默认预算下只许 stalled（可重试），不许把诱饵旁边的文案当成「到底」
try:
    short = decoy(max_pages=5, wait_ms=300)                 # 第 2 页真容器 3s 后才填充
    d = short.get("stop_detail") or {}
    cont = (short.get("containers") or [{}])[0]
    settled = decoy(max_pages=5, wait_ms=300, settle_ms=4500)   # 给足预算：一趟到底
    hrefs = [x.get("href") or "" for x in settled.get("records") or []]
    ok76 = (short.get("count") == 5 and short.get("stop_reason") == "stalled"
            and short.get("resume") is True
            and (short.get("end_evidence") or {}).get("end") is False   # 关键：没有终态主张
            and d.get("reason") == "empty_group_pager_disabled_ambiguous"
            and (cont.get("hint_path") or "").startswith("body>")       # 提示是结构路径，不是裸选择器
            and settled.get("count") == 15                              # 15 条真的都在
            and [h.rsplit("=", 1)[-1] for h in hrefs] == [str(i) for i in range(15)])
    check("T76 跨文档同选择器诱饵不许假 exhausted", ok76,
          f"短预算: {short.get('stop_reason')}/{d.get('reason')} count={short.get('count')} "
          f"end={((short.get('end_evidence') or {}).get('end'))} hint_path={cont.get('hint_path')}；"
          f"settle=4500 count={settled.get('count')}")
except Exception as e:  # noqa: BLE001
    check("T76 跨文档同选择器诱饵不许假 exhausted", False, f"{type(e).__name__}: {str(e)[:90]}")

# T77 同一文档内也有同选择器诱饵（真容器永远空 + 无宣告 + 分页器消失）：
#     不许因为「分页器没了」就判到底——终态必须由页面的正向宣告决定
try:
    r = edge("decoysel", limit=100, max_pages=5, wait_ms=300)
    d = r.get("stop_detail") or {}
    ok77 = (r.get("count") == 5 and r.get("stop_reason") == "stalled" and r.get("resume") is True
            and d.get("reason") == "empty_group_pager_disabled_ambiguous" and d.get("pager_at_stop") == "absent")
    # 注：空组详情里的键也统一叫 `pager_at_stop`（第十二轮评审要求这一格在**每条**停止详情里都有）

    check("T77 同文档诱饵 + 分页器消失也不判到底", ok77,
          f"count={r.get('count')} stop={r.get('stop_reason')}/{d.get('reason')} pager={d.get('pager')}")
except Exception as e:  # noqa: BLE001
    check("T77 同文档诱饵 + 分页器消失也不判到底", False, f"{type(e).__name__}: {str(e)[:90]}")

# T78 settle 是**可调预算**而不是固定睡眠：settle_used 报出「预算 vs 墙钟」，
#     墙钟 < 预算 就证明一见数据就停（把 1.2s 睡死会把 3s 才渲染的站点白白多跑一轮）
try:
    slow = edge("slowtwice", limit=100, max_pages=8, wait_ms=300, settle_ms=3000)
    used = slow.get("settle_used") or {}
    ok78 = (slow.get("count") == 25 and used.get("budget_ms") == 3000
            and 0 < (used.get("elapsed_ms") or 0) < 3000 and used.get("settled") is True
            and (used.get("polls") or 0) >= 1)
    check("T78 settle_ms 是预算而非固定睡眠", ok78,
          f"count={slow.get('count')} settle_used={used}")
except Exception as e:  # noqa: BLE001
    check("T78 settle_ms 是预算而非固定睡眠", False, f"{type(e).__name__}: {str(e)[:90]}")

# T79 作用域退化成整页时，页面级的区间计数器只许当弱证据（不许「别的组件的账」判这份列表到底）
#     顺带钉住：空组判决交出的 end_evidence 必须是**做判决那次探针**的证据
try:
    r = edge("pagewiderange", limit=100, max_pages=5, wait_ms=300)
    end = r.get("end_evidence") or {}
    d = r.get("stop_detail") or {}
    ok79 = (r.get("count") == 5 and r.get("stop_reason") == "stalled" and r.get("resume") is True
            and "range_prose" in (end.get("signals") or [])
            and "range_reached" not in (end.get("signals") or [])
            and (end.get("scope") or {}).get("kind") == "body"      # 第 2 页结构全换 → 作用域退化成整页
            and (end.get("scope") or {}).get("path") is None        # 不是上一页那份证据
            and end.get("total") is None                            # 别人的 15 不许写进 total
            and end.get("total_claimed") == 15                      # 但要如实报告「页面上是这么说的」
            and d.get("reason") == "empty_group_pager_disabled_ambiguous")
    check("T79 整页作用域的区间只当弱证据", ok79,
          f"count={r.get('count')} stop={r.get('stop_reason')}/{d.get('reason')} "
          f"signals={end.get('signals')} scope={end.get('scope')} total={end.get('total')} "
          f"claimed={end.get('total_claimed')}")
except Exception as e:  # noqa: BLE001
    check("T79 整页作用域的区间只当弱证据", False, f"{type(e).__name__}: {str(e)[:90]}")

# T80 同文档重建 + 零新键，但**去重键取粗了** → 不许替调用方宣称「站点在循环喂同一页」
#     （跨文档分支早就是这么判的，同文档分支曾经直接 repeat = 终态 + resume=false）
try:
    r = edge("keycollide_spa", limit=100, max_pages=5, fields={"kind": "@data-kind"})
    d = r.get("stop_detail") or {}
    ok80 = (r.get("stop_reason") == "stalled" and r.get("resume") is True
            and d.get("reason") == "cycle_key_collision"
            and d.get("possible_key_collision") is True
            and d.get("colliding_fields") == ["kind"]
            and d.get("cross_document") is False           # 确实是同文档
            and d.get("rebuilt_records", 0) >= 5           # 组真的被重建了
            and r["dedupe"]["dropped_duplicates"] >= 5)
    check("T80 同文档键碰撞不许判 repeat", ok80,
          f"stop={r.get('stop_reason')}/{d.get('reason')} rebuilt={d.get('rebuilt_records')} "
          f"colliding={d.get('colliding_fields')} cross_doc={d.get('cross_document')}")
except Exception as e:  # noqa: BLE001
    check("T80 同文档键碰撞不许判 repeat", False, f"{type(e).__name__}: {str(e)[:90]}")

# T81 `no_group_from_anchor` 与 auto 必须共用同一个空组分诊：
#     锚点还在、兄弟全没了 → 有正向宣告才 exhausted，没有就只报可重试的 stalled
try:
    s_ = open_session(f"{EDGE}?mode=anchororphan")
    try:
        probe = act_in_session(s_["session_id"], {"action": "collect", "limit": 1,
                                                  "max_pages": 1, "advance": None})
        anchor = ((probe.get("records") or [{}])[0] or {}).get("_src", {}).get("block")
        got = act_in_session(s_["session_id"], {"action": "collect", "limit": 100, "max_pages": 3,
                                                "from": anchor, "wait_ms": 300})
    finally:
        close_session(s_["session_id"])
    d_nomore = edge("anchororphan", q="&nomore=1", limit=100, max_pages=3, wait_ms=300)
    dn = d_nomore.get("stop_detail") or {}
    dg = got.get("stop_detail") or {}
    ok81 = (anchor and got.get("count") == 5
            and got.get("stop_reason") == "stalled" and got.get("resume") is True
            and dg.get("reason") == "empty_group_pager_disabled_ambiguous"
            and d_nomore.get("stop_reason") == "exhausted"      # 有宣告 → 才算到底
            and dn.get("reason") == "empty_group"
            and (d_nomore.get("end_evidence") or {}).get("end") is True)
    check("T81 锚点组解散走同一套空组分诊", ok81,
          f"anchor={anchor} 无宣告: {got.get('stop_reason')}/{dg.get('reason')}；"
          f"有宣告: {d_nomore.get('stop_reason')}/{dn.get('reason')} count={got.get('count')}")
except Exception as e:  # noqa: BLE001
    check("T81 锚点组解散走同一套空组分诊", False, f"{type(e).__name__}: {str(e)[:90]}")

# T82 打包/元数据文件必须在**测试面**里：判级 agent 指出 89 项全绿也覆盖不到
#     pyproject.toml / __init__.py（没人 import 它们），而它们真的被写坏过并提交。
#     所以编码体检不是旁路脚本，是回归的一部分：断言它的退出码是 0。
#     ⚠️ 第五轮评审实测：**必须**用非 UTF-8 的 stdout 跑一次（中文 Windows 默认 GBK），
#     否则「体检工具自己在 GBK 控制台上抛 UnicodeEncodeError → 退出码 1」这个 bug
#     在中国区机器上永远重现不出来（我原来正是被这个环境差异骗过：本地设了
#     PYTHONIOENCODING=utf-8，于是红的时候只看到 `rc=1` 而看不到原因）。
try:
    import subprocess

    env_gbk = dict(os.environ, PYTHONIOENCODING="gbk")
    proc = subprocess.run([sys.executable, str(HERE / "check_encoding.py")],
                          capture_output=True, text=True, encoding="gbk", errors="replace",
                          cwd=str(HERE), env=env_gbk)
    tail = ((proc.stderr or "").strip().splitlines() or [""])[-1]
    ok82 = proc.returncode == 0 and "[OK]" in (proc.stdout or "")
    check("T82 编码/元数据体检接进回归（GBK 控制台下退出码 0）", ok82,
          f"rc={proc.returncode} stdout={(proc.stdout or '').strip()[-90:]!r} stderr末行={tail[:90]!r}")
except Exception as e:  # noqa: BLE001
    check("T82 编码/元数据体检接进回归（GBK 控制台下退出码 0）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T110 属性隐患自检（上游报告里 `self.opened_id` 那一类）
#     形状：`self.X` 在 A 路径被裸读、只在 B 路径赋值 ⇒ 库消费者必崩、CLI 恰好不崩。
#     人工看不出来（CLI 路径永远绿），所以要机器扫。判据见 check_attrs.py（三条，缺一就误报）。
#     ⚠️ 加检查必须先见它红一次：本项在实现时对「撤掉两处赋值」的版本报了两条红
#     （`_expand_groups` 与 `send` 各一处），恢复后转绿——见 r111 提交消息。
try:
    import subprocess as _sp

    proc110 = _sp.run([sys.executable, str(HERE / "check_attrs.py"),
                       str(HERE / "skills" / "agent-chat" / "agent_chat.py")],
                      capture_output=True, text=True, encoding="utf-8", errors="replace",
                      cwd=str(HERE))
    ok110 = proc110.returncode == 0 and "[OK]" in (proc110.stdout or "")
    check("T110 技能属性隐患自检（裸读未初始化属性 ⇒ 必崩）", ok110,
          f"rc={proc110.returncode} out={(proc110.stdout or '').strip()[-120:]!r}")
except Exception as e:  # noqa: BLE001
    check("T110 技能属性隐患自检（裸读未初始化属性 ⇒ 必崩）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T83 证据必须带**匹配原文**：只看枚举值时，agent 发现不了「没有更多**评论**」讲的是另一张列表
#     （判级 agent 的 ask：这是把「scope 假 exhausted」从 S 压到 A 的那一行）
try:
    nm = edge("emptynomore", limit=100, max_pages=5, wait_ms=300)
    tx = (nm.get("end_evidence") or {}).get("text") or {}
    pw = edge("pagewiderange", limit=100, max_pages=5, wait_ms=300)
    tx2 = (pw.get("end_evidence") or {}).get("text") or {}
    ok83 = ("没有更多" in (tx.get("snippet") or "") and tx.get("source") == "next_sibling"
            and "1-5 of 15" in (tx2.get("snippet") or "") and tx2.get("source") == "body_text")
    check("T83 结束证据带匹配原文（可发现「讲的是另一张列表」）", ok83,
          f"有宣告: source={tx.get('source')} snippet={tx.get('snippet')!r}；"
          f"整页区间: source={tx2.get('source')} snippet={tx2.get('snippet')!r}")
except Exception as e:  # noqa: BLE001
    check("T83 结束证据带匹配原文（可发现「讲的是另一张列表」）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T84 容器要带**内容签名**，让调用方能对账「我定位到的是不是我那张列表」
#     （判级 agent 的 ask：结构路径只证明「位置对」，证明不了「语义对」——同 id/class 不同语义
#      仍然会匹配，那是身份**错配**，比身份丢失更危险。签名是把它从 S 降到 A 的那一行。）
try:
    p1 = decoy(max_pages=1, advance=None)
    c1 = (p1.get("containers") or [{}])[0]
    full = decoy(max_pages=5, wait_ms=300, settle_ms=4500)
    c2 = (full.get("containers") or [{}])[0]
    ok84 = (c1.get("signature") and c1.get("first_child") == "li"
            and c2.get("signature") == c1.get("signature")     # 跨页语义一致 → 定位可信
            and c2.get("first_child") == c1.get("first_child")
            and (full.get("record_shape") or {}).get("signature") == c2.get("signature"))
    check("T84 容器带内容签名（可对账定位是否错配）", ok84,
          f"p1: sig={c1.get('signature')} first={c1.get('first_child')}；"
          f"末页: sig={c2.get('signature')} first={c2.get('first_child')}")
except Exception as e:  # noqa: BLE001
    check("T84 容器带内容签名（可对账定位是否错配）", False, f"{type(e).__name__}: {str(e)[:90]}")

# T85 settle_used 的账要对得上**墙钟**，且 settled 必须机器可读
#     第五轮评审实测：只累加 wait_for_timeout 的 chunk 会少报 1.6–2.5 秒（轮询里的
#     `_evaluate(COLLECT_JS)` 自己也要几十到上百毫秒）。
#     ⚠️ 判级 agent 提醒：上界**不能**钉成「超预算 < 300ms」——那是机器负载相关的（快机上
#     实测只超 254ms，慢机上必然超 300 → flaky）。所以真正的判据是**语义**：
#     撞预算顶 → `settled=false` 且 `elapsed_ms > budget_ms`（严格大于，因为探针不可能免费；
#     旧的「只算 sleep」实现恰好等于 budget，会在这里露馅）；早停 → `settled=true` 且
#     `elapsed < budget`。墙钟只做一条**方向性**兜底：elapsed 不可能**超过**实测墙钟
#     （超了就是重复计数）——这条与机器负载无关，慢机上照样成立。
try:
    s_ = open_session(f"{EDGE}?mode=emptypage")
    try:
        t0 = time.time()
        floor = act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                  "limit": 100, "max_pages": 5, "wait_ms": 300,
                                                  "settle_ms": 4000})
        wall_ms = int((time.time() - t0) * 1000)
    finally:
        close_session(s_["session_id"])
    u = floor.get("settle_used") or {}
    unaccounted = wall_ms - (u.get("elapsed_ms") or 0)
    early = edge("slowtwice", limit=100, max_pages=8, wait_ms=300, settle_ms=3000)
    u3 = early.get("settle_used") or {}
    ok85 = (u.get("settled") is False and (u.get("polls") or 0) >= 1
            and (u.get("elapsed_ms") or 0) > (u.get("budget_ms") or 0)     # 严格大于 = 探针成本被记账
            and unaccounted > -50                                           # 不许超过实测墙钟
            and u3.get("settled") is True and (u3.get("elapsed_ms") or 0) < u3.get("budget_ms"))
    check("T85 settle 的账把探针成本算进去（settled 可读）", ok85,
          f"压预算顶: {u}（act 墙钟 {wall_ms}ms，未记账 {unaccounted}ms）；早停: {u3}")
except Exception as e:  # noqa: BLE001
    check("T85 settle 的账把探针成本算进去（settled 可读）", False, f"{type(e).__name__}: {str(e)[:90]}")

# T86 证据必须说清「这次判决用的是**谁的容器**」（第五轮评审最想要而拿不到的字段：
#     没有它，审核者只能靠造夹具反推提示解析结果）。scope.source 五档 + span 让域的大小可见
try:
    cur = edge("bogus", limit=100, max_pages=5)                     # 本趟自己分组
    hint = edge("emptynomore", limit=100, max_pages=5, wait_ms=300)  # 本页无组，用上一页提示
    none = edge("pagewiderange", limit=100, max_pages=5, wait_ms=300)  # 提示解析不到 → 整页
    sc, sh, sn = [((x.get("end_evidence") or {}).get("scope") or {}) for x in (cur, hint, none)]
    ok86 = (sc.get("source") == "current_container" and (sc.get("span") or 0) > 0
            and sc.get("kind") == "container_pager_lca"      # 名字只承诺「这是谁的 LCA」
            and sh.get("source") == "hint_path" and (sh.get("span") or 0) > 0
            and sn.get("source") == "none" and sn.get("kind") == "body" and (sn.get("span") or 0) > 0)
    check("T86 scope.source 说明判决用的是谁的容器", ok86,
          f"current={sc}；hint={sh}；none={sn}")
except Exception as e:  # noqa: BLE001
    check("T86 scope.source 说明判决用的是谁的容器", False, f"{type(e).__name__}: {str(e)[:90]}")

# T87 「没有更多」必须属于**这份列表**：容器的下一个兄弟常是另一张列表（评论区/推荐位）
#     第六轮评审 S：旧实现把「容器文本 + 下一个兄弟文本」拼接后匹配，于是一句
#     「没有更多**评论**了」把结果列表宣告终结（end:true + exhausted + resume:false）。
#     修法：两个元素分开匹配 + 兄弟来源过「像列表页脚」的结构闸（无标题/元素少/文案短），
#     没过闸的仍上报但降级为弱信号 `no_more_text_sibling`，并用 text.element 指出是谁说的。
try:
    s_ = open_session(f"{BASE}/r6comment.html")            # 4 页 × 5 = 20 条，分页器一直可用
    try:
        r = act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                              "limit": 100, "max_pages": 6, "wait_ms": 300})
    finally:
        close_session(s_["session_id"])
    end = r.get("end_evidence") or {}
    sigs = end.get("signals") or []
    tx = end.get("text") or {}
    ok87 = (r.get("count") == 20 and r.get("pages") == 4
            and "no_more_text" not in sigs                       # 别人的账不许当自己的终态证据
            and end.get("end") is False
            and r.get("stop_reason") == "stalled" and r.get("resume") is True
            and "no_more_text_sibling" in sigs                   # 但仍如实上报，且能看出是谁说的
            and tx.get("element") == "p#cmsg"                     # 命中所在元素（不是它的包裹 div）
            and "评论" in (tx.get("snippet") or ""))
    check("T87 「没有更多评论了」不许终结结果列表", ok87,
          f"count={r.get('count')} pages={r.get('pages')} stop={r.get('stop_reason')} "
          f"signals={sigs} element={tx.get('element')} snippet={tx.get('snippet')!r}")
except Exception as e:  # noqa: BLE001
    check("T87 「没有更多评论了」不许终结结果列表", False, f"{type(e).__name__}: {str(e)[:90]}")

# T88 结构闸不能把**真的**列表页脚也挡掉：一句短的、没有标题的「没有更多了」仍是强**观测**
#     （`no_more_text` 照报、`text.element` 指出是谁说的）。但第七轮起，**兄弟来源**的宣告
#     不再单独判空组到底（下面 T89 是它为什么必须这样的实测），真要判到底得把这句话放进容器里。
try:
    ok_sib = edge("emptynomore", limit=100, max_pages=5, wait_ms=300)
    end2 = ok_sib.get("end_evidence") or {}
    tx2 = end2.get("text") or {}
    ok88 = ((((ok_sib.get("end_evidence") or {}).get("ignored_declarations") or [{}])[0]).get("source") == "next_sibling"
            and "no_more_text" in (end2.get("signals") or [])
            and tx2.get("source") == "next_sibling" and tx2.get("element") == "p.nomore"
            and "没有更多" in (tx2.get("snippet") or ""))
    check("T88 兄弟页脚照报（结构闸不误伤观测）", ok88,
          f"signals={end2.get('signals')} element={tx2.get('element')} "
          f"snippet={tx2.get('snippet')!r} ignored={end2.get('ignored_declarations')}")
except Exception as e:  # noqa: BLE001
    check("T88 兄弟页脚照报（结构闸不误伤观测）", False, f"{type(e).__name__}: {str(e)[:90]}")

# T89 **第七轮实测 S 的回归**：裸而短的兄弟（结构上像页脚、语义上属于另一张列表）
#     + 这一页还在慢加载（分页器临时禁用）→ 旧实现把 15 条说成 5 条且 resume=false（静默丢数据）。
#     现在：不进终态（stalled + ignored_declaration），而且**立刻续跑不许抛错**、稍后续跑要把
#     剩下的 10 条全拿到（游标原样带回）。
try:
    s_ = open_session(f"{BASE}/r6commentbare.html")
    try:
        a = act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                              "limit": 100, "max_pages": 5, "wait_ms": 300})
        da = a.get("stop_detail") or {}
        b = act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                              "limit": 100, "max_pages": 5, "wait_ms": 300,
                                              "next": a.get("next")})
        time.sleep(3.2)                                    # 等站点把这一页渲染出来
        c = act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                              "limit": 100, "max_pages": 5, "wait_ms": 3000,
                                              "next": b.get("next") or a.get("next")})
    finally:
        close_session(s_["session_id"])
    total = (a.get("count") or 0) + (b.get("count") or 0) + (c.get("count") or 0)
    ign_a = (((a.get("end_evidence") or {}).get("ignored_declarations") or [{}])[0])
    ok89 = (a.get("count") == 5 and a.get("stop_reason") == "stalled" and a.get("resume") is True
            and ign_a.get("source") == "next_sibling"
            and b.get("stop_reason") == "stalled" and b.get("next")     # 续跑不抛、游标不丢
            and total == 15)                                            # 一条都不许丢
    check("T89 裸兄弟的假宣告不许丢数据（续跑拿满 15 条）", ok89,
          f"首趟={a.get('count')}/{a.get('stop_reason')} ignored={ign_a.get('source')}；"
          f"立刻续跑={b.get('count')}/{b.get('stop_reason')} 游标={bool(b.get('next'))}；"
          f"稍后续跑={c.get('count')}/{c.get('stop_reason')}；合计={total}")
except Exception as e:  # noqa: BLE001
    check("T89 裸兄弟的假宣告不许丢数据（续跑拿满 15 条）", False, f"{type(e).__name__}: {str(e)[:90]}")

# T90 记录组非空 + 分页器禁用 + 裸兄弟宣告：旧实现直接 exhausted（我怀疑同样能丢数据）。
#     实测（r6commentbare.html?partial=1）：这条路径**没有**复现丢数据（短预算落到
#     empty_group_pager_disabled_ambiguous，长预算拿满 15 条后才是 exhausted）——
#     所以按「未复现 = 待实测」不动判据，只把结果钉住，避免以后有人说「这条没测过」。
try:
    import subprocess as _sp  # noqa: F401  （仅为与本文件其它测试一致的导入风格）

    one = edge("slowtwice", limit=100, max_pages=6, wait_ms=300)
    ok90 = one.get("count") == 25          # 有记录的真页脚：仍以终态收尾（没有被上一轮的政策误伤）
    check("T90 有记录路径未被空组政策误伤", ok90,
          f"slowtwice count={one.get('count')} stop={one.get('stop_reason')}/"
          f"{(one.get('stop_detail') or {}).get('reason')}")
except Exception as e:  # noqa: BLE001
    check("T90 有记录路径未被空组政策误伤", False, f"{type(e).__name__}: {str(e)[:90]}")

# T91 **第七轮实测 S 的回归**：作用域级的区间计数器不许终结空列表
#     评审 C：`range_reached` 的来源是 scope = lca(容器, 分页器)（常常是整个 main），
#     于是**任何**一块无关内容写的自洽计数器都成了「这份列表到底了」——
#     `r7range.html`（另一块内容写「本期榜单 1-5 共 5」+ 结果列表 20 条 + 第 2 页慢加载）
#     旧实现：5 条 + exhausted + resume=false。现在：`end=false`、`range_reached` 进
#     `ignored_declarations`、stalled（可续跑），给足预算后拿满 20 条。try:
    s_ = open_session(f"{BASE}/r7range.html")
    try:
        a = act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                             "limit": 100, "max_pages": 8, "wait_ms": 300})
        b = act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                             "limit": 100, "max_pages": 8, "wait_ms": 300,
                                             "settle_ms": 5000, "next": a.get("next")})
    finally:
        close_session(s_["session_id"])
    ea = a.get("end_evidence") or {}
    ign = ea.get("ignored_declarations") or []
    ok91 = (a.get("count") == 5 and a.get("stop_reason") == "stalled" and a.get("resume") is True
            and ea.get("end") is False and ea.get("total") is None       # 照报，但不认、不写进 total
            and any(x.get("signal") == "range_reached" and x.get("source") == "scope_text"
                    for x in ign)
            and b.get("count") == 15 and (a.get("count") or 0) + (b.get("count") or 0) == 20)
    check("T91 无关的区间计数器不许终结空列表", ok91,
          f"默认预算: count={a.get('count')} {a.get('stop_reason')} end={ea.get('end')} "
          f"end_raw={ea.get('end_raw')} ignored={ign}；续跑 count={b.get('count')}")
except Exception as e:  # noqa: BLE001
    check("T91 无关的区间计数器不许终结空列表", False, f"{type(e).__name__}: {str(e)[:90]}")

# T92 不变量（第八轮评审把它加严）：对每个夹具断言
#     ① `end` 与 `stop_reason` 互证：非终态（stalled/repeat/…）必为 False、exhausted 必为 True；
#        `end=True` 只允许出现在「已发终态」或「调用方自己的上限先到了」（limit/max_pages/user_none）
#     ② `owned_signals` 非空 ⟺ `end is True`（没有「有主却没用」或「用了却无主」的信号）
#     夹具列表按评审要求扩容：把 r7range / r8setsize 两个实测 S 的夹具放进来——
#     它们**用完就留下**，以后谁再拆掉一条判据，这里会立刻红。
try:
    cases = ["collect_edge.html?mode=emptypage", "collect_edge.html?mode=emptynomore",
             "collect_edge.html?mode=emptynomoreinner", "collect_edge.html?mode=anchororphan",
             "collect_edge.html?mode=anchororphan&nomore=1", "collect_edge.html?mode=bogus",
             "collect_edge.html?mode=setsizeonly", "collect_edge.html?mode=pagewiderange",
             "collect_edge.html?mode=slowtwice", "r7range.html", "r7range.html?fast=1&dead=1",
             "r8setsize.html", "r9fake2.html?per=15&decl=15&disabled=1",
             "r10svg.html?pages=3", "r10svg.html?pages=1",
             "r11text.html?pages=3", "r11text.html?pages=1",
             "r14decoy.html?disabled=0", "r14decoy.html?disabled=1",
             "r15two.html?disabled=1", "r15two.html?disabled=0",
             # 第十六轮：真计数器与假进度**互相矛盾**（分歧档）与**跟着已收条数走**（反例档）
             "r16fake.html?pages=3&fake=50", "r16fake.html?pages=3&fake=15",
             "r16fake.html?pages=3&decoy=run&dis=2", "r16fake.html?pages=3&decoy=run",
             # 第十七/十八轮：跨页声明不稳定（撤销声明）与追加式站点（B 成本档）
             "r16fake.html?pages=3&decoy=run&dis=2&real=0", "r18append.html",
             "r18append.html?dis=3",
             # 第十九轮：对抗评审交接的四个夹具（跨页规则的原始证据）
             "r18stab2.html", "r18grow.html", "r18two.html?site=grow", "r18uneven.html",
             # `repeat(cycle)` 也是终态，但它的证据是「重渲染 + 零新键」，**不产生 owned 声明** ——
             # 判级 agent 要求把这条钉住：`end` 只指「有主强宣告」，不该被 cycle 误杀
             # （实测 ajcycle：`repeat/cycle` + `end=false` + `owned=[]`）。
             "collect_edge.html?mode=ajcycle"]
    bad = []
    for path in cases:
        s_ = open_session(f"{BASE}/{path}")
        try:
            r = act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                  "limit": 100, "max_pages": 5, "wait_ms": 300})
        finally:
            close_session(s_["session_id"])
        e = r.get("end_evidence") or {}
        end, owned, sr = e.get("end"), (e.get("owned_signals") or []), r.get("stop_reason")
        # ① `end=True` 必须有据（owned 非空）；反向不再成立：owned 里的信号仍可能被**值闸**挡下
        #    （fits_page_yet_pager_present / inconsistent_with_collected / window_mismatch 都会把它摘掉并点名），
        #    那时 `end` 也必须是 False——正是这条保证了 ② 不会被绕过。
        if end is True and not owned:
            bad.append(f"{path}:end=true but owned=[]")
        if sr == "exhausted" and end is not True:
            bad.append(f"{path}:exhausted/end={end}")
        # 第十轮评审的要求：`exhausted` 时，被用作终态的声明必须能在 owned_signals 里找到
        # （挡住「owned 被摘空却仍然 exhausted」这种未来形态）
        if sr == "exhausted" and not owned:
            bad.append(f"{path}:exhausted 但 owned_signals 为空")
        if sr in ("stalled", "repeat", "wall", "blocked", "timeout", "error", "needs_confirm") and end is True:
            bad.append(f"{path}:{sr}/end={end}")
        # `repeat` 是终态但**不产生 owned 声明**（证据是「重渲染 + 零新键」）：两者都必须为空，
        # 否则就是拿「宣告」去解释一个与宣告无关的终态（判级 agent 的裁定）
        if sr == "repeat" and (end is True or owned):
            bad.append(f"{path}:repeat 却 end={end}/owned={owned}")
        if end is True and sr not in ("exhausted", "limit", "max_pages", "user_none"):
            bad.append(f"{path}:end=true but stop={sr}")
        # ② 每个「见过强信号但没认」的探头都要在 ignored_declarations 里点名（可审计）
        if e.get("end_raw") and end is not True and not (e.get("ignored_declarations") or []):
            bad.append(f"{path}:end_raw=true/end=false 但 ignored_declarations 是空的")
        # ②b 同一信号只许点名一次（第十三轮实测 bug：两道闸各 append 一条、why 还互斥）
        ig_sigs = [x.get("signal") for x in (e.get("ignored_declarations") or [])]
        if len(ig_sigs) != len(set(ig_sigs)):
            bad.append(f"{path}:ignored_declarations 有重复信号 {ig_sigs}")
        # ②c 区间候选表：被选中的**恰好一个**（第十四轮：引擎做了选择就要记录它排除了什么）
        cands = e.get("range_candidates") or []
        if cands and sum(1 for c in cands if c.get("chosen")) != 1:
            bad.append(f"{path}:range_candidates 里 chosen 不是恰好一个 {cands}")
        # ②d `declared_total_ignored` 必须与证据对齐（第十五轮 C：同一行里
        #    ignored_declarations 有被拒的区间/setsize，而这一格是 null = 两个字段讲同一件事只有一个有值）
        rej = [x for x in (e.get("ignored_declarations") or [])
               if x.get("signal") in ("range_reached", "setsize_reached") and x.get("tot") is not None]
        d_ign = (r.get("stop_detail") or {}).get("declared_total_ignored")
        if rej and d_ign != rej[0].get("tot"):
            bad.append(f"{path}:declared_total_ignored={d_ign} 与被拒声明 {rej[0]} 对不上")
        # ②e 「裁量带名字」与「分歧可对账」的不变量（第十六轮评审 C + D3，第十七轮判级 agent 加严）：
        #   · 有自洽候选时 `chosen_reason` 必须有值（选择不许是无名的）
        #   · `candidate_disagreement` 非空 ⟺ 自洽候选的 tot 不止一个取值；被选中的 tot 必在其中
        #   · **每一项必须自带原文**（`{tot, where, text}`）：只给数字等于把两个形状相同的数字
        #     再摆一遍（15/50 与 10/15 的数值形状完全一样，能分辨的只有计数器的文本）
        sane_c = [c for c in cands if c.get("sane")]
        dis = e.get("candidate_disagreement")
        if sane_c and not e.get("chosen_reason"):
            bad.append(f"{path}:有自洽候选却没有 chosen_reason")
        if dis and not all(isinstance(d, dict) for d in dis):
            bad.append(f"{path}:candidate_disagreement 必须是对象数组（带原文），实际 {dis}")
        # 「值旁边的结论位」必须与材料互证（第十七轮评审的建议，已落地）：
        # `total_conflicted` 恒在场，且 ⟺ 自洽候选的 tot 不止一个取值。
        tc = e.get("total_conflicted")
        if tc is None:
            bad.append(f"{path}:total_conflicted 缺席（它必须与 total 一样恒在场）")
        elif bool(tc) != bool(dis):
            bad.append(f"{path}:total_conflicted={tc} 与 candidate_disagreement={dis} 对不上")
        # ②f **承诺与撤回互斥**（第二十轮判级 agent 的不变量）：冲突时 `total` 必须**不落地**。
        #   注意方向：`total_conflicted=True ⟹ total is None` 成立，**反向不成立**——
        #   `total=None` 只是「没有一个可落地的总数」，它有无数非冲突来源（弱证据、被闸门摘掉、
        #   根本没读到计数器……实测反例：`emptynomoreinner` 与 `r18two?site=grow` 都是
        #   total=None/conflicted=False）。判级 agent 的原话是双条件，但按实测只能是单向蕴含。
        if tc and e.get("total") is not None:
            bad.append(f"{path}:total_conflicted=True 却落了 total={e.get('total')}（承诺与撤回并存）")
        # ②f' **链的下游**（第二十一轮判级 agent 的补正）：冲突 ⟹ total=null ⟹ **无数值终态**。
        #    `range_reached` 的定义就是「我数到了**总数**」，所以它**预设**总数存在；
        #    total 被撤销后它就没有地基——`exhausted`（数到了总数）+ `total=null`（没有总数）
        #    不能同真。这里钉**更准的形式**：冲突时 owned 里不许出现数值类信号
        #    （文本宣告 `no_more_text` 不受限：它不依赖那个有争议的总数）。
        NUMERIC_SIGS = {"range_reached", "setsize_reached"}
        if tc and (NUMERIC_SIGS & set(e.get("owned_signals") or [])):
            bad.append(f"{path}:冲突却仍由数值信号收尾 owned={e.get('owned_signals')}"
                       "（数到了总数却没有总数）")
        # ②g 不自洽的候选要**说清错在哪**（第二十轮对抗评审 B）：`sane=False` 必须带 `drop_why`，
        #    自洽的必须为 None（否则「为什么丢」又被一个布尔吞掉）。
        for c in cands:
            if c.get("sane") and c.get("drop_why") is not None:
                bad.append(f"{path}:自洽候选却带 drop_why={c.get('drop_why')}")
            if c.get("sane") is False and not c.get("drop_why"):
                bad.append(f"{path}:不自洽候选没有 drop_why（只剩一个布尔，审计不了）")
        if len({c.get("tot") for c in sane_c}) > 1:
            if not dis:
                bad.append(f"{path}:自洽候选的 tot 不一致却没报 candidate_disagreement")
            else:
                d_tots = [d.get("tot") for d in dis if isinstance(d, dict)]
                if (cands and [c.get("tot") for c in cands if c.get("chosen")] or [None])[0] not in d_tots:
                    bad.append(f"{path}:chosen 的 tot 不在 candidate_disagreement={d_tots} 里")
                if len(d_tots) != len({c.get("tot") for c in sane_c}):
                    bad.append(f"{path}:candidate_disagreement 没覆盖全部互斥取值 {d_tots}")
                if not all((d.get("text") or "").strip() for d in dis if isinstance(d, dict)):
                    bad.append(f"{path}:candidate_disagreement 有项没带原文 {dis}")
        elif dis:
            bad.append(f"{path}:候选总数一致却报了 candidate_disagreement={dis}")
        # ③ 不相交不变量（判级 agent 的裁定：字段互证只能查「打不打架」，查不了「对不对」）：
        #    没被采用的观测不许同时出现在 owned 里；正确性只交给夹具。
        owned_set = set(e.get("owned_signals") or [])
        ignored_set = {x.get("signal") for x in (e.get("ignored_declarations") or [])}
        if owned_set & ignored_set:
            bad.append(f"{path}:owned ∩ ignored = {sorted(owned_set & ignored_set)}")
    ok92 = not bad
    check("T92 end/owned_signals 与 stop_reason 永远互证", ok92, f"矛盾项={bad or '无'}")
except Exception as e:  # noqa: BLE001
    check("T92 end/owned_signals 与 stop_reason 永远互证", False, f"{type(e).__name__}: {str(e)[:90]}")

# T94 **第八轮实测 S（同一形状的第三个实例）**：容器**子树内**的无关 setsize 控件
#     + 慢加载清空记录（控件留着）→ 旧实现：`setsize_reached` 无条件有主 + 一致性降级
#     只装在主循环、没进空组早退路径 → `exhausted` + `total:1` + resume=false（5/20 条）。
#     现在：**降级函数两条路径都调**，`total` 不写、`end` 重算、按可续跑的 stalled 收尾。
try:
    s_ = open_session(f"{BASE}/r8setsize.html")
    try:
        short = act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                 "limit": 100, "max_pages": 8, "wait_ms": 300})
        long_ = act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                 "limit": 100, "max_pages": 8, "wait_ms": 300,
                                                 "settle_ms": 5000, "next": short.get("next")})
    finally:
        close_session(s_["session_id"])
    e = short.get("end_evidence") or {}
    ign = e.get("ignored_declarations") or []
    # 同型变体（评审 E1 的预演）：容器**内部**嵌一个 `1-3 of 3` 的区间小控件。
    # 结构上它「有主」（在容器文本里），但值上不可能（已收 6 > 声明 3）→ 一致性降级兜住。
    s2 = open_session(f"{BASE}/r8setsize.html?range=1")
    try:
        sr_ = act_in_session(s2["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                "limit": 100, "max_pages": 8, "wait_ms": 300})
    finally:
        close_session(s2["session_id"])
    er = sr_.get("end_evidence") or {}
    ok94 = (short.get("stop_reason") == "stalled" and short.get("resume") is True
            and e.get("end") is False and e.get("owned_signals") == []
            and e.get("total") is None and e.get("total_claimed") == 1
            and any(x.get("signal") == "setsize_reached" and x.get("why") == "inconsistent_with_collected"
                    for x in ign)
            and (short.get("count") or 0) >= 5
            # 两趟合计要 ≥20（夹具里那个控件 <li> 自己也会被当成一条记录，所以不钉死单趟数字）
            and (short.get("count") or 0) + (long_.get("count") or 0) >= 20
            # E1 变体：容器内的区间小控件同样被一致性降级挡住
            and sr_.get("stop_reason") == "stalled" and er.get("end") is False
            and er.get("total") is None and er.get("total_claimed") == 3)
    check("T94 容器子树内的无关 setsize 也要走一致性降级", ok94,
          f"短预算: count={short.get('count')} {short.get('stop_reason')} end={e.get('end')} "
          f"total={e.get('total')} claimed={e.get('total_claimed')} "
          f"ign={[(x.get('signal'), x.get('why')) for x in ign]}；"
          f"两趟合计 count={(short.get('count') or 0) + (long_.get('count') or 0)}；"
          f"E1变体 stop={sr_.get('stop_reason')}/total={er.get('total')}/claimed={er.get('total_claimed')}")
except Exception as e:  # noqa: BLE001
    check("T94 容器子树内的无关 setsize 也要走一致性降级", False, f"{type(e).__name__}: {str(e)[:90]}")

# T93 判级 agent 的探针：**有记录**那一侧，「无关计数器 + 分页器消失」会不会让 total 说谎
#     形状：`r7range.html?fast=1&dead=1` —— 20 条真的全部渲染、main 里另一块写「本期榜单 1-5 共 5」、
#     末页分页器禁用。旧行为：`total=5` 与 `count=20` 同域打架，还拿它当 `total_reached` 的停止理由。
#     现在：`total` 降级为 `total_claimed`（+`ignored_declarations` 的 `inconsistent_with_collected`），
#     `end` 跟着重算 → 不出现「end:true 却与 count 矛盾」。
try:
    s_ = open_session(f"{BASE}/r7range.html?fast=1&dead=1")
    try:
        r = act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                              "limit": 100, "max_pages": 8, "wait_ms": 300})
    finally:
        close_session(s_["session_id"])
    e = r.get("end_evidence") or {}
    ign = e.get("ignored_declarations") or []
    ok93 = (r.get("count") == 20                                # 一条都不许丢
            and e.get("total") is None and e.get("total_source") is None   # 别人的账不许写进 total
            and e.get("total_claimed") == 5                     # 但要如实报告「页面是这么说的」
            and any(x.get("why") == "inconsistent_with_collected" for x in ign)
            and r.get("stop_reason") == "stalled"               # 不是 total_reached
            and e.get("end") is False)
    check("T93 无关计数器不许写进 total（collected > declared 即降级）", ok93,
          f"count={r.get('count')} stop={r.get('stop_reason')}/{(r.get('stop_detail') or {}).get('reason')} "
          f"total={e.get('total')} claimed={e.get('total_claimed')} end={e.get('end')} "
          f"ignored={[x.get('why') for x in ign]}")
except Exception as e:  # noqa: BLE001
    check("T93 无关计数器不许写进 total（collected > declared 即降级）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T95 判级 agent 的**对称探针**：无关计数器**恰好等于**已收条数（20==20）
#     → 「collected > declared」查不出来；靠**值一致性（一）：窗口 == 本页条数**拦下
#     （无关「本期榜单 1-20 共 20」的窗口 20 条 ≠ 本页 5 条）。
#     不修的话：total=20 + 已收 20 + 分页器临时禁用 → `total_reached` → exhausted/resume=false，
#     而后面还有 15 条。这里断言两条：默认预算 → stalled 且不写 total；给足预算 → 拿满 20 条。
try:
    s_ = open_session(f"{BASE}/r7range.html?match=1")
    try:
        a = act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                             "limit": 100, "max_pages": 8, "wait_ms": 300})
        b = act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                             "limit": 100, "max_pages": 8, "wait_ms": 300,
                                             "settle_ms": 5000, "next": a.get("next")})
    finally:
        close_session(s_["session_id"])
    e = a.get("end_evidence") or {}
    ign = e.get("ignored_declarations") or []
    ok95 = (a.get("stop_reason") == "stalled" and e.get("end") is False
            and e.get("total") is None and e.get("total_claimed") == 20
            and any(x.get("why") == "window_mismatch" for x in ign)
            and (a.get("count") or 0) + (b.get("count") or 0) == 20)      # 后面的数据一条不丢
    check("T95 无关计数器恰好等于已收条数也只报 stalled", ok95,
          f"默认: count={a.get('count')} {a.get('stop_reason')} total={e.get('total')} "
          f"claimed={e.get('total_claimed')} ign={[(x.get('signal'), x.get('why')) for x in ign]}；"
          f"两趟合计={(a.get('count') or 0) + (b.get('count') or 0)}")
except Exception as e:  # noqa: BLE001
    check("T95 无关计数器恰好等于已收条数也只报 stalled", False, f"{type(e).__name__}: {str(e)[:90]}")

# T96 **第九轮实测 S（等式形态）**：声明总数**恰好等于**本页条数
#     两道值闸都是不等式（窗口 ≠ 本页、已收 > 声明），于是「声明 == 已收」这一格是空的：
#     容器内小控件写「本组 1-15 of 15」+ 本页 15 条 + 分页器临时不可用 → 旧行为 `total_reached`
#     → exhausted/resume=false，而第 2 页还有 15 条永远读不到。
#     现在：具名观测 `total_fits_page_yet_pager_present` 为真 → 摘掉该声明并点名
#     `fits_page_yet_pager_present` → `end=false`、按可续跑的 stalled 收尾
#     （`stop_detail.declared_total_ignored` 说明原因）。
try:
    s_ = open_session(f"{BASE}/r9fake2.html?per=15&decl=15&disabled=1")
    try:
        a = act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                             "limit": 100, "max_pages": 8, "wait_ms": 300})
        time.sleep(2.8)                                     # 等站点把分页器放开
        b = act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                             "limit": 100, "max_pages": 8, "wait_ms": 800,
                                             "next": a.get("next")})
    finally:
        close_session(s_["session_id"])
    e = a.get("end_evidence") or {}
    d = a.get("stop_detail") or {}
    ok96 = (a.get("stop_reason") == "stalled" and a.get("resume") is True
            and a.get("count") == 15 and e.get("end") is False
            and e.get("one_page_total_pager") == "present"
            and any(x.get("why") == "fits_page_yet_pager_present" for x in (e.get("ignored_declarations") or []))
            and d.get("declared_total_ignored") == 15
            and (a.get("count") or 0) + (b.get("count") or 0) >= 30)   # 第 2 页的数据一条不许丢
    check("T96 声明总数恰好等于本页条数时不许判到底", ok96,
          f"首趟: count={a.get('count')} {a.get('stop_reason')}/{d.get('reason')} end={e.get('end')} "
          f"one_page_pager={e.get('one_page_total_pager')} blocked={d.get('declared_total_ignored')}；"
          f"合计={(a.get('count') or 0) + (b.get('count') or 0)}")
except Exception as e:  # noqa: BLE001
    check("T96 声明总数恰好等于本页条数时不许判到底", False, f"{type(e).__name__}: {str(e)[:90]}")

# T97 **第十轮 + 第十一轮实测 S**：认不出的推进控件不许让「一页装得下」收工
#     `adv == null` 只说明「引擎**认不出**控件」，不说明「页面上没有控件」：
#     第十轮是纯图形箭头（探测要求 `iconish`），第十一轮是**非图标、非词汇表**的候选
#     （`<span role=button class=go>继续看下一批</span>`）——两次都让 `one_page_total_pager`
#     落到「没有」那一档，于是「本组 1-5 of 5」被允许收工 → exhausted/resume=false。
#     现在：候选不再要求像图标，第三态从 `absent` 改名 `unverified`（只证明「我没看见」），
#     **三态都不允许**拿这条总数收工；同时钉住 `stop_detail.pager_at_stop` 与
#     `text.element`（命中所在元素，不是整个容器）。
try:
    cases = [("r10svg.html?pages=3", "undetectable"), ("r11text.html?pages=3", "undetectable"),
             ("r10svg.html?pages=1", "unverified"), ("r11text.html?pages=1", "unverified")]
    bad = []
    for path, want in cases:
        s_ = open_session(f"{BASE}/{path}")
        try:
            r = act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                  "limit": 100, "max_pages": 8, "wait_ms": 300})
        finally:
            close_session(s_["session_id"])
        e = r.get("end_evidence") or {}
        d = r.get("stop_detail") or {}
        if (r.get("stop_reason"), e.get("end"), e.get("one_page_total_pager"),
                any(x.get("why") == "pager_undetectable" for x in (e.get("ignored_declarations") or [])),
                d.get("pager_at_stop") == "absent") != ("stalled", False, want, True, True):
            bad.append(f"{path}: {r.get('stop_reason')}/end={e.get('end')}/"
                       f"{e.get('one_page_total_pager')}/ignored={e.get('ignored_declarations')}")
    ok97 = not bad
    check("T97 认不出的推进控件不许让「一页装得下」收工", ok97, f"异常档={bad or '无'}")
except Exception as e:  # noqa: BLE001
    check("T97 认不出的推进控件不许让「一页装得下」收工", False, f"{type(e).__name__}: {str(e)[:90]}")

# T98 契约事实必须出现在**工具描述**这一通道里（判级 agent 的裁定：README 是 rationale、
#     工具描述是契约；agent 未必读 README，但那一格恰是最容易误读的——把 stalled 当失败去重试，
#     或把 exhausted 当唯一可信终态）。这条断言把「一行契约事实」钉住，防止它被静默删掉。
try:
    from ai_browser import mcp_server as _mcp

    want = "是否到底由你判断"
    cdoc, sdoc = getattr(_mcp.collect, "__doc__", "") or "", getattr(_mcp.session_act, "__doc__", "") or ""
    ok98 = (want in cdoc and want in sdoc
            and 'stop_reason="stalled"' in cdoc)
    check("T98 工具描述里有一行「stalled 只是证不了」契约事实", ok98,
          f"collect={want in cdoc} session_act={want in sdoc} doc_len={len(cdoc)}")
except ImportError as e:  # noqa: BLE001
    detail.append(f"    T98 跳过（缺依赖 {e}）：这一格本轮没有证据")
except Exception as e:  # noqa: BLE001
    check("T98 工具描述里有一行「stalled 只是证不了」契约事实", False, f"{type(e).__name__}: {str(e)[:90]}")

# T99 夹具自检（第十二轮评审的建议，来自他自己踩的坑）：**夹具没渲染**会被读成**引擎行为异常**。
#     他造变体时把 `</div>` 写进 JS 字符串，那段脚本没执行、控件根本没进 DOM，
#     于是「候选集漏了它」这个**假发现**差点成立——他靠逐候选取证才发现。
#     这里用最便宜的一道闸兜住「整页没渲染」：每个夹具都要能解析出至少 1 个块。
#     诚实边界：它只挡「整页空白/脚本整体没跑」；**局部**脚本报错仍可能漏，
#     要挡那个得在 core 里加 pageerror 捕获（记为待办，不假称已覆盖）。
try:
    fixtures = sorted(p.name for p in (HERE / "test_site").glob("*.html"))
    empty = []
    for name in fixtures:
        page = parse_url(f"{BASE}/{name}", static=False)
        if not page.get("blocks"):
            empty.append(name)
    ok99 = bool(fixtures) and not empty
    check("T99 夹具自检：每个 test_site/*.html 都要渲染出内容", ok99,
          f"夹具 {len(fixtures)} 个；没渲染出块的={empty or '无'}")
except Exception as e:  # noqa: BLE001
    check("T99 夹具自检：每个 test_site/*.html 都要渲染出内容", False, f"{type(e).__name__}: {str(e)[:90]}")

# T100 **第十三轮实测 bug**：两道闸（页内闸 + 累计闸）同时拒同一个声明时，
#      `ignored_declarations` 会出现**同一信号两条、两条 why 互相矛盾**
#      （「不在容器里 / 一页装得下」与「在容器里但值不对」）——按审计字段做决策的调用方
#      无法在不写特例的情况下处理。现在按 signal 去重、保留覆盖力最强的 why。
try:
    s_ = open_session(f"{BASE}/r13dup.html?pages=4")
    try:
        r = act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                             "limit": 100, "max_pages": 8, "wait_ms": 300})
    finally:
        close_session(s_["session_id"])
    e = r.get("end_evidence") or {}
    ign = e.get("ignored_declarations") or []
    sigs = [x.get("signal") for x in ign]
    rng = [x for x in ign if x.get("signal") == "range_reached"]
    stz = [x for x in ign if x.get("signal") == "setsize_reached"]
    ok100 = (r.get("count") == 20 and r.get("stop_reason") == "stalled"
             and e.get("end") is False and e.get("owned_signals") == []
             and len(sigs) == len(set(sigs))                    # 同一信号只许一条
             and len(rng) == 1 and rng[0].get("why") == "inconsistent_with_collected"
             # setsize 只报一条即可：累计闸只说「被选中的那个总数」的信号（这里 total_source=range），
             # 所以 setsize 那条保留页内闸的理由是**正确**的（它仍然只出现一次）
             and len(stz) == 1
             and stz[0].get("why") in ("fits_page_yet_pager_present", "inconsistent_with_collected"))
    check("T100 同一信号只报一条、保留最强理由", ok100,
          f"count={r.get('count')} {r.get('stop_reason')} ignored="
          f"{[(x.get('signal'), x.get('why')) for x in ign]}")
except Exception as e:  # noqa: BLE001
    check("T100 同一信号只报一条、保留最强理由", False, f"{type(e).__name__}: {str(e)[:90]}")

# T101 「判决依据与**第几趟**无关」（第十三轮评审 B 的正面结论）：一趟直达 vs 两趟续跑，
#      在**停止原因与停止页相同**的前提下，证据/判决字段必须逐字一致。
#      ⚠️ 前置断言：两边 `stop_reason` 与 `pages` 必须相同——否则对拍出来的一堆「不一致」
#      全是**停止点不同**造成的假阳性（评审第一次对拍就踩了这个坑，见本轮 D 节）。
try:
    one = open_session(f"{BASE}/r13dup.html?pages=4")
    try:
        rA = act_in_session(one["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                "limit": 100, "max_pages": 8, "wait_ms": 300})
    finally:
        close_session(one["session_id"])
    two = open_session(f"{BASE}/r13dup.html?pages=4")
    try:
        r1 = act_in_session(two["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                "limit": 5, "max_pages": 8, "wait_ms": 300})
        rB = act_in_session(two["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                "limit": 100, "max_pages": 8, "wait_ms": 300,
                                                "next": r1.get("next")})
    finally:
        close_session(two["session_id"])
    def _machine(v):
        """剥掉散文（`hint`）：散文里会引用**本趟**收到的条数（20 vs 15），那不是判据。
        判据是机器可读的那几项（signal/source/why/end/owned/total…）——它们必须逐字一致。"""
        if isinstance(v, dict):
            return {k: _machine(x) for k, x in v.items() if k != "hint"}
        if isinstance(v, list):
            return [_machine(x) for x in v]
        return v
    comparable = (rA.get("stop_reason") == rB.get("stop_reason") and rA.get("pages") == rB.get("pages"))
    keys = ("stop_reason", "data", "resume", "end_evidence", "stop_detail", "settle_used")
    diff = [k for k in keys if _machine(rA.get(k)) != _machine(rB.get(k))]
    ok101 = (r1.get("stop_reason") == "limit" and comparable and not diff
             and rA.get("count") == 20 and rB.get("count") == 15)
    check("T101 判决依据与「第几趟」无关（停止点对齐后逐字一致）", ok101,
          f"可比的={comparable}（A={rA.get('stop_reason')}/{rA.get('pages')}页 "
          f"B={rB.get('stop_reason')}/{rB.get('pages')}页）差异字段={diff or '无'} "
          f"count A={rA.get('count')} B={rB.get('count')}")
except Exception as e:  # noqa: BLE001
    check("T101 判决依据与「第几趟」无关（停止点对齐后逐字一致）", False, f"{type(e).__name__}: {str(e)[:90]}")

# T102 **第十四轮实测 bug**（第十五轮的选择规则修正后语义更强）：区间匹配只取文档序第一个
#      → 容器内的诱饵计数器把容器外的**真进度**挤掉。现在：①收集**全部候选**并逐个标明
#      where/window/tot/chosen；②选择规则 **不按文档序**（没收满优先、tot 大优先）——
#      于是这一档**真计数器被直接选中**（比第十四轮「至少在场」更强）；
#      ③降级理由只陈述事实、`source` 用真实来源（这两条在 r13dup 那档上断言）。
try:
    s_ = open_session(f"{BASE}/r14decoy.html?disabled=0")
    try:
        r = act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                             "limit": 100, "max_pages": 8, "wait_ms": 300})
    finally:
        close_session(s_["session_id"])
    s2 = open_session(f"{BASE}/r14decoy.html?disabled=1")
    try:
        r_off = act_in_session(s2["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                 "limit": 100, "max_pages": 8, "wait_ms": 300})
    finally:
        close_session(s2["session_id"])
    s3 = open_session(f"{BASE}/r13dup.html?pages=4")
    try:
        r_dup = act_in_session(s3["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                 "limit": 100, "max_pages": 8, "wait_ms": 300})
    finally:
        close_session(s3["session_id"])
    e = r.get("end_evidence") or {}
    cands = e.get("range_candidates") or []
    ign_dup = ((r_dup.get("end_evidence") or {}).get("ignored_declarations") or [{}])[0]
    ok102 = (r.get("count") == 15 and e.get("total_claimed") == 15    # 真进度被选中（旧行为：被诱饵挤掉）
             # 第二十轮：这一页有两个自洽但互相矛盾的总数（真 15 / 诱饵 5）⇒ **撤销落地**：
             # `total` 为 None、被撤销的值进 total_claimed、点名 + 材料照给；同时**不再用数字收工**
             # （`stalled` 而不是 `exhausted`）——这是那条裁定的**代价**，登记在 README 的「已知代价」里。
             and e.get("total") is None and e.get("total_conflicted") is True
             and any(c.get("chosen") and c.get("tot") == 15 for c in cands)
             and any((not c.get("chosen")) and c.get("tot") == 5 for c in cands)
             and r_off.get("stop_reason") == "stalled" and r_off.get("resume") is True
             # 降级理由不许说「不是这份列表的账」（它的 source 明明是 container）——在累计闸那档上验
             and ign_dup.get("source") == "container"
             and "不是这份列表的账" not in (ign_dup.get("hint") or "")
             and "整体" in (ign_dup.get("hint") or ""))
    check("T102 诱饵计数器不许遮蔽真计数器（且理由不许指错）", ok102,
          f"可用档: count={r.get('count')} {r.get('stop_reason')} total={e.get('total')} "
          f"cands={[(c.get('where'), c.get('tot'), c.get('chosen')) for c in cands]}；"
          f"禁用档: {r_off.get('stop_reason')}；r13dup 理由={ign_dup.get('source')}/"
          f"{'整体' in (ign_dup.get('hint') or '')}")
except Exception as e:  # noqa: BLE001
    check("T102 诱饵计数器不许遮蔽真计数器（且理由不许指错）", False, f"{type(e).__name__}: {str(e)[:90]}")

# T103 iframe 盲区必须**可观测**（AGENT_BOARD 上 Kimi 的第 3 条）：
#      「正文在 iframe 里看不到」这件事本身是**已知限制**（做不到），但回包必须能让调用方
#      区分「内容就这些」与「正文在内嵌页面里没被看到」。所以页面 JSON 带 `frames`（可见 iframe
#      的 src/尺寸，≤5 条）、`stats.counts.frames` 也计数。
try:
    page = parse_url(f"{BASE}/frames.html")
    frames = page.get("frames") or []
    ok103 = (bool(page.get("blocks"))
             and len(frames) >= 1
             and "feed.html" in (frames[0].get("src") or "")
             and (page.get("stats", {}).get("counts", {}) or {}).get("frames") == len(frames))
    check("T103 iframe 盲区可观测（frames 计数与 src）", ok103,
          f"frames={frames} stats.frames={((page.get('stats') or {}).get('counts') or {}).get('frames')}")
except Exception as e:  # noqa: BLE001
    check("T103 iframe 盲区可观测（frames 计数与 src）", False, f"{type(e).__name__}: {str(e)[:90]}")

# T104 **交付前体积上限** `max_chars`（AGENT_BOARD 上 Kimi 的 P0：过滤必须发生在交付前）：
#      超限时丢掉**文档序末尾**的块，并在 `coverage.truncated` 里如实报告丢了什么——
#      绝不静默截断；`stats.json_chars` 也必须等于**实际交付**的长度。
#      边界：上限小到连固定字段都装不下时，标注 `fixed_overhead_over_cap` 而不是假装成功。
try:
    big = "<html><body>" + "".join(
        f"<p>第 {i} 段：这是一段用来把页面撑大的测试文本，长度大概五十个字符左右。</p>"
        for i in range(120)) + "</body></html>"
    full = parse_html(big)
    capped = parse_html(big, max_chars=3000)
    tiny = parse_html(big, max_chars=400)
    tr = (capped.get("coverage") or {}).get("truncated") or {}
    tt = (tiny.get("coverage") or {}).get("truncated") or {}
    ok104 = (len(full.get("blocks") or []) >= 100                     # 正例：不传 cap 不改行为
             and not (full.get("coverage") or {}).get("truncated")
             and len(json.dumps(capped, ensure_ascii=False)) <= 3000  # 真的在上限内
             and capped.get("stats", {}).get("json_chars") == len(json.dumps(capped, ensure_ascii=False))
             and tr.get("reason") == "max_chars" and tr.get("cap") == 3000
             and tr.get("blocks_dropped", 0) > 0
             and tr.get("blocks_delivered") == len(capped.get("blocks") or [])
             and tr.get("blocks_total") == len(full.get("blocks") or [])
             and tt.get("fixed_overhead_over_cap") is True)           # 边界如实标注
    check("T104 max_chars 交付前截断（如实报告，不静默）", ok104,
          f"full={len(full.get('blocks') or [])}块 capped={len(capped.get('blocks') or [])}块/"
          f"{capped.get('stats', {}).get('json_chars')}字符 truncated={tr} tiny={tt}")
except Exception as e:  # noqa: BLE001
    check("T104 max_chars 交付前截断（如实报告，不静默）", False, f"{type(e).__name__}: {str(e)[:90]}")

# T105 **会话级 cookie 也要过得了 profile 重启**（交流板 SylviaMuse 的对照实验；她建议编号 T104，
#      但 T104 已被 max_chars 占用，故这里用 T105，并已在板上说明）。
#      夹具是她加的 `test_site/cookiepersist.html`：两条登录路径**只差一个 max-age**。
#      T1 的夹具用的是 max-age=86400，所以 T1 一直绿、却不覆盖真实登录页最常见的会话级 cookie。
#      修法：`_persist_session_cookies()` 在浏览器关闭前把无 `expires` 的 cookie 补一年有效期
#      （与 `import_cookies(persist_session_cookies=True)` 同一策略、**同一处实现**，`_close()` 里调用），
#      并在 `close_session()` 的返回里报出补了几条（消费端可见的标记）。
try:
    import shutil as _shutil

    prof_pos = f"T105-pos-{os.getpid()}"
    prof_neg = f"T105-neg-{os.getpid()}"
    for prof, label in ((prof_pos, "模拟登录（带 max-age 的 cookie）"),
                        (prof_neg, "模拟登录（会话级 cookie，无 expires）")):
        page = open_session(f"{BASE}/cookiepersist.html", profile=prof)
        btn = next(b for b in page["blocks"] if b["type"] == "action" and b.get("label") == label)
        act_in_session(page["session_id"], {"id": btn["id"]})
        closed = close_session(page["session_id"])
        if prof == prof_neg:
            neg_marker = closed
    pos = [b.get("text", "") for b in parse_url(f"{BASE}/cookiepersist.html",
                                                profile=prof_pos).get("blocks", [])]
    neg = [b.get("text", "") for b in parse_url(f"{BASE}/cookiepersist.html",
                                                profile=prof_neg).get("blocks", [])]
    ok105 = (any("已登录：persist" in t for t in pos)
             and any("已登录：sess" in t for t in neg)
             and (neg_marker or {}).get("session_cookies_persisted", 0) >= 1)
    check("T105 会话级 cookie 也要过得了 profile 重启", ok105,
          f"正例={'存活' if any('已登录：persist' in t for t in pos) else '丢了'} "
          f"反例={'存活' if any('已登录：sess' in t for t in neg) else '丢了'} "
          f"close_session 报告补了 {neg_marker}")
    for prof in (prof_pos, prof_neg):
        _shutil.rmtree(PROFILES / prof, ignore_errors=True)
except Exception as e:  # noqa: BLE001
    check("T105 会话级 cookie 也要过得了 profile 重启", False, f"{type(e).__name__}: {str(e)[:90]}")

# T106 **第十五轮实测 bug**：容器内有两个自洽计数器时，选择规则不能退化成「文档序第一个」。
#      夹具 `r15two.html`：假「本组 1-5 of 5」写在**前**、真「进度 1-5 of 15」写在**后**，
#      两个都 sane、窗口都等于本页条数——旧的「容器内优先」在同时命中两个时就等于取前面那个，
#      于是 `total:5` 而回包自己的候选表里就列着 `tot:15`（判据与证据打架）。
#      新规则：**没收满的优先（hi<tot），其次 tot 大的优先**（假计数器描述的是「已完整的东西」，
#      而真列表的总数 ≥ 任何子组的总数）。`where` 仍如实上报，但不参与选择。
try:
    off = open_session(f"{BASE}/r15two.html?disabled=1")
    try:
        r_off = act_in_session(off["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                  "limit": 100, "max_pages": 8, "wait_ms": 300})
    finally:
        close_session(off["session_id"])
    on = open_session(f"{BASE}/r15two.html?disabled=0")
    try:
        r_on = act_in_session(on["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                 "limit": 100, "max_pages": 8, "wait_ms": 800})
    finally:
        close_session(on["session_id"])
    e = r_off.get("end_evidence") or {}
    cands = e.get("range_candidates") or []
    ok106 = (e.get("total_claimed") == 15                 # 真计数器被选中（旧行为是 5）
             and e.get("total") is None and e.get("total_conflicted") is True   # 第二十轮：冲突即撤销落地
             and any(c.get("chosen") and c.get("tot") == 15 for c in cands)
             and any((not c.get("chosen")) and c.get("tot") == 5 for c in cands)   # 诱饵仍可对账
             and r_off.get("stop_reason") == "stalled" and r_off.get("resume") is True
             and r_on.get("count") == 15)                # 正例：分页器可用时一趟拿满
    check("T106 两个自洽计数器时选中真进度（不按文档序）", ok106,
          f"禁用档: count={r_off.get('count')} {r_off.get('stop_reason')} total={e.get('total')} "
          f"cands={[(c.get('where'), c.get('tot'), c.get('chosen')) for c in cands]}；"
          f"可用档: count={r_on.get('count')} {r_on.get('stop_reason')}")
except Exception as e:  # noqa: BLE001
    check("T106 两个自洽计数器时选中真进度（不按文档序）", False, f"{type(e).__name__}: {str(e)[:90]}")

# T107 **第十六轮评审 A 的反例（夹具 `r16fake.html`）**：末页上真计数器已收满、假进度没收满。
#      A 档（`fake=50`）：容器内「本组 11-15 of 50」（没收满）+ 作用域「进度 11-15 of 15」（已收满），
#      已收 15 条而分页器不可用。评审主张把排序键倒过来（「收满的优先」）→ 应当 exhausted。
#      这个反事实**实测过**：它确实让 A 档变成 exhausted/total=15，但同一份夹具的
#      `?decoy=run&dis=2` 档（诱饵「本组 6-10 of 10」也「已收满」，排在真进度之前）会从
#      `stalled(10/15, 可续跑)` 变成 **`exhausted` + `total=10` + `resume=false`**——静默丢 5 条（S）。
#      两档的可观测形状完全相同（已收满那条的 tot 更小、没收满那条的 tot 更大、窗口都等于本页条数），
#      页面没说谁在说谎 ⇒ 没有任何排序键能分开它们；而代价不对称（多跑一轮 vs 静默丢数据）。
#      所以排序键**不改**，改的是「让裁量可对账」：`chosen_reason`（每一次选择都要有名字）
#      + `candidate_disagreement`（这一页有几个互相矛盾的总数）。
try:
    def _r16(path):
        s_ = open_session(f"{BASE}/{path}")
        try:
            return act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                     "limit": 100, "max_pages": 5, "wait_ms": 300})
        finally:
            close_session(s_["session_id"])

    big = _r16("r16fake.html?pages=3&fake=50")        # A 档：假进度说 50
    tie = _r16("r16fake.html?pages=3&fake=15")        # 平局档：两个都是 15
    run_ = _r16("r16fake.html?pages=3&decoy=run&dis=2")   # 反例档：诱饵跟着已收条数走 + 第 2 页起禁用
    run_ok = _r16("r16fake.html?pages=3&decoy=run")   # 反例档的正例：分页器可用
    eb, et, er_, eo = (x.get("end_evidence") or {} for x in (big, tie, run_, run_ok))

    def _dis_tots(ev):                       # 分歧项是 `{tot, where, text}`（第十七轮判级 agent 的加严）
        return [d.get("tot") for d in (ev.get("candidate_disagreement") or [])]

    ok107 = (
        # A 档：引擎**看见**两个矛盾的总数、如实上报，但**不替它们裁决**（stalled 可续跑，不丢数据）
        big.get("stop_reason") == "stalled" and big.get("resume") is True and big.get("count") == 15
        and eb.get("end") is False and eb.get("owned_signals") == []
        and _dis_tots(eb) == [50, 15]
        and _dis_tots(big.get("stop_detail") or {}) == [50, 15]
        # 分歧项必须**自带原文**——数值形状在两档里相同，能分辨的只有计数器的文本
        and all((d.get("text") or "").strip() for d in (eb.get("candidate_disagreement") or []))
        and len({(d.get("text") or "") for d in (eb.get("candidate_disagreement") or [])}) == 2
        and any("本组" in (d.get("text") or "") for d in (eb.get("candidate_disagreement") or []))
        and any("进度" in (d.get("text") or "") for d in (eb.get("candidate_disagreement") or []))
        and eb.get("chosen_reason") == "unreached_first"
        and eb.get("total_conflicted") is True      # 值旁边的结论位（只读 total 的人不会去翻数组）
        # 第二十轮判级 agent 的裁定：**撤销落地**（承诺与撤回不许并存）——
        # `total` 一律 None，被撤销的值进 `total_claimed` + 点名（why=conflicting_candidates），
        # 且**不是改用另一条候选**（若改用，total 会是 15）
        and eb.get("total") is None and eb.get("total_claimed") == 50
        and ("range_reached", "conflicting_candidates") in
            [(x.get("signal"), x.get("why")) for x in (eb.get("ignored_declarations") or [])]
        and (big.get("stop_detail") or {}).get("declared_total_ignored") == 50
        and "互相矛盾的总数" in ((big.get("stop_detail") or {}).get("hint") or "")
        # 平局档：总数一致（没有分歧可报），平局的选择**带名字**（文档序靠后的那个 = 真计数器）
        and tie.get("stop_reason") == "exhausted" and tie.get("count") == 15
        and et.get("total") == 15 and et.get("end") is True
        and et.get("candidate_disagreement") == []
        and et.get("total_conflicted") is False
        and et.get("chosen_reason") == "tie_later_in_document"
        and (tie.get("stop_detail") or {}).get("candidate_disagreement") is None
        # 反例档：**不许**拿「已收满的那个诱饵」收工（那会 exhausted+total=10+resume=false，丢 5 条）
        and run_.get("stop_reason") == "stalled" and run_.get("resume") is True
        and run_.get("count") == 10 and er_.get("total") is None
        and er_.get("total_claimed") == 15      # 第二十轮：这一页同样有两个矛盾的自洽候选 ⇒ 撤销落地
        and er_.get("end") is False
        and _dis_tots(er_) == [15, 10]
        and not any(c.get("chosen") and c.get("tot") == 10 for c in (er_.get("range_candidates") or []))
        # 正例：分页器可用时它照样收满 15 条并正常收尾
        and run_ok.get("count") == 15 and run_ok.get("stop_reason") == "exhausted"
        and eo.get("total") == 15)
    check("T107 反例档不许拿「已收满的诱饵」收工；分歧与裁量必须可对账", ok107,
          f"A档: {big.get('stop_reason')}/{big.get('count')} dis={eb.get('candidate_disagreement')} "
          f"reason={eb.get('chosen_reason')}；平局档: {tie.get('stop_reason')}/total={et.get('total')} "
          f"reason={et.get('chosen_reason')}；反例档: {run_.get('stop_reason')}/{run_.get('count')} "
          f"total={er_.get('total')} dis={er_.get('candidate_disagreement')}；"
          f"正例档: {run_ok.get('stop_reason')}/{run_ok.get('count')}")
except Exception as e:  # noqa: BLE001
    check("T107 反例档不许拿「已收满的诱饵」收工；分歧与裁量必须可对账", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T108 **第十八轮：把第十七轮那条 S 关掉**（判级 agent 的裁定：已复现的 S 本 round 就修）。
#      规则：**声明总数必须跨页稳定**——同一个计数器（同 `where` + 同窗口宽度）在相邻两页给出
#      **不同**的 `tot` ⇒ 它不是这份列表的账（列表的总数不会因为翻页而变大）。
#      形状是 **verify-before**：只在本页声明**已收满**（正要当终态用）时才比，且**没有上一页观测
#      就不比**（不乱杀）；命中后的动作是**撤销这条声明**（`total=None` + `total_claimed` + 点名），
#      **不是改用另一条候选**——后者会把 `total` 交给「更会装」的那个（评审的 B 节预判的那条路）。
#      回归四档：① S 档关闭 ② 两个计数器一致的末页不许误伤 ③ 追加式站点不许发错误终态、
#      也绝不许把 total 交给那个恒定的子组计数 ④ 恒定假计数器（残余档）如实钉住它的**边界**。
try:
    def _r18(path):
        s_ = open_session(f"{BASE}/{path}")
        try:
            return act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                     "limit": 100, "max_pages": 6, "wait_ms": 800})
        finally:
            close_session(s_["session_id"])

    fix = _r18("r16fake.html?pages=3&decoy=run&dis=2&real=0")   # 第十七轮那条 S
    ok_agree = _r18("r16fake.html?pages=3&fake=15")            # 两个计数器一致 → 真末页
    app = _r18("r18append.html")                               # 追加式：真总数 13→15→18→20
    app_dis = _r18("r18append.html?dis=3")                     # 追加式 + 第 3 页起分页器禁用
    const = _r18("r16fake.html?pages=3&fake=10&dis=2&real=0")  # 恒定假计数器（稳定性抓不到）
    ef, eo_, ea, ead, ec = (x.get("end_evidence") or {}
                            for x in (fix, ok_agree, app, app_dis, const))
    ign_f = [(x.get("signal"), x.get("why")) for x in (ef.get("ignored_declarations") or [])]
    ok108 = (
        # ① S 档：撤销声明 → stalled 可续跑；total 被摘掉、声明值进 total_claimed 并点名
        fix.get("stop_reason") == "stalled" and fix.get("resume") is True and fix.get("count") == 10
        and ef.get("total") is None and ef.get("total_claimed") == 10
        and ef.get("end") is False and ef.get("owned_signals") == []
        and ("range_reached", "total_not_stable_across_pages") in ign_f
        and (fix.get("stop_detail") or {}).get("declared_total_ignored") == 10
        # ② 一致性末页不误伤：跨页稳定 → 照旧 exhausted（这条规则不许把正常收尾也拖下水）
        and ok_agree.get("stop_reason") == "exhausted" and ok_agree.get("count") == 15
        and eo_.get("total") == 15 and eo_.get("end") is True
        and not eo_.get("ignored_declarations")
        # ③ 追加式站点：**不许**发错误终态，且 20 条一条不少；
        #    关键是 total **不许**变成那个恒定的子组计数 15（评审 B 节预判的路径）
        and app.get("count") == 20 and app.get("stop_reason") != "exhausted"
        and ea.get("total") != 15
        and (ea.get("total") is None or ea.get("total") == 20)
        and any(x.get("why") == "total_not_stable_across_pages"
                for x in (ea.get("ignored_declarations") or []))
        and app_dis.get("count") == 15 and app_dis.get("stop_reason") != "exhausted"
        and ead.get("total") != 15
        # ④ 残余档（**边界，不是已修**）：恒定假计数器在跨页上是「稳定」的，稳定性抓不到它。
        #    这里只钉**事实**：引擎跟随的是「页面自己那一条自洽声明」，而不是它抓到了真相
        #    （评审提供的更窄修法见 README「已知限制」；这条断言是那条修法的验收起点）。
        and const.get("stop_reason") == "exhausted" and const.get("count") == 10
        and ec.get("total") == 10 and not ec.get("ignored_declarations")
        and (ec.get("range_candidates") or [{}])[0].get("tot") == 10)
    check("T108 跨页声明不稳定时撤销声明（并核追加式站点与残余边界）", ok108,
          f"S档: {fix.get('stop_reason')}/{fix.get('count')} total={ef.get('total')} "
          f"claimed={ef.get('total_claimed')} ign={ign_f}；"
          f"一致末页: {ok_agree.get('stop_reason')}/{ok_agree.get('count')} total={eo_.get('total')}；"
          f"追加式: {app.get('stop_reason')}/{app.get('count')} total={ea.get('total')}；"
          f"追加+禁用: {app_dis.get('stop_reason')}/{app_dis.get('count')} total={ead.get('total')}；"
          f"恒定档: {const.get('stop_reason')}/{const.get('count')} total={ec.get('total')}")
except Exception as e:  # noqa: BLE001
    check("T108 跨页声明不稳定时撤销声明（并核追加式站点与残余边界）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T109 **第十九轮：对抗评审交接的四个夹具入库**（它的原话：「那是这条规则收窄的**原始证据**，
#      应与 r14decoy/r15two/r16fake 同等待遇长期钉住」）。四个夹具都在 `test_site/`：
#        · r18stab2：真总数**逐页增长**（13→15→18→20）+ 容器内**恒定**子组计数「本组…of 15」
#        · r18grow：同型另一组参数（`batch=20` 让两候选都自洽、都未收满）
#        · r18two?site=grow：真总数增长 + 恒定假计数（这档会真的触发跨页规则）
#        · r18uneven：**每页条数不等**（5/7/3），两候选的窗口都等于本页条数
#      外加一条**「没有隐藏第 N 道前提」的证据**：`fake=10` 那档**不触发**跨页规则，
#      原因是**逐页被选中的候选始终是真计数器（15/15/15，稳定）**，不是因为有别的暗条件。
#      这条快照就是评审 F1 那个未决点的答案（它的假设「prev 在第 2 页是 10」与实测不符：
#      键①未收满优先全程把真计数器排在前面，而第 3 页的假计数器 lo>tot 直接 sane=false 出局）。
try:
    def _r19(path, **extra):
        s_ = open_session(f"{BASE}/{path}")
        try:
            return act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                     "limit": 100, "max_pages": 8, "wait_ms": 300,
                                                     **extra})
        finally:
            close_session(s_["session_id"])

    stab = _r19("r18stab2.html")
    grow = _r19("r18grow.html")
    two = _r19("r18two.html?site=grow")
    unev = _r19("r18uneven.html")
    es, eg, et2, eu = (x.get("end_evidence") or {} for x in (stab, grow, two, unev))
    snap = [_r19("r16fake.html?pages=3&fake=10", max_pages=n) for n in (1, 2, 3)]

    def _chosen(ev):
        for c in (ev.get("range_candidates") or []):
            if c.get("chosen"):
                return (c.get("where"), c.get("window"), c.get("tot"))
        return None

    ok109 = (
        # ① r18stab2：恒定的子组计数**不许**被落地成这份列表的总数；停可续跑。
        #    第二十轮起：那一页本来就有两个自洽候选 → **冲突撤销**，所以 `total` 是 None、
        #    被撤销的值（真计数器那一侧 18）进 `total_claimed` 并点名。
        stab.get("stop_reason") == "stalled" and stab.get("resume") is True
        and es.get("total") is None and es.get("total_claimed") == 18
        and es.get("total_conflicted") is True
        and all(not (x.get("why") == "total_not_stable_across_pages" and x.get("tot") == 15)
                for x in (es.get("ignored_declarations") or []))
        # ② r18uneven：每页条数不等也能收全 15；两候选的窗口都等于**本页**条数（3）
        and unev.get("count") == 15
        and all(c.get("window") == 3 for c in (eu.get("range_candidates") or []) if c.get("sane"))
        # ③ 两个「识别前提会漏判」的固定样本：都不许发出终态
        and grow.get("stop_reason") == "stalled" and two.get("stop_reason") == "stalled"
        # r18two 这档真的触发了跨页规则：撤销声明（total=None）+ 点名
        and et2.get("total") is None and et2.get("total_claimed") == 15
        and any(x.get("why") == "total_not_stable_across_pages"
                for x in (et2.get("ignored_declarations") or []))
        # 那个**自己塌掉**的候选必须说清错在哪（第二十轮对抗评审 B：布尔答不了「为什么丢」）
        and any(c.get("sane") is False and c.get("drop_why") == "hi_gt_tot"
                for c in (et2.get("range_candidates") or []))
        # ④ F1：逐页快照 —— 被选中的始终是真计数器（15/15/15），所以规则**不**触发
        #    （不是「有隐藏前提」，而是「声明本来就稳定」）；末页照旧 exhausted/total=15
        and [_chosen(x.get("end_evidence") or {}) for x in snap] ==
            [("scope_text", 5, 15), ("scope_text", 5, 15), ("scope_text", 5, 15)]
        and not (snap[2].get("end_evidence") or {}).get("ignored_declarations")
        and snap[2].get("stop_reason") == "exhausted"
        and (snap[2].get("end_evidence") or {}).get("total") == 15)
    check("T109 评审交接的四个夹具入库 + 跨页规则「不触发」的逐页证据", ok109,
          f"stab2: {stab.get('stop_reason')}/{stab.get('count')} total={es.get('total')}；"
          f"grow: {grow.get('stop_reason')}；two: {two.get('stop_reason')} total={et2.get('total')} "
          f"claimed={et2.get('total_claimed')}；uneven: {unev.get('stop_reason')}/{unev.get('count')} "
          f"windows={[c.get('window') for c in (eu.get('range_candidates') or [])]}；"
          f"逐页 chosen={[_chosen(x.get('end_evidence') or {}) for x in snap]}")
except Exception as e:  # noqa: BLE001
    check("T109 评审交接的四个夹具入库 + 跨页规则「不触发」的逐页证据", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T111 **第二十一轮对抗评审 B**：`drop_why` 的取值必须**恰好是可达的那三个**。
#      ⚠️ 编号原本想用 T110，但远端（r111）已经把 T110 用在「技能属性隐患自检」上了——
#      合并后**两个 T110** 会撞号，所以本项改用 T111。教训记在这里：**编号是共享命名空间**，
#      合并前先扫一遍最大号（本次实测：合并后 1..110 已占用、下一个空号是 111）。
#      它逐条打完 `unsaneWhy` 的每条分支后指出：`window_gt_tot` **按构造不可达**
#      （走到那条时已有 `lo <= hi` 且 `hi <= tot` ⇒ `hi-lo+1 <= hi <= tot` 恒真）——是**死代码**，
#      也是一个永远不会出现的枚举值。危害不是误判，而是「枚举表比实现宽」：
#      按 `drop_why` 穷举写分支的调用方会以为存在第四种形态，下一个改 `saneRange` 的人会以为那条检查有用。
#      已删。本测试用注入夹具（`r21why.html?c=<文本>`，它的注入法）把可达形态逐个跑一遍：
#      ① 三种可达取值都要**出现**；② 观测到的集合**恰好等于**这三个（多一个就红）；
#      ③ 自洽候选的 `drop_why` 必须是 None。
try:
    R21_CASES = [("0-5 of 10", "lo_lt_1"), ("20-10 of 30", "reversed_range"),
                 ("6-0 of 10", "reversed_range"), ("20-25 of 10", "hi_gt_tot"),
                 ("8-12 of 10", "hi_gt_tot"), ("1-100 of 10", "hi_gt_tot"),
                 ("1-5 of 15", None), ("10-10 of 10", None)]
    seen = set()
    ok_cases = []
    for text, want in R21_CASES:
        s_ = open_session(f"{BASE}/r21why.html?c={text.replace(' ', '%20')}")
        try:
            r_ = act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                   "limit": 100, "max_pages": 1, "wait_ms": 300})
        finally:
            close_session(s_["session_id"])
        got = [c.get("drop_why") for c in ((r_.get("end_evidence") or {}).get("range_candidates") or [])
               if (c.get("text") or "").strip().startswith(text)]
        val = got[0] if got else "（没读到这条候选）"
        ok_cases.append(val == want)
        if val not in (None, "（没读到这条候选）"):
            seen.add(val)
    ok110 = (all(ok_cases)
             and seen == {"lo_lt_1", "reversed_range", "hi_gt_tot"}
             and "window_gt_tot" not in seen)
    check("T111 drop_why 恰好三个可达取值（不可达的那个已删）", ok110,
          f"逐档={list(zip([c[0] for c in R21_CASES], ok_cases))}；观测到的集合={sorted(seen)}")
except Exception as e:  # noqa: BLE001
    check("T111 drop_why 恰好三个可达取值（不可达的那个已删）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T112 **第二十三轮评审 A 的实测 S（两条路径）**：容器内**深层**的「没有更多」不许当这份列表的宣告。
#      形状（它的 `r22slow/r22inner`，我按描述复刻成 `test_site/r22cmt.html`）：
#      `#results` 容器内部一直挂着一块讲**评论**的子块
#      （`<li class="cmtbox"><div class="cmtfoot">没有更多评论了</div></li>`），
#      第 2 页起记录**空壳**（3s 后到）、分页器**临时**禁用。
#      旧行为（S）：容器内**任何后代**命中 NO_MORE_RE 都算 `no_more_text@container`（有主）⇒
#      空组判决 `declared=true` ⇒ `exhausted` + `resume=false` + 只拿 5/20 条（真数据 3 秒后到）。
#      根因：兄弟那条早有结构闸（`footerish`），**容器那条没有**。
#      修法：命中元素到容器的距离 ≤1（容器自身 / 直接子元素）才算这份列表的账；
#      更深 ⇒ 弱信号 `no_more_text_deep`（照报、`text.element` 指向那句话所在的后代，但不进 owned）。
#      S 修复规约两样都在：①回归夹具（本测试）②消费端可见标记（新信号名 + element）。
try:
    def _r22(path, **extra):
        s_ = open_session(f"{BASE}/{path}")
        try:
            return act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                     **{"limit": 100, "max_pages": 8, **extra}})
        finally:
            close_session(s_["session_id"])

    short = _r22("r22cmt.html?slow=1&inner=1", wait_ms=300)     # A1：空壳 + 临时禁用
    long_ = _r22("r22cmt.html?slow=1&inner=1", wait_ms=4000)    # 正例：预算够 → 一条不丢
    iso = _r22("r22cmt.html?slow=0&inner=1", wait_ms=300, advance=None, max_pages=1)   # A2
    iso_out = _r22("r22cmt.html?slow=0&inner=0", wait_ms=300, advance=None, max_pages=1)
    legit = _r22("collect_edge.html?mode=emptynomoreinner")     # 合法容器内页脚：不许被误伤
    es, el, ei, eio, eg = (x.get("end_evidence") or {}
                           for x in (short, long_, iso, iso_out, legit))
    ok112 = (
        # A1：不许拿容器内深层那句评论文案收工（旧行为是 exhausted/5 条/resume=false）
        short.get("stop_reason") == "stalled" and short.get("resume") is True
        and es.get("end") is False and "no_more_text" not in (es.get("owned_signals") or [])
        and "no_more_text_deep" in (es.get("signals") or [])
        and (es.get("text") or {}).get("element") == "div.cmtfoot"
        # 正例：预算够时一条不丢（旧行为在这档也会提前收尾）
        and long_.get("count") == 20
        # A2（advance=null 那条路）：与「容器外」对照档**结论一致**（都不许 exhausted）
        and iso.get("stop_reason") == "stalled" and iso_out.get("stop_reason") == "stalled"
        and ei.get("end") is False and eio.get("end") is False
        and "no_more_text_deep" in (ei.get("signals") or [])
        # 不许「收紧过头」：合法的**容器自身/直接子元素**页脚仍必须有主、仍能收尾
        and legit.get("stop_reason") == "exhausted"
        and "no_more_text" in (eg.get("owned_signals") or []))
    check("T112 容器内深层的「没有更多」不许当这份列表的宣告（含正例与不过度收紧）", ok112,
          f"A1: {short.get('stop_reason')}/{short.get('count')} resume={short.get('resume')} "
          f"signals={es.get('signals')} element={(es.get('text') or {}).get('element')}；"
          f"正例: count={long_.get('count')}；A2: {iso.get('stop_reason')} vs 对照 "
          f"{iso_out.get('stop_reason')}；合法容器内页脚: {legit.get('stop_reason')} "
          f"owned={eg.get('owned_signals')}")
except Exception as e:  # noqa: BLE001
    check("T112 容器内深层的「没有更多」不许当这份列表的宣告（含正例与不过度收紧）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T113 **第二十三轮评审 C 的复刻**：容器内嵌的子列表。它实测两条后果，本轮**只固化纯曝光那一半**：
#      C1 记录组被污染：整块子列表被当成一条记录（`count` 10→11，多出来的文本里写着「相关推荐」）——
#         本轮**不改判决**（那要动分组判据，属新原则，按冻结规则与判级 agent 第 5 条子句
#         「结构同型 ≠ 修法同型」须先量代价面；评审下轮会量详情页的误伤面），
#         但**必须让调用方认得出它**：新增 `_src.nested_group`（恒在场）。
#      C2 子计数器参与 `range` 选择：`sub=3` 时两个自洽候选 ⇒ 冲突 ⇒ `total` **撤销**（不丢数据）；
#         `subbig=1` 时子计数未收满 ⇒ 被键①选中 ⇒ `total_claimed=13`（仍是撤销态，**没有**落地假总数）。
#      所以本测试钉三件：①对照组不许坏 ②那条垃圾记录必须被标出来 ③任何一档都不许落地假 `total`。
try:
    def _r23(path):
        s_ = open_session(f"{BASE}/{path}")
        try:
            return act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                     "limit": 100, "max_pages": 4, "wait_ms": 300})
        finally:
            close_session(s_["session_id"])

    ctl = _r23("r23nested.html?sub=0")
    sub = _r23("r23nested.html?sub=3")
    big = _r23("r23nested.html?sub=3&subbig=1")
    ec, es_, eb = (x.get("end_evidence") or {} for x in (ctl, sub, big))

    def _marks(r_):
        return [bool(x.get("_src", {}).get("nested_group")) for x in (r_.get("records") or [])]

    def _garbage(r_):
        return [x for x in (r_.get("records") or []) if "相关推荐" in (x.get("text") or "")]

    ok113 = (
        # ① 对照组：没有子块时一切照旧（正例不许坏）
        ctl.get("stop_reason") == "exhausted" and ctl.get("count") == 10
        and ec.get("total") == 10 and not any(_marks(ctl))
        # ② 子块被吞成的那条记录**必须**被标出来（`_src.nested_group` 恒在场），其余行为 False
        and len(_garbage(sub)) == 1 and len(_garbage(big)) == 1
        and _marks(sub)[_marks(sub).index(True)] is True and sum(_marks(sub)) == 1
        and sum(_marks(big)) == 1
        and all(x.get("_src", {}).get("nested_group") is not None for x in (sub.get("records") or []))
        # ③ 任何一档都不许落地假 total（子计数即使被选中，也只能进 total_claimed）
        and sub.get("stop_reason") == "stalled" and sub.get("resume") is True
        and es_.get("total") is None and es_.get("total_conflicted") is True
        and big.get("stop_reason") == "stalled" and big.get("resume") is True
        and eb.get("total") is None and eb.get("total_claimed") == 13
        and any(c.get("chosen") and c.get("tot") == 13
                for c in (eb.get("range_candidates") or [])))
    check("T113 容器内嵌子列表：垃圾记录必须可辨认（nested_group）且不许落地假总数", ok113,
          f"对照: {ctl.get('stop_reason')}/{ctl.get('count')}；子块档: {sub.get('stop_reason')}/"
          f"count={sub.get('count')} marked={sum(_marks(sub))} total={es_.get('total')} "
          f"conflicted={es_.get('total_conflicted')}；子计数档: {big.get('stop_reason')}/"
          f"claimed={eb.get('total_claimed')} total={eb.get('total')}")
except Exception as e:  # noqa: BLE001
    check("T113 容器内嵌子列表：垃圾记录必须可辨认（nested_group）且不许落地假总数", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T114 **第二十五轮判级 agent 的裁定**：**退出码 0 只证明「进程正常结束」，不证明「测试跑过」**。
#      实测踩过：两个后台作业写同一个日志文件，后一个重定向失败（Out-File 报错）而 `$LASTEXITCODE`
#      仍然打印 0 —— **「没跑」看起来和「跑绿了」一样**。唯一文件名只是治标；
#      治本是「套件的结论要看输出存在、非空、有完整结论」，而这正好是
#      **检查必须 fail-closed（不能验证 ⇒ 红），不许 fail-open（不能验证 ⇒ 绿）** 那条规约的实例。
#      本测试直接打 `run_suite.py --check-log` 的四档（它的 `check_log` 是唯一实现，跑完校验也调它）：
#        ①正常日志 → 绿  ②空文件 → 红  ③文件不存在 → 红  ④只有 FAIL → 红
try:
    import subprocess as _sp114
    import tempfile as _tf114

    def _chk(content: str | None):
        d = _tf114.mkdtemp(prefix="r25chk-")
        p = Path(d) / "log.txt"          # 本文件是 `from pathlib import Path`（没有 `import pathlib`）
        if content is not None:
            p.write_text(content, encoding="utf-8")
        r_ = _sp114.run([sys.executable, str(HERE / "run_suite.py"), "--check-log", str(p)],
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        cwd=str(HERE))
        return r_.returncode

    ok114 = (_chk("PASS  T1 样例\nPASS  T2 样例\n") == 0
             and _chk("") != 0                      # 空 → 红
             and _chk(None) != 0                    # 不存在 → 红
             and _chk("PASS  T1 x\nFAIL  T2 y\n") != 0)
    # ⚠️ f-string **表达式里不能出现反斜杠**（Python < 3.12；本机 3.11 直接 SyntaxError）。
    #    实测：r132 上游这行写成 `f"...{_chk('PASS  T1 x\nFAIL  T2 y\n')}..."` ⇒ 整个套件
    #    连第一条断言都跑不到就崩（`SyntaxError: f-string expression part cannot include a backslash`）。
    #    修法：把带反斜杠的样本先赋给变量，f-string 里只引用变量名。
    _log_fail = "PASS  T1 x\nFAIL  T2 y\n"
    check("T114 套件结论 fail-closed（没跑/空日志/有 FAIL 都不算绿）", ok114,
          f"正常={_chk('PASS  T1 样例' + chr(10))} 空={_chk('')} 不存在={_chk(None)} "
          f"含FAIL={_chk(_log_fail)}（0=绿，非0=红）")
except Exception as e:  # noqa: BLE001
    check("T114 套件结论 fail-closed（没跑/空日志/有 FAIL 都不许算绿）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T115 **第二十六轮：详情页的代价面 = 绊线**（对抗评审第二十四轮量出：naive 的「排除嵌套组」
#      判据会把合法详情页记录**整组清零** 10/10）。它给的替代判据是**合取**——
#      `nested_group && deviant`（两个都是既有观测），实测 `r23nested` 排除 1 条（对）、
#      `r24detail` 排除 0 条（对）。本轮**不动判据**（判据改 `count`/记录集合，属新原则，
#      而它预告的失效点 headSig 污染还没量完），只把「详情页不许被误伤」钉成回归：
#      **将来谁想加排除判据，先把这条跑红**。同时钉住 `<3 不触发` 与「文档序第一的垃圾块不污染签名」
#      两条边界（后者是我这轮实测的：单个前置垃圾块下，家族逻辑仍挑主导族，`deviants=0`）。
try:
    def _r24(path):
        s_ = open_session(f"{BASE}/{path}")
        try:
            return act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                     "limit": 100, "max_pages": 4, "wait_ms": 300})
        finally:
            close_session(s_["session_id"])

    det = _r24("r24detail.html?subs=3")          # 合法嵌套：记录**应该**含子列表
    two = _r24("r24detail.html?subs=2")          # 子项 <3 ⇒ 不触发嵌套判定
    mix = _r24("r24detail.html?subs=3&mix=1")    # 垃圾块排在文档序第一（评审 D1 的失效点）
    ed, e2, em = (x.get("end_evidence") or {} for x in (det, two, mix))

    def _marks24(r_):
        return sum(1 for x in (r_.get("records") or [])
                   if x.get("_src", {}).get("nested_group"))

    def _garbage24(r_):
        return [x for x in (r_.get("records") or []) if "相关推荐" in (x.get("text") or "")]

    ok115 = (
        # ① 详情页：10 条一条不许少、照旧 exhausted；嵌套标记照给（10/10）——**绊线**
        det.get("count") == 10 and det.get("stop_reason") == "exhausted"
        and ed.get("total") == 10 and _marks24(det) == 10
        and (det.get("record_shape") or {}).get("deviant") == 0
        # ② 边界：子项 <3 不触发嵌套判定
        and two.get("count") == 10 and _marks24(two) == 0
        # ③ 失效点：文档序第一的垃圾块**没有**污染签名（我实测：家族逻辑仍挑主导族）
        and mix.get("count") == 10 and (mix.get("record_shape") or {}).get("deviant") == 0
        and not _garbage24(mix) and _marks24(mix) == 10)
    check("T115 详情页（合法嵌套）不许被误伤 + 嵌套阈值的两条边界", ok115,
          f"详情页: count={det.get('count')} {det.get('stop_reason')} 标记={_marks24(det)}/10 "
          f"deviant={(det.get('record_shape') or {}).get('deviant')}；"
          f"subs=2: 标记={_marks24(two)}；混合页: count={mix.get('count')} "
          f"垃圾行={len(_garbage24(mix))} 标记={_marks24(mix)}")
except Exception as e:  # noqa: BLE001
    check("T115 详情页（合法嵌套）不许被误伤 + 嵌套阈值的两条边界", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T116 **第二十五轮评审 A 的实测 S（静默丢数据）**：记录模板**允许变体**时，`max_gap=1` 的贪心合并
#      把「完整记录」当成前一条的**续行**吞掉。形状（复刻夹具 `test_site/r25mix.html?subvar=1`）：
#      卡片合法地含子列表、而子列表项数逐条交替 3/2/3/2 ⇒ 两种签名交替，恰好满足
#      「一个续行族 + 严格交替」这道合并门槛 ⇒ 实测 5 条收成 3 条（两页 10 → 6），
#      两条记录的文本被拼进一个字段值，而 `deviant=0`/`data=ok`/`needs_confirm` 不响。
#      修法：**续行里带链接的一律不并**（真记录几乎必带链接，典型续行——标签行/副信息行——不带）；
#      该并不并 ⇒ 多出一条记录（deviant/needs_confirm 会响），该不并却并了 ⇒ **静默丢数据**。
#      实测：变体档不再合并（改为 `needs_confirm` 问调用方，`merge.merged=0`、无 `_src.extra`），
#      且带 `deviants="keep"` 时 **5 条一条不少**；`subvar=0` 正例仍 5 条。
try:
    def _r25(path, **extra):
        s_ = open_session(f"{BASE}/{path}")
        try:
            return act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                     "limit": 100, "max_pages": 1, "wait_ms": 300,
                                                     **extra})
        finally:
            close_session(s_["session_id"])

    var = _r25("r25mix.html?subvar=1")                       # 变体：旧行为静默 5→3
    keep = _r25("r25mix.html?subvar=1", deviants="keep")     # 调用方表态「变体照收」
    ctl = _r25("r25mix.html?subvar=0")                       # 正例：模板一致
    head = _r25("r25mix.html?mix=3&pos=first")                # B：块在头（旧行为完全静默）
    tail = _r25("r25mix.html?mix=3&pos=last")                 # B：块在尾（一直会响）

    def _extras(r_):
        return [x.get("_src", {}).get("extra") for x in (r_.get("records") or [])]

    def _two_titles(r_):
        return [x for x in (r_.get("records") or [])
                if (x.get("text") or "").count("条记录") >= 2]

    _hc = ((head.get("containers") or [{}])[0])
    ok116 = (
        # ① 变体档：不许再有「被并进来的续行」（旧行为 3 条里两条带 extra）
        var.get("count") == 0 and var.get("stop_reason") == "needs_confirm"
        and not any(_extras(var)) and not _two_titles(var)
        and ((var.get("containers") or [{}])[0].get("merge") or {}).get("merged") == 0
        # ② 调用方表态后一条不少（5 条，且没有一条的文本里挤着两条记录）
        and keep.get("count") == 5 and not _two_titles(keep) and not any(_extras(keep))
        # ③ 正例：模板一致时照旧 5 条、无合并
        and ctl.get("count") == 5 and not any(_extras(ctl))
        # ④ B：头部的块**必须留痕**（旧行为：既不进 records 也不进 deviants，完全静默）
        and _hc.get("lead_siblings") == 3
        and len(_hc.get("lead_dropped") or []) == 3
        and all("相关推荐" in (d.get("text") or "") for d in (_hc.get("lead_dropped") or []))
        and all(d.get("signature") for d in (_hc.get("lead_dropped") or []))
        # 尾侧保持原样（会响），头尾至少都在回包里有痕迹
        and tail.get("stop_reason") == "needs_confirm"
        and (tail.get("record_shape") or {}).get("deviant") == 3)
    check("T116 记录变体不许被静默合并（S）+ 头部被丢的块必须留痕", ok116,
          f"变体档: {var.get('stop_reason')}/{var.get('count')} extra={_extras(var)}；"
          f"keep: {keep.get('count')} 条 双标题行={len(_two_titles(keep))}；"
          f"正例: {ctl.get('count')}；"
          f"头部: lead_siblings={_hc.get('lead_siblings')} lead_dropped={len(_hc.get('lead_dropped') or [])}；"
          f"尾部: {tail.get('stop_reason')}/deviant={(tail.get('record_shape') or {}).get('deviant')}")
except Exception as e:  # noqa: BLE001
    check("T116 记录变体不许被静默合并（S）+ 头部被丢的块必须留痕", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T117 **第二十九轮：把「续行带链接不并」这条修法的**代价**固定下来**（对抗评审第二十六轮 B）。
#      形状（复刻夹具 `test_site/r26pair.html`）：**干净的两行一条**（严格交替，合并门槛真的开火），
#      只改「续行带不带链接」一件事。实测代价：`contlink=1` 时条数**翻倍**（5→10），
#      而 `deviant=0` / `needs_confirm` 不响 —— 按规则层第 6 条（结构规则「错的时候响不响」），
#      这个「错」必须留痕，所以新增 **`merge.refused_link_continuations`**（因为带链接被拒并的续行数）。
#      本测试两档都钉：正例不许坏（`contlink=0` 合并照旧）+ 代价必须可见（`contlink=1` 拒并且计数=5）。
try:
    def _r26(path, **extra):
        s_ = open_session(f"{BASE}/{path}")
        try:
            return act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                     "limit": 100, "max_pages": 1, "wait_ms": 300,
                                                     **extra})
        finally:
            close_session(s_["session_id"])

    ok_pair = _r26("r26pair.html?contlink=0")     # 正例：续行不带链接 ⇒ 正常合并
    no_pair = _r26("r26pair.html?contlink=1")     # 代价档：续行带链接 ⇒ 拒并（可见）
    keep_pair = _r26("r26pair.html?contlink=1", deviants="keep")   # 调用方表态 ⇒ 拿到全部元素
    mp = ((ok_pair.get("containers") or [{}])[0].get("merge") or {})
    mn = ((no_pair.get("containers") or [{}])[0].get("merge") or {})
    # 实测（修好夹具、让那道闸真的开火之后）：拒并 ⇒ 那些续行各自成组 ⇒ 全成 deviant ⇒
    # `needs_confirm` **可见**（不是静默翻倍）；调用方带 deviants="keep" 时才拿到 2× 条数。
    ok117 = (ok_pair.get("count") == 5 and mp.get("merged") == 5
             and not mp.get("refused_link_continuations")
             and mn.get("refused_link_continuations") == 5          # ①代价可见（本轮修法）
             and no_pair.get("stop_reason") == "needs_confirm"      # ②代价会响，不静默
             and (no_pair.get("record_shape") or {}).get("deviant") == 5
             and keep_pair.get("count") == 10)                      # ③表态后拿到 2×（可逆）
    check("T117 「续行带链接不并」的代价必须可见（refused_link_continuations）", ok117,
          f"正例: count={ok_pair.get('count')} merged={mp.get('merged')} "
          f"refused={mp.get('refused_link_continuations')}；"
          f"代价档: count={no_pair.get('count')} merged={mn.get('merged')} "
          f"refused={mn.get('refused_link_continuations')} deviant="
          f"{(no_pair.get('record_shape') or {}).get('deviant')}")
except Exception as e:  # noqa: BLE001
    check("T117 「续行带链接不并」的代价必须可见（refused_link_continuations）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T118 **第三十一轮：合并闸的**代价面**绊线（对抗评审第二十七轮的 `r28gate` 复刻）**。
#      形状（夹具 `test_site/r28gate.html`）：记录 `li.row > a.title[href]`、续行 `li.cont > span`（无链接），
#      但 **i===1 那条故意不配续行**；`spon=1` 时在指定位次插一个**外来块** `li.spon > span`
#      （同标签、同签名、无链接）——即"看着像记录、其实不是"的那种块。
#      判据原文：合并门槛 =「续行只有一个族」且「整体按下标奇偶严格交替（容错 20%）」。
#      本轮原本要把它换成「`K = max_gap + 1` 个连续元素里不许有两个 head」；**先量之后前提不成立**
#      （见下）⇒ 不落任何补丁，改为把实测出来的真契约钉死。
#      ⚠️ **第三十一轮的实测把上面两条「缺陷」都推翻了**（先量再钉的价值就在这里）：
#      ① `spon=0` 这一档**本身就是破节奏的**（`i=1` 故意缺续行 ⇒ `row1`/`row2` 两个 head 相邻），
#         它不合并、直接 `needs_confirm`+`count=0`+`deviant=3`（=3 条孤儿续行）是**该有的 fail-closed**，
#         不是"假响"；要让这一档合并，就得换成"只要续行前面有 head 就成"的宽判据（孤儿判据），
#         而那条判据会把**野外实测过**的 `A B A A B`（serp 4→2 那种半量）重新并掉 ⇒ 反例优先级不够，不许落地。
#      ② `spon=1&sponpos=2` 的 `count=4` **正是真值**（外来块没被算成一条记录），
#         而且被并进来的那块**可对账**：`records[1]._src.extra=["a8"]`（同形续行 `a6/a10/a12` 亦然）——
#         它是"续行槽位被同结构的外来块占了"，结构上与续行**不可区分**，属已知代价而非可修缺陷。
#      ⇒ 本测试钉**实测出来的真契约**（三档，全部为已量值）：破节奏必须**响**（不许静默给个数）；
#      落在续行槽位的外来块**不许改变条数**且**必须留痕可对账**；其余位次必须继续 fail-closed。
#      ⚠️ **机制说准**（对抗评审第 28 轮裁定的措辞要求）：`spon=0` 之所以响，是**奇偶节奏被「缺一条
#      续行」打断** ⇒ 合并门槛不过 ⇒ 续行全成孤儿 ⇒ `deviant=N-1` ⇒ `needs_confirm`；
#      **不是**这条闸识别出了「缺续行」这个语义。结论对、理由也要对——否则下一个人会以为它在防
#      「缺续行」而不敢动它。
#      （同轮裁定：① `pos=2` 归**已知代价**；②「`K` 个连续元素里不许有两个 head」那条建议，对方
#      **撤回**——它在 `pos=2` 不触发，却会在「缺一条续行」的合法列表上开火；③ 本测试按裁定钉全：
#      `spon=0` 的**规模律** `deviant == N-1`（n=4/5/6/7/8/10）与 `pos=2` 的 n=6/n=8 档。）
try:
    def _r28(path, **extra):
        s_ = open_session(f"{BASE}/{path}")
        try:
            return act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                     "limit": 100, "max_pages": 1, "wait_ms": 300,
                                                     **extra})
        finally:
            close_session(s_["session_id"])

    broken4 = _r28("r28gate.html?n=4&spon=0")               # 破节奏档：i=1 缺续行 ⇒ 两个 head 相邻
    broken6 = _r28("r28gate.html?n=6&spon=0")
    # 规模律档（评审第 28 轮给的期望值，本轮先自己量过再钉）：deviant 恒 = N-1
    scaling = [(n, _r28(f"r28gate.html?n={n}&spon=0")) for n in (5, 7, 8, 10)]
    decoy4 = _r28("r28gate.html?n=4&spon=1&sponpos=2")      # 外来块正落在**续行槽位**（同标签同签名）
    decoy6 = _r28("r28gate.html?n=6&spon=1&sponpos=2")
    decoy8 = _r28("r28gate.html?n=8&spon=1&sponpos=2")
    loud = [_r28(f"r28gate.html?n=4&spon=1&sponpos={p}") for p in (1, 3, 4)]

    def _deviant(r_):
        return (r_.get("record_shape") or {}).get("deviant") or 0

    def _glued(r_):
        """被并进续行的元素（`_src.extra`）逐条端出来：[(记录下标, [元素 id])]。"""
        out = []
        for i, x in enumerate(r_.get("records") or []):
            ex = (x.get("_src") or {}).get("extra") or []
            if ex:
                out.append((i, ex))
        return out

    def _own_block(r_, i):
        return ((r_.get("records") or [])[i].get("_src") or {}).get("block")

    ok118 = (
        # ① 破节奏（头相邻）：必须**响**并拒答 —— 不许静默给出一个数（这是 fail-closed 的下界）
        broken4.get("stop_reason") == "needs_confirm" and broken4.get("count") == 0
        and _deviant(broken4) == 3                       # 3 = 三条孤儿续行，与实测一致
        and broken6.get("stop_reason") == "needs_confirm" and broken6.get("count") == 0
        and _deviant(broken6) == 5
        # ①′ 规模律（裁定要求钉全）：deviant == N-1、count==0、data 必须是 suspect
        and all(x.get("stop_reason") == "needs_confirm" and x.get("count") == 0
                and _deviant(x) == n - 1 and x.get("data") == "suspect" for n, x in scaling)
        # ② 外来块落进续行槽位：**条数必须仍是真值**（4/6/8）——外来块没被算成一条记录
        and decoy4.get("count") == 4 and decoy6.get("count") == 6 and decoy8.get("count") == 8
        # ②′ 而且服务端把它判成 **ok**（真值给得出，就不该降级成 suspect）
        and decoy4.get("data") == "ok" and decoy8.get("data") == "ok"
        # ③ 但**必须留痕可对账**：被并的那块 id 端出来，且不等于记录自己的块 id
        and len(_glued(decoy4)) == 4
        and all(ex and ex[0] != _own_block(decoy4, i) for i, ex in _glued(decoy4))
        and len(_glued(decoy6)) == 6 and len(_glued(decoy8)) == 8
        # ④ 落进槽位的那块在**文本**里仍看得见（证据原样暴露，不是被抹掉）
        and any("赞助商" in (x.get("text") or "") for x in (decoy4.get("records") or []))
        # ⑤ 块没落进续行槽位的位次（1/3/4）：必须继续 fail-closed，不许退化成静默
        and all(x.get("stop_reason") == "needs_confirm" and _deviant(x) >= 1 for x in loud))
    check("T118 合并闸代价面：破节奏必须响 + 槽位内的外来块不改条数但必须可对账", ok118,
          f"破节奏: n=4→{broken4.get('stop_reason')}/{broken4.get('count')}/deviant={_deviant(broken4)} "
          f"n=6→{broken6.get('stop_reason')}/{broken6.get('count')}/deviant={_deviant(broken6)}；"
          f"规模律 deviant={[(_deviant(x)) for _, x in scaling]}（应为 N-1）data="
          f"{[x.get('data') for _, x in scaling]}；"
          f"槽位档: n=4→count={decoy4.get('count')}/{decoy4.get('data')} 留痕={len(_glued(decoy4))} "
          f"n=6→count={decoy6.get('count')} 留痕={len(_glued(decoy6))} "
          f"n=8→count={decoy8.get('count')} 留痕={len(_glued(decoy8))}；"
          f"其余位次 stop={[x.get('stop_reason') for x in loud]}")
except Exception as e:  # noqa: BLE001
    check("T118 合并闸代价面：破节奏必须响 + 槽位内的外来块不改条数但必须可对账", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T119 **第三十一轮的反例档**：`fill` 会说「写成功」，而页面可能是空的/被改写 —— **必须响**。
#      事实链（本轮实测，夹具 `test_site/r30typing.html`）：`fill` 的 `type_keys` 慢路径写完会读回
#      `el.value`（`core.py:2605`）并存进 `info["filled"]`（`:2621`），但**全库没有一处拿它跟调用方
#      要写的值对照**。实测把 `delay_ms` 压到 0（按键成串）时，计时敏感的页面（keyup 里做格式化/
#      校验的常见形态）把字符逐个丢掉 ⇒ 页面最终是**空值**，而回包**不报任何错**。
#      ⚠️ **机制要写准**（对抗评审第 32 轮的更正）：缺口**不是**"没报实际值"——`filled` 一直在报；
#      缺的是 ① **入参 `value` 从不回显** ② **没有"不一致"的判据**。所以这里断言的是**两样都补齐**。
#      ⚠️ **只钉与机器速度无关的档**：`delay_ms=0`（成串 ⇒ 任何机器都丢光，实测 10/10 丢）
#      ＋ `?eat=1`（第一次 input 恒定吃一个字符，与计时无关）；`delay_ms=5` 那类**中间档**是否丢字
#      随机器而变（我这边 0 丢、评审那边丢 3 个）⇒ 只当**复现证据**写进 README，**不进断言**
#      （否则就是一条会随机变红的绊线）。
try:
    def _r30(page, value, **extra):
        s_ = open_session(f"{BASE}/{page}")
        try:
            box = next(b for b in s_["blocks"] if b.get("kind") == "type")
            return act_in_session(s_["session_id"], {"action": "fill", "selector": "#q",
                                                     "value": value, "type_keys": True,
                                                     "delay_ms": 40, **extra})
        finally:
            close_session(s_["session_id"])

    okv = _r30("r30typing.html?th=15", "ABCDEFGHIJ")                        # 正例：页面没改写
    eat = _r30("r30typing.html?th=0&upper=0&eat=1", "ABCDEFGHIJ")           # 确定性：被吃掉一个字符
    burst = _r30("r30typing.html?th=15&upper=0", "ABCDEFGHIJ", delay_ms=0)
    # ⚠️ **这一档只当"复现例子"，不进断言**（第三十一轮·被实跑抓住的错）：
    # 本机多次跑出 `filled=''`（全丢），但在另一位维护者机器上跑出 **`filled='DEFI'`（只丢 4 个）**——
    # 因为 `delay_ms=0` 只是"**通常**成串"，Playwright 逐键仍有开销，机器一忙**键间隔就可能越过夹具的
    # 15ms 阈值** ⇒ 丢多少是**墙钟判据**。把它写死进断言就是把"通常"当成"必然"。
    # ⚠️ 顺带自我批评：下面注释里我本来就写了"计时档不进断言"，却没把这条规矩用在自己身上——
    # `delay_ms=0` 与 `delay_ms=5` 只差"通常会/通常不会"，**都不是确定性判据**。
    legit = _r30("r30typing.html?th=0&upper=1", "abcdefghij")               # 合法改写（自动转大写）

    def _mm(r_):
        return r_.get("value_mismatch") or {}

    ok119 = (
        # ① 正例：`requested` 回显入参、`filled` 与之一致、**不许响**（不许误伤）
        okv.get("requested") == "ABCDEFGHIJ" and okv.get("filled") == "ABCDEFGHIJ"
        and not _mm(okv)
        # ② 确定性部分丢失 ⇒ **必须响**，且两个值都可对账（部分丢失比全空更危险：全空一眼看得出）
        and eat.get("filled") == "BCDEFGHIJ"
        and _mm(eat).get("requested") == "ABCDEFGHIJ" and _mm(eat).get("filled") == "BCDEFGHIJ"
        # ③ ~~成串全丢 ⇒ 必须响~~ **已删除**（见上面 `burst` 处的说明：`delay_ms=0` 只是"通常"成串，
        #   丢多少是墙钟判据 ⇒ 那一档只当复现例子，不进断言）。"部分丢失也必须响"由 ② 的确定性档覆盖。
        # ④ **合法改写也留痕**（自动转大写不算错，但工具不替调用方裁决）：一律报，判断权交出去
        and legit.get("filled") == "ABCDEFGHIJ" and _mm(legit).get("requested") == "abcdefghij")
    check("T119 写入值被页面改写时必须响（requested 回显 + value_mismatch）", ok119,
          f"正例: requested={okv.get('requested')!r} filled={okv.get('filled')!r} 响={bool(_mm(okv))}；"
          f"吃字符: filled={eat.get('filled')!r} 响={bool(_mm(eat))}；"
          f"成串全丢: filled={burst.get('filled')!r} 响={bool(_mm(burst))}；"
          f"合法改写: filled={legit.get('filled')!r} 响={bool(_mm(legit))}")
except Exception as e:  # noqa: BLE001
    check("T119 写入值被页面改写时必须响（requested 回显 + value_mismatch）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T120 **第三十一轮·A′**：被丢在文档序开头、但**本来就是一条记录**的块，不许**不留声**。
#      根因（对抗评审 D1 更正后本机复现）：`leadSiblings` 把开头那些"签名不属于主族"的兄弟划进 `lead_dropped`
#      ——**位置决定走哪条路**：开头档被它挡在门口（`count` 少 1、`data=ok`），中间/末尾档本来就 `needs_confirm`。
#      它**不是"静默消失"**（`lead_siblings` + `lead_dropped[].text` 全文可对账），真正的欠账只有一条：
#      **被丢的是一块"像记录的东西"而 `data` 仍是 `ok`**（`lead_dropped` 的设计假设是"分组标题/分隔行"）。
#      **判据（A′）**：主族记录头签名是它的**token 前缀**（＝"记录头 + 多长了点东西"），用 `sigTokens` 展开逐 token 比；
#      **不用**字符串前缀、**也不用**"签名里有没有 `a:link`"——`<h3><a>本周精选</a></h3>` 这类**带链接的组标题**
#      会骗过后者，而它与记录头在第一、二个 token 上就分开了（实测 T116 档 `h3:heading` vs `h4:heading`）。
#      ⚠️ 判据在 `.slice(0, 3)` **之前**算：`lead_dropped` 只留 3 条而 `lead_siblings` 没有上限。
#      落地前**先验证过会红**（9 条子句 5 条真）。
try:
    def _r31(page):
        s_ = open_session(f"{BASE}/{page}")
        try:
            return act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                     "limit": 100, "max_pages": 1, "wait_ms": 300})
        finally:
            close_session(s_["session_id"])

    def _ldr(r_):
        return ((r_.get("containers") or [{}])[0]).get("lead_dropped_records")

    l1 = _r31("r31nestedmix.html?n=4&mixed=1&at=0")      # 要修的档：被丢的是"记录"
    l2 = _r31("r31nestedmix.html?n=5&mixed=2&at=0")
    clean = _r31("r31nestedmix.html?n=4&mixed=0")         # 干净档
    inj = _r31("r25mix.html?mix=3&pos=first")             # T116 档：组标题形状（**不许误伤**）
    mid = _r31("r31nestedmix.html?n=4&mixed=1&at=2")      # 中间位：本就 fail-closed
    _ld = ((l1.get("containers") or [{}])[0].get("lead_dropped") or [{}])[0]
    ok120 = (
        # ① 开头档：计数可见 + `data` 降级（响的那一半与可对账的那一半都在）+ `count` 不变 + 全文仍在
        _ldr(l1) == 1 and l1.get("data") == "suspect" and l1.get("count") == 3
        and "第 0 条记录" in (_ld.get("text") or "")
        # ② 两条被丢 ⇒ 计数 2
        and _ldr(l2) == 2 and l2.get("data") == "suspect"
        # ③ 干净档不许误伤（`data` 仍 ok、计数 0）
        and clean.get("data") == "ok" and not _ldr(clean)
        # ④ T116 档（组标题形状：`h3:heading` vs `h4:heading`）**不许误伤**
        and inj.get("data") == "ok" and not _ldr(inj)
        # ⑤ 中间/末尾位（`lead_siblings=0`，判据走不到）必须保持 fail-closed，不许变成"静默交付 3 条"
        and mid.get("stop_reason") == "needs_confirm")
    check("T120 开头被丢的块若是「记录」必须响（lead_dropped_records + data 降级）", ok120,
          f"开头档: count={l1.get('count')} data={l1.get('data')} 计数={_ldr(l1)}；"
          f"两条档: 计数={_ldr(l2)} data={l2.get('data')}；"
          f"干净: data={clean.get('data')} 计数={_ldr(clean)}；"
          f"T116: data={inj.get('data')} 计数={_ldr(inj)}；中间: {mid.get('stop_reason')}")
except Exception as e:  # noqa: BLE001
    check("T120 开头被丢的块若是「记录」必须响（lead_dropped_records + data 降级）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T121 **第三十一轮·续**：`id`+`text` 那条写值路由（`_typing_params()` 的**长文本 8ms 地板**）静默截短。
#      反例（对抗评审造出，本机复现）：`_typing_params()`（`core.py:2343`）对短文本保持人类节奏
#      （`randint(50,120)`），但 `L*delay > 12000` 时压到 `max(8, 12000 // L)` —— **长文本必然落到 8ms 地板**；
#      而**靠 keyup 做校验/自动补全的站点恰恰就是调用方要 `type_keys` 的那类站点**，它们在按键过快时丢字符
#      ⇒ 页面收到的文本被**静默截短**，且这条路由**什么都不报**（连 `filled` 都没有，比 T119 那条更彻底）。
#      实测（夹具 `r30typing.html?th=15&upper=0`）传 1500 字 ⇒ 页面只落下 **1358** 字。
#      修法：与 `fill` **同形**的回执（`requested` + 读回 `filled` + 不等时 `value_mismatch`），
#      两条写值路的**回执形状对齐**；**故意不加 `delay_ms` 旋钮**（那是把"预算算错"变成"调用方调错"）。
#      ⚠️ **只钉"响不响"与"filled 与入参是否相等"**，**不钉丢了多少字**：丢字量与时序有关
#      （评审那边 900→879 / 1100→995 / 1500→1357，本机 1500→1358，不是单调常量）。
#      落地前**先验证过会红**（6 条子句 2 条真）。
try:
    def _r32(page, text, **extra):
        s_ = open_session(f"{BASE}/{page}")
        try:
            box = next(b for b in s_["blocks"] if b.get("kind") == "type")
            return act_in_session(s_["session_id"], {"id": box["id"], "text": text,
                                                     "type_keys": True, **extra})
        finally:
            close_session(s_["session_id"])

    def _mm121(r_):
        return r_.get("value_mismatch") or {}

    LONG121 = "a" * 1500
    SHORT121 = "a" * 700
    # ⚠️ **确定性档**（本条的断言只认这条）：`?eat=1` 在第一次 `input` 恒定吃一个字符，
    # **与按键间隔/机器速度无关** ⇒ `filled` 必然比入参少一个字符、必然响。
    eat121 = _r32("r30typing.html?th=0&upper=0&eat=1", "ABCDEFGHIJ")
    # ⚠️ **复现档，不进断言**：长文本会落到 `_typing_params()` 的 8ms 地板 ⇒ **通常**会丢字，
    # 但"丢多少/丢不丢"是**墙钟判据**（机器一忙，键间隔就越过夹具的 15ms 阈值）——
    # 与 T119 的 `delay_ms=0` 同病，**只当 README 的复现例子**。
    ldrop = _r32("r30typing.html?th=15&upper=0", LONG121)
    sdrop = _r32("r30typing.html?th=15&upper=0", SHORT121)    # 短文本 ⇒ 50~120ms ⇒ 不丢
    ndrop = _r32("r30typing.html?th=0&upper=0", LONG121)      # 站点无阈值 ⇒ 不丢（正例）
    ok121 = (
        # ① 确定性档：`requested` 回显入参（正例也有，不是只在出错时出现）
        eat121.get("requested") == "ABCDEFGHIJ"
        # ② 确定性档：`filled` 是页面**真实落下**的值（少一个字符），且**必须响**、两个值都可对账
        and eat121.get("filled") == "BCDEFGHIJ"
        and _mm121(eat121).get("requested") == "ABCDEFGHIJ"
        and _mm121(eat121).get("filled") == "BCDEFGHIJ"
        # ③ 短文本（不丢）**不许误伤**
        and not _mm121(sdrop)
        # ④ 站点无阈值（不丢）**不许误伤**，但 `requested` 仍要在
        and not _mm121(ndrop) and ndrop.get("requested") == LONG121)
    check("T121 id+text 路由：页面改写写入值时回执必须齐（requested + filled + value_mismatch）", ok121,
          f"确定性档: filled={eat121.get('filled')!r} 响={bool(_mm121(eat121))}；"
          f"短文本: 响={bool(_mm121(sdrop))}；无阈值: filled 长度={len(ndrop.get('filled') or '')} "
          f"响={bool(_mm121(ndrop))}；"
          f"（复现档·不进断言：长文本 filled 长度={len(ldrop.get('filled') or '')} 响={bool(_mm121(ldrop))}）")
except Exception as e:  # noqa: BLE001
    check("T121 id+text 路由：页面改写写入值时回执必须齐（requested + filled + value_mismatch）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T122 **第三十一轮·④**：**挂起连接不许拖满超时**（野外实证 + 本机复现）。
#      来源：40 站巡礼第 ④ 条——七个站全卡在 25.7–25.8s 而正文只有 0–818 字，"不像慢，像**等满超时**"。
#      本机复现（夹具 `r33hold.html?mode=hold`：5 条记录**立刻渲染** + 一个**永不返回**的 `fetch('/hold')`）：
#        `wait_until="networkidle"`（现状默认）在"有连接永不关闭"的页面上**永不成立**
#        ⇒ 白等满 caller 的整个 `timeout`（实测 11.2s / 10s 预算），而内容**早已渲染好**；
#        对照三种形状（无挂起 / 挂起 `fetch` / 挂起子资源）**内容逐字节相同** ⇒ 等静置纯粹是白等。
#      修法：默认档把"等静置"降级为**有界预算**（2500ms）；够 ⇒ `network_settled=true`；
#      不够 ⇒ **带标记交付**（`network_settled=false`）——**不是失败，是"交付了但没等到静置"**。
#      ⚠️ 调用方**显式**传别的 `wait_until` 时行为一字不改；⚠️ 标记**必须**跟内容一起回，
#      否则就是把"少等 15 秒"换成"悄悄少内容"（我们整轮在防的东西）。
#      落地前**先验证过会红**（4 条子句 1 条真：挂起档 11.2s、无标记）。
try:
    def _r33(mode):
        t0 = time.time()
        d = parse_url(f"{BASE}/r33hold.html?mode={mode}", timeout=10000)
        return (time.time() - t0,
                [b.get("text", "") for b in (d.get("blocks") or [])],
                (d.get("stats") or {}).get("network_settled"))

    dt_c, tx_c, st_c = _r33("none")      # 对照：无挂起请求
    dt_h, tx_h, st_h = _r33("hold")      # 挂起档：一个永不返回的 fetch
    ok122 = (
        # ① 挂起档**不许烧满 timeout**（现状 ≈10s；修后 ≈3.5s）。阈值给足余量，只钉"没烧满"。
        dt_h < 6.0
        # ② 挂起档交付的**内容**与对照档**逐字节相同**（有界等静置不许丢内容）
        and tx_h == tx_c and len(tx_h) >= 6
        # ③ 挂起档必须**显式标出**"没等到静置"
        and st_h is False
        # ④ 对照档必须标真（不许把所有档都标成没静置 = 假响）
        and st_c is True)
    check("T122 挂起连接不许拖满超时（有界等静置 + network_settled 标记）", ok122,
          f"对照: {dt_c:.1f}s settled={st_c} 块={len(tx_c)}；"
          f"挂起: {dt_h:.1f}s settled={st_h} 块={len(tx_h)}；内容相同={tx_h == tx_c}")
except Exception as e:  # noqa: BLE001
    check("T122 挂起连接不许拖满超时（有界等静置 + network_settled 标记）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T123 **第三十一轮·①**：**只读的可点性查询**（`{"action":"probe","id":...}`）。
#      为什么要有它（本轮先量出来的三个缺口）：`_preclick` 只在"已经要点了"时才报
#      `blocked_by=overlay:<tag.class>`，而且 **`_HIT_JS` 只取中心一个点** ⇒
#      **「全屏遮罩」与「只压住这个元素的普通 div」报同一个值**（`overlay:div`）——**处置相反、信号相同**（假同源）；
#      调用方也**无法先问后点**（唯一的杠杆是 `overlay_off`）。
#      `probe` 只回答三件**事实**：① 分类 `fullscreen | partial` ② 几何（目标/覆盖者 rect ＋ 被盖比例）
#      ③ 覆盖者身份（tag/id/class/`pointer-events`）。**只观测：不改 DOM、不点击、不裁决**——
#      r98 亲手把「页内点击兜底」从默认打开改回**显式开启**，正因为那会把 `_preclick` 的审计静默吃掉。
#      ⚠️ **两处期望是实测修正的，不是"设计想要的"**：
#        · `canvas` 档报 `partial`——`elementFromPoint` 命中的**最上层是 canvas 自己**（300×200），
#          这是**准确的事实**（挡住你的是那个 canvas，不是整个遮罩）；要报 `fullscreen` 需另加
#          "父级是否全屏"的判据，本轮**没有反例要求它，故不做**；
#        · 夹具第一版把 3000px 占位块放在按钮**之后** ⇒ 按钮其实还在视口内，靶子期望写错；已改为占位块在**之前**。
try:
    def _r34(mode):
        s_ = open_session(f"{BASE}/r34occl.html?mode={mode}")
        try:
            tgt = next((b for b in s_["blocks"] if b.get("type") == "action"
                        and b.get("label") == "目标按钮"), None)
            return act_in_session(s_["session_id"], {"action": "probe",
                                                     "id": tgt["id"] if tgt else "a0"})
        finally:
            close_session(s_["session_id"])

    def _p(mode):
        pr = _r34(mode).get("probe") or {}
        return (pr.get("covered"), (pr.get("cover") or {}).get("kind"),
                (pr.get("target") or {}).get("in_viewport"), pr)

    c_none, k_none, v_none, _ = _p("none")
    c_back, k_back, _, _ = _p("backdrop")
    c_canv, k_canv, _, p_canv = _p("canvas")
    c_cover, k_cover, _, p_cover = _p("cover")
    c_zero, _, v_zero, _ = _p("zero")
    c_off, _, v_off, _ = _p("offscreen")
    ok123 = (
        # ① 无遮挡：不报被盖、在视口内
        c_none is False and k_none is None and v_none is True
        # ② **全屏遮罩 vs 只压住一块**：这两档必须**分开**（这正是修前报同一个 `overlay:div` 的地方）
        and c_back is True and k_back == "fullscreen"
        and c_cover is True and k_cover == "partial"
        # ③ 几何：被盖档必须给出覆盖者 rect 与"被盖住的采样点比例"（>0）
        and isinstance((p_cover.get("cover") or {}).get("rect"), list)
        and (p_cover.get("covered_points") or 0) > 0
        and (p_cover.get("coverage") or 0) > 0
        # ④ 身份：覆盖者要有 tag，且 `pointer-events` 如实报出（Live2D 那类坑就靠它）
        and (p_canv.get("cover") or {}).get("tag") == "canvas"
        and (p_canv.get("cover") or {}).get("pointer_events") in ("auto", "none")
        # ⑤ `0×0` 与视口外**不许**被误判成"被盖"；视口外必须如实标出（调用方据此决定要不要先滚）
        and c_zero is False and v_zero is True
        and c_off is False and v_off is False)
    check("T123 只读可点性查询：全屏遮罩与局部覆盖必须分开（probe）", ok123,
          f"none: covered={c_none} kind={k_none!r}；backdrop: {k_back!r}；canvas: {k_canv!r}"
          f"（命中 canvas 自身 ⇒ partial）；cover: {k_cover!r}；"
          f"被盖比例={p_cover.get('coverage')}；offscreen: in_viewport={v_off}")
except Exception as e:  # noqa: BLE001
    check("T123 只读可点性查询：全屏遮罩与局部覆盖必须分开（probe）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T124 **第三十一轮·② 候选 2**：`stats.approx_tokens` 必须**在截断之后**重算。
#      反例（SylviaMuse 40 站巡礼报的、本机复现）：`_with_stats` 算一次之后**再没人重算它**，
#      而 `max_chars` 的截断发生在它之后 ⇒ 交付体被裁短了、它还报**截断前**的体量：
#      实测 3000 条记录 + `max_chars=2000` ⇒ `json_chars=748` 而 `approx_tokens=275950`
#      （748 个字符不可能值 27.6 万 token）。
#      修法：把它并进 `refresh_stats` 那个"迭代到不动点"的循环——**两个数互相影响**
#      （它进 `stats` ⇒ 改它会改 payload 长度），所以必须**一起收敛**，不能各算各的。
#      ⚠️ 同一轮还发现**同一个病在新位置复发**：`parse_url` 在 `_finish` 之后才写
#      `stats.network_settled` ⇒ 又把它写脏（实测 186 vs 重算 192）⇒ 追加字段后**必须再收口一次**。
#      断言形状（Lumen 给的）：**对"最终交付体"用同一公式重算**，而不是对中间体。
try:
    def _big_page():
        rows = "".join(f'<li class="card"><a class="title" href="?i={i}">第 {i} 条记录</a>'
                       f'<p class="meta">标签 {i} 正文正文正文正文正文正文正文正文正文正文</p></li>'
                       for i in range(3000))
        return ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>b</title>"
                f"</head><body><ul id='l'>{rows}</ul></body></html>").encode("utf-8")

    _BIG = HERE / "test_site" / ".tmp_big_t124.html"   # `.tmp_` 前缀：打包排除 + 编码门禁跳过，且不必纳管
    _BIG.write_bytes(_big_page())
    d124 = parse_url(f"{BASE}/.tmp_big_t124.html", timeout=20000, max_chars=2000)
    st124 = d124.get("stats") or {}
    payload124 = json.dumps(d124, ensure_ascii=False)
    cjk124 = sum(1 for ch in payload124
                 if "\u3000" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef")
    re124 = cjk124 + -(-(len(payload124) - cjk124) // 4)
    ok124 = (st124.get("approx_tokens") == re124
             and st124.get("json_chars") == len(payload124)
             and (st124.get("approx_tokens") or 0) < 100000)   # 不许再是"截断前"的量级
    check("T124 approx_tokens 必须在截断后重算（对最终交付体重算必须相等）", ok124,
          f"json_chars={st124.get('json_chars')}（实际 {len(payload124)}）"
          f" approx_tokens={st124.get('approx_tokens')} 重算={re124}")
except Exception as e:  # noqa: BLE001
    check("T124 approx_tokens 必须在截断后重算（对最终交付体重算必须相等）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T125 **第三十一轮·② 门禁牙齿**：`check_encoding.py --path` 让这道门禁**可被无副作用地测**。
#      为什么需要 `--path`（对抗评审 Lumen 的改前基线）：`check_encoding.py` 原本**没有 argparse**
#      ⇒ 多给的参数**被静默忽略** ⇒ `--path <tmp>` 时它**照旧扫仓库根、rc=0**
#      ——**"静默扫错了地方"，比报错更坏**（那是"一个静默的错答案"）。
#      ⚠️ **三条断言必须联立**（Lumen 的改前基线实测：绿的那两格正是对照组）：
#        · ①「1+2」：BOM 的 `.py` ⇒ rc=1 且**点名**；**同一目录去掉 BOM ⇒ rc=0**
#          （第 2 格单独在"什么都没实现"时也是绿的 ⇒ 拆开就是**装饰**）；
#        · ②「3+4」：`--path` 指向非仓库根 ⇒ rc=0 **且报"跳过"**；**不带 `--path` ⇒ 报"已执行"**
#          （这两句话**互斥、是稳定片段、不影响 rc**——按"结果域封闭"，**跳过要说出来**）；
#        · ③「5+5b」：`.cmd` 的 BOM 由**`.cmd` 专用分支**报（文案「批处理带 BOM」），
#          夹具必须**无 BOM + CRLF**（用 LF 写会因"只有 LF 行尾"而红 ⇒ **红错事由，且藏在夹具里**）。
#      ⚠️ 断言依赖**文案**是**权宜**：② 给 rule 发 `rule_id` 之后升级成 id（**升级是省维护，不是补安全**——
#      有人改措辞时这条断言会**响**，不是悄悄过期）。
try:
    import shutil
    import subprocess
    import tempfile

    def _ce(path=None):
        args = [sys.executable, str(HERE / "check_encoding.py")] + \
               (["--path", str(path)] if path else [])
        # ⚠️ **子进程的编码必须钉死**（照 T82 的做法）：父套件在 `--gbk` 模式下没有 PYTHONIOENCODING，
        # 子进程就会用控制台默认（GBK）输出，而这里按 utf-8 解 ⇒ **中文断言全变乱码**。
        # 实测（本轮）：不钉时 `--gbk` 腿 T125b/T125c 红、T125a 只匹配 ASCII 才侥幸过。
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        pr = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", cwd=str(HERE), env=env)
        return pr.returncode, (pr.stdout or "") + (pr.stderr or "")

    _tmp = Path(tempfile.mkdtemp(prefix="t125-"))
    try:
        _py = _tmp / "bom.py"
        _py.write_bytes(b"\xef\xbb\xbfx = 1\n")
        rc_a, out_a = _ce(_tmp)                       # ① 上半：BOM ⇒ 红且点名
        _py.write_text("x = 1\n", encoding="utf-8")
        rc_b, _ = _ce(_tmp)                           # ① 下半：去 BOM ⇒ 绿（对照组）
        ( _tmp / "clean.py").write_text("y = 2\n", encoding="utf-8")
        ( _tmp / "bom.py").unlink()
        rc_c, out_c = _ce(_tmp)                       # ② 干净非根目录 ⇒ 报"跳过"
        rc_d, out_d = _ce()                           # ④ 仓库根 ⇒ 报"已执行"
        _cmd = _tmp / "a.cmd"
        _cmd.write_bytes(b"\xef\xbb\xbf@echo off\r\nrem hi\r\n")
        rc_e, out_e = _ce(_tmp)                       # ③ 上半：`.cmd` 专用文案
        _cmd.write_bytes(b"@echo off\r\nrem hi\r\n")
        rc_f, _ = _ce(_tmp)                           # ③ 下半：无 BOM + CRLF ⇒ 绿（对照组）
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)

    check("T125a 门禁牙齿：BOM 的 .py ⇒ rc=1 且点名（去 BOM ⇒ rc=0，两半联立）",
          rc_a == 1 and "bom.py" in out_a and rc_b == 0,
          f"BOM: rc={rc_a} 点名={'bom.py' in out_a}；去 BOM: rc={rc_b}")
    check("T125b 门禁牙齿：--path 作用域只含文件扫描（非根报『跳过』、仓库根报『已执行』）",
          rc_c == 0 and "版本一致性检查：本次跳过" in out_c and "版本一致性检查：已执行" not in out_c
          and rc_d == 0 and "版本一致性检查：已执行" in out_d and "本次跳过" not in out_d,
          f"非根: rc={rc_c} 跳过={'本次跳过' in out_c}；仓库根: rc={rc_d} 已执行={'已执行' in out_d}")
    check("T125c 门禁牙齿：.cmd 的 BOM 由专用分支报（无 BOM + CRLF 的对照 ⇒ rc=0）",
          rc_e == 1 and "批处理带 BOM" in out_e and rc_f == 0,
          f"BOM .cmd: rc={rc_e} 专用文案={'批处理带 BOM' in out_e}；无 BOM+CRLF: rc={rc_f}")
except Exception as e:  # noqa: BLE001
    check("T125a 门禁牙齿：BOM 的 .py ⇒ rc=1 且点名（去 BOM ⇒ rc=0，两半联立）", False,
          f"{type(e).__name__}: {str(e)[:90]}")
    check("T125b 门禁牙齿：--path 作用域只含文件扫描（非根报『跳过』、仓库根报『已执行』）", False,
          f"{type(e).__name__}: {str(e)[:90]}")
    check("T125c 门禁牙齿：.cmd 的 BOM 由专用分支报（无 BOM + CRLF 的对照 ⇒ rc=0）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T126 **第三十一轮·② 的门禁接线**：`check_subproc_encoding.py`（跨项目根工具面，Lumen 建、我接）。
#      它管的是一类**只在某一个环境里显形**的错：**进程/管道边界上的编码没被显式钉住**。
#      这一类在这条线上已撞三次（T82 的非 UTF-8 控制台、`--check-log` 的 UTF-16 重定向、T125 的子进程编码），
#      所以按"同类缺陷第二次撞到就补门禁"落了它。
#      ⚠️ **本测试自己那次 `subprocess` 必须显式钉编码**（否则门禁会点名 `test_v03.py` 自己，
#      本测试就**自相矛盾**；且 `--gbk` 腿会重演 T125 的乱码）——**这正是它要防的那件事**，
#      所以这里 `env=dict(os.environ, PYTHONIOENCODING="utf-8")` **不是可有可无的样板**。
#      ⚠️ **钉字样不钉数字**（第四问）：输出里的"审了 N 个 .py / M 处调用点"是**运行期记账**（分母会随轮次变），
#      **不许**写进断言；断言的只有"rc=0"与那句稳定结论。
#      ⚠️ **红路（断线试验，一次只拔一根线）**：临时拔掉 `package_zip.py` 那行 `encoding="utf-8"`
#      ⇒ 门禁必须 `problems=1` **且只有 `package_zip.py` 被点名**（"**且只有**"是必要条件：
#      两处一起出现时分不清是谁在响）；还原判据 = 门禁 rc 回 0 **＋** `grep` 确认那行在，
#      不是"我记得我还原了"。本测试**只断言绿**（红路是落地前的实验，不是常驻断言）。
try:
    _gate = HERE / "check_subproc_encoding.py"
    if not _gate.exists():
        raise FileNotFoundError(f"门禁不在：{_gate}")     # fail-closed：门禁没进工作副本不算过
    _gp = subprocess.run([sys.executable, str(_gate)], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", cwd=str(HERE),
                         env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    _gout = (_gp.stdout or "") + (_gp.stderr or "")
    ok126 = (_gp.returncode == 0
             and "没有「文本模式却没钉编码」的调用点" in _gout)
    check("T126 门禁接线：subprocess 编码门禁必须绿（且它没点名本测试自己）", ok126,
          f"rc={_gp.returncode} 结论行={'没有「文本模式却没钉编码」的调用点' in _gout} "
          f"点名 test_v03.py={'test_v03.py' in _gout}")
except Exception as e:  # noqa: BLE001
    check("T126 门禁接线：subprocess 编码门禁必须绿（且它没点名本测试自己）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T127 **第三十一轮·② 候选 1**：`collect` 的 `max_chars`（此前**根本不读**它 ⇒ 传了也静默不裁）。
#      修前的病：`_apply_max_chars` 只被 `_finish` 调用，而 `collect` 自己造回包 ⇒ **静默不裁**
#      （实测传 `max_chars=2000` 仍交付 527065 字节、`truncated=null`），而 docstring 承诺了它。
#      三条硬条件（对抗评审 Lumen 定的，比"标记要响"更要害的是第 2 条）：
#        ① **两个数都在**：`count`=实际交付、`records_total`=本次抓到；标记照抄 `coverage.truncated` 的形状；
#        ② **裁剪只能裁"交付"，不能裁"进度"**——`resume`/cursor/`next` 一律原样（否则续跑会从被裁处接上、
#           **静默丢掉中间那一段**，比"没裁"更坏）；
#        ③ **缺省不裁**（不传 `max_chars` 行为逐字不变）。
#      ⚠️ **它同时是"软上限"，这一层必须钉住**：实测 `cap=800` 时 25 条全丢、回包**仍 3996 字节**
#      （信封 `containers`/`dedupe`/`cursor` 本身就超 cap）⇒ 断言**只钉"账要平"**
#      （`records_dropped + records_delivered == records_total`），**不钉"体积 ≤ cap"**——
#      后者在 cap 小于信封时**不可能成立**，写进去就是一条"实现了也红"的判据（第二问）。
try:
    def _r37(**extra):
        s_ = open_session(f"{BASE}/collect_edge.html?mode=slowtwice&delay=0")
        try:
            return act_in_session(s_["session_id"], {"action": "collect", "fields": COLLECT_FIELDS,
                                                     "limit": 100, "max_pages": 5, "wait_ms": 300,
                                                     **extra})
        finally:
            close_session(s_["session_id"])

    _full37 = _r37()
    _cap37 = _r37(max_chars=800)
    _tr37 = _cap37.get("truncated") or {}
    _prog = ("resume", "pages", "stop_reason", "next")
    _same_prog = all(json.dumps(_full37.get(k), ensure_ascii=False, sort_keys=True)
                     == json.dumps(_cap37.get(k), ensure_ascii=False, sort_keys=True)
                     for k in _prog)
    ok127 = (
        # ③ 缺省不裁：行为逐字不变（无 truncated、count==len(records)）
        _full37.get("truncated") is None
        and _full37.get("count") == len(_full37.get("records") or [])
        # ① 两个数都在 + 标记形状照抄既有那套
        and _cap37.get("count") == len(_cap37.get("records") or [])
        and _cap37.get("records_total") == _full37.get("count")
        and {"reason", "cap", "order", "records_dropped", "records_total", "records_delivered"}
        <= set(_tr37)
        # 账要平（软上限下唯一恒真的不变量）
        and _tr37.get("records_dropped", 0) + _tr37.get("records_delivered", 0)
        == _tr37.get("records_total")
        and _tr37.get("records_delivered") == _cap37.get("count")
        # ② 进度没被裁：与未裁时逐字节相同
        and _same_prog)
    check("T127 collect 的 max_chars：只裁交付、不裁进度（两个数都在，账要平）", ok127,
          f"未裁: count={_full37.get('count')}；裁剪: count={_cap37.get('count')} "
          f"records_total={_cap37.get('records_total')} truncated={json.dumps(_tr37, ensure_ascii=False)[:90]} "
          f"进度相同={_same_prog}")
except Exception as e:  # noqa: BLE001
    check("T127 collect 的 max_chars：只裁交付、不裁进度（两个数都在，账要平）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T128 **发布包凭据门禁的判据边界**：主目录规则里，用户名位置上的**字面 `*`** 是通配符参数，不是人名。
#      出身：r189 打完包门禁红两处 `[个人主目录] C:\Users\*`（成员 `ai_browser/AGENT_BOARD.md`
#      3487 / 3507 行），原文是 `glob_paths(r"C:\Users\*\*")`——在讲**一个通配符参数**，与本机主目录无关。
#      为什么这条断言必须有：**抑制类改动最危险的方向是"顺手把真名字也放过"**，所以两个方向一起钉：
#        · 红→绿：通配符那条路径（盘符 + `\Users\` + 字面 `*`）不许再被判成个人主目录；
#        · 绿仍是绿：**真用户名**那两格（Windows / Linux 各一个）**照样要红**；
#        · 附带钉住"另两条分支本来就更窄"：`/home/*/x`、`/Users/*/x` 干净，是因为 `*` 匹配不上
#          那两条分支要求的 `[a-z]`/`[A-Za-z]` 首字符，**不是**因为白名单。
#          写进断言是为了拦住下一轮"顺手给它俩也加条豁免"——那会连真名字一起放过。
#      样本用**制造 zip**（不碰真发布包）：判据的边界该拿制造样本量出来，而不是等真包再漏一次。
#      ⚠️ **会红的样本必须"拼"出来，不许写成字面量**：门禁扫的是**包内文件内容**，而这个测试文件
#      自己就在包里——r190 实测：字面量版本让发布包当场红 6 处，其中 4 处就是下面这几行。
#      拼装**不是躲检查**：判据读的是内容，拼出来的串一旦进了包还是那个形态、照样会被扫到（T129 查这个）。
try:
    import zipfile as _zf

    from check_package_secrets import scan as _scan_secrets

    _BS = "\\"                     # 一个反斜杠。样本逐段相拼，理由见上
    _samples128 = {
        "wild_windows.md": 'glob_paths(r"C:' + _BS + 'Users' + _BS + '*' + _BS + '*")',
        # ↑ 路径里写**一个**反斜杠的写法（r189 板上撞的那种）
        "wild_escaped.md": 'glob_paths(r"C:' + _BS * 2 + 'Users' + _BS * 2 + '*' + _BS * 2 + '*")',
        # ↑ **源码里的转义写法**（两个反斜杠）：r194 在 Lumen 的 `ai_shell/fs.py:313`、
        #   `test_ai_shell.py:403` 全仓口径量到的就是这一种。r190 只豁免了单反斜杠写法 ⇒
        #   那是"按反斜杠**个数**判"，而判据该按"**用户名位置上是不是通配符**"判。
        "real_windows.md": "path C:" + _BS + "Users" + _BS + "user" + _BS + "x here",
        "real_linux.md": "path /home/" + "zhangsan" + "/x here",
        "wild_linux.md": "path /home/*/x here",
        "wild_mac.md": "path /Users/*/x here",
    }
    with tempfile.TemporaryDirectory() as _td128:
        _mz128 = Path(_td128) / "manuf.zip"
        with _zf.ZipFile(_mz128, "w") as _z128:
            for _n128, _t128 in _samples128.items():
                _z128.writestr(_n128, _t128)
        _hits128 = _scan_secrets(_mz128)
    _hit128 = lambda _tag: any(_h.startswith(_tag + ":") for _h in _hits128)  # noqa: E731
    ok128 = (not _hit128("wild_windows.md") and not _hit128("wild_escaped.md")
             and _hit128("real_windows.md") and _hit128("real_linux.md")
             and not _hit128("wild_linux.md") and not _hit128("wild_mac.md"))
    check("T128 凭据门禁：字面 `*` 的通配符路径不是个人主目录（两种写法都算；真用户名仍要红）", ok128,
          f"命中 {len(_hits128)} 条；通配符-win={_hit128('wild_windows.md')} "
          f"通配符-转义={_hit128('wild_escaped.md')} "
          f"真名-win={_hit128('real_windows.md')} 真名-linux={_hit128('real_linux.md')} "
          f"通配符-linux={_hit128('wild_linux.md')} 通配符-mac={_hit128('wild_mac.md')}")
except Exception as e:  # noqa: BLE001
    check("T128 凭据门禁：字面 `*` 的通配符路径不是个人主目录（两种写法都算；真用户名仍要红）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T129 **凭据门禁的"仓库口径预检"必须接线** —— 这条是 r190 的代价换来的：
#      判据原先只在**打包之后**跑，于是"仓库里的东西自己命中"（新加的绊线把自己的制造样本写成字面量）
#      要等到打包那一步才暴露，只能再补一轮提交 + 全量跑。
#      **两个口径、一个判据**（`scan_text`）：`--tree` 扫"会随 svn/GitHub 公开出去的文件"（仓库口径），
#      不带 `--tree` 时扫发布包（包内口径）。发布包是仓库口径的**子集**（仓库里还有 `ai_shell/`、
#      `AGENT_BOARD.md` 这类不进包但**照样公开**的文件）⇒ 子集性质保证 `--tree` 绿是打包前的预检，
#      但它**不等于**"不进包就不用查"；r191 曾把这两件事混为一谈（借 `package_zip._excluded`），
#      r194 把板排除出包之后那种写法就会**连体检一起排除**。
#      为什么必须接线：不接线的门禁＝只被记录的产物，下一次"自己撞自己"照样要等打包才发现。
try:
    _gate129 = HERE / "check_package_secrets.py"
    if not _gate129.exists():
        raise FileNotFoundError(f"门禁不在：{_gate129}")     # fail-closed
    _tp129 = subprocess.run([sys.executable, str(_gate129), "--tree"], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", cwd=str(HERE),
                            env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    _tout129 = (_tp129.stdout or "") + (_tp129.stderr or "")
    ok129 = (_tp129.returncode == 0 and "仓库口径（会公开出去的文件）" in _tout129
             and "[OK] 无明文凭据/私人标识" in _tout129)
    check("T129 凭据门禁 `--tree`：仓库口径（会公开出去的文件）预检必须绿（接线）", ok129,
          f"rc={_tp129.returncode} 结论行={'[OK] 无明文凭据/私人标识' in _tout129} "
          f"末行={_tout129.strip().splitlines()[-1][:90] if _tout129.strip() else ''}")
except Exception as e:  # noqa: BLE001
    check("T129 凭据门禁 `--tree`：仓库口径（会公开出去的文件）预检必须绿（接线）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T130 **「未知指令」报错必须指路**（#3，来自某 Linux 节点的 GUI 实测）：
#      他们第一轮就撞上 `{"action": "click", "id": ...}` ⇒ 报错只列了动作名，**没提另一条路**。
#      真因（读代码得出的，不是猜）：`act` 里是 `if "action" in action:` 分流 —— 写了 `action`
#      就走**页面级指令**分支，而**按 id 点击/输入根本不写 `action`**（直接给 `{"id","doc"}`）。
#      所以这条报错缺的不是"更多动作名"，是**那半句分流说明**；判据：报错里必须同时出现
#      ①非法的动作名（便于对账）②按 id 的写法（`{"id"`）③`action` 这个词（讲清分流）。
#      先跑红见证（改动前实测）：同一调用报错为「未知指令 click，支持 scroll/…/probe」——**无 ②③**。
try:
    _s130 = open_session(f"{BASE}/list.html")
    try:
        _err130 = ""
        try:
            act_in_session(_s130["session_id"], {"action": "click", "id": "whatever"})
        except ValueError as _e130:
            _err130 = str(_e130)
        _ok130 = ('click' in _err130 and '{"id"' in _err130 and "action" in _err130)
        check("T130 `未知指令` 报错必须指路（按 id 点击不要写 action）", _ok130, _err130[:170])
    finally:
        close_session(_s130["session_id"])
except Exception as e:  # noqa: BLE001
    check("T130 `未知指令` 报错必须指路（按 id 点击不要写 action）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# T131 **自定义下拉的选项必须进 blocks**（#1；出处：某 Linux 节点的 GUI 实测 + 本机夹具
#      `test_site/r41popover.html`）。真因（读代码 + 本机实测得出）：`[role="option"]` 此前
#      **既不在 `ACTION`**（⇒ 没有动作身份、没有 kind）**又落在 FALLBACK 里**（⇒ 被兜底成**匿名段落**）。
#      实测后果（改动前，展开态）：16 个选项全是 `type=paragraph`，而 `aria-selected` 在**整份 JSON 里
#      一个字都没有** ⇒ 消费端能读到文本，但**不知道"这是一组可选项"、也不知道"当前选中的是哪个"**
#      （报告方就是这么绕过去的：去读触发器按钮的 label）。**正文也被污染**（16 句模型名混进段落）。
#      判据三条（都能机械判定，且两个方向都钉）：
#        ① 展开态：选项是 `kind='option'` 的动作块、**每个都有 id**（可寻址）、且**不再混进段落**；
#        ② 选中态：页面**自己声明** `aria-selected=true` 的那个必须带 `selected: true`
#           （`selected` 只在页面明说时出现，缺省**不猜**成 false——所以这里只钉"有一个且是它"）；
#        ③ 关闭态：面板不挂载（Radix 的真实行为）⇒ **一个 option 块都不许有**（"不误伤"的反方向）。
#      另外钉虚拟列表那一半：只渲染 12 行时必须报出**页面声明的** 16（`coverage.unrendered`），
#      否则消费端会把"只渲染了 12 行"读成"一共就 12 个模型"（这是 `aria-setsize` 的既有机制，不是新猜的）。
#      端到端（本机实测，不入断言）：按 `id` 点击 `Gemini Pro` ⇒ 回包段落变成「当前模型：Gemini Pro」+
#      「已选 Gemini Pro」——**选项是可寻址、可点的**，这一里是通的。
try:
    _u41 = f"{BASE}/r41popover.html"
    _open41 = parse_url(_u41 + "?open=1")
    _opts41 = [b for b in (_open41.get("blocks") or []) if b.get("kind") == "option"]
    _sel41 = [b.get("label") for b in _opts41 if b.get("selected") is True]
    _para41 = [b for b in (_open41.get("blocks") or [])
               if b.get("type") == "paragraph" and "Gemini Pro" in (b.get("text") or "")]
    _closed41 = [b for b in (parse_url(_u41).get("blocks") or []) if b.get("kind") == "option"]
    _virt41 = parse_url(_u41 + "?open=1&virt=1")
    _virtopts41 = [b for b in (_virt41.get("blocks") or []) if b.get("kind") == "option"]
    _unr41 = ((_virt41.get("coverage") or {}).get("unrendered") or {}).get("known")
    ok131 = (len(_opts41) == 16 and all(b.get("id") for b in _opts41)
             and _sel41 == ["Kimi K3"] and not _para41
             and not _closed41
             and len(_virtopts41) == 12 and _unr41 == 16)
    check("T131 自定义下拉：选项是可寻址的 option 块 + 带选中态（关闭态不许泄漏）", ok131,
          f"展开 option 块={len(_opts41)} 带id={sum(1 for b in _opts41 if b.get('id'))} "
          f"选中={_sel41} 仍混在段落={len(_para41)} 关闭态 option 块={len(_closed41)} "
          f"虚表 option 块={len(_virtopts41)} unrendered={_unr41}")
except Exception as e:  # noqa: BLE001
    check("T131 自定义下拉：选项是可寻址的 option 块 + 带选中态（关闭态不许泄漏）", False,
          f"{type(e).__name__}: {str(e)[:90]}")

# ---------------- T13 REST 鉴权 ----------------
try:
    from fastapi.testclient import TestClient

    from ai_browser import server as srv_mod

    c = TestClient(srv_mod.app)
    check("T13a /health 免鉴权", c.get("/health").status_code == 200)
    srv_mod.TOKEN = "s3cret"
    check("T13b 无令牌被拒", c.get("/sessions").status_code == 401)
    check("T13c 正确令牌放行",
          c.get("/sessions", headers={"X-Auth-Token": "s3cret"}).status_code == 200)
    srv_mod.TOKEN = None
except ImportError as e:  # httpx 未安装时跳过而不是假通过
    detail.append(f"    T13 跳过：{e}")

srv.shutdown()
shutil.rmtree(PROFILES, ignore_errors=True)

print()
for k, v in results.items():
    print(f"{'PASS' if v else 'FAIL'}  {k}")
if detail:
    print("\n明细：")
    print("\n".join(detail))
print()
sys.exit(0 if all(results.values()) else 1)
