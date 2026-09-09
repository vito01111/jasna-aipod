# -*- coding: utf-8 -*-
"""段任务断点续跑补丁（JASNA_WORKDIR_PATCH）。

背景：让位（SIGKILL 硬停）后 smart render 的临时片段目录被 TemporaryDirectory
语义带走（SIGTERM 则被上游优雅清理主动删除），导致重试只能从头跑。

改法（不动推理核心，纯编排层）：
1. Pipeline.working_dir（上游已有字段 + CLI --working-directory）语义扩展为
   "确切段工作目录"：提供时不再建随机 TemporaryDirectory（不自删），
   成功 mux 后才清掉；失败/被杀保留片段供续跑。
2. 进入循环前清理半成品（*-raw.nut）与上次未完成的拼接产物；
   完整片段（000N.ts/.mkv）存在且非空则跳过该段（progress 补帧）。
3. web 层（jasna_web.py）负责：有段任务时预生成 /outputs/.work-<jobid> 传
   --working-directory；让位改 SIGKILL；重试时带同一 work_dir。

幂等：所有替换带 JASNA_WORKDIR_PATCH 标记检测，已打则跳过。
用法：python3 add_workdir_resume.py [/jasna]  （默认 SRC=/jasna）
"""
import sys
from pathlib import Path

MARK = "JASNA_WORKDIR_PATCH"

TEMP_OLD = """        try:
            with TemporaryDirectory(
                dir=work_root,
                prefix=f".{self.output_video.stem}.segments-",
            ) as temp_dir_name:
                temp_dir = Path(temp_dir_name)
                fragments: list[tuple[Path, float]] = []
                fragment_suffix = ".ts" if codec in {"h264", "hevc"} else ".mkv"
"""
TEMP_NEW = """        resume_mode = self.working_dir is not None   # """ + MARK + """
        try:
            temp_manager = (
                nullcontext(work_root) if resume_mode
                else TemporaryDirectory(
                    dir=work_root,
                    prefix=f".{self.output_video.stem}.segments-",
                )
            )
            with temp_manager as temp_dir_name:
                temp_dir = Path(temp_dir_name)
                fragments: list[tuple[Path, float]] = []
                fragment_suffix = ".ts" if codec in {"h264", "hevc"} else ".mkv"
                if resume_mode:
                    # 断点续跑：清半成品与上次未完成的拼接产物（完整片段保留复用）
                    for stale in temp_dir.glob("*-raw.nut"):
                        stale.unlink(missing_ok=True)
                    for stale in temp_dir.glob("fragments.ffconcat"):
                        stale.unlink(missing_ok=True)
                    for suffix in {".ts", ".mkv"}:
                        (temp_dir / ("assembled" + suffix)).unlink(missing_ok=True)
"""

SKIP_OLD = """                    duration = float((span.end_pts - span.start_pts) * index.time_base)
                    if span.is_render:
"""
SKIP_NEW = """                    duration = float((span.end_pts - span.start_pts) * index.time_base)
                    if normalized.is_file() and normalized.stat().st_size > 0:
                        # """ + MARK + """: 已完成片段直接复用（进度补齐后跳过）
                        progress.update(max(1, round(duration * metadata.video_fps)))
                        fragments.append((normalized, duration))
                        continue
                    if span.is_render:
"""

CLEAN_OLD = """                mux_final_output(
                    assembled,
                    self.input_video,
                    self.output_video,
                    codec=codec,
                )
        finally:
"""
CLEAN_NEW = """                mux_final_output(
                    assembled,
                    self.input_video,
                    self.output_video,
                    codec=codec,
                )
                if resume_mode:
                    shutil.rmtree(temp_dir, ignore_errors=True)   # """ + MARK + """: 成功即清工作目录
        finally:
"""

IMPORT_OLD = """from queue import Empty, Queue
from tempfile import TemporaryDirectory
"""
IMPORT_NEW = """from contextlib import nullcontext
from queue import Empty, Queue
from tempfile import TemporaryDirectory
"""
SHUTIL_OLD = """import os
import threading
"""
SHUTIL_NEW = """import os
import shutil
import threading
"""


def patch(src: Path) -> None:
    p = src / "jasna" / "pipeline.py"
    text = p.read_text(encoding="utf-8")
    if MARK in text:
        print("already patched:", p)
        return
    for old, new, name in (
        (IMPORT_OLD, IMPORT_NEW, "import nullcontext"),
        (SHUTIL_OLD, SHUTIL_NEW, "import shutil"),
        (TEMP_OLD, TEMP_NEW, "temp dir manager"),
        (SKIP_OLD, SKIP_NEW, "skip completed spans"),
        (CLEAN_OLD, CLEAN_NEW, "cleanup on success"),
    ):
        if text.count(old) != 1:
            raise SystemExit(f"anchor not unique/found for {name}: count={text.count(old)}")
        text = text.replace(old, new)
    p.write_text(text, encoding="utf-8")
    print("patched:", p)


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "/jasna")
    patch(target)
