"""重新生成文档夹具 demo_output.json / bloated_output.json。

夹具里的 url 必须与机器无关，所以这里用固定端口起一个本地服务来渲染页面，
而不是直接解析 file:// 路径（那样会把本机绝对路径写进夹具）。

    python make_fixtures.py
"""
from __future__ import annotations

import functools
import http.server
import json
import sys
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ai_browser import parse_url  # noqa: E402

PORT = 8899
Handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(HERE))
srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()

JOBS = [("demo_page.html", "demo_output.json", 2),
        ("bloated_page.html", "bloated_output.json", None)]

try:
    for page, out, indent in JOBS:
        data = parse_url(f"http://127.0.0.1:{PORT}/{page}")
        text = json.dumps(data, ensure_ascii=False, indent=indent)
        (HERE / out).write_text(text + "\n", encoding="utf-8")
        st = data["stats"]
        print(f"{out:<22} blocks={st['counts']['blocks']:<3} "
              f"{st['html_chars']} → {st['json_chars']} 字符  压缩率 {st['reduction']}")
finally:
    srv.shutdown()
