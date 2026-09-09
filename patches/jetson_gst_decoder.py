"""Jetson (L4T) GStreamer NVDEC 解码通道——AIPod x3（Orin）部署层模块。

背景：L4T 不暴露桌面 NVDEC SDK（python_vali / PyAV cuda hwaccel 均不可用），
上游 auto 链在 Jetson 上必然落到 pyav 软解。1080p 软解实测 425fps 非瓶颈，但
硬解价值在：CPU 减负（多租户共存）、4K 素材、"不要 CPU 软解"合规。

管线设计（子进程对，无 gi 依赖）：
  ffmpeg8（解封装 + seek，-c:v copy 出 AnnexB ES）
    └ stdout → gst-launch: fdsrc ! h264/h265parse ! nvv4l2decoder(NVDEC)
               ! nvvidconv(VIC) ! video/x-raw,format=RGB ! fdsink stdout
  Python 从 gst stdout 按 W*H*3 顺序读帧。

PTS 语义（对齐 _ValiFrameSource 契约）：
  ES 无容器时间戳 → 用 ffprobe 取 seek 落点关键帧的真实 pts 作锚，
  后续 pts 按 CFR（video_fps_exact）累计重建；非关键帧 seek 时丢帧对齐。
  scan 不走本通道（cv2 独立路径）；smart render span 起点本就是关键帧。

启用：DECODE_BACKEND=jetson-gst（显式；auto 链不自动选，见补丁#13）。
限制：仅 h264/hevc；VFR 片源 pts 会有重建误差（上游管线本身按 CFR 设计）。
"""
from __future__ import annotations

import logging
import subprocess
from fractions import Fraction

import numpy as np
import torch

from jasna.media.video_decoder import VideoDecodeError

logger = logging.getLogger(__name__)

_MARK = "JASNA_GST_DEC"
_VIABLE: bool | None = None
_KF_CACHE: dict[str, list[float]] = {}

_ELEMENT_MAP = {"h264": "h264parse", "hevc": "h265parse"}
_RAW_FORMAT = {"h264": "h264", "hevc": "hevc"}


def _gst_ok(element: str) -> bool:
    try:
        return subprocess.run(
            ["gst-inspect-1.0", element], capture_output=True, timeout=15
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _keyframe_list(file: str) -> list[float]:
    """全片关键帧时刻表（秒，升序），按文件缓存。"""
    cached = _KF_CACHE.get(file)
    if cached is not None:
        return cached
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-skip_frame", "nokey",
         "-show_entries", "frame=pts_time", "-of", "csv=p=0", file],
        capture_output=True, text=True, timeout=120)
    kfs = []
    for line in (proc.stdout or "").splitlines():
        try:
            kfs.append(float(line.strip().rstrip(",")))
        except ValueError:
            continue
    if not kfs:
        raise VideoDecodeError(f"ffprobe keyframes failed for {file}: {proc.stderr[-200:]}")
    _KF_CACHE[file] = kfs
    return kfs


def _seek_anchor_pts(file: str, seek_ts: float | None, time_base: float) -> int:
    """ffmpeg -ss T -c copy 的落点关键帧 pts。

    落点规则（diag3 实测）：keyframe ≤ T 的最后一个（T 恰在关键帧上则命中自身）。
    不能用 -read_intervals 包窗口探测——其输出从 seek 关键帧起全量列出，首行
    会拿到上一个 GOP 的关键帧（曾致锚点错一个 GOP、首 GOP 被丢帧逻辑误杀）。
    """
    kfs = _keyframe_list(file)
    if seek_ts is None or seek_ts <= 0:
        return int(round(kfs[0] / time_base))
    cand = [k for k in kfs if k <= seek_ts + 0.05]
    anchor = cand[-1] if cand else kfs[0]
    return int(round(anchor / time_base))


