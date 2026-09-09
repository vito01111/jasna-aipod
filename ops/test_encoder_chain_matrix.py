#!/usr/bin/env python3
"""M3.7 编码器工厂探测矩阵单测（无 pytest 依赖，容器内直接跑）。

mock 各 lane 的存在性 × JASNA_ENCODER × codec，断言 make_encoder 选对 lane：
  lane 组合：PyAV nvenc / gst-nvv4l2 / ffmpeg-nvenc / 全无
  环境开关：auto（缺省）/ gst / nvenc / cpu
  codec：h264 / av1（av1 不走硬编 lane）

用法（thor 或 agxorin 镜像内）：
  python3 /jasna/test_encoder_chain_matrix.py
"""
from __future__ import annotations

import importlib
import os
import sys
import types

sys.path.insert(0, os.environ.get("JASNA_HOME", "/jasna"))

PASS = FAIL = 0


def _lane_class(lane):
    """可被工厂当编码器类调用的 mock（实例化后 .name = lane 名）。"""
    class C:
        def __init__(self, *args, **kwargs):
            self.name = lane
    C.__name__ = lane.replace("-", "_")
    return C


def _setup_env(pav_nvenc, gst_ok, nvenc_ok, env_choice, codec="h264"):
    """注入 mock 模块后重载工厂，返回干净的 make_encoder。"""
    for m in ("jasna.jetson_gst_encoder", "jasna.nvenc_output",
              "jasna.media.video_encoder", "jasna.cpu_encoder_fallback"):
        sys.modules.pop(m, None)
    import jasna  # noqa: F401  确保包在

    # PyAV：mock av.Codec(nvenc_name,'w') 可用性
    fake_av = types.ModuleType("av")

    def _codec(name, direction):
        if direction == "w" and pav_nvenc and name.endswith("_nvenc"):
            return object()
        raise RuntimeError("codec unavailable")

    fake_av.Codec = _codec
    sys.modules["av"] = fake_av

    ve_mod = types.ModuleType("jasna.media.video_encoder")
    ve_mod.NvidiaVideoEncoder = _lane_class("NvidiaVideoEncoder")
    sys.modules["jasna.media.video_encoder"] = ve_mod

    gst_mod = types.ModuleType("jasna.jetson_gst_encoder")
    gst_mod.probe_jetson_gst = lambda: "/fake/gst-launch" if gst_ok else None
    gst_mod.JetsonGstEncoder = _lane_class("gst")
    sys.modules["jasna.jetson_gst_encoder"] = gst_mod

    nv_mod = types.ModuleType("jasna.nvenc_output")
    nv_mod.probe_nvenc_ffmpeg = lambda: "/fake/ffmpeg" if nvenc_ok else None
    nv_mod.NvencPipeEncoder = _lane_class("nvenc-pipe")
    sys.modules["jasna.nvenc_output"] = nv_mod

    if env_choice is None:
        os.environ.pop("JASNA_ENCODER", None)
    else:
        os.environ["JASNA_ENCODER"] = env_choice

    fac = importlib.import_module("jasna.cpu_encoder_fallback")
    return fac.make_encoder, codec


def _expect(label, pav, gst, nvenc, choice, codec, want):
    global PASS, FAIL
    make, c = _setup_env(pav, gst, nvenc, choice, codec)
    try:
        enc = make(output_path="/tmp/x.mp4", codec=c)
        got = getattr(enc, "name", None) or type(enc).__name__
        ok = want in got
    except Exception as e:  # CPU lane 之外不应抛
        got, ok = f"EXC:{type(e).__name__}", False
    if ok:
        PASS += 1
        print(f"  [PASS] {label} -> {got}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label} -> {got}（期望含 {want}）")


print("== 编码器工厂探测矩阵 ==")
# PyAV nvenc 命中（Spark 形态）
_expect("pav-nvenc 可用",            True,  False, False, None,  "h264", "NvidiaVideoEncoder")
# Jetson 形态：PyAV 无 → gst lane
_expect("gst lane 可用",             False, True,  False, None,  "h264", "gst")
_expect("gst 挂→nvenc 兜底",         False, False, True,  None,  "h264", "nvenc-pipe")
_expect("全挂→CPU",                  False, False, False, None,  "h264", "Cpu")
# 环境开关
_expect("强制 gst 且可用",           False, True,  False, "gst", "h264", "gst")
# 显式 lane = 严格模式：挂了不兜底其它硬编 lane，直接 CPU；仅 auto 走全级联
# （工厂里 "trying ffmpeg nvenc" 的日志文案与该行为不符——上游小瑕疵，非缺陷）
_expect("强制 gst 但挂→严格 CPU",    False, False, True,  "gst", "h264", "Cpu")
_expect("强制 nvenc",                False, True,  True,  "nvenc", "h264", "nvenc-pipe")
_expect("强制 cpu（gst 可用仍 CPU）", False, True,  True,  "cpu", "h264", "Cpu")
# codec 面：av1 不进硬编 lane（仅 h264/hevc）
_expect("av1 走 CPU",               False, True,  True,  None,  "av1", "Cpu")

print(f"== 汇总 PASS={PASS} FAIL={FAIL} ==")
sys.exit(0 if FAIL == 0 else 1)
