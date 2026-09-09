"""Jasna web 后端（部署层，标准库零依赖）：批处理队列 + 视频库 + 模型清单。

端口 8766。与 --stream 服务（8765）同容器共存；GPU 单占策略 = 作业串行。
子进程调 `python -m jasna --input ... --output ...`，解析 tqdm 进度行。
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote


def _safe_name(raw: str) -> str | None:
    """URL 段还原成文件名并做穿越校验（%2F..%2F 之类直接拒）。"""
    name = unquote(raw)
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        return None
    return name

VIDEOS_DIR = Path(os.environ.get("JASNA_VIDEOS_DIR", "/videos"))
OUTPUTS_DIR = Path(os.environ.get("JASNA_OUTPUTS_DIR", "/outputs"))
JASNA_HOME = Path(os.environ.get("JASNA_HOME", "/jasna"))
FFPROBE = os.environ.get("JASNA_FFMPEG_DIR", "/opt/ff8/bin") + "/ffprobe"
FFMPEG_DIR = os.environ.get("JASNA_FFMPEG_DIR", "/opt/ff8/bin")

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".ts", ".m2ts"}
ALLOWED_DETECTORS = {
    "rfdetr-v6", "rfdetr-v6-large", "rfdetr-vr-v1",
    "lada-yolo-v2", "lada-yolo-v4", "zelefans-vr-yolo-v2",
}

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
# create_job 并发输出预留：占用判定与入队之间隔着 ffprobe/probe 等慢调用，
# 7ms 双 POST 实证两条都能读到对方插入前的 claimed → 同名输出。预留带时间戳，超时自愈
_output_reserve_lock = threading.Lock()
_reserved_outputs: dict[str, float] = {}
_reserved_inputs: dict[str, float] = {}   # retry 同输入预留：dup 检查到 create_job 插入之间的竞态窗
_queue: list[str] = []
_worker_cv = threading.Condition()


def _probe(path: Path) -> dict:
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries",
             "format=duration", "-show_entries",
             "stream=width,height,avg_frame_rate", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30, check=False)
        d = json.loads(out.stdout or "{}")
        st = next((s for s in d.get("streams", []) if s.get("width")), {})
        fps = 0.0
        try:
            num, _, den = st.get("avg_frame_rate", "0/1").partition("/")
            if float(den):
                fps = float(num) / float(den)
        except (TypeError, ValueError):
            fps = 0.0
        return {
            "duration": round(float(d.get("format", {}).get("duration", 0)), 1),
            "width": st.get("width"), "height": st.get("height"),
            "fps": round(fps, 3),
        }
    except Exception:
        return {"duration": 0, "width": None, "height": None, "fps": 0.0}


def list_videos() -> list[dict]:
    items = []
    for p in sorted(VIDEOS_DIR.iterdir()):
        if p.is_file() and p.suffix.lower() in VIDEO_EXTS:
            info = _probe(p)
            items.append({
                "name": p.name, "path": str(p),
                "size_mb": round(p.stat().st_size / 1048576, 1),
                "mtime": int(p.stat().st_mtime),
                **info,
            })
    return items


def list_models() -> dict:
    weights = JASNA_HOME / "model_weights"
    det = {name: (weights / f"{name.replace('lada-yolo', 'lada_mosaic_detection_model_')}.pt").exists()
           for name in []}
    # 显式枚举（文件名映射与上游 detection_registry 对齐）
    det_files = {
        "rfdetr-v6": "rfdetr-v6.onnx",
        "rfdetr-v6-large": "rfdetr-v6-large.onnx",
        "rfdetr-vr-v1": "rfdetr-vr-v1.onnx",
        "lada-yolo-v2": "lada_mosaic_detection_model_v2.pt",
        "lada-yolo-v4": "lada_mosaic_detection_model_v4_fast.pt",
        "zelefans-vr-yolo-v2": "lada_vr_mosaic_detection_model_v2_accurate.pt",
    }
    det = {k: (weights / v).is_file() for k, v in det_files.items()}
    restore = (weights / "lada_mosaic_restoration_model_generic_v1.2.pth").is_file()
    return {"detection": det, "restoration_ready": restore,
            "default_detection": ("rfdetr-v6-large" if det.get("rfdetr-v6-large")
                                  else "lada-yolo-v4" if det.get("lada-yolo-v4")
                                  else next((k for k, v in det.items() if v), None))}


_ERR_HINT = re.compile(r"error|exception|not found|no such|out of memory|killed|failed|refused", re.I)


def _error_hint(buf) -> str:
    """从日志尾部逆序提取一条失败特征行（跳过 tqdm 进度行），给前端直接展示。"""
    for line in reversed(buf):
        line = line.strip()
        if not line or "it/s" in line or ("fps" in line and "%" in line):
            continue
        if _ERR_HINT.search(line):
            return line[:160]
    return ""


def _parse_progress(line: str, total_frames: int) -> tuple[float, float] | None:
    # 采信 CLI 进度条自带的百分比：自算 frames/total 会错——total_frames 按 30fps 估
    # （59.94fps 片半程即 100%），smart render 只处理 render span（低覆盖片全程 <10%）
    m = re.search(r"(\d+)%\|.*?\((\d+)f\).*?Speed:\s+([\d.]+)fps", line)
    if m and total_frames:
        return min(100.0, float(m.group(1))), float(m.group(3))
    return None


def _job_worker(job_id: str):
    job = _jobs.get(job_id)
    if job is None:  # 已被 clear-finished 清掉（队列里残留的取消任务）
        return
    cmd = job["command"]
    # 任务优先于观看：开跑前踢掉流式预览，避免与流式管线并发抢显存双双 OOM
    try:
        urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:8765/stop", method="POST"), timeout=3).read()
    except Exception:
        pass
    proc = subprocess.Popen(
        cmd, cwd=str(JASNA_HOME), start_new_session=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={**os.environ, "PATH": FFMPEG_DIR + ":" + os.environ.get("PATH", ""),
             "PYTHONPATH": _child_pythonpath(), **job.get("env", {})},
    )
    job["pid"] = proc.pid
    job["state"] = "running"
    job["started_at"] = time.time()  # 真实开跑时刻：耗时=finished-started（排队等待不算）
    _persist_jobs()
    buf = deque(maxlen=400)
    job["log_buf"] = buf
    for line in proc.stdout:
        buf.append(line.rstrip()[:300])
        job["log_lines"] = len(buf)
        job["log_tail"] = list(buf)[-6:]
        pr = _parse_progress(line, job["total_frames"])
        if pr:
            job["progress_pct"], job["fps"] = round(pr[0], 1), round(pr[1], 1)
    code = proc.wait()
    job["state"] = "cancelled" if job.get("state") == "cancelling" else ("done" if code == 0 else "failed")
    if job["state"] == "failed":
        job["error_hint"] = _error_hint(buf)
    job["finished_at"] = time.time()
    out = Path(job["output"])
    job["output_size_mb"] = round(out.stat().st_size / 1048576, 1) if out.exists() else 0
    # 全量日志落盘（对应 GUI 日志面板导出）：内存 deque 重启即失，文件兜底
    try:
        log_dir = OUTPUTS_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / f"job-{job_id}.log").write_text("\n".join(buf) + "\n", encoding="utf-8")
        job["log_file"] = f"logs/job-{job_id}.log"
        _cap_job_logs()   # P4：日志封顶
    except OSError:
        pass
    _persist_jobs()
    # P3：完成后动作 del_source——成品已落地且无其他活跃任务占用该源片时自动清理素材
    if job["state"] == "done" and str((job.get("payload") or {}).get("post_export_action", "")).strip().lower() == "del_source":
        src = Path(job["input"])
        try:
            if src.is_file() and not any(
                x.get("input") == str(src) and x["id"] != job_id
                and x.get("state") in {"queued", "running", "paused", "cancelling"}
                for x in _jobs.values()
            ):
                src.unlink()
                with _scans_lock:
                    for sid, sc in list(_scans.items()):
                        if sc.get("input") == str(src) and sc.get("state") not in {"queued", "running"}:
                            _scans.pop(sid, None)
                for sf in OUTPUTS_DIR.glob("scan-*.json"):
                    try:
                        if json.loads(sf.read_text(encoding="utf-8")).get("input") == str(src):
                            sf.unlink(missing_ok=True)
                    except Exception:
                        continue
                print(f"[pea] deleted source after success: {src}", flush=True)
        except OSError as e:
            print(f"[pea] del_source failed: {e!r}", flush=True)
    _autochain_kick_idle()   # JASNA_AUTOCHAIN_PATCH: 队列清空 → 补发挂起的入库扫描



_scans: dict[str, dict] = {}
_scans_lock = threading.Lock()
_scan_serial = threading.Semaphore(1)

# ---- 后端自动处理链（JASNA_AUTOCHAIN_PATCH）：入库→自动扫描→检出段→自动建任务 ----
# 前端 stagedScanTick 的服务端化：页面关闭/刷新/断网不再断链（2026-09-05 MKMP-636 教训，
# 自动编排全在浏览器 localStorage+轮询里，上传断/页面关整条链就哑）。
# 开关与档位走环境变量：JASNA_AUTO_PROC=off 关闭；JASNA_DEFAULT_CRF 设自动任务质量档
# （不设则落到编码器默认 cq20）；_pending_autoscan 为内存态，web 重启丢失后由前端
# stagedScanTick 兜底补扫（两端都有"已有扫描/已有任务"去重，不会双跑）。
AUTO_PROC = os.environ.get("JASNA_AUTO_PROC", "on").strip().lower() not in {"off", "0", "false"}
# P3（2026-09-05）：任务运行时是否允许并发扫描。默认关（GPU 互扰会拖慢任务）；
# Spark 128G UMA 扛得住并存（扫描约 20GB VRAM），批量入库日可开——
# 开启后 _autochain_after_ingest 不再挂起，直接开扫（扫描本身仍串行）。
AUTO_SCAN_PARALLEL = os.environ.get("JASNA_AUTO_SCAN_PARALLEL", "off").strip().lower() in {"on", "1", "true"}
AUTO_CRF = os.environ.get("JASNA_DEFAULT_CRF", "").strip()
AUTO_DETECTION_MODEL = os.environ.get("JASNA_DEFAULT_DETECTION_MODEL", "").strip() or "rfdetr-v6-large"
_pending_autoscan: set[str] = set()


def _autochain_crf() -> int | None:
    try:
        v = int(AUTO_CRF)
    except ValueError:
        return None
    return v if 14 <= v <= 32 else None


def _has_active_job() -> bool:
    return any(j.get("state") in {"running", "queued", "paused"} for j in _jobs.values())


def _has_job_for_input(input_path: str) -> bool:
    return any(str(j.get("input", "")) == input_path for j in _jobs.values())


def _has_scan_for_input(input_path: str) -> bool:
    with _scans_lock:
        return any(s.get("input") == input_path and s.get("src_valid") is not False
                   for s in _scans.values())


def _spawn_scan(src: Path, det: str) -> dict:
    scan_id = uuid.uuid4().hex[:12]
    st = src.stat()
    sc = {"id": scan_id, "input": str(src), "detection_model": det,
          "state": "queued", "spans": [], "created_at": time.time(), "tail": [],
          "src_size": st.st_size, "src_mtime": st.st_mtime}
    with _scans_lock:
        _scans[scan_id] = sc
    threading.Thread(target=_run_scan, args=(scan_id,), daemon=True).start()
    return sc


def _autochain_after_ingest(input_path: str) -> None:
    """上传合并落位后调用：GPU 空闲即开扫，忙则挂起，任务收尾时 _autochain_kick_idle 补发。"""
    print(f"[autochain] ingest hook: {input_path} AUTO_PROC={AUTO_PROC} "
          f"has_scan={_has_scan_for_input(input_path)} active={_has_active_job()}", flush=True)
    if not AUTO_PROC or _has_scan_for_input(input_path):
        return
    if _has_active_job() and not AUTO_SCAN_PARALLEL:
        _pending_autoscan.add(input_path)
        return
    try:
        _spawn_scan(Path(input_path), AUTO_DETECTION_MODEL)
    except Exception:
        pass


def _autochain_after_scan(scan: dict) -> None:
    """扫描收尾调用：检出段且该输入无任何任务 → 按设备缺省档自动建任务（0 段留给人工）。"""
    if not AUTO_PROC:
        return
    try:
        if scan.get("state") != "done" or not scan.get("spans") or scan.get("src_valid") is False:
            return
        input_path = str(scan.get("input", ""))
        if not input_path or _has_job_for_input(input_path):
            return
        payload: dict = {
            "input": input_path,
            "segments": ",".join(f"{round(float(s['start']))}-{round(float(s['end']))}"
                                 for s in scan["spans"]),
            "conflict": "auto",
            "detection_model": scan.get("detection_model") or AUTO_DETECTION_MODEL,
        }
        crf = _autochain_crf()
        if crf is not None:
            payload["crf"] = crf
        create_job(payload)
    except Exception as e:
        print(f"[autochain] scan->job failed: {e!r}", flush=True)


def _autochain_kick_idle() -> None:
    """任务收尾调用：队列清空后补发挂起的入库扫描。"""
    if not AUTO_PROC or not _pending_autoscan or _has_active_job():
        return
    for input_path in list(_pending_autoscan):
        _pending_autoscan.discard(input_path)
        if not _has_scan_for_input(input_path) and Path(input_path).is_file():
            try:
                _spawn_scan(Path(input_path), AUTO_DETECTION_MODEL)
            except Exception:
                pass


def _load_persisted_scans() -> None:
    """重启恢复：/outputs/scan-*.json 回填扫描注册表。
    顺带保留策略（2026-09-05）：只留最新 40 个——单个 json 含 sample_scores
    + span 缩略图最大 ~3MB，只增不减会无限吃 /outputs。"""
    files = sorted(OUTPUTS_DIR.glob("scan-*.json"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files[40:]:
        try:
            f.unlink()
        except OSError:
            pass
    for f in files[:40]:
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
            scan_id = f.stem.replace("scan-", "")
            _scans[scan_id] = {
                "id": scan_id, "input": payload.get("input", "?"),
                "detection_model": payload.get("detection_model", "?"),
                "state": "done", "spans": payload.get("spans", []),
                "duration": payload.get("duration", 0),
                "src_size": payload.get("src_size"), "src_mtime": payload.get("src_mtime"),
                "created_at": f.stat().st_mtime, "tail": [],
            }
        except Exception:
            continue


_load_persisted_scans()


UPLOAD_PART_SIZE = 32 * 1024 * 1024
_upload_sessions: dict[str, dict] = {}


# ---- 临时区回收（2026-09-05 文件管理 P0/P1）：上传会话与孤儿 workdir 都"只增不减" ----
UPLOAD_SESSION_TTL = 48 * 3600          # 半途会话 48h 后整目录回收（占盘 + 挡同名 409）
WORKDIR_GC_DAYS = 7                     # workdir 孤儿（记录已清/丢失）7 天后回收


def _gc_upload_sessions() -> None:
    root = OUTPUTS_DIR / "uploads"
    cutoff = time.time() - UPLOAD_SESSION_TTL
    if root.is_dir():
        for d in root.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    shutil.rmtree(d, ignore_errors=True)
            except OSError:
                continue
    for uid in [u for u, s in _upload_sessions.items() if s.get("created_at", 0) < cutoff]:
        _upload_sessions.pop(uid, None)


def _gc_orphan_workdirs() -> None:
    refs = {(j.get("payload") or {}).get("work_dir") for j in _jobs.values()}
    cutoff = time.time() - WORKDIR_GC_DAYS * 86400
    for d in OUTPUTS_DIR.glob(".work-*"):
        try:
            if d.is_dir() and str(d) not in refs and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue


def _start_janitor() -> None:
    def _loop():
        while True:
            try:
                _gc_upload_sessions()
                _gc_orphan_workdirs()
            except Exception as e:
                print(f"[janitor] gc failed: {e!r}", flush=True)
            time.sleep(3600)
    threading.Thread(target=_loop, name="janitor", daemon=True).start()


def _cap_job_logs(keep: int = 200) -> None:
    """任务日志封顶（P4）：保留最新 keep 个，超出删除。"""
    try:
        logs = sorted((OUTPUTS_DIR / "logs").glob("job-*.log"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for f in logs[keep:]:
            f.unlink(missing_ok=True)
    except OSError:
        pass


def _upload_dir(upload_id: str) -> Path:
    d = OUTPUTS_DIR / "uploads" / upload_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_upload_sessions() -> None:
    root = OUTPUTS_DIR / "uploads"
    if not root.is_dir():
        return
    for meta_file in root.glob("*/meta.json"):
        try:
            payload = json.loads(meta_file.read_text(encoding="utf-8"))
            upload_id = meta_file.parent.name
            parts = {int(p.name) for p in meta_file.parent.iterdir() if p.name.isdigit()}
            _upload_sessions[upload_id] = {
                "id": upload_id, **payload,
                "parts": parts, "created_at": meta_file.stat().st_mtime,
            }
        except Exception:
            continue


_load_upload_sessions()


# ---- 媒体标记 sidecar：/outputs/.media-meta.json（收藏等；文件即状态，无 DB） ----
MEDIA_META_FILE = OUTPUTS_DIR / ".media-meta.json"
_media_meta: dict[str, dict] = {}
_media_meta_lock = threading.Lock()


def _load_media_meta() -> None:
    global _media_meta
    try:
        raw = json.loads(MEDIA_META_FILE.read_text(encoding="utf-8"))
        # 静默清孤儿（成品已删的条目）
        _media_meta = {n: v for n, v in raw.items()
                       if isinstance(v, dict) and (OUTPUTS_DIR / n).is_file()} \
            if isinstance(raw, dict) else {}
    except Exception:
        _media_meta = {}


_load_media_meta()


def _persist_media_meta() -> None:
    try:
        tmp = MEDIA_META_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_media_meta, ensure_ascii=False), encoding="utf-8")
        tmp.replace(MEDIA_META_FILE)
    except OSError:
        pass


def _run_scan(scan_id: str):
    import selectors
    sc = _scans[scan_id]
    with _scan_serial:
        proc = None
        watchdog = None
        try:
            sc["state"] = "running"
            out_json = OUTPUTS_DIR / f"scan-{scan_id}.json"
            cmd = ["python3", str(JASNA_HOME / "scan_worker.py"),
                   "--input", sc["input"], "--out", str(out_json),
                   "--detection-model", sc["detection_model"]]
            proc = subprocess.Popen(cmd, cwd=str(JASNA_HOME),
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    start_new_session=True,
                                    env={**os.environ, "PATH": FFMPEG_DIR + ":" + os.environ.get("PATH", ""),
                                         "PYTHONPATH": _child_pythonpath()})

            def _kill_group():
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass

            watchdog = threading.Timer(3600.0, _kill_group)  # 硬上限：任何悬挂不得焊死串行锁
            watchdog.daemon = True
            watchdog.start()
            sel = selectors.DefaultSelector()
            sel.register(proc.stdout, selectors.EVENT_READ)
            while True:
                if not sel.select(timeout=30.0):
                    if proc.poll() is not None:
                        break  # 子进程已退出但管道被孙进程拖着不关 → 主动收尾，不再傻等 EOF
                    continue
                line = proc.stdout.readline()
                if not line:
                    break
                sc["tail"] = (sc.get("tail") or [])[-8:] + [line.strip()[:200]]
                if '"done"' in line:
                    try:
                        payload = json.loads(line)
                        full = json.loads(Path(payload["out"]).read_text(encoding="utf-8"))
                        sc["spans"] = full.get("spans", [])
                        sc["duration"] = full.get("duration", 0)
                        full["src_size"] = sc.get("src_size")
                        full["src_mtime"] = sc.get("src_mtime")
                        Path(payload["out"]).write_text(json.dumps(full, ensure_ascii=False), encoding="utf-8")
                    except Exception:
                        pass
            rc = proc.wait()
            sc["state"] = "done" if rc == 0 else "failed"
        except Exception:
            sc["state"] = "failed"
        finally:
            if watchdog:
                watchdog.cancel()
            if proc is not None and proc.poll() is None:
                _kill_group_local = proc.pid
                try:
                    os.killpg(_kill_group_local, signal.SIGKILL)
                except Exception:
                    pass
            sc["finished_at"] = time.time()
        _autochain_after_scan(sc)   # JASNA_AUTOCHAIN_PATCH: done 且检出段 → 自动建任务


# ---- 扫描阈值重过滤（对应上游 GUI MosaicScanWorker"改阈值免重扫"）----
SCAN_MIN_GAP = 2.0
SCAN_PAD = 0.6


def _rescan_at_threshold(scan_id: str, threshold: float) -> dict:
    """从持久化 JSON 的 sample_scores 按新阈值重聚合 spans，不碰检测器。"""
    sc = _scans[scan_id]
    path = OUTPUTS_DIR / f"scan-{scan_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    samples = payload.get("sample_scores")
    if samples is None:
        raise ValueError("该扫描无逐样本分数（旧版扫描产出），请重新扫描")
    duration = float(payload.get("duration") or 0)
    ratio_by_ts = {s[0]: s[2] for s in samples if len(s) > 2}
    old_thumbs = {round(o["start"], 1): o.get("thumb", "") for o in sc.get("spans", [])}
    positives = sorted(s[0] for s in samples if s[1] >= threshold)
    spans = []
    if positives:
        groups = [[positives[0]]]
        for ts in positives[1:]:
            if ts - groups[-1][-1] > SCAN_MIN_GAP:
                groups.append([ts])
            else:
                groups[-1].append(ts)
        for g in groups:
            start = round(max(0.0, g[0] - SCAN_PAD), 2)
            end = round(min(duration, g[-1] + SCAN_PAD + 0.5), 2)
            if end - start < 1.0:
                end = min(duration, start + 1.0)
            end = min(end, duration - 0.15) if duration > 0.3 else end
            spans.append({"start": start, "end": end,
                          "max_box_ratio": max((ratio_by_ts.get(ts, 0.0) for ts in g), default=0.0),
                          "samples": len(g),
                          "thumb": old_thumbs.get(round(start, 1), "")})
    threshold = round(min(0.9, max(0.05, threshold)), 2)
    sc["spans"] = spans
    sc["threshold"] = threshold
    payload["spans"] = spans
    payload["threshold"] = threshold
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return sc


def _scan_valid(sc) -> bool:
    """源文件指纹校验：size/mtime 未变才算缓存命中（无指纹的旧记录视为有效）。"""
    if sc.get("src_size") is None:
        return True
    try:
        st = Path(sc["input"]).stat()
        return st.st_size == sc.get("src_size") and abs(st.st_mtime - sc.get("src_mtime", 0)) < 1
    except OSError:
        return False


def _queue_loop():
    while True:
        with _worker_cv:
            while not _queue:
                _worker_cv.wait()
            job_id = _queue.pop(0)
        # 排队期间被取消的任务直接跳过，不再执行
        if _jobs.get(job_id, {}).get("state") == "cancelled":
            continue
        _job_worker(job_id)
        with _worker_cv:
            _worker_cv.notify_all()


# ---- 任务持久化：/outputs/.jobs.json（重启不再丢任务历史/队列） ----
JOBS_FILE = OUTPUTS_DIR / ".jobs.json"


def _persist_jobs():
    try:
        with _jobs_lock:
            terminal = [j for j in _jobs.values()
                        if j["state"] in {"done", "failed", "cancelled", "interrupted"}]
            terminal.sort(key=lambda j: j.get("created_at", 0), reverse=True)
            keep = {j["id"] for j in terminal[:60]}   # 终态记录封顶（2026-09-05 P4）：只增不减会无限累积
            for jid in [jid for jid, j in _jobs.items()
                        if j["state"] in {"done", "failed", "cancelled", "interrupted"}
                        and jid not in keep]:
                del _jobs[jid]
            data = [{k: v for k, v in j.items() if k != "log_buf"} for j in _jobs.values()]
        tmp = JOBS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(JOBS_FILE)
    except (OSError, TypeError):
        pass


def _load_jobs():
    """重启恢复：排队任务重新入队；running/cancelling 子进程已死 → interrupted 可重试。"""
    if not JOBS_FILE.is_file():
        return
    try:
        data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return
    for d in data:
        jid = d.get("id")
        if not jid:
            continue
        st = d.get("state")
        if st in {"running", "cancelling", "paused"}:
            d["state"] = "interrupted"
            d.pop("pid", None)
        _jobs[jid] = d
        if st == "queued":
            _queue.append(jid)
    _queue.sort(key=lambda jid: (
        _jobs[jid]["seq"] if isinstance(_jobs.get(jid, {}).get("seq"), int) else 10 ** 9,
        _jobs[jid].get("created_at", 0)))


_load_jobs()
threading.Thread(target=_queue_loop, daemon=True).start()


def _child_pythonpath() -> str:
    """子进程 PYTHONPATH：/jasna 前置 + 保留父进程（AIPod 上含 TRT bind 的
    /opt/jetson-python-dist——覆盖式赋值会让任务进程 import tensorrt 失败）。"""
    inherited = os.environ.get("PYTHONPATH", "")
    return str(JASNA_HOME) + (":" + inherited if inherited else "")


def create_job(payload: dict) -> dict:
    src = Path(str(payload.get("input", "")))
    if not src.is_file() or VIDEOS_DIR.resolve() not in src.resolve().parents:
        # 重试/发起都可能命中：源片已删时给人话，别让用户猜英文内部话术
        raise ValueError("源文件已不在素材库（可能已被删除），请重新上传后再发起")
    det = str(payload.get("detection_model", "")).strip()
    if det and det not in ALLOWED_DETECTORS:
        raise ValueError("unknown detection model")
    if not det:
        # 缺省不用 CLI 默认值（上游默认 rfdetr-v6 未必装了权重），
        # 用 /models 同款探测：挑一个权重在场的模型
        det = list_models().get("default_detection") or ""
        if det and det not in ALLOWED_DETECTORS:
            det = ""
    # 输出名：显式 output_name 或 pattern（{original}=输入名）；缺省 {original}-restored.mp4
    pattern = str(payload.get("output_pattern", "")).strip()
    if pattern:
        out_name = Path(pattern.replace("{original}", src.stem)).name
    else:
        out_name = Path(payload.get("output_name") or (src.stem + "-restored.mp4")).name
    # 封装只收 mp4/mkv；其他后缀（含空）一律落到 .mp4
    if Path(out_name).suffix.lower() not in {".mp4", ".mkv"}:
        out_name = out_name + ".mp4"
    # 冲突策略：auto=自动改名(默认) / overwrite=覆盖 / skip=跳过
    # 冲突判定含队列中已声明的输出（未落盘也算占用）+ 并发创建预留（见 _reserved_outputs 注）
    conflict = str(payload.get("conflict", "auto")).strip().lower()
    with _output_reserve_lock:
        now_ts = time.time()
        for k in [k for k, t in _reserved_outputs.items() if now_ts - t > 120]:
            _reserved_outputs.pop(k, None)
        claimed = {j["output"] for j in _jobs.values()
                   if j.get("state") in {"queued", "running"}}
        claimed |= set(_reserved_outputs)
        out = OUTPUTS_DIR / out_name
        if (str(out) in claimed or out.exists()) and conflict not in {"overwrite"}:
            if conflict == "skip":
                raise ValueError(f"输出已存在（skip）：{out_name}")
            stem, suf = out_name[: -len(Path(out_name).suffix)], Path(out_name).suffix
            for i in range(1, 1000):
                cand = OUTPUTS_DIR / f"{stem}-{i}{suf}"
                if str(cand) not in claimed and not cand.exists():
                    out = cand
                    break
        _reserved_outputs[str(out)] = now_ts
    codec = str(payload.get("codec", "hevc")).lower()
    # av1 = ff8 CPU 编码（libsvtav1，无 av1_nvenc），慢但可用
    if codec not in {"hevc", "h264", "av1"}:
        codec = "hevc"
    segments = str(payload.get("segments", "")).strip()
    if segments:
        # smart render 约束：--segments 输出 codec 必须与输入一致
        probe_out = subprocess.run(
            [FFPROBE, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "default=nw=1:nokey=1", str(src)],
            capture_output=True, text=True, timeout=30, check=False)
        src_codec = (probe_out.stdout or "").strip().splitlines()[0] if probe_out.stdout.strip() else ""
        codec = {"h264": "h264", "hevc": "hevc", "av1": "av1"}.get(src_codec, codec)
    cmd = ["python3", "-m", "jasna",
           "--input", str(src), "--output", str(out),
           "--codec", codec, "--log-level", "info"]
    if det:
        cmd += ["--detection-model", det]
    if segments:
        cmd += ["--segments", segments]
    thr = payload.get("detection_score_threshold")
    try:
        if thr is not None and 0 < float(thr) < 1:
            cmd += ["--detection-score-threshold", str(float(thr))]
    except (TypeError, ValueError):
        pass
    # 设备档位缺省（JASNA_DEFAULT_*：AIPod 等设备按环境注入推荐档，payload 显式值优先）
    _dev_defaults = {
        "batch_size": os.environ.get("JASNA_DEFAULT_BATCH_SIZE"),
        "max_clip_size": os.environ.get("JASNA_DEFAULT_MAX_CLIP"),
    }
    for key, flag in (("batch_size", "--batch-size"), ("temporal_overlap", "--temporal-overlap"),
                      ("max_clip_size", "--max-clip-size"),
                      ("max_detection_gap", "--max-detection-gap"),
                      ("min_detection_duration", "--min-detection-duration")):
        val = payload.get(key)
        if val is None:
            val = _dev_defaults.get(key)
        if val is not None:
            try:
                cmd += [flag, str(int(val))]
            except (TypeError, ValueError):
                pass
    den = str(payload.get("denoise", "")).strip().lower()
    if den and den in {"low", "medium", "high"}:
        cmd += ["--denoise", den]
    step = str(payload.get("denoise_step", "")).strip().lower()
    if step in {"after_primary", "after_secondary"}:
        cmd += ["--denoise-step", step]
    # 布尔项对齐 GUI 语义：CLI 默认即 True，仅显式 --no-* 才真正关闭
    if "enable_crossfade" in payload:
        if not payload.get("enable_crossfade"):
            cmd += ["--no-enable-crossfade"]
    if "scene_detection" in payload:
        if not payload.get("scene_detection"):
            cmd += ["--no-scene-detection"]
    if payload.get("vr_mode") in {"auto", "off", "sbs", "sbs-fisheye"}:
        cmd += ["--vr-mode", str(payload["vr_mode"])]
    if payload.get("fp16") is False:
        cmd += ["--no-fp16"]
    if payload.get("compile_basicvsrpp") is False:
        cmd += ["--no-compile-basicvsrpp"]
    # 质量档位 → JASNA_CRF 环境变量（cpu_encoder_fallback 读取，仅 x264/x265）
    job_env = {}
    try:
        crf = int(payload.get("crf") or 0)
        if 14 <= crf <= 32:
            job_env["JASNA_CRF"] = str(crf)
    except (TypeError, ValueError):
        pass
    # 编码器通道：nvenc 硬编（GB10）/ cpu（画质优先）/ 缺省 auto（探测到 NVENC 即用）
    enc_choice = str(payload.get("encoder", "")).strip().lower()
    if enc_choice in {"nvenc", "cpu"}:
        job_env["JASNA_ENCODER"] = enc_choice
    try:
        sharp = float(payload.get("sharpen") or 0)
        if sharp > 0:
            cmd += ["--sharpen", str(min(1.0, max(0.05, sharp)))]
    except (TypeError, ValueError):
        pass
    # 自定义编码器参数（key=value,逗号分隔 或 JSON 对象），整段作单个 argv 传递不经 shell
    enc_settings = str(payload.get("encoder_settings", "")).strip()
    if enc_settings:
        cmd += ["--encoder-settings", enc_settings]
    if payload.get("retarget_high_fps"):
        cmd += ["--retarget-high-fps"]
    if payload.get("fmp4") and not segments:
        cmd += ["--fmp4"]  # CLI 约束：fmp4 与 --segments 互斥
    # 导出后动作：只放行 command（shutdown 不提供——Spark 是共享盒子）
    if str(payload.get("post_export_action", "")).strip().lower() == "command":
        pec = str(payload.get("post_export_command", "")).strip()
        if pec:
            cmd += ["--post-export-action", "command", "--post-export-command", pec]
    # 断点续跑（JASNA_WORKDIR_PATCH）：段任务预生成确定性工作目录传 --working-directory；
    # 重试时 payload 带旧 work_dir 且目录仍在 → 复用（已完成片段跳过）
    job_id = uuid.uuid4().hex[:12]
    work_dir = ""
    if segments:
        old_wd = str(payload.get("work_dir", "") or "")
        reusable = False
        if old_wd:
            wd_path = Path(old_wd)
            reusable = (wd_path.is_dir() and wd_path.parent == OUTPUTS_DIR
                        and wd_path.name.startswith(".work-"))
        if reusable:
            work_dir = old_wd
        else:
            wd = OUTPUTS_DIR / f".work-{job_id}"
            wd.mkdir(parents=True, exist_ok=True)
            work_dir = str(wd)
        cmd += ["--working-directory", work_dir]
        payload = dict(payload)
        payload["work_dir"] = work_dir
    elif "work_dir" in payload:
        payload = dict(payload)
        payload.pop("work_dir", None)
    probe = _probe(src)
    total = probe.get("duration", 0)
    job = {
        "id": job_id, "input": str(src), "output": str(out),
        "detection_model": det or "default", "codec": codec, "segments": segments,
        "state": "queued", "progress_pct": 0.0, "fps": 0.0,
        "created_at": time.time(), "command": cmd, "env": job_env,
        "payload": payload,
        "total_frames": int(total * (probe.get("fps") or 30.0)) or 1,
        "log_tail": [], "log_lines": 0, "output_size_mb": 0,
    }
    with _jobs_lock:
        _jobs[job["id"]] = job
    with _worker_cv:
        _queue.append(job["id"])
        job["seq"] = len(_queue) - 1
        _worker_cv.notify_all()
    _persist_jobs()
    with _output_reserve_lock:   # 已入 _jobs（claimed 按 state 判定），预留使命完成
        _reserved_outputs.pop(str(out), None)
    return job


# ---- 懒猫网盘桥（WebDAV，可配置：宿主 /jasna/drive.json）----
# {"url": "http://盒子:端口/网盘webdav路径", "user": "...", "pass": "...",
#  "remote_dir": "/工作台" }
DRIVE_CFG_PATH = JASNA_HOME / "drive.json"


def _drive_cfg():
    try:
        cfg = json.loads(DRIVE_CFG_PATH.read_text())
        if cfg.get("url"):
            return cfg
    except Exception:
        pass
    return None


def _dav_url(cfg, name=None):
    base = cfg["url"].rstrip("/") + cfg.get("remote_dir", "").rstrip("/")
    if name:
        from urllib.parse import quote
        base += "/" + quote(name)
    return base


def _dav_headers(cfg, extra=None):
    import base64
    h = {"Authorization": "Basic " + base64.b64encode(
        f"{cfg.get('user', '')}:{cfg.get('pass', '')}".encode()).decode()}
    if extra:
        h.update(extra)
    return h


# ---- 系统状态（底栏轮询） ----
_stats_last_cpu = None  # (idle_total, total, ts)


def _read_cpu_pct():
    global _stats_last_cpu
    try:
        lines = open("/proc/stat").read().splitlines()
        parts = [int(x) for x in lines[0].split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        total = sum(parts)
        now = time.time()
        if _stats_last_cpu is None or now - _stats_last_cpu[2] > 30:
            _stats_last_cpu = (idle, total, now)
            return None
        didle, dtotal = idle - _stats_last_cpu[0], total - _stats_last_cpu[1]
        _stats_last_cpu = (idle, total, now)
        if dtotal <= 0:
            return 0.0
        return round((1 - didle / dtotal) * 100, 1)
    except Exception:
        return None


_vram_probe = {"mod": None}


def _read_vram():
    """torch.cuda.mem_get_info（懒加载一次模块）；GB10 UMA 报告含 torch 缓存。"""
    if _vram_probe["mod"] is None:
        try:
            import torch
            torch.cuda.init()
            _vram_probe["mod"] = torch.cuda
        except Exception:
            _vram_probe["mod"] = False
    if not _vram_probe["mod"]:
        return None
    try:
        free, total = _vram_probe["mod"].mem_get_info()
        return {"used_gb": round((total - free) / 1073741824, 1),
                "total_gb": round(total / 1073741824, 1)}
    except Exception:
        return None


def _read_gpu_util():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5)
        return int(out.stdout.strip().splitlines()[0]) if out.returncode == 0 else None
    except Exception:
        return None


def _engines_compiled() -> bool:
    """M2.2：基础引擎是否已编译（basicvsrpp 子引擎 ≥6 件 + 任一 rfdetr TRT 引擎）。
    新装机为 False——首个任务会先触发 TRT 编译（15-60 分钟），前端据此提示。
    rfdetr 不锁 bs1-2 具体名：各机按 batch 档位编译出 bs1-4/8/16 等变体
    （2026-09-05 Spark 误报横幅实证：引擎齐但无 bs1-2，被判未编译）。"""
    try:
        import glob
        weights = Path(os.environ.get("JASNA_HOME", ".")) / "model_weights"
        sub = glob.glob(str(weights / "*_sub_engines" / "*.engine"))
        rfdetr = glob.glob(str(weights / "rfdetr*.engine"))
        return len(sub) >= 6 and bool(rfdetr)
    except Exception:
        return False


_STALE_VER_RE = re.compile(r"^(.*?)-restored(?:-\d+)?\.(mp4|mkv)$", re.I)
_space_cache: dict = {"ts": 0.0, "data": None}


def _tree_bytes(root: Path) -> tuple[int, int]:
    total = count = 0
    if not root.is_dir():
        return 0, 0
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            try:
                total += os.stat(os.path.join(dirpath, fn)).st_size
                count += 1
            except OSError:
                continue
    return total, count


def space_stats() -> dict:
    """空间占用统计（2026-09-05）：df + 各区扫描（成品/素材/临时/杂项），
    成品按 -restored(-N) 版本链拆出旧版可回收量。15s 缓存防轮询放大。"""
    now = time.time()
    if _space_cache["data"] and now - _space_cache["ts"] < 15:
        return _space_cache["data"]
    du = shutil.disk_usage(str(OUTPUTS_DIR))
    outs = [f for f in OUTPUTS_DIR.iterdir()
            if f.is_file() and f.suffix.lower() in {".mp4", ".mkv"}]
    outs_bytes = sum(f.stat().st_size for f in outs)
    groups: dict[str, list[tuple[float, int]]] = {}
    for f in outs:
        m = _STALE_VER_RE.match(f.name)
        if not m:
            continue
        groups.setdefault(m.group(1), []).append((f.stat().st_mtime, f.stat().st_size))
    stale_bytes = stale_n = 0
    for versions in groups.values():
        if len(versions) < 2:
            continue
        versions.sort(reverse=True)   # 最新 mtime 在前
        stale_bytes += sum(sz for _mt, sz in versions[1:])
        stale_n += len(versions) - 1
    vid_bytes = vid_n = 0
    if VIDEOS_DIR.is_dir():
        for f in VIDEOS_DIR.iterdir():
            if f.is_file() and f.suffix.lower() in VIDEO_EXTS:
                vid_bytes += f.stat().st_size
                vid_n += 1
    up_bytes, up_n = _tree_bytes(OUTPUTS_DIR / "uploads")
    wd_bytes = wd_n = 0
    for d in OUTPUTS_DIR.glob(".work-*"):
        b, n2 = _tree_bytes(d)
        wd_bytes += b
        wd_n += 1
    misc_bytes, _ = _tree_bytes(OUTPUTS_DIR / "logs")
    for f in OUTPUTS_DIR.glob("scan-*.json"):
        try:
            misc_bytes += f.stat().st_size
        except OSError:
            continue
    other = max(0, du.used - outs_bytes - vid_bytes - up_bytes - wd_bytes - misc_bytes)
    data = {
        "total": du.total, "used": du.used, "avail": du.free,
        "outputs": {"bytes": outs_bytes, "count": len(outs)},
        "stale": {"bytes": stale_bytes, "count": stale_n},
        "videos": {"bytes": vid_bytes, "count": vid_n},
        "temp": {"bytes": up_bytes + wd_bytes, "sessions": up_n, "workdirs": wd_n},
        "misc": {"bytes": misc_bytes},
        "other": other,
    }
    _space_cache.update(ts=now, data=data)
    return data


def _clear_stale_outputs() -> dict:
    """批量清成品旧版（2026-09-06）：每组版本链保留 mtime 最新，收藏（sidecar ★）跳过。"""
    outs = [f for f in OUTPUTS_DIR.iterdir()
            if f.is_file() and f.suffix.lower() in {".mp4", ".mkv"}]
    groups: dict[str, list[tuple[float, int, Path]]] = {}
    for f in outs:
        m = _STALE_VER_RE.match(f.name)
        if m:
            groups.setdefault(m.group(1), []).append((f.stat().st_mtime, f.stat().st_size, f))
    deleted = freed = skipped = 0
    with _media_meta_lock:
        favs = {n for n, v in _media_meta.items() if v.get("fav")}
    for versions in groups.values():
        if len(versions) < 2:
            continue
        versions.sort(reverse=True)   # 最新 mtime 在前
        for _mt, sz, f in versions[1:]:
            if f.name in favs:
                skipped += 1
                continue
            try:
                f.unlink()
                deleted += 1
                freed += sz
            except OSError:
                pass
    _space_cache["ts"] = 0.0   # 立即失效缓存，让前端刷新看到真实占用
    return {"ok": True, "deleted": deleted, "freed": freed, "skipped_fav": skipped}


def _clean_temp(min_age_hours: float = 1.0) -> dict:
    """立即清临时区（2026-09-06）：>1h 的上传会话 + 无任务引用的孤儿 workdir。
    比 janitor（48h/7天）激进，但活跃上传（目录 mtime 新）与待续跑断点不受影响。"""
    freed = sessions = workdirs = 0
    cutoff = time.time() - min_age_hours * 3600
    root = OUTPUTS_DIR / "uploads"
    if root.is_dir():
        for d in root.iterdir():
            try:
                if d.is_dir() and d.stat().st_mtime < cutoff:
                    b, _n = _tree_bytes(d)
                    shutil.rmtree(d, ignore_errors=True)
                    freed += b
                    sessions += 1
            except OSError:
                continue
    for uid in [u for u, s in _upload_sessions.items() if s.get("created_at", 0) < cutoff]:
        _upload_sessions.pop(uid, None)
    refs = {(j.get("payload") or {}).get("work_dir") for j in _jobs.values()}
    for d in OUTPUTS_DIR.glob(".work-*"):
        try:
            if d.is_dir() and str(d) not in refs:
                b, _n = _tree_bytes(d)
                shutil.rmtree(d, ignore_errors=True)
                freed += b
                workdirs += 1
        except OSError:
            continue
    _space_cache["ts"] = 0.0
    return {"ok": True, "freed": freed, "sessions": sessions, "workdirs": workdirs}


def read_stats() -> dict:
    cpu = _read_cpu_pct()
    try:
        mi = dict(
            (l.split(":", 1)[0], int(l.split()[1]))
            for l in open("/proc/meminfo").read().splitlines()[:30] if ":" in l)
        ram = {"used_gb": round((mi["MemTotal"] - mi["MemAvailable"]) / 1048576, 1),
               "total_gb": round(mi["MemTotal"] / 1048576, 1)}
    except Exception:
        ram = None
    return {"cpu_pct": cpu, "ram": ram, "vram": _read_vram(), "gpu_pct": _read_gpu_util(),
            "ts": int(time.time()),
            # M2.3：算力舱 LAN 直连口（compose 注入；前端作直连候选之一）
            "lan_hint": os.environ.get("JASNA_LAN_HINT", ""),
            # M2.2：引擎就绪标志（新装机首任务触发 15-60min 编译）
            "engines_compiled": _engines_compiled()}


def device_capabilities() -> dict:
    """M3.3 能力分层端点：设备画像 + 编码 lane 探测（只读，不建编码器）。
    tier 推断：sm_87=orin / sm_90+=gb10 家族 / sm_100+（Blackwell 含 Thor）=thor。
    档位只做信息上报与实验开关依据——性能默认值变更必须有 benchmark 证据，
    无证据不自动上调（PLAN §M3.3 纪律）。"""
    caps = {
        "tier": "baseline",
        "uma_total_gb": None,
        "gpu": "",
        "sm": None,
        "cuda": None,
        "trt": None,
        "python": sys.version.split()[0],
        "encoder_lane": "unknown",
        "decode_backend": os.environ.get("DECODE_BACKEND", "auto"),
        "engines_compiled": _engines_compiled(),
        "default_batch": os.environ.get("JASNA_DEFAULT_BATCH_SIZE", ""),
        "default_max_clip": os.environ.get("JASNA_DEFAULT_MAX_CLIP", ""),
    }
    try:
        import torch
        caps["cuda"] = torch.version.cuda
        p = torch.cuda.get_device_properties(0)
        caps["gpu"] = p.name
        caps["uma_total_gb"] = round(p.total_memory / 2**30, 1)
        sm = p.major * 10 + p.minor
        caps["sm"] = sm
        if sm == 87:
            caps["tier"] = "orin"
        elif sm >= 100:
            caps["tier"] = "thor"        # Blackwell（Thor T4000/T5000 = sm_110）
        elif sm >= 90:
            caps["tier"] = "gb10"        # DGX Spark 家族
    except Exception:
        pass
    try:
        import tensorrt
        caps["trt"] = tensorrt.__version__
    except Exception:
        pass
    # 编码 lane 只探测不实例化（ gst nvv4l2 是 Jetson 主 lane；PyAV/nvenc lane 由工厂
    # 在任务时逐级探测，这里报 jetson lane 存在性即可覆盖 x3/thor 两型）
    try:
        from jasna.jetson_gst_encoder import probe_jetson_gst
        caps["encoder_lane"] = "gst-nvv4l2" if probe_jetson_gst() else "none"
    except Exception:
        caps["encoder_lane"] = "none"
    return caps


def _video_blockers(name: str) -> list[dict]:
    """占用检测三源：任务/扫描/上传会话（命中即 409；上传会话 complete 会重组装同名文件）。"""
    target = str(VIDEOS_DIR / name)
    blockers = []
    with _jobs_lock:
        for j in _jobs.values():
            if j.get("input") == target and j.get("state") in {"queued", "running", "paused"}:
                blockers.append({"type": "job", "id": j["id"], "state": j["state"]})
    with _scans_lock:
        for sc in _scans.values():
            if sc.get("input") == target and sc.get("state") in {"queued", "running"}:
                blockers.append({"type": "scan", "id": sc["id"], "state": sc["state"]})
    for us in _upload_sessions.values():
        if us.get("name") == name:
            blockers.append({"type": "upload", "id": us["id"]})
    return blockers


class Handler(BaseHTTPRequestHandler):

    def _handle_upload_part(self, upload_id, part_number):
        sess = _upload_sessions.get(upload_id)
        if not sess:
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        expected = min(UPLOAD_PART_SIZE, sess["size"] - part_number * UPLOAD_PART_SIZE)
        if expected <= 0 or length != expected:
            return self._json({"error": f"bad part size {length} != {expected}"}, 400)
        part_path = _upload_dir(upload_id) / str(part_number)
        received = 0
        with open(part_path, "wb") as fh:
            while received < length:
                chunk = self.rfile.read(min(8 * 1024 * 1024, length - received))
                if not chunk:
                    break
                fh.write(chunk)
                received += len(chunk)
        if received != length:
            part_path.unlink(missing_ok=True)
            return self._json({"error": "truncated"}, 400)
        sess["parts"].add(part_number)
        return self._json({"ok": True, "part": part_number, "received_parts": len(sess["parts"])})

    def _json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        # CORS 预检：直连 PUT 上传带 video/* Content-Type 会触发；没有这条
        # 预检 501，浏览器直接判失败（表现为"上传成功探测、上传必挂"）。
        # Allow-Private-Network 面向 Chrome PNA（公网站点 → 局域网地址）。
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, PUT, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Access-Control-Max-Age", "86400")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass

    def _route(self) -> str:
        """归一化路径：nginx 反代可能剥或不剥 /api 或 /wapi 前缀。"""
        path = self.path.split("?")[0]
        for prefix in ("/api", "/wapi"):
            if path == prefix:
                return "/"
            if path.startswith(prefix + "/"):
                return path[len(prefix):]
        return path

    def do_GET(self):
        path = self._route()
        if path == "/stats":
            return self._json(read_stats())
        if path == "/space":
            return self._json(space_stats())
        if path == "/capabilities":
            return self._json(device_capabilities())
        if path == "/outputs":
            items = []
            for f in sorted(OUTPUTS_DIR.iterdir()):
                if f.is_file() and f.suffix.lower() in {".mp4", ".mkv"}:
                    items.append({"name": f.name, "size_mb": round(f.stat().st_size / 1048576, 1),
                                  "mtime": int(f.stat().st_mtime)})
            return self._json({"outputs": items})
        if path == "/drive/config":
            cfg = _drive_cfg()
            return self._json({"configured": bool(cfg),
                               "url": (cfg or {}).get("url", ""),
                               "remote_dir": (cfg or {}).get("remote_dir", "")})
        if path == "/media-meta":
            with _media_meta_lock:
                return self._json({"meta": _media_meta})
        if path == "/drive/list":
            return self._drive_list()
        m = re.match(r"^/outputs/([^/]+)$", path)
        if m:
            name = _safe_name(m.group(1))
            if not name:
                return self._json({"error": "bad name"}, 400)
            return self._serve_output(name)
        if self.do_GET_media(path):
            return
        if path == "/videos":
            return self._json({"videos": list_videos()})
        if path == "/models":
            return self._json(list_models())
        if path == "/jobs":
            with _jobs_lock:
                jobs = sorted(_jobs.values(), key=lambda j: -j["created_at"])
                # 排队中的按真实执行序（_queue 下标）整组排到列表尾部；
                # 前端任务列表据此呈现"下一个跑谁"（时间倒序在置顶/重排后会失真）
                qorder = {jid: i for i, jid in enumerate(_queue)}
                if qorder:
                    queued = sorted((j for j in jobs if j["state"] == "queued"),
                                    key=lambda j: qorder.get(j["id"], 10 ** 9))
                    jobs = [j for j in jobs if j["state"] != "queued"] + queued
            out = []
            for j in jobs:
                # 内部字段不出网：命令行/env/payload/log_buf(deque 不可序列化)
                pub = {k: v for k, v in j.items()
                       if k not in {"command", "env", "payload", "log_buf"}}
                if j["state"] == "queued":
                    # 真实队列位（按 _queue 下标；created_at 口径在置顶/重排后会失真）
                    try:
                        pub["queued_ahead"] = _queue.index(j["id"])
                    except ValueError:
                        pass
                out.append(pub)
            return self._json({"jobs": out})
        m = re.match(r"^/jobs/([\w]+)/log$", path)
        if m:
            job = _jobs.get(m.group(1))
            if not job:
                return self._json({"error": "no such job"}, 404)
            lines = list(job.get("log_buf") or [])
            if not lines:  # 重启后内存丢失 → 读落盘日志
                lf = OUTPUTS_DIR / "logs" / f"job-{m.group(1)}.log"
                if lf.is_file():
                    lines = lf.read_text(encoding="utf-8", errors="replace").splitlines()
            return self._json({"log": lines[-1500:], "total": len(lines)})
        if path == "/scans":
            with _scans_lock:
                scans = sorted(_scans.values(), key=lambda s: -s["created_at"])
            out = []
            for sc in scans:
                pub = dict(sc)
                pub["src_valid"] = _scan_valid(sc)
                # 列表瘦身（2026-09-05）：剔除 span.thumb（base64 缩略图，单条记录
                # 可达数 MB、65 条历史累计 67MB/轮，前端 2.5s 轮询≈27MB/s 空转流量）。
                # 前端/编辑器只消费 start/end/samples/max_box_ratio；thumb 留在
                # scan-*.json 里供单查，不进列表。
                if pub.get("spans"):
                    pub["spans"] = [
                        {k: s.get(k) for k in ("start", "end", "samples", "max_box_ratio")}
                        for s in pub["spans"] if isinstance(s, dict)
                    ]
                pub.pop("sample_scores", None)
                out.append(pub)
            return self._json({"scans": out})
        m = re.match(r"^/upload-session/([\w]+)$", path)
        if m:
            sess = _upload_sessions.get(m.group(1))
            if not sess:
                return self._json({"error": "no such session"}, 404)
            return self._json({"upload_id": sess["id"], "name": sess["name"], "size": sess["size"],
                               "received_parts": sorted(sess["parts"]),
                               "total_parts": (sess["size"] + UPLOAD_PART_SIZE - 1) // UPLOAD_PART_SIZE})
        m = re.match(r"^/scans/([\w]+)$", path)
        if m:
            sc = _scans.get(m.group(1))
            if not sc:
                return self._json({"error": "no such scan"}, 404)
            pub = dict(sc)
            pub["src_valid"] = _scan_valid(sc)
            return self._json(pub)
        self.send_error(404)


    def do_PUT(self):
        """分片块上传：PUT /upload-session/{id}/{n}；旧式单文件 PUT /upload/{name} 保留。"""
        path = self._route()
        if path == "/media-meta":
            # 收藏标记（sidecar 单条更新；只接受已存在的成品名）
            length = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json({"error": "bad json"}, 400)
            name = _safe_name(str(body.get("name", "")))
            if not name or Path(name).suffix.lower() not in {".mp4", ".mkv"}:
                return self._json({"error": "bad name"}, 400)
            if not (OUTPUTS_DIR / name).is_file():
                return self._json({"error": "no such output"}, 404)
            fav = bool(body.get("fav"))
            with _media_meta_lock:
                if fav:
                    _media_meta[name] = {"fav": True}
                else:
                    _media_meta.pop(name, None)
                _persist_media_meta()
            return self._json({"ok": True, "name": name, "fav": fav})
        m = re.match(r"^/upload-session/([\w]+)/(\d+)$", path)
        if m:
            return self._handle_upload_part(m.group(1), int(m.group(2)))
        path = self._route()
        m = re.match(r"^/upload/([^/]+)$", path)
        if not m:
            self.send_error(404)
            return
        name = _safe_name(m.group(1))
        if not name:
            return self._json({"error": "bad name"}, 400)
        if Path(name).suffix.lower() not in VIDEO_EXTS:
            return self._json({"error": "unsupported file type"}, 400)
        dest = VIDEOS_DIR / name
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > 50 * 1024**3:
            return self._json({"error": "bad length"}, 400)
        received = 0
        with open(dest, "wb") as fh:
            while True:
                chunk = self.rfile.read(min(8 * 1024 * 1024, length - received))
                if not chunk:
                    break
                fh.write(chunk)
                received += len(chunk)
        if received != length:
            dest.unlink(missing_ok=True)
            return self._json({"error": f"truncated ({received}/{length})"}, 400)
        return self._json({"ok": True, "name": name, "size_mb": round(received / 1048576, 1)})

    def _serve_output(self, name):
        """局域网回传：GET /outputs/{name}（Range 支持，?dl=1 强制下载命名）。"""
        f = OUTPUTS_DIR / name
        if not f.is_file() or f.suffix.lower() not in {".mp4", ".mkv"}:
            self.send_error(404)
            return
        size = f.stat().st_size
        ctype = "video/mp4" if f.suffix.lower() == ".mp4" else "video/x-matroska"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if rng and rng.startswith("bytes="):
            try:
                s, _, e = rng[6:].partition("-")
                start = int(s) if s else 0
                end = int(e) if e else size - 1
                status = 206
            except ValueError:
                pass
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if "dl=1" in (self.path or ""):
            q = quote(name)
            self.send_header("Content-Disposition",
                             f"attachment; filename=\"{q}\"; filename*=UTF-8''{q}")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(f, "rb") as fh:
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = fh.read(min(4 * 1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_GET_media(self, path):
        """GET /media/{name}：源视频直读（对比预览用，支持 Range）。"""
        m = re.match(r"^/media/([^/]+)$", path)
        if not m:
            return False
        name = _safe_name(m.group(1))
        if not name:
            self.send_error(404)
            return True
        f = VIDEOS_DIR / name
        if not f.is_file():
            self.send_error(404)
            return True
        size = f.stat().st_size
        ctype = "video/mp4" if f.suffix.lower() == ".mp4" else "video/x-matroska"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if rng and rng.startswith("bytes="):
            try:
                s, _, e = rng[6:].partition("-")
                start = int(s) if s else 0
                end = int(e) if e else size - 1
                status = 206
            except ValueError:
                pass
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(f, "rb") as fh:
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = fh.read(min(4 * 1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)
        return True

    def _drive_list(self):
        cfg = _drive_cfg()
        if not cfg:
            return self._json({"error": "未配置网盘：请在 Spark 的 /jasna/drive.json 填 WebDAV 地址与账号（懒猫网盘设置里开启 WebDAV）"}, 400)
        try:
            req = urllib.request.Request(
                _dav_url(cfg), method="PROPFIND",
                headers=_dav_headers(cfg, {"Depth": "1", "Content-Type": "application/xml"}),
                data=b'<?xml version="1.0"?><D:propfind xmlns:D="DAV:"><D:prop><D:displayname/><D:getcontentlength/><D:resourcetype/></D:prop></D:propfind>')
            with urllib.request.urlopen(req, timeout=30) as r:
                xml = r.read().decode("utf-8", "replace")
        except Exception as e:
            return self._json({"error": f"网盘连接失败: {e!r}"}, 502)
        files = []
        for m in re.finditer(r"<D:response>(.*?)</D:response>", xml, re.S):
            blk = m.group(1)
            if "<D:collection" in blk:
                continue
            nm = re.search(r"<D:displayname>(.*?)</D:displayname>", blk)
            sz = re.search(r"<D:getcontentlength>(\d+)</D:getcontentlength>", blk)
            name = (nm.group(1) if nm else "").strip()
            if name and Path(name).suffix.lower() in VIDEO_EXTS:
                files.append({"name": name, "size_mb": round(int(sz.group(1)) / 1048576, 1) if sz else 0})
        return self._json({"files": sorted(files, key=lambda x: -x["size_mb"])})

    def _drive_import(self, name):
        cfg = _drive_cfg()
        if not cfg:
            return self._json({"error": "未配置网盘（drive.json）"}, 400)
        if not name or name.startswith("/") or ".." in name.split("/"):
            return self._json({"error": "bad name"}, 400)
        dest = VIDEOS_DIR / name
        try:
            req = urllib.request.Request(_dav_url(cfg, name), headers=_dav_headers(cfg))
            with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as out:
                received = 0
                while True:
                    chunk = r.read(8 * 1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    received += len(chunk)
        except Exception as e:
            dest.unlink(missing_ok=True)
            return self._json({"error": f"网盘拉取失败: {e!r}"}, 502)
        return self._json({"ok": True, "name": name, "size_mb": round(received / 1048576, 1)})

    def _drive_save(self, name):
        cfg = _drive_cfg()
        if not cfg:
            return self._json({"error": "未配置网盘（drive.json）"}, 400)
        f = OUTPUTS_DIR / name
        if not f.is_file():
            return self._json({"error": "no such output"}, 404)
        try:
            opener = urllib.request.build_opener(urllib.request.HTTPHandler)
            with open(f, "rb") as fh:
                # 文件对象直传（分块 HTTP body，避免整文件进内存）
                req = urllib.request.Request(
                    _dav_url(cfg, name), data=fh, method="PUT",
                    headers=_dav_headers(cfg))
                with opener.open(req, timeout=300):
                    pass
            sent = f.stat().st_size
        except Exception as e:
            return self._json({"error": f"网盘上传失败: {e!r}"}, 502)
        return self._json({"ok": True, "name": name, "size_mb": round(sent / 1048576, 1)})

    def do_POST(self):
        path = self._route()
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if path == "/jobs":
            try:
                return self._json(create_job(body))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
        if path == "/drive/import":
            return self._drive_import(str(body.get("name", "")))
        if path == "/drive/save":
            return self._drive_save(str(body.get("name", "")))
        if path == "/scan":
            src = Path(str(body.get("input", "")))
            if not src.is_file() or VIDEOS_DIR.resolve() not in src.resolve().parents:
                return self._json({"error": "invalid input path"}, 400)
            det = str(body.get("detection_model", "")) or "rfdetr-v6-large"
            return self._json(_spawn_scan(src, det))   # JASNA_AUTOCHAIN_PATCH: 提取复用
        m = re.match(r"^/upload-session/([\w]+)/complete$", path)
        if m:
            sess = _upload_sessions.get(m.group(1))
            if not sess:
                return self._json({"error": "no such session"}, 404)
            total_parts = (sess["size"] + UPLOAD_PART_SIZE - 1) // UPLOAD_PART_SIZE
            missing = [i for i in range(total_parts) if i not in sess["parts"]]
            if missing:
                return self._json({"error": "missing parts", "missing": missing[:20]}, 400)
            dest = VIDEOS_DIR / sess["name"]
            received = 0
            with open(dest, "wb") as out:
                for i in range(total_parts):
                    data = (_upload_dir(sess["id"]) / str(i)).read_bytes()
                    out.write(data)
                    received += len(data)
            if received != sess["size"]:
                dest.unlink(missing_ok=True)
                return self._json({"error": f"assembled {received}/{sess['size']}"}, 400)
            import shutil as _sh
            _sh.rmtree(_upload_dir(sess["id"]), ignore_errors=True)
            del _upload_sessions[sess["id"]]
            _autochain_after_ingest(str(dest))   # JASNA_AUTOCHAIN_PATCH
            return self._json({"ok": True, "name": sess["name"], "size_mb": round(received / 1048576, 1)})
        if path == "/upload-session":
            name = Path(str(body.get("name", ""))).name
            size = int(body.get("size") or 0)
            if not name or Path(name).suffix.lower() not in VIDEO_EXTS or size <= 0 or size > 50 * 1024 ** 3:
                return self._json({"error": "invalid name/size"}, 400)
            upload_id = uuid.uuid4().hex[:12]
            meta = {"name": name, "size": size}
            _upload_sessions[upload_id] = {"id": upload_id, **meta, "parts": set(), "created_at": time.time()}
            _upload_dir(upload_id).joinpath("meta.json").write_text(json.dumps(meta), encoding="utf-8")
            return self._json({"upload_id": upload_id,
                               "part_size": UPLOAD_PART_SIZE,
                               "total_parts": (size + UPLOAD_PART_SIZE - 1) // UPLOAD_PART_SIZE})
        m = re.match(r"^/jobs/([\w]+)/cancel$", path)
        if m:
            job = _jobs.get(m.group(1))
            if not job:
                return self._json({"error": "no such job"}, 404)
            pid = job.get("pid")
            yield_mode = str(body.get("mode", "")) == "yield"
            if job["state"] in {"running", "paused"} and pid:
                if yield_mode:
                    # 断点续跑：SIGKILL 硬杀——TERM 会触发上游优雅清理删掉片段目录；
                    # paused 进程 KILL 同样直接生效，无需先 CONT
                    subprocess.run(["pkill", "-KILL", "-P", str(pid)], check=False)
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    job["state"] = "cancelling"
                    wd = (job.get("payload") or {}).get("work_dir")
                    if wd and job.get("segments"):
                        wd_path = Path(wd)
                        if wd_path.is_dir():
                            # smart render 按关键帧切段：total=实际 span 数（编号最大+1），done=有成品片段的
                            idxs = sorted({int(p.name[:4]) for p in wd_path.iterdir()
                                           if p.name[:4].isdigit()})
                            done = sum(1 for i in idxs
                                       if (wd_path / f"{i:04d}.ts").exists()
                                       or (wd_path / f"{i:04d}.mkv").exists())
                            job["resume_info"] = {"done": done, "total": len(idxs)}
                        else:
                            job["resume_info"] = {"done": 0, "total": 0}
                else:
                    if job["state"] == "paused":
                        try:
                            os.killpg(pid, signal.SIGCONT)
                        except (ProcessLookupError, PermissionError):
                            pass
                    subprocess.run(["pkill", "-TERM", "-P", str(pid)], check=False)
                    try:
                        os.killpg(pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError):
                        pass
                    job["state"] = "cancelling"
            elif job["state"] == "queued":
                job["state"] = "cancelled"
            _persist_jobs()
            return self._json({"ok": True})
        m = re.match(r"^/jobs/([\w]+)/(pause|resume)$", path)
        if m:
            job = _jobs.get(m.group(1))
            if not job:
                return self._json({"error": "no such job"}, 404)
            pid = job.get("pid")
            if m.group(2) == "pause":
                if job["state"] != "running" or not pid:
                    return self._json({"error": "仅运行中的任务可暂停"}, 400)
                try:
                    os.killpg(pid, signal.SIGSTOP)
                except (ProcessLookupError, PermissionError):
                    return self._json({"error": "进程已退出"}, 400)
                job["state"] = "paused"
            else:
                if job["state"] != "paused" or not pid:
                    return self._json({"error": "仅已暂停的任务可继续"}, 400)
                try:
                    os.killpg(pid, signal.SIGCONT)
                except (ProcessLookupError, PermissionError):
                    return self._json({"error": "进程已退出"}, 400)
                job["state"] = "running"
            _persist_jobs()
            return self._json({"ok": True, "state": job["state"]})
        if path == "/jobs/reorder":
            jid = str(body.get("id", ""))
            to = str(body.get("to", "top"))
            if to not in {"top", "up", "down", "bottom"}:
                return self._json({"error": "bad to"}, 400)
            with _worker_cv:
                if jid not in _queue:
                    return self._json({"error": "任务不在排队中"}, 400)
                i = _queue.index(jid)
                if to == "top":
                    if i > 0:
                        _queue.insert(0, _queue.pop(i))
                elif to == "bottom":
                    _queue.append(_queue.pop(i))
                elif to == "up" and i > 0:
                    _queue[i - 1], _queue[i] = _queue[i], _queue[i - 1]
                elif to == "down" and i < len(_queue) - 1:
                    _queue[i + 1], _queue[i] = _queue[i], _queue[i + 1]
                for k, qid in enumerate(_queue):
                    if isinstance(_jobs.get(qid), dict):
                        _jobs[qid]["seq"] = k
                _persist_jobs()
                return self._json({"ok": True, "queue": list(_queue)})
        m = re.match(r"^/jobs/([\w]+)/retry$", path)
        if m:
            old = _jobs.get(m.group(1))
            if not old:
                return self._json({"error": "no such job"}, 404)
            if old["state"] in {"queued", "running", "cancelling"}:
                return self._json({"error": "任务仍在队列/运行中"}, 400)
            payload = old.get("payload")
            if not payload:
                return self._json({"error": "旧版任务无参数快照，无法重试（请重新发起）"}, 400)
            # 同输入去重（7ms 双 retry POST 竞态实证）：同源已有活动任务即拒绝，
            # 终态记录（done/failed 等）不算——那是历史行，重试本来就合法。
            # dup 检查到 create_job 真正入 _jobs 之间隔着 probe 慢调用，
            # 用输入级预留把窗口焊死（并发实测第一版被打脸：双发都过了 dup 检查）
            src_in = str(payload.get("input", ""))
            with _output_reserve_lock:
                now_ts = time.time()
                for k in [k for k, t in _reserved_inputs.items() if now_ts - t > 120]:
                    _reserved_inputs.pop(k, None)
                dup = next((j for j in _jobs.values()
                            if j.get("input") == src_in and j["id"] != old["id"]
                            and j.get("state") in {"queued", "running", "paused", "cancelling"}), None)
                if dup:
                    reserved = False
                elif src_in in _reserved_inputs:
                    reserved = False
                else:
                    _reserved_inputs[src_in] = now_ts
                    reserved = True
            if not reserved:
                why = dup["id"][:8] if dup else "另一个重试正在入队"
                return self._json({"error": f"同源任务已在队列/运行中（{why}），请等它结束或先取消"}, 400)
            try:
                return self._json(create_job(payload))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            finally:
                with _output_reserve_lock:
                    _reserved_inputs.pop(src_in, None)
        if path == "/jobs/clear-finished":
            with _jobs_lock:
                for jid in [jid for jid, j in _jobs.items()
                            if j["state"] in {"done", "failed", "cancelled"}]:
                    del _jobs[jid]
            _persist_jobs()
            return self._json({"ok": True})
        if path == "/outputs/clear-stale":   # 批量清旧版（收藏跳过）
            return self._json(_clear_stale_outputs())
        if path == "/space/clean-temp":      # 立即清临时区
            return self._json(_clean_temp())
        m = re.match(r"^/scans/([\w]+)/refilter$", path)
        if m:
            sc = _scans.get(m.group(1))
            if not sc:
                return self._json({"error": "no such scan"}, 404)
            if sc.get("state") != "done":
                return self._json({"error": "扫描未完成"}, 400)
            try:
                thr = float(body.get("threshold"))
            except (TypeError, ValueError):
                return self._json({"error": "bad threshold"}, 400)
            try:
                return self._json(_rescan_at_threshold(m.group(1), thr))
            except ValueError as e:
                return self._json({"error": str(e)}, 400)
            except Exception as e:
                return self._json({"error": f"重过滤失败: {e!r}"}, 500)
        self.send_error(404)


    def do_DELETE(self):
        path = self._route()
        m = re.match(r"^/jobs/([\w]+)$", path)
        if m:
            # 删除任务记录：仅终态（done/failed/cancelled/interrupted）；
            # 活态 409（记录没了但进程还跑会变孤儿）；不删 /outputs 成品文件
            with _jobs_lock:
                job = _jobs.get(m.group(1))
                if not job:
                    return self._json({"error": "no such job"}, 404)
                if job["state"] in {"queued", "running", "paused", "cancelling"}:
                    return self._json({"error": "job still active", "state": job["state"]}, 409)
                del _jobs[m.group(1)]
            wd = (job.get("payload") or {}).get("work_dir")   # P1：记录删除=放弃续跑，断点目录连带清
            if wd:
                shutil.rmtree(wd, ignore_errors=True)
            _persist_jobs()
            return self._json({"ok": True, "id": m.group(1)})
        m = re.match(r"^/upload-session/([\w]+)$", path)
        if m:
            # 取消上传时清服务端会话：内存 + 分片目录（否则永久占盘、挡 /videos 删除）
            uid = m.group(1)
            sess = _upload_sessions.pop(uid, None)
            import shutil
            shutil.rmtree(OUTPUTS_DIR / "uploads" / uid, ignore_errors=True)
            if not sess:
                return self._json({"error": "no such session"}, 404)
            return self._json({"ok": True, "name": sess.get("name", "")})
        m = re.match(r"^/videos/([^/]+)$", path)
        if m:
            name = _safe_name(m.group(1))
            if not name or Path(name).suffix.lower() not in VIDEO_EXTS:
                return self._json({"error": "bad name"}, 400)
            f = VIDEOS_DIR / name
            if not f.is_file():
                return self._json({"error": "no such video"}, 404)
            blockers = _video_blockers(name)
            if blockers:
                return self._json({"error": "in use", "blocked_by": blockers}, 409)
            f.unlink()
            # 连带清理该片的扫描缓存（json 落盘 + 内存注册表）；
            # 正确性上 src_valid 指纹本就护得住，这里只做磁盘/列表整洁
            removed_scans = 0
            with _scans_lock:
                for sid, sc in list(_scans.items()):
                    if sc.get("input") == str(f) and sc.get("state") not in {"queued", "running"}:
                        _scans.pop(sid, None)
                        removed_scans += 1
            for sf in OUTPUTS_DIR.glob("scan-*.json"):
                try:
                    if json.loads(sf.read_text(encoding="utf-8")).get("input") == str(f):
                        sf.unlink(missing_ok=True)
                except Exception:
                    continue
            return self._json({"ok": True, "name": name, "removed_scans": removed_scans})
        m = re.match(r"^/outputs/([^/]+)$", path)
        if m:
            name = _safe_name(m.group(1))
            if not name:
                return self._json({"error": "bad name"}, 400)
            f = OUTPUTS_DIR / name
            if f.is_file() and f.suffix.lower() in {".mp4", ".mkv"}:
                f.unlink(missing_ok=True)
                return self._json({"ok": True})
            return self._json({"error": "no such output"}, 404)
        self.send_error(404)


if __name__ == "__main__":
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    _start_janitor()   # P0/P1：上传会话与孤儿 workdir 每小时回收
    print(f"jasna-web on 0.0.0.0:8766, videos={VIDEOS_DIR}, outputs={OUTPUTS_DIR}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8766), Handler).serve_forever()
