"""DGX Spark (aarch64) 部署层补丁：软件编码器回退（部署于 jasna/cpu_encoder_fallback.py）。

PyAV 官方 wheel 的 bundled ffmpeg 不含 nvenc，且 BtbN linuxarm64 构建亦无 nvenc
（x86 才编）；本模块在 nvenc 不可用时回退 libx264/libx265/libsvtav1（PyAV wheel
自带这些 GPL 编码器）。

限制（相对 NvidiaVideoEncoder）：LUT/锐化/十比特/NVENC 专属 encoder_settings
忽略；CFR 输出按帧计数 PTS。上游升级后由 apply.sh 重新套用。
"""
from __future__ import annotations

import logging
import os
from fractions import Fraction
from pathlib import Path

import av
import torch

logger = logging.getLogger(__name__)

_CPU_ENCODERS = {"hevc": "libx265", "h264": "libx264", "av1": "libsvtav1"}
_NVENC_ENCODERS = {"hevc": "hevc_nvenc", "h264": "h264_nvenc", "av1": "av1_nvenc"}
_CRF_DEFAULT = {"hevc": "24", "h264": "20", "av1": "28"}


def make_encoder(*args, **kwargs):
    """编码器工厂（DGX Spark 部署层）：三级探测链。

    1. PyAV 内嵌 ffmpeg 的 nvenc（上游 NvidiaVideoEncoder，本机 PyAV wheel 无）
    2. NVENC 子进程通道（nvenc_output.NvencPipeEncoder，GB10 实测可用；
       JASNA_ENCODER=cpu 可强制跳过）
    3. CPU 回退（libx264/x265/svtav1）

    参数面与 NvidiaVideoEncoder 构造函数一致（pipeline 两个实例化点同名传参，
    output_path 为第一个位置参数）。
    """
    codec = str(kwargs.get("codec") or "hevc").lower()
    nvenc_name = _NVENC_ENCODERS.get(codec, "hevc_nvenc")
    try:
        av.Codec(nvenc_name, "w")
        from jasna.media.video_encoder import NvidiaVideoEncoder

        return NvidiaVideoEncoder(*args, **kwargs)
    except Exception:
        pass
    choice = os.environ.get("JASNA_ENCODER", "auto").strip().lower()
    if choice != "cpu" and codec in {"h264", "hevc"}:
        # Jetson L4T：无桌面 NVENC SDK，硬编码走 gst nvv4l2 lane（AIPod x3）
        if choice in ("auto", "gst"):
            try:
                from jasna.jetson_gst_encoder import probe_jetson_gst, JetsonGstEncoder

                if probe_jetson_gst() is not None:
                    return JetsonGstEncoder(*args, **kwargs)
                logger.info("[encoder] gst nvv4l2 lane unavailable; trying ffmpeg nvenc")
            except Exception:
                logger.warning("[encoder] jetson gst lane unusable; trying ffmpeg nvenc",
                               exc_info=True)
        if choice in ("auto", "nvenc"):
            try:
                from jasna.nvenc_output import NvencPipeEncoder, probe_nvenc_ffmpeg

                if probe_nvenc_ffmpeg() is not None:
                    return NvencPipeEncoder(*args, **kwargs)
                logger.warning("[encoder] NVENC probe failed; CPU fallback")
            except Exception:
                logger.warning("[encoder] nvenc_output unusable; CPU fallback",
                               exc_info=True)
    logger.warning(
        "encoder %s unavailable; falling back to %s CPU encode",
        nvenc_name,
        _CPU_ENCODERS.get(codec, "libx264"),
    )
    return CpuVideoEncoder(*args, **kwargs)