class JetsonGstFrameSource:
    """接口对齐 _ValiFrameSource：width/height/frames(seek_ts)/close()。

    frames() 每次调用重建解码子进程对（每次 seek 一个新会话）；NVDEC 会话
    建立约百毫秒级，span/预览粒度可接受。构造函数含一次真解冒烟。
    """

    def __init__(self, file, batch_size, device, metadata, frame_stride):
        global _VIABLE
        self.file = str(file)
        self.batch_size = int(batch_size)
        self.device = device
        self.metadata = metadata
        self.frame_stride = max(1, int(frame_stride))
        self.codec = str(getattr(metadata, "codec_name", "")).lower()
        if self.codec not in _ELEMENT_MAP:
            raise VideoDecodeError(
                f"{_MARK}: codec {self.codec!r} has no nvv4l2 lane (h264/hevc only)")
        self.width = int(metadata.video_width)
        self.height = int(metadata.video_height)
        self._frame_bytes = self.width * self.height * 3
        fps = getattr(metadata, "video_fps_exact", None) or metadata.average_fps
        self._fps = fps if isinstance(fps, Fraction) else Fraction(str(round(float(fps), 6)))
        self._time_base = float(metadata.time_base)
        self._procs: list[subprocess.Popen] = []

        if _VIABLE is None:
            needed = ("nvv4l2decoder", "nvvidconv", _ELEMENT_MAP[self.codec], "fdsink")
            if not all(_gst_ok(e) for e in needed):
                _VIABLE = False
                raise VideoDecodeError(f"{_MARK}: gst elements missing ({needed})")
            try:
                n = 0
                for _b, _p in self._decode(None, warmup=True):
                    n += 1
                    break
                if n < 1:
                    raise VideoDecodeError(f"{_MARK}: warmup decode produced no frame")
            except BaseException as exc:
                _VIABLE = False
                raise VideoDecodeError(f"{_MARK}: warmup failed: {exc}") from exc
            _VIABLE = True
            logger.info("[%s] nvv4l2decoder lane viable", _MARK)

    # ---- 进程对管理 ----

    def _spawn(self, seek_ts: float | None) -> subprocess.Popen:
        ff_args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        if seek_ts is not None and seek_ts > 0:
            ff_args += ["-ss", f"{float(seek_ts):.6f}"]
        ff_args += ["-i", self.file, "-map", "0:v:0", "-an", "-sn",
                    "-c:v", "copy", "-f", _RAW_FORMAT[self.codec], "pipe:1"]
        # nvvidconv 在 L4T gst 1.20 无 RGB raw caps（实测 "can't handle caps RGB"）：
        # VIC 出 I420（colorimetry 按源定，防 videoconvert 猜 601 造成色偏——
        # 实测 bt709 源默认走 601 时 maxdiff=46/mean=8.8，强制 709 后 6/1.4）
        cs_name = str(getattr(self.metadata.color_space, "name", "ITU709") or "ITU709")
        colorimetry = "smpte170m" if "601" in cs_name else "bt709"
        self._colorimetry = colorimetry
        gst_args = ["gst-launch-1.0", "-q", "fdsrc",
                    "!", _ELEMENT_MAP[self.codec],
                    "!", "nvv4l2decoder", "enable-max-performance=true",
                    "!", "nvvidconv",
                    "!", f"video/x-raw,format=I420,colorimetry={colorimetry}",
                    "!", "videoconvert",
                    "!", "video/x-raw,format=RGB",
                    "!", "fdsink", "fd=1", "sync=false"]
        ff_proc = subprocess.Popen(ff_args, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL)
        gst_proc = subprocess.Popen(gst_args, stdin=ff_proc.stdout,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
        if ff_proc.stdout is not None:
            ff_proc.stdout.close()  # gst 持有唯一引用后父进程关闭自己的副本
        self._procs = [ff_proc, gst_proc]
        return gst_proc

    def _read_exact(self, stream, n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            chunk = stream.read(n - len(buf))
            if not chunk:
                return bytes(buf) if buf else None
            buf.extend(chunk)
        return bytes(buf)

    def _kill_procs(self) -> None:
        for p in self._procs:
            if p.poll() is None:
                p.kill()
        for p in self._procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self._procs = []

    # ---- 帧生成 ----

    def _decode(self, seek_ts, warmup: bool = False):
        gst = self._spawn(seek_ts)
        assert gst.stdout is not None
        target_pts = None
        if seek_ts is not None and seek_ts > 0:
            target_pts = int(round(float(seek_ts) / self._time_base))
        anchor_pts = _seek_anchor_pts(self.file, seek_ts, self._time_base)
        anchor_sec = anchor_pts * self._time_base
        batch = torch.empty((self.batch_size, 3, self.height, self.width),
                            dtype=torch.uint8)
        pts: list[int] = []
        frame_index = 0
        try:
            while True:
                data = self._read_exact(gst.stdout, self._frame_bytes)
                if data is None or len(data) < self._frame_bytes:  # EOF/尾部残包
                    break
                idx = frame_index
                frame_index += 1
                frame_pts = int(round(
                    (anchor_sec + idx / float(self._fps)) / self._time_base))
                if target_pts is not None and frame_pts < target_pts:
                    continue  # 非关键帧 seek：丢掉落点前的帧
                if not warmup and idx % self.frame_stride != 0:
                    continue
                arr = np.frombuffer(data, dtype=np.uint8)
                frame = torch.from_numpy(
                    arr.reshape(self.height, self.width, 3).copy()).permute(2, 0, 1)
                if warmup:
                    yield frame.unsqueeze(0).to(self.device), [frame_pts]
                    return
                batch[len(pts)] = frame
                pts.append(frame_pts)
                if len(pts) == self.batch_size:
                    yield batch.clone().to(self.device), pts
                    pts = []
            if not warmup and pts:
                yield batch[: len(pts)].clone().to(self.device), pts
        finally:
            self._kill_procs()

    def frames(self, seek_ts: float | None):
        yield from self._decode(seek_ts)

    def close(self) -> None:
        self._kill_procs()
