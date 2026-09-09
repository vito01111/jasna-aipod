"""M1.4 gst 硬编码 lane 单测（容器内）：探测 → 整片编码 → 直出 TS 片段 → 工厂选择。"""
import os
import subprocess
import sys

sys.path.insert(0, "/jasna")
os.environ.setdefault("JASNA_ENCODER", "auto")

import torch  # noqa: E402

from jasna.jetson_gst_encoder import (  # noqa: E402
    JetsonGstEncoder, maybe_normalized_fragment, probe_jetson_gst)


class Meta:
    video_width = 640
    video_height = 360
    average_fps = 30.0
    video_file = "/videos/nonexistent.mp4"
    color_space = None
    color_range = None


def frames(n, device="cpu"):
    for i in range(n):
        t = torch.randint(0, 255, (3, 360, 640), dtype=torch.uint8, device=device)
        yield (t.float() / 255.0).to(torch.uint8)  # 走 uint8 快路径


def ffprobe(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_name,width,height,color_space",
         "-show_entries", "format=duration", "-of", "default=nw=1", path],
        capture_output=True, text=True)
    return r.stdout.strip(), r.returncode


print("== 1) probe ==")
print("gst-launch:", probe_jetson_gst())

print("== 2) 整片模式（CPU 帧张量 → rgb24/BGR 管道）==")
out = "/outputs/gst-test.mp4"
with JetsonGstEncoder(out, Meta(), codec="h264", mux_audio=False) as enc:
    for i, t in enumerate(frames(90)):
        enc.encode(t, i)
info, rc = ffprobe(out)
print("size:", os.path.getsize(out), "| ffprobe rc:", rc)
print(info)
assert rc == 0 and "h264" in info, "整片编码验证失败"

print("== 3) smart fragment 直出 TS（GPU NV12 快通道 if available）==")
raw = "/outputs/frag-raw.nut"
norm = "/outputs/frag-normalized.ts"
with JetsonGstEncoder(raw, Meta(), codec="h264", mux_audio=False,
                      smart_fragment=True) as enc:
    print("  in_fmt:", enc._in_fmt)
    dev = "cuda" if enc._conv is not None else "cpu"
    for i, t in enumerate(frames(90, device=dev)):
        enc.encode(t, i)
with open(raw, "rb") as f:
    print("  first byte:", hex(f.read(1)[0]))
maybe_normalized_fragment(raw, norm, codec="h264")
print("  normalized exists:", os.path.exists(norm), "| size:", os.path.getsize(norm))
info2, rc2 = ffprobe(norm)
print(" ", info2)
assert os.path.exists(norm) and os.path.getsize(norm) > 0

print("== 4) 工厂链选择 ==")
import logging  # noqa: E402
logging.basicConfig(level=logging.INFO)
from jasna.cpu_encoder_fallback import make_encoder  # noqa: E402
enc = make_encoder("/outputs/factory-test.mp4", metadata=Meta(), codec="h264",
                   mux_audio=False)
print("factory picked:", type(enc).__name__)
assert type(enc).__name__ == "JetsonGstEncoder"

print("ALL_GST_TESTS_OK")
