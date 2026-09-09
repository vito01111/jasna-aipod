"""M1.5 jetson-gst 解码 lane 测试：与 pyav 软解逐像素对照 + seek 对齐验证。"""
import os
import sys

sys.path.insert(0, "/jasna")

import torch  # noqa: E402

from jasna.media import get_video_meta_data  # noqa: E402
from jasna.media.video_decoder import NvidiaVideoReader  # noqa: E402
from jasna.jetson_gst_decoder import JetsonGstFrameSource  # noqa: E402

FILE = "/videos/synth-90s.mp4"
DEV = torch.device("cuda")
meta = get_video_meta_data(FILE)
TB = float(meta.time_base)
print(f"meta: {meta.video_width}x{meta.video_height} @{meta.average_fps} "
      f"tb={meta.time_base} codec={meta.codec_name}")


def pyav_frames(seek_ts, n, batch=2):
    got = []
    with NvidiaVideoReader(FILE, batch, DEV, meta) as r:  # auto → pyav 软解（无 VALI）
        for b, pts in r.frames(seek_ts):
            got.append((b.cpu(), list(pts)))
            if sum(x[0].shape[0] for x in got) >= n:
                break
    frames = torch.cat([g[0] for g in got])[:n]
    pts = [p for g in got for p in g[1]][:n]
    return frames, pts


def gst_frames(seek_ts, n):
    src = JetsonGstFrameSource(FILE, 2, DEV, meta, 1)
    got = []
    try:
        for b, pts in src.frames(seek_ts):
            got.append((b.cpu(), list(pts)))
            if sum(x[0].shape[0] for x in got) >= n:
                break
    finally:
        src.close()
    frames = torch.cat([g[0] for g in got])[:n]
    pts = [p for g in got for p in g[1]][:n]
    return frames, pts


def diff(a, b):
    d = (a.to(torch.int16) - b.to(torch.int16)).abs()
    return int(d.max().item()), float(d.float().mean())


N = 64
print("== 1) 从头解码（seek None），前 64 帧对照 ==")
g_frames, g_pts = gst_frames(None, N)
p_frames, p_pts = pyav_frames(None, N)
print(f"gst pts[0..2]={g_pts[:3]} pyav pts[0..2]={p_pts[:3]}")
print(f"pixel diff = {diff(g_frames, p_frames)}")
assert abs(g_pts[0] - p_pts[0]) <= 2, f"首帧 pts 偏差过大 {g_pts[0]} vs {p_pts[0]}"
mx, mn = diff(g_frames, p_frames); assert mx <= 8 and mn < 3, f"从头解码像素不一致 max={mx} mean={mn:.2f}"

print("== 2) 关键帧 seek（10.0s，素材每 2s 一个关键帧）==")
g_frames, g_pts = gst_frames(10.0, N)
p_frames, p_pts = pyav_frames(10.0, N)
print(f"gst pts[0]={g_pts[0]} ({g_pts[0]*TB:.3f}s) pyav pts[0]={p_pts[0]} ({p_pts[0]*TB:.3f}s)")
print(f"pixel diff = {diff(g_frames, p_frames)}")
assert abs(g_pts[0] - p_pts[0]) <= 2, "关键帧 seek 对齐失败"
mx, mn = diff(g_frames, p_frames); assert mx <= 8 and mn < 3, f"关键帧 seek 像素不一致 max={mx} mean={mn:.2f}"

print("== 3) 非关键帧 seek（10.5s，丢帧对齐验证）==")
g_frames, g_pts = gst_frames(10.5, N)
p_frames, p_pts = pyav_frames(10.5, N)
print(f"gst pts[0]={g_pts[0]} ({g_pts[0]*TB:.3f}s) pyav pts[0]={p_pts[0]} ({p_pts[0]*TB:.3f}s)")
print(f"pixel diff = {diff(g_frames, p_frames)}")
assert abs(g_pts[0] - p_pts[0]) <= 2, f"非关键帧 seek 对齐失败 {g_pts[0]} vs {p_pts[0]}"
mx, mn = diff(g_frames, p_frames); assert mx <= 8 and mn < 3, f"非关键帧 seek 像素不一致 max={mx} mean={mn:.2f}"

print("== 4) 全片帧数（seek None 到 EOF，可能略慢）==")
src = JetsonGstFrameSource(FILE, 4, DEV, meta, 1)
total = 0
last_pts = 0
try:
    for b, pts in src.frames(None):
        total += b.shape[0]
        last_pts = pts[-1]
finally:
    src.close()
dur = last_pts * TB
print(f"frames={total} (expect 2700), last={dur:.3f}s (expect ~89.97)")
assert total == 2700, f"帧数不符 {total}"

print("ALL_GST_DEC_TESTS_OK")
