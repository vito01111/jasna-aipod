# -*- coding: utf-8 -*-
"""流式播放器相对路径补丁（JASNA_STREAM_RELPATH）。

问题：上游播放器页的 API 全用根绝对路径（fetch('/open')、loadSource('/stream.m3u8')）。
经 nginx 子路径反代（页面在 /jasna/ 下）打开时，这些请求打到域名根而非流式服务，
被微服根路由用 HTML 响应（"Unexpected token '<'"）。

改法：_PAGE_HTML 内五个绝对路径改相对（无前导斜杠），相对 /jasna/ 基路径解析：
  /api/browse → api/browse；/open → open；/stop → stop；
  /stream.m3u8 → stream.m3u8（loadSource 与 v.src 两处）。
直连 18765 打开时相对路径同样正确（基路径即根）。

幂等：检测 JASNA_STREAM_RELPATH 标记。用法：python3 add_stream_relpaths.py [/jasna]
"""
import sys
from pathlib import Path

MARK = "JASNA_STREAM_RELPATH"

REPLACES = [
    ("fetch('/api/browse'", "fetch('api/browse'"),
    ("fetch('/open'", "fetch('open'"),
    ("fetch('/stop'", "fetch('stop'"),
    ("hls.loadSource('/stream.m3u8')", "hls.loadSource('stream.m3u8')"),
    ("v.src='/stream.m3u8'", "v.src='stream.m3u8'"),
    ("v.src = '/stream.m3u8'", "v.src = 'stream.m3u8'"),
]


def patch(src: Path) -> None:
    p = src / "jasna" / "streaming.py"
    text = p.read_text(encoding="utf-8")
    if MARK in text:
        print("already patched:", p)
        return
    n = 0
    for old, new in REPLACES:
        c = text.count(old)
        if c:
            text = text.replace(old, new)
            n += c
    # 标记：注释进页面脚本首行
    text = text.replace(
        "<script>\nvar hls=null;",
        "<script>\n/* " + MARK + " */\nvar hls=null;", 1)
    p.write_text(text, encoding="utf-8")
    print(f"patched: {p} ({n} 处路径)")


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "/jasna")
    patch(target)
