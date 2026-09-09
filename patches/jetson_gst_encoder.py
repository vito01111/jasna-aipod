"""Jetson (L4T) GStreamer NVENC 输出通道——AIPod x3（Orin）部署层模块。

背景：L4T 不暴露桌面 NVENC SDK（ffmpeg/PyAV 的 *_nvenc 全不可用），硬编码只能走
GStreamer nvv4l2 元素（nvv4l2h264enc / nvv4l2h265enc，V4L2 M2M + NVMM）。本模块以
gst-launch 子进程接入，接口与 CpuVideoEncoder / NvencPipeEncoder duck-type 对齐：
encode(frame, pts, apply_lut) / __enter__ / __exit__。

设计（解耦约定与 nvenc_output.py 相同）：
- 不 import jasna 上游符号；接入点唯一 = cpu_encoder_fallback.make_encoder 探测链
  （PyAV nvenc → 本模块 → ffmpeg nvenc → CPU）。
- 帧通道优先 GPU NV12（复用 jasna 的 rgb_to_yuv CUDA 内核，同 nvenc_output）；
  不可用回退 rgb24 管道（gst videoconvert 上转）。
- smart_fragment：gst 无 NUT muxer → 直接写 mpegts 到 raw 路径（首字节 0x47），
  由 maybe_normalized_fragment 识别并 move 到 normalized，绕过 NUT 往返
  （与补丁#3 copy 片段直出 TS 同理）。
- 整片模式（mux_audio）：gst 只出视频，__exit__ 后用 ffmpeg8 无转码 remux 音轨。
- BT.709 标注：capssetter 置 caps colorimetry=bt709（lada 同款管线）。

环境开关：
- JASNA_ENCODER = auto|gst|nvenc|cpu（工厂层）
- JASNA_GST_BITRATE            强制码率（bps，覆盖源码率自适应）
- JASNA_GST_GSTLAUNCH          指定 gst-launch-1.0 路径
- JASNA_GST_RGB=1              强制 rgb24 管道（禁 GPU NV12 快通道）
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

_MARK = "JASNA_GST_ENC"
_MUXERS = {
    ".mp4": "qtmux", ".mov": "qtmux", ".m4v": "qtmux",
    ".mkv": "matroskamux", ".webm": "matroskamux",
    ".ts": "mpegtsmux", ".nut": "mpegtsmux",  # smart fragment 直出 TS
}
_PROBE_CACHE: dict[str, str | None] = {}

# 码率缺省按像素线性缩放（1080p 基准；lada pixel-scaling 同思路）
_BASE_BITRATE = {"h264": 5_000_000, "hevc": 3_200_000}
# 源码率自适应因子（对齐上游 media/video_encoder.SOURCE_BITRATE_CAP_FACTORS：
# HEVC 源 ×1.25 余量，其余 ×1.0）。缺省码率 = max(像素基准, 源码率×因子)。
_SOURCE_BITRATE_FACTORS = {"hevc": 1.25}


def _gst_inspect(element: str, gst_launch: str) -> bool:
    exe = str(Path(gst_launch).with_name("gst-inspect-1.0"))
    try:
        return subprocess.run([exe, element], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def probe_jetson_gst() -> str | None:
    """L4T gst 硬编码探测：gst-launch + nvv4l2 编码器 + 管线必备元素 + 15 帧真编冒烟。"""
    if "gst" in _PROBE_CACHE:
        return _PROBE_CACHE["gst"]
    forced = os.environ.get("JASNA_GST_GSTLAUNCH")
    candidates = [forced] if forced else [
        p for p in (shutil.which("gst-launch-1.0"),) if p
    ]
    needed = ("nvv4l2h264enc", "nvv4l2h265enc", "nvvidconv", "h264parse",
              "capssetter", "qtmux", "mpegtsmux")
    for gl in candidates:
        try:
            if not all(_gst_inspect(e, gl) for e in needed):
                logger.warning("[%s] %s 缺少 nvv4l2 管线元素", _MARK, gl)
                continue
            smoke = subprocess.run(
                [gl, "-q", "videotestsrc", "num-buffers=15",
                 "!", "video/x-raw,width=320,height=240,format=I420,framerate=30/1",
                 "!", "nvvidconv",
                 "!", "video/x-raw(memory:NVMM),format=NV12",
                 "!", "nvv4l2h264enc", "insert-sps-pps=true",
                 "!", "h264parse", "!", "fakesink"],
                capture_output=True, text=True, timeout=60)
            if smoke.returncode == 0:
                _PROBE_CACHE["gst"] = gl
                logger.info("[%s] using %s (nvv4l2 smoke ok)", _MARK, gl)
                return gl
            logger.warning("[%s] nvv4l2 smoke failed: %s", _MARK,
                           (smoke.stderr or "")[-300:])
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("[%s] probe %s failed: %r", _MARK, gl, e)
    _PROBE_CACHE["gst"] = None
    return None


def maybe_normalized_fragment(raw, normalized, codec: str) -> None:
    """补丁#12 钩子：raw 若为 gst 直出 mpegts（首字节 0x47）则直接落位，
    否则走上游 normalize_fragment（NUT→TS）。"""
    raw_p, norm_p = Path(raw), Path(normalized)
    try:
        with open(raw_p, "rb") as f:
            head = f.read(1)
        if head == b"\x47":
            shutil.move(str(raw_p), str(norm_p))
            logger.info("[%s] render fragment direct-TS: %s", _MARK, norm_p.name)
            return
    except OSError:
        pass
    from jasna.pipeline import normalize_fragment
    normalize_fragment(raw, normalized, codec=codec)


def _try_gpu_converter(metadata):
    """复用 jasna rgb_to_yuv CUDA 内核做 NV12 快通道（同 nvenc_output 逻辑）。"""
    try:
        from jasna.media.rgb_to_yuv import RgbToYuvConverter

        cs = str(getattr(metadata.color_space, "name", "ITU709"))
        variant = "bt709_limited" if "709" in cs else "bt601_limited"
        return RgbToYuvConverter(f"nv12_{variant}", device=torch.device("cuda"))
    except Exception as e:  # pragma: no cover - 回退路径
        logger.warning("[%s] GPU nv12 converter unavailable, rgb24 pipe: %r", _MARK, e)
        return None


class JetsonGstEncoder:
    """gst nvv4l2 子进程编码器（接口与 CpuVideoEncoder / NvencPipeEncoder 对齐）。"""

    def __init__(self, output_path, metadata, codec: str = "h264", *,
                 mux_audio: bool = True, smart_fragment: bool = False,
                 output_fps=None, fmp4: bool = False, **_ignored) -> None:
        self.output_path = str(output_path)
        self.metadata = metadata
        self.codec = codec.lower()
        self.gst_element = {"h264": "nvv4l2h264enc", "hevc": "nvv4l2h265enc"}.get(
            self.codec, "nvv4l2h264enc")
        self.smart_fragment = smart_fragment
        self.mux_audio = mux_audio and not smart_fragment
        self._gst = probe_jetson_gst()
        if not self._gst:
            raise RuntimeError(f"{_MARK}: gst nvv4l2 lane unavailable")
        self._conv = None if os.environ.get("JASNA_GST_RGB") == "1" else _try_gpu_converter(metadata)
        self._in_fmt = "NV12" if self._conv is not None else "BGR"
        fps = output_fps or (metadata.average_fps if metadata else 30.0)
        self.rate_frac = fps if isinstance(fps, Fraction) else Fraction(str(round(float(fps), 6)))
        self._frame_index = 0
        self._proc: subprocess.Popen | None = None
        self._err_file = None
        Path(self.output_path).parent.mkdir(parents=True, exist_ok=True)

    def _bitrate(self) -> int:
        forced = os.environ.get("JASNA_GST_BITRATE", "").strip()
        if forced.isdigit() and int(forced) > 100_000:
            return int(forced)
        w = int(self.metadata.video_width) if self.metadata else 1920
        h = int(self.metadata.video_height) if self.metadata else 1080
        scale = max(0.25, (w * h) / (1920 * 1080))
        floor = int(_BASE_BITRATE.get(self.codec, 5_000_000) * scale)
        # 源码率自适应：渲染段跟随源码率（2026-09-04 双机实测，Spark cq 模式
        # 自然落在 ~11Mbps，固定 5M 相当于白亏 2.2× 码率）；源码率不可知或
        # 低码率源回退像素基准 floor。
        src_bps = int(getattr(self.metadata, "video_bitrate", 0) or 0) if self.metadata else 0
        if src_bps <= 0:
            return floor
        factor = _SOURCE_BITRATE_FACTORS.get(
            str(getattr(self.metadata, "codec_name", "") or "").lower(), 1.0
        )
        return max(floor, int(src_bps * factor))

    def _build_cmd(self) -> list[str]:
        w = int(self.metadata.video_width)
        h = int(self.metadata.video_height)
        rate = f"{self.rate_frac.numerator}/{self.rate_frac.denominator}"
        muxer = "mpegtsmux" if self.smart_fragment else _MUXERS.get(
            Path(self.output_path).suffix.lower(), "qtmux")
        bitrate = self._bitrate()
        peak = int(bitrate * 1.35)

        cmd = [self._gst, "-q", "-e", "fdsrc", "fd=0",
               "!", f"rawvideoparse", f"format={self._in_fmt.lower()}",
               f"width={w}", f"height={h}", f"framerate={rate}"]
        if self._in_fmt == "BGR":
            cmd += ["!", "videoconvert", "!", "video/x-raw,format=I420"]
        cmd += ["!", "nvvidconv",
                "!", "video/x-raw(memory:NVMM),format=NV12",
                "!", self.gst_element,
                f"bitrate={bitrate}", f"peak-bitrate={peak}",
                "control-rate=1", "preset-level=2", "maxperf-enable=false",
                "insert-sps-pps=true", "iframeinterval=30", "idrinterval=30"]
        if self.codec == "h264":
            cmd += ["profile=4"]
        else:
            cmd += ["insert-vui=true"]
        stream_caps = "video/x-h264" if self.codec == "h264" else "video/x-h265"
        cmd += ["!", "h264parse" if self.codec == "h264" else "h265parse",
                "!", "capssetter", f"caps={stream_caps},colorimetry=(string)bt709",
                "!", "queue", "!", muxer,
                "!", "filesink", f"location={self.output_path}"]
        return cmd

    def __enter__(self):
        self._err_file = tempfile.NamedTemporaryFile(
            prefix="gst_err_", suffix=".log", delete=False)
        self._proc = subprocess.Popen(
            self._build_cmd(), stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=self._err_file)
        logger.info("[%s] %s -> %s (%s)", _MARK, self.gst_element,
                    self.output_path, "ts-fragment" if self.smart_fragment else "file")
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
                    raise RuntimeError(f"gst encoder exited {rc}: {tail}")
                if self.mux_audio:
                    self._remux_audio()
            else:
                self._proc.kill()
                self._proc.wait(timeout=30)
        finally:
            if self._err_file:
                self._err_file.close()
                Path(self._err_file.name).unlink(missing_ok=True)

    def _remux_audio(self) -> None:
        """gst 只出视频；整片模式用 ffmpeg 无转拷贝音轨（第二输入源文件）。"""
        src = str(self.metadata.video_file)
        out = self.output_path
        tmp = out + ".aremix.mp4"
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-i", out, "-i", src,
               "-map", "0:v:0", "-map", "1:a:0?", "-c", "copy",
               "-movflags", "+faststart", tmp]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode == 0 and Path(tmp).stat().st_size > 0:
            os.replace(tmp, out)
            logger.info("[%s] audio remuxed (%d frames)", _MARK, self._frame_index)
        else:
            logger.warning("[%s] audio remux failed (video kept): %s",
                           _MARK, (proc.stderr or "")[-300:])
            Path(tmp).unlink(missing_ok=True)
