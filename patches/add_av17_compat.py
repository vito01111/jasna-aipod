#!/usr/bin/env python3
"""补丁 #10（幂等）：PyAV 17 兼容层（Orin/JP6 路线）。

背景：上游要求 av>=18（BT2020 色彩空间枚举），但 av 18.x 全系 requires-python>=3.11
（wheel 为 cp311-abi3），而 JP6 的 torch 生态只有 cp310 → x3 只能装 av 17。

做法：落一个 jasna/av17_compat.py 提供 AvColorspace 代理——av>=18 时原样透传
（GB10 行为零变化），av 17 缺失成员（BT2020）返回可哈希 sentinel，模块级字典
import 不再崩溃；真遇 BT2020 片源在使用点报"不支持"而非 import 崩溃。
6 处 `from av.video.reformatter import Colorspace as AvColorspace` 导入点改指 shim。

幂等：文件已含 jasna.av17_compat 导入则跳过。marker: JASNA_AV17_COMPAT。
"""
import re
import sys
from pathlib import Path

MARKER = "JASNA_AV17_COMPAT"

COMPAT_MODULE = '''"""JASNA_AV17_COMPAT — PyAV>=18 requires Python>=3.11; the JetPack 6 stack
(Orin AIPod) pins Python 3.10 + av 17, which lacks Colorspace.BT2020 (and any
other member added after 17). This proxy passes real members through untouched;
missing ones resolve to a hashable sentinel so module-level dicts still import.
BT.709/601 content is unaffected; BT2020 sources fail at the point of use."""
import av
from av.video.reformatter import Colorspace as _RealColorspace


class _UnavailableColorspace:
    __slots__ = ("name",)

    def __init__(self, name):
        self.name = name

    def __repr__(self):  # 错误信息里能看出是哪个成员、为什么缺
        return f"<Colorspace.{self.name} (unavailable on PyAV {av.__version__})>"


class _ColorspaceProxy:
    def __getattr__(self, name):
        try:
            return getattr(_RealColorspace, name)
        except AttributeError:
            return _UnavailableColorspace(name)


AvColorspace = _RealColorspace if hasattr(_RealColorspace, "BT2020") else _ColorspaceProxy()
'''

PY310_COMPAT_MODULE = '''"""JASNA_AV17_COMPAT — Python 3.10 fallbacks for the JetPack 6 stack (AIPod x3).
Upstream targets Python 3.12; the only 3.11+ runtime feature the non-GUI code
uses is enum.StrEnum (jasna/accelerator.py). This is the canonical pre-3.11
recipe from the Python docs, sufficient for vendor constants."""
from enum import Enum


class StrEnum(str, Enum):
    __str__ = str.__str__

    def _generate_next_value_(name, start, count, last_values):  # noqa: N805
        return name.lower()
'''

# accelerator.py 的 StrEnum 导入改 try/except（模块级，缩进 0）
STRENUM_OLD = "from enum import StrEnum"
STRENUM_NEW = (
    "try:\n"
    "    from enum import StrEnum\n"
    "except ImportError:  # Python 3.10（JetPack 6 / AIPod x3）→ JASNA_AV17_COMPAT\n"
    "    from jasna.py310_compat import StrEnum"
)

# (file, old import line, new import lines)
SITES = [
    ("jasna/media/video_encoder.py",
     "from av.video.reformatter import Colorspace as AvColorspace, ColorRange as AvColorRange",
     ["from av.video.reformatter import ColorRange as AvColorRange",
      "from jasna.av17_compat import AvColorspace"]),
    ("jasna/media/__init__.py",
     "from av.video.reformatter import Colorspace as AvColorspace, ColorRange as AvColorRange",
     ["from av.video.reformatter import ColorRange as AvColorRange",
      "from jasna.av17_compat import AvColorspace"]),
    ("jasna/media/yuv_to_rgb.py",
     "from av.video.reformatter import Colorspace as AvColorspace",
     ["from jasna.av17_compat import AvColorspace"]),
    ("jasna/pipeline.py",
     "from av.video.reformatter import Colorspace as AvColorspace",
     ["from jasna.av17_compat import AvColorspace"]),
    ("jasna/streaming_pipeline.py",
     "from av.video.reformatter import Colorspace as AvColorspace",
     ["from jasna.av17_compat import AvColorspace"]),
]


def main(src_dir: str) -> None:
    src = Path(src_dir)
    compat_path = src / "jasna" / "av17_compat.py"
    compat_path.write_text(COMPAT_MODULE, encoding="utf-8")
    print(f"[{MARKER}] wrote {compat_path}")
    py310_path = src / "jasna" / "py310_compat.py"
    py310_path.write_text(PY310_COMPAT_MODULE, encoding="utf-8")
    print(f"[{MARKER}] wrote {py310_path}")

    # accelerator.py: StrEnum 导入兜底（幂等）
    accel = src / "jasna" / "accelerator.py"
    text = accel.read_text(encoding="utf-8")
    if "py310_compat" in text:
        print(f"[{MARKER}] already patched: jasna/accelerator.py")
    elif STRENUM_OLD in text:
        accel.write_text(text.replace(STRENUM_OLD, STRENUM_NEW), encoding="utf-8")
        print(f"[{MARKER}] patched jasna/accelerator.py (StrEnum fallback)")
    else:
        print(f"[{MARKER}] !! anchor not found in jasna/accelerator.py (upstream drift?)")

    patched = skipped = 0
    for rel, old, new_lines in SITES:
        path = src / rel
        text = path.read_text(encoding="utf-8")
        if "jasna.av17_compat" in text:
            print(f"[{MARKER}] already patched: {rel}")
            skipped += 1
            continue
        if old not in text:
            print(f"[{MARKER}] !! anchor not found in {rel}: {old}")
            print(f"[{MARKER}] !! manual review needed (upstream drift?)")
            continue
        indent = ""
        m = re.search(rf"^([ \t]*){re.escape(old)}", text, re.M)
        if m and m.group(1):  # 函数/块内局部导入：后续行保持缩进
            indent = m.group(1)
        # 注意：str.replace 保留锚点行首的原有缩进，故首行不得再加 indent（会双重缩进）
        replacement = new_lines[0] + "".join("\n" + indent + line for line in new_lines[1:])
        text = text.replace(old, replacement)
        path.write_text(text, encoding="utf-8")
        print(f"[{MARKER}] patched {rel}")
        patched += 1

    print(f"[{MARKER}] done: {patched} patched, {skipped} already-done")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
