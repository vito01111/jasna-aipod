#!/usr/bin/env python3
"""首启预烤引擎装配（jasna-aipod 商店镜像，0.1.28 起）。

解决新设备首个任务阻塞 15-60 分钟 TensorRT 引擎编译的问题。思路借自
lada-restore 的指纹化缓存键：TRT 引擎按宿主软件栈绑定（容器内 TRT 是
bind-mount 的宿主副本），跨设备/跨固件直接拷引擎可能拒载——所以预烤
引擎目录按「环境指纹」命名，装配时按当前环境指纹精确匹配：

  /jasna/model_weights.prebuilt/{fp}/     ← 产出端用同一 fingerprint() 命名
      *.engine + *_sub_engines/           （只放引擎；onnx/pt/pth 种子已有）

装配规则（幂等，model_weights 非空即跳过）：
  1. 种子权重复制 seed → model_weights（onnx/pt/pth，必做）；
  2. 指纹命中 prebuilt/{fp} → 引擎整体叠加拷入 → 「FAST PATH」开箱即满速；
  3. 未命中 → 只留种子 → 首个任务 ensure_engines_compiled 现场重编（= 0.1.27 前的现状）。

指纹 = L4T 版本（/etc/nv_tegra_release，宿主 bind-mount）+ TRT 版本
（宿主 dist-info 目录名）。环境变量可覆写：JASNA_PREBAKE_FP（强制指定，
用于回落路径测试）、JASNA_PREBAKE_TRT_DIST / JASNA_PREBAKE_SEED_DIR /
JASNA_PREBAKE_PREBUILT_DIR（目录覆写）。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

JASNA = Path(os.getenv("JASNA_HOME", "/jasna"))
MW = JASNA / "model_weights"
SEED = Path(os.getenv("JASNA_PREBAKE_SEED_DIR", str(JASNA / "model_weights.seed")))
PREBUILT = Path(os.getenv("JASNA_PREBAKE_PREBUILT_DIR", str(JASNA / "model_weights.prebuilt")))
TRT_DIST = Path(os.getenv("JASNA_PREBAKE_TRT_DIST", "/opt/jetson-python-dist"))


def trt_version() -> str:
    """宿主 TRT 版本（bind-mount 的 dist-info 目录名），读不到记 na。"""
    try:
        vers = [p.name[len("tensorrt-"):-len(".dist-info")]
                for p in TRT_DIST.glob("tensorrt-*.dist-info")]
        return max(vers) if vers else "na"
    except OSError:
        return "na"


def l4t_version() -> str:
    """宿主 L4T 版本（R36.5 这类短形式），读不到记 na。"""
    try:
        txt = Path("/etc/nv_tegra_release").read_text(errors="ignore")
        rel = re.search(r"R(\d+)", txt)
        rev = re.search(r"revision[:\s]*(\d+(?:\.\d+)?)", txt, re.IGNORECASE)
        if not rel:
            return "na"
        return f"R{rel.group(1)}" + (f".{rev.group(1)}" if rev else "")
    except OSError:
        return "na"


def fingerprint() -> str:
    return f"l4t{l4t_version()}-trt{trt_version()}"


def has_weights(d: Path) -> bool:
    return d.is_dir() and any(d.glob("*.onnx")) and any(d.glob("*.pth") or d.glob("*.pt"))


def has_engines(d: Path) -> bool:
    return d.is_dir() and (any(d.glob("*.engine")) or any(d.glob("*_sub_engines")))


def overlay_copy(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, dirs_exist_ok=True)


def main() -> int:
    if MW.is_dir() and any(MW.iterdir()):
        print("[prebake] model_weights 非空，跳过（幂等）")
        return 0
    fp = os.getenv("JASNA_PREBAKE_FP", "").strip() or fingerprint()
    print(f"[prebake] fingerprint={fp}")
    if not has_weights(SEED):
        print(f"[prebake] ERROR: 种子目录不可用 {SEED}（无 onnx/pth）——引擎将无法编译", flush=True)
        return 0  # 不阻塞容器启动；与种子缺失的旧行为一致（任务会显式报错）
    overlay_copy(SEED, MW)
    pre = PREBUILT / fp
    if has_engines(pre):
        overlay_copy(pre, MW)
        n_eng = len(list(MW.glob("*.engine")))
        n_sub = len(list(MW.glob("*_sub_engines")))
        print(f"[prebake] FAST PATH: 预烤引擎已装配（engine={n_eng}, sub_engines={n_sub}）"
              "——首个任务零编译", flush=True)
    else:
        print(f"[prebake] 无指纹匹配的预烤引擎（{pre}）→ 已播种种子，"
              "首个任务将现场编译 TensorRT 引擎（约 15-60 分钟）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
