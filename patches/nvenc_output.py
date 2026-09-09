"""DGX Spark (GB10) NVENC 输出通道——部署层加速模块（解耦设计）。

背景：jasna 的 CPU 编码回退（libx264/x265）是全管线瓶颈（36s 段：GPU 修复
9.7s vs CPU 编码 40.9s）。GB10 有 NVENC 硬编单元，本模块以独立子进程方式
接入：帧以 rgb24 rawvideo 喂 ffmpeg stdin，编码/封装/音频直拷全在子进程内
完成，不依赖 PyAV 内嵌 ffmpeg 的编码器面。

解耦约定：
- 本模块不 import jasna 上游任何符号（仅 duck-type 对齐 encoder 协议：
  encode(frame, pts, apply_lut) / __enter__ / __exit__）；
- 接入点唯一：cpu_encoder_fallback.make_encoder 的探测链
  （PyAV nvenc → 本模块 → CPU 回退），上游 pipeline.py 不感知；
- 行为镜像 CpuVideoEncoder：CFR（帧号即 PTS）、片段模式写 NUT 从 0 起
  （下游 normalize_fragment/concat 负责时间线）、pts_origin/apply_lut/
  LUT/锐化忽略；音频走源文件第二输入直拷（mkv/mp4 均在首包前由 ffmpeg
  自行声明流）。

环境开关（全部可选，缺省 auto 探测）：
- JASNA_ENCODER = auto|nvenc|cpu   强制选择（auto=探测到 NVENC 即用）
- JASNA_NVENC_FFMPEG               指定 ffmpeg 二进制（默认 /opt/ff8 优先）
- JASNA_NVENC_PRESET               p1-p7 或默认 p4
- JASNA_CRF                        复用质量档（映射为 -cq，仅 x264/x265 语义）
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path

import torch

logger = logging.getLogger(__name__)

_DEFAULT_CQ = {"h264": "20", "hevc": "23"}
_FORMATS = {
    ".mp4": "mp4", ".mov": "mp4", ".m4v": "mp4",
    ".mkv": "matroska", ".webm": "webm",
    ".nut": "nut", ".ts": "mpegts",
}

_probe_cache: dict[str, str | None] = {}


def probe_nvenc_ffmpeg() -> str | None:
    """返回可用的 NVENC ffmpeg 二进制（带 3s 真编冒烟），不可用返回 None。"""
    if "ffmpeg" in _probe_cache:
        return _probe_cache["ffmpeg"]
    forced = os.environ.get("JASNA_NVENC_FFMPEG")
    candidates = [forced] if forced else [
        p for p in ("/opt/ff8/bin/ffmpeg", "/usr/bin/ffmpeg") if Path(p).is_file()
    ]
    for ff in candidates:
        try:
            enc = subprocess.run([ff, "-hide_banner", "-encoders"],
                                 capture_output=True, text=True, timeout=15)
            if "h264_nvenc" not in (enc.stdout or ""):
                continue
            smoke = subprocess.run(
                [ff, "-hide_banner", "-loglevel", "error",
                 "-f", "lavfi", "-i", "testsrc2=duration=1:size=1280x720:rate=30",
                 "-c:v", "h264_nvenc", "-f", "null", "-"],
                capture_output=True, text=True, timeout=30)
            if smoke.returncode == 0:
                _probe_cache["ffmpeg"] = ff
                logger.info("[nvenc] using %s (smoke ok)", ff)
                return ff
            logger.warning("[nvenc] %s lists nvenc but smoke failed: %s",
                           ff, (smoke.stderr or "").strip()[:200])
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("[nvenc] probe %s failed: %r", ff, e)
    _probe_cache["ffmpeg"] = None
    return None


_COLOR_VARIANTS = {
    ("ITU709", "MPEG"): "bt709_limited",
    ("ITU709", "JPEG"): "bt709_full",
    ("ITU601", "MPEG"): "bt601_limited",
    ("ITU601", "JPEG"): "bt601_full",
    ("BT2020", "MPEG"): "bt2020_limited",
    ("BT2020", "JPEG"): "bt2020_full",
}


def _try_gpu_converter(metadata):
    """可选启用 jasna 自带 RGB→NV12 CUDA 内核（复用非分叉）；失败返回 None
    走 rgb24 管道回退（由 ffmpeg swscale 转换）。"""
    try:
        from jasna.media.rgb_to_yuv import RgbToYuvConverter

        variant = _COLOR_VARIANTS.get(
            (str(getattr(metadata.color_space, "name", "ITU709")),
             str(getattr(metadata.color_range, "name", "MPEG"))))
        if variant is None:
            return None
        return RgbToYuvConverter(f"nv12_{variant}", device=torch.device("cuda"))
    except Exception as e:
        logger.warning("[nvenc] GPU nv12 converter unavailable, rgb24 pipe: %r", e)
        return None


class NvencPipeEncoder:
    """NVENC 子进程编码器（接口与 CpuVideoEncoder 对齐）。

    帧通道优先 GPU NV12（jasna rgb_to_yuv CUDA 内核，管道字节减半、
    ffmpeg 侧免 swscale），不可用回退 rgb24 rawvideo。"""

    def __init__(self, output_path, metadata, codec: str = "h264", *,
                 mux_audio: bool = True, smart_fragment: bool = False,
                 output_fps=None, fmp4: bool = False, **_ignored) -> None:
        self.output_path = str(output_path)
        self.metadata = metadata
        self.codec = codec.lower()
        self.encoder_name = {"h264": "h264_nvenc", "hevc": "hevc_nvenc"}.get(
            self.codec, "h264_nvenc")
        self.mux_audio = mux_audio and not smart_fragment
        self.smart_fragment = smart_fragment
        self._conv = None if os.environ.get("JASNA_NVENC_RGB") == "1" else _try_gpu_converter(metadata)
        self._in_pixfmt = "nv12" if self._conv is not None else "rgb24"
        fps = output_fps or (metadata.average_fps if metadata else 30.0)
        self.rate_frac = fps if isinstance(fps, Fraction) else Fraction(str(round(float(fps), 6)))
        self._frame_index = 0
        self._proc: subprocess.Popen | None = None
        self._err_file = None
        Path(self.output_path).parent.mkdir(parents=True, exist_ok=True)

    def _build_cmd(self) -> list[str]:
        ff = probe_nvenc_ffmpeg() or "ffmpeg"
        fmt = _FORMATS.get(Path(self.output_path).suffix.lower(), "mp4")
        if self.smart_fragment:
            fmt = "nut"  # 上游约定：render 片段写 NUT，normalize_fragment 再转 mpegts
        w, h = int(self.metadata.video_width), int(self.metadata.video_height)
        cmd = [ff, "-y", "-hide_banner", "-loglevel", "error",
               "-f", "rawvideo", "-pix_fmt", self._in_pixfmt, "-s", f"{w}x{h}",
               "-r", str(self.rate_frac), "-i", "pipe:0"]
        if self.mux_audio:
            cmd += ["-i", str(self.metadata.video_file),
                    "-map", "0:v:0", "-map", "1:a:0?"]
        else:
            cmd += ["-map", "0:v:0"]
        cq = os.environ.get("JASNA_CRF", "").strip()
        if not (cq.isdigit() and 0 <= int(cq) <= 51):
            cq = _DEFAULT_CQ.get(self.codec, "20")
        preset = os.environ.get("JASNA_NVENC_PRESET", "p4").strip() or "p4"
        cmd += ["-c:v", self.encoder_name, "-preset", preset,
                "-rc", "vbr", "-cq", cq, "-b:v", "0"]
        if self._in_pixfmt == "rgb24":
            cmd += ["-pix_fmt", "yuv420p"]
        cmd += ["-c:a", "copy"] if self.mux_audio else []
        cmd += ["-f", fmt, self.output_path]
        return cmd

    def __enter__(self):
        self._err_file = tempfile.NamedTemporaryFile(
            prefix="nvenc_err_", suffix=".log", delete=False)
        self._proc = subprocess.Popen(
            self._build_cmd(), stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=self._err_file)
        logger.info("[nvenc] %s -> %s", self.encoder_name, self.output_path)
        return self

    def encode(self, frame, pts: int, *, apply_lut: bool = True):
        if isinstance(frame, torch.Tensor):
            tensor = frame.detach()
            if tensor.dim() == 3 and tensor.shape[0] == 3:
                tensor = tensor.permute(1, 2, 0)  # CHW → HWC
            if tensor.dtype != torch.uint8:
                if tensor.max() <= 1.5:
                    tensor = tensor * 255.0
                tensor = tensor.clamp(0, 255).to(torch.uint8)
            if self._conv is not None and tensor.is_cuda:
                # GPU 通道：CHW uint8 → NV12 packed（CUDA 内核）→ 1.5B/px 拷回
                nv12 = self._conv.convert(tensor.permute(2, 0, 1))
                data = nv12.detach().contiguous().cpu().numpy().tobytes()
            else:
                data = tensor.contiguous().cpu().numpy().tobytes()
        else:
            data = memoryview(frame).tobytes()
        assert self._proc is not None and self._proc.stdin
        self._proc.stdin.write(data)
        self._frame_index += 1

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if self._proc is None:
                return
            if exc_type is None:
                self._proc.stdin.close()
                rc = self._proc.wait(timeout=600)
                if rc != 0:
                    self._err_file.flush()
                    tail = Path(self._err_file.name).read_text(errors="replace")[-800:]
                    raise RuntimeError(f"NVENC encoder exited {rc}: {tail}")
            else:
                self._proc.kill()
                self._proc.wait(timeout=30)
        finally:
            if self._err_file:
                self._err_file.close()
                Path(self._err_file.name).unlink(missing_ok=True)
