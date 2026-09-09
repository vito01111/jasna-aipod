# -*- coding: utf-8 -*-
"""编码卡死看门狗升级补丁（JASNA_STALL_ESCALATION_PATCH）。

背景（2026-09-05 390JAC-068 实证）：GB10 上 nvenc ffmpeg 子进程段编码中途挂死
（0% CPU、半截 -raw.nut）→ BlendEncode 崩溃退出（error_holder 有账但永远轮不到算）→
SecondaryRestore 堵死在 encode_queue.put（消费方已死，队列永远满）→ MainThread
在 pipeline._run_pass 的 t.join() 无限等。vram_offloader 的 stall 看门狗每 30s
dump 一次线程栈但只告警，任务在 web 层永远显示 running。

改法（只动 vram_offloader.py 看门狗线程，不碰推理核心）：
1. elapsed > STALL_ESCALATE_SECONDS（默认 900s，心跳=每编码一帧刷一次，
   连续渲染段之间不存在 15 分钟级合法空窗；30s 告警阈值长期运行零误报佐证）：
   SIGKILL 本进程全部直接子进程（/proc 遍历 ppid），给卡在管道写上的线程
   一次 EPIPE 自解开、走 error_holder 正常收尾的机会。
2. 再等 STALL_EXIT_GRACE_SECONDS（默认 180s）仍无编码产出（join 死锁形状，
   杀子进程救不回来）：flush 日志后 os._exit(2)。web 层 proc.wait() 非零
   → 任务标 failed → /jobs/<id>/retry 复用 work_dir 断点续跑。
   注意不能用 SIGTERM：上游优雅清理会删掉片段目录（见 add_workdir_resume.py 头注）。

心跳恢复（else 分支）时复位升级状态，pause_stall_check（BlendEncode 正常收尾
标记）继续整体跳过检查，误报面与原 30s 告警一致。

幂等：所有替换带 JASNA_STALL_ESCALATION_PATCH 标记检测，已打则跳过。
用法：python3 add_encode_stall_escalation.py [/jasna]  （默认 SRC=/jasna）
"""
import sys
from pathlib import Path

MARK = "JASNA_STALL_ESCALATION_PATCH"

IMPORT_OLD = """import logging
import sys
import threading
import time
import traceback
"""
IMPORT_NEW = """import logging
import os
import signal
import sys
import threading
import time
import traceback
"""

CONST_OLD = """_POLL_INTERVAL = 0.1
_MIB = 1024 * 1024
STALL_WARN_SECONDS = 30.0
"""
CONST_NEW = """_POLL_INTERVAL = 0.1
_MIB = 1024 * 1024
STALL_WARN_SECONDS = 30.0
# """ + MARK + """: 告警只管日志，超过升级阈值后看门狗负责止损
STALL_ESCALATE_SECONDS = 900.0
STALL_EXIT_GRACE_SECONDS = 180.0
"""

STATE_OLD = """        self._last_encode_time: list[float] | None = None
        self._last_stall_warn_time: float = 0.0
        self._stall_check_paused = False
"""
STATE_NEW = """        self._last_encode_time: list[float] | None = None
        self._last_stall_warn_time: float = 0.0
        self._stall_check_paused = False
        self._escalated_at: float | None = None   # """ + MARK + """
"""

CHECK_OLD = """                self._dump_stall_diagnostics(elapsed)
                self._last_stall_warn_time = now
        else:
            self._last_stall_warn_time = 0.0
"""
CHECK_NEW = """                self._dump_stall_diagnostics(elapsed)
                self._last_stall_warn_time = now
            self._maybe_escalate_stall(elapsed, now)   # """ + MARK + """
        else:
            self._last_stall_warn_time = 0.0
            self._escalated_at = None   # """ + MARK + """: 心跳恢复，复位升级状态
"""

METHODS_OLD = """    def _dump_stall_diagnostics(self, elapsed: float) -> None:
"""
METHODS_NEW = """    def _maybe_escalate_stall(self, elapsed: float, now: float) -> None:
        \"\"\"""" + MARK + """: 告警只是日志，这里负责止损（两段式）。\"\"\"
        if elapsed <= STALL_ESCALATE_SECONDS:
            return
        if self._escalated_at is None:
            self._escalated_at = now
            killed = self._kill_child_processes()
            _log.warning(
                "[vram-offloader] stall escalation: no encoded frame for %.0fs, "
                "killed child processes %s; hard exit in %.0fs if the pipeline does not unwind",
                elapsed,
                killed,
                STALL_EXIT_GRACE_SECONDS,
            )
            return
        if now - self._escalated_at >= STALL_EXIT_GRACE_SECONDS:
            _log.warning(
                "[vram-offloader] stall escalation: still stalled %.0fs after killing "
                "children (deadlocked join); hard-exiting so the job fails and can "
                "resume from completed segments",
                elapsed,
            )
            for handler in logging.getLogger().handlers:
                try:
                    handler.flush()
                except Exception:
                    pass
            os._exit(2)

    @staticmethod
    def _kill_child_processes() -> list[int]:
        me = os.getpid()
        killed: list[int] = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid == me:
                continue
            try:
                with open(f"/proc/{pid}/stat", "rb") as fh:
                    stat = fh.read()
                ppid = int(stat[stat.rindex(b")") + 2:].split()[1])
            except (OSError, ValueError, IndexError):
                continue
            if ppid == me:
                try:
                    os.kill(pid, signal.SIGKILL)
                    killed.append(pid)
                except OSError:
                    continue
        return killed

    def _dump_stall_diagnostics(self, elapsed: float) -> None:
"""


def patch(src: Path) -> None:
    p = src / "jasna" / "vram_offloader.py"
    text = p.read_text(encoding="utf-8")
    if MARK in text:
        print("already patched:", p)
        return
    for old, new, name in (
        (IMPORT_OLD, IMPORT_NEW, "import os/signal"),
        (CONST_OLD, CONST_NEW, "escalation constants"),
        (STATE_OLD, STATE_NEW, "escalation state"),
        (CHECK_OLD, CHECK_NEW, "check hook + reset"),
        (METHODS_OLD, METHODS_NEW, "escalation methods"),
    ):
        if text.count(old) != 1:
            raise SystemExit(f"anchor not unique/found for {name}: count={text.count(old)}")
        text = text.replace(old, new)
    p.write_text(text, encoding="utf-8")
    print("patched:", p)


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "/jasna")
    patch(target)
