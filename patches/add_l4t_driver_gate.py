#!/usr/bin/env python3
"""补丁 #11（幂等）：L4T 驱动版本门放行（JASNA_L4T_DRIVER_PATCH）。

上游 check_gpu_driver_version() 要求桌面驱动 ≥580（Linux）。Jetson L4T 的
版本号体系完全不同：AGX Orin JP6 的 nvidia-smi 报 540.5.0（对应 CUDA 12.6 /
TRT 10.3，功能齐全），会被误拦。L4T 环境（存在 /etc/nv_tegra_release）直接放行。

幂等：os_utils.py 已含 JASNA_L4T_DRIVER_PATCH 标记则跳过。
"""
import sys
from pathlib import Path

MARK = "JASNA_L4T_DRIVER_PATCH"

ANCHOR = '''    if major < MIN_DRIVER_VERSION:
        return False, f"{version_str} (requires {MIN_DRIVER_VERSION}+)"'''

NEW = '''    if major < MIN_DRIVER_VERSION:
        # JASNA_L4T_DRIVER_PATCH: Jetson L4T 的驱动版本号体系与桌面 NVIDIA 驱动无关
        # （AGX Orin JP6 nvidia-smi 报 540.5.0，对应 CUDA 12.6/TRT 10.3），版本门槛
        # 不适用；L4T 环境判定 = /etc/nv_tegra_release 存在。GB10 Spark 驱动 ≥580
        # 走原路径不受影响。
        import os as _os

        if _os.path.exists("/etc/nv_tegra_release"):
            return True, f"{version_str} (L4T; gate bypassed by JASNA_L4T_DRIVER_PATCH)"
        return False, f"{version_str} (requires {MIN_DRIVER_VERSION}+)"'''


def main(src_dir: str) -> None:
    path = Path(src_dir) / "jasna" / "os_utils.py"
    text = path.read_text(encoding="utf-8")
    if MARK in text:
        print(f"[{MARK}] already patched")
        return
    if ANCHOR not in text:
        print(f"[{MARK}] !! anchor not found — upstream drift, manual review needed")
        return
    path.write_text(text.replace(ANCHOR, NEW), encoding="utf-8")
    print(f"[{MARK}] patched jasna/os_utils.py")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
