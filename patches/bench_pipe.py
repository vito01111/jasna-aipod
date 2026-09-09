"""孤立管道车道基准：预生成帧 → 与 NvencPipeEncoder 相同的 ffmpeg 命令。
测纯消费端 fps（无检测/修复争用）。"""
import subprocess
import sys
import time

sys.path.insert(0, "/jasna")
import numpy as np
import torch

from jasna.media import get_video_meta_data
from jasna.nvenc_output import NvencPipeEncoder, probe_nvenc_ffmpeg

meta = get_video_meta_data("/videos/dass-377-seg3138.mp4")
w, h = int(meta.video_width), int(meta.video_height)
ff = probe_nvenc_ffmpeg()

frame_rgb = torch.randint(0, 255, (3, h, w), dtype=torch.uint8, device="cuda")

for pix in ("nv12", "rgb24"):
    cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", pix, "-s", f"{w}x{h}",
           "-r", "30000/1001", "-i", "pipe:0", "-map", "0:v:0",
           "-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr",
           "-cq", "20", "-b:v", "0", "-f", "nut", "/tmp/pipebench.nut"]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.time()
    n = 1078
    for i in range(n):
        if pix == "nv12":
            # GPU 转换 + D2H（与管线内 encode 相同路径）
            from jasna.media.rgb_to_yuv import RgbToYuvConverter
            if i == 0:
                conv = RgbToYuvConverter("nv12_bt709_limited", device=torch.device("cuda"))
            data = conv.convert(frame_rgb).contiguous().cpu().numpy().tobytes()
        else:
            data = frame_rgb.contiguous().cpu().numpy().tobytes()
        proc.stdin.write(data)
    proc.stdin.close()
    proc.wait()
    dt = time.time() - t0
    print(f"{pix}: {n} frames in {dt:.2f}s = {n/dt:.0f} fps (rc={proc.returncode})")
