"""最小复现：CpuVideoEncoder 写 NUT → normalize_fragment 转 TS → 验证。"""
import sys

sys.path.insert(0, "/jasna")

import torch
from jasna.cpu_encoder_fallback import CpuVideoEncoder
from jasna.media import get_video_meta_data

meta = get_video_meta_data("/videos/VDD-207-20min-bench.mp4")
print("meta:", meta.video_width, meta.video_height, float(meta.video_fps_exact))

enc = CpuVideoEncoder(
    "/tmp/t-frag.nut",
    device=torch.device("cuda:0"),
    metadata=meta,
    codec="h264",
    mux_audio=False,
    output_fps=meta.video_fps_exact,
)
with enc:
    for i in range(120):  # 4 秒
        frame = torch.randint(0, 255, (3, meta.video_height // 4, meta.video_width // 4), dtype=torch.uint8)
        # 用小尺寸会跟 metadata 不一致——直接用全尺寸随机帧
        frame = torch.randint(0, 255, (3, meta.video_height, meta.video_width), dtype=torch.uint8)
        enc.encode(frame, i * 3000)

print("nut written")

from jasna.media.splice import normalize_fragment
from pathlib import Path

normalize_fragment(Path("/tmp/t-frag.nut"), Path("/tmp/t-frag.ts"), codec="h264")
print("normalized")
