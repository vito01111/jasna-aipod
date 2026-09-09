"""M1.2 验收烟测：jasna 全模块在 JP6/py3.10/av17 栈上可导入。

经 compose（L4T 挂载齐全）跑：
  docker compose -f docker-compose.orin.yml run --rm jasna python3 /jasna/smoke_m12.py
"""
import sys

print("python", sys.version.split()[0])
import av
print("av", av.__version__)
import torch
print("torch", torch.__version__, "| cuda_available", torch.cuda.is_available())
if torch.cuda.is_available():
    p = torch.cuda.get_device_properties(0)
    print("  gpu", p.name, f"sm_{p.major}{p.minor}", f"{p.total_memory/2**30:.0f}G")
import tensorrt
print("tensorrt", tensorrt.__version__)
import torch_tensorrt
print("torch_tensorrt", torch_tensorrt.__version__)

from jasna import av17_compat
print("av17_compat BT2020 ->", av17_compat.AvColorspace.BT2020)

import jasna.main  # noqa: F401  最重的一环：CLI 入口拉起 pipeline 全家
import jasna.pipeline  # noqa: F401
import jasna.streaming_pipeline  # noqa: F401
import jasna.media.video_encoder  # noqa: F401
import jasna.media.video_decoder  # noqa: F401
import jasna.trt.trt_runner  # noqa: F401
import jasna.restorer.basicvsrpp_mosaic_restorer  # noqa: F401
import jasna.mosaic.detection_registry  # noqa: F401
print("ALL_IMPORTS_OK")

import subprocess
print("ffmpeg:", subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout.splitlines()[0])
gst = subprocess.run(["gst-inspect-1.0", "nvv4l2h264enc"], capture_output=True, text=True)
print("gst nvv4l2h264enc:", "OK" if gst.returncode == 0 else f"MISSING({gst.returncode})")