class CpuVideoEncoder:
    """软件编码回退：RGB tensor → yuv420p → libx264/x265/svtav1，音频 remux。"""

    BUFFER_MAX_SIZE = 8

    def __init__(
        self,
        output_path,
        device=None,
        metadata=None,
        codec: str = "hevc",
        encoder_settings: dict | None = None,
        lut_path: str | None = None,
        sharpen_strength: float = 0.0,
        output_fps=None,
        fmp4: bool = False,
        mux_audio: bool = True,
        pts_origin: int = 0,
        match_input_bit_depth: bool = False,
        smart_fragment: bool = False,
        **_ignored,
    ) -> None:
        self.output_path = str(output_path)
        self.metadata = metadata
        self.codec = codec.lower()
        self.encoder_name = _CPU_ENCODERS.get(self.codec, "libx264")
        self.mux_audio = mux_audio
        fps = output_fps or (metadata.average_fps if metadata else 30.0)
        if isinstance(fps, Fraction):
            self.rate_frac = fps.limit_denominator(1001 * 1000)
        else:
            self.rate_frac = Fraction(round(float(fps) * 1001), 1001 * 1000).limit_denominator(1001 * 1000)
        self.rate = float(self.rate_frac)
        self._frame_index = 0
        Path(self.output_path).parent.mkdir(parents=True, exist_ok=True)

    def __enter__(self):
        self.dst = av.open(self.output_path, "w")
        # CRF 可由 JASNA_CRF 环境变量覆盖（web 任务面板"质量"档位注入；
        # 仅作用于 x264/x265，av1 的刻度不同不套用）
        crf = os.environ.get("JASNA_CRF", "").strip()
        if not (crf.isdigit() and 0 <= int(crf) <= 51):
            crf = _CRF_DEFAULT.get(self.codec, "20")
        options = {"preset": "veryfast", "crf": crf}
        if self.encoder_name == "libsvtav1":
            options = {"preset": "8", "crf": "30"}
        self.stream = self.dst.add_stream(self.encoder_name, rate=self.rate_frac)
        self.stream.width = self.metadata.video_width
        self.stream.height = self.metadata.video_height
        self.stream.pix_fmt = "yuv420p"
        self.stream.options = options
        # CFR：帧号即 PTS，time_base 由 rate（1/fps）派生，勿手动覆盖
        self._audio_out = None
        self._audio_src = None
        if self.mux_audio:
            self.src = av.open(self.metadata.video_file)
            audio_streams = [s for s in self.src.streams if s.type == "audio"]
            if audio_streams:
                # mkv 要求所有流在首个 packet mux 前声明——音频流必须在此添加
                self._audio_src = audio_streams[0]
                adder = getattr(self.dst, "add_stream_from_template", None)
                self._audio_out = (
                    adder(self._audio_src)
                    if adder
                    else self.dst.add_stream(self._audio_src.codec_context.name, template=self._audio_src)
                )
        else:
            self.src = None
        return self

    def encode(self, frame: torch.Tensor, pts: int, *, apply_lut: bool = True):
        if isinstance(frame, torch.Tensor):
            tensor = frame.detach()
            if tensor.dim() == 3 and tensor.shape[0] == 3:
                tensor = tensor.permute(1, 2, 0)  # CHW → HWC
            if tensor.dtype != torch.uint8:
                # 自适应 0-1 / 0-255 两种浮点域（jasna 管线 blend 输出）
                if tensor.max() <= 1.5:
                    tensor = tensor * 255.0
                tensor = tensor.clamp(0, 255).to(torch.uint8)
            array = tensor.contiguous().cpu().numpy()
        else:
            array = frame
        vf = av.VideoFrame.from_ndarray(array, format="rgb24")
        yuv = vf.reformat(format="yuv420p")
        yuv.pts = self._frame_index
        self._frame_index += 1
        for packet in self.stream.encode(yuv):
            self.dst.mux(packet)

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if exc_type is None:
                for packet in self.stream.encode(None):
                    self.dst.mux(packet)
                if self._audio_out is not None:
                    for packet in self.src.demux(self._audio_src):
                        if packet.dts is None:
                            continue
                        packet.stream = self._audio_out
                        self.dst.mux(packet)
        finally:
            self.dst.close()
            if self.src is not None:
                self.src.close()


def create_copy_ts_fragment(source, span, index, destination, codec: str) -> None:
    """smart render copy 片段：ffmpeg CLI 一步从源切 mpegts（部署层补丁）。

    上游路径（PyAV remux→NUT→normalize）在 BtbN ffmpeg8 下丢 B 帧
    （54 包只认 13）——PyAV18 写的 NUT 与该构建的 demuxer 不兼容。
    copy span 起点本就是 KeyframeIndex 安全切点，-ss 输入侧 seek 直切。
    """
    import subprocess

    start = float(index.seconds_for_pts(span.start_pts))
    duration = float((span.end_pts - span.start_pts) * index.time_base)
    bsf = {
        "h264": "h264_mp4toannexb,dump_extra=freq=keyframe",
        "hevc": "hevc_mp4toannexb,dump_extra=freq=keyframe",
    }.get(codec, "h264_mp4toannexb,dump_extra=freq=keyframe")
    args: list[str] = []
    if start > 0.001:
        args += ["-ss", f"{start:.6f}"]
    args += [
        "-i", str(source),
        "-t", f"{duration:.6f}",
        "-map", "0:v:0", "-an",
        "-c:v", "copy", "-bsf:v", bsf,
        "-muxdelay", "0", "-avoid_negative_ts", "make_zero",
        "-f", "mpegts", str(destination),
    ]
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"copy fragment failed: {proc.stderr[-300:]}")
