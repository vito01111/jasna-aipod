#!/usr/bin/env python3
"""补丁 #13（幂等）：jetson-gst NVDEC 解码后端接入（JASNA_GST_DEC）。

三处改动（jasna/media/video_decoder.py）：
1. DECODE_BACKEND 由写死 "auto" 改为环境变量可覆盖（上游注释本就说 flip in code）
2. _DECODE_BACKENDS 增加 "jetson-gst"
3. __enter__ 在 VALI 分支前插入 jetson-gst 显式分支（构造即冒烟，失败抛
   VideoDecodeError；auto 链不自动选——scan 的 cv2 路径与流式预览不受影响）

幂等：文件已含 JASNA_GST_DEC 则跳过。
"""
import sys
from pathlib import Path

MARK = "JASNA_GST_DEC"

OLD_BACKEND = 'DECODE_BACKEND = "auto"'
NEW_BACKEND = (
    f'DECODE_BACKEND = __import__("os").environ.get("DECODE_BACKEND", "auto")  # {MARK}: 环境变量可覆盖'
)

OLD_LIST = '_DECODE_BACKENDS = ("auto", "vali", "pyav-hw", "pyav-sw")'
NEW_LIST = '_DECODE_BACKENDS = ("auto", "vali", "jetson-gst", "pyav-hw", "pyav-sw")'

OLD_BRANCH = """        if backend in ("auto", "vali"):"""
NEW_BRANCH = f"""        if backend == "jetson-gst":
            # {MARK}: L4T NVDEC 走 gst 子进程对（ffmpeg 解封装+seek | nvv4l2decoder）。
            # 显式后端：失败直接抛（同 vali 显式语义）；auto 不自动选。
            from jasna.jetson_gst_decoder import JetsonGstFrameSource

            self._vali_source = JetsonGstFrameSource(
                self.file, self.batch_size, self.device, self.metadata, self.frame_stride,
            )
            self.width = self._vali_source.width
            self.height = self._vali_source.height
            log.info("Using jetson-gst NVDEC decoder for %s", self.file)
            return self
        if backend in ("auto", "vali"):"""


def main(src_dir: str) -> None:
    path = Path(src_dir) / "jasna" / "media" / "video_decoder.py"
    text = path.read_text(encoding="utf-8")
    if MARK in text:
        print(f"[{MARK}] already patched")
        return
    for old, new, what in (
        (OLD_BACKEND, NEW_BACKEND, "env backend"),
        (OLD_LIST, NEW_LIST, "backend list"),
        (OLD_BRANCH, NEW_BRANCH, "enter branch"),
    ):
        if old not in text:
            print(f"[{MARK}] !! anchor not found ({what}) — upstream drift, manual review")
            return
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")
    print(f"[{MARK}] patched (env backend + backend list + enter branch)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
