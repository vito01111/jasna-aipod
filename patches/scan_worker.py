"""Jasna v2 段编辑器 · 检测扫描 worker（部署层，子进程）。

采样解码 → 检测模型 scan_scores_masks 取逐样本最优分（分数与阈值无关，
对齐上游 GUI MosaicScanWorker 的快速通道）→ 按阈值聚合阳性采样点为 spans
→ 缩略帧 base64 → JSON。sample_scores 一并落盘，web 后端据此支持
"改阈值即时重聚合、免重扫"（旧版只存阳性点，改阈值必须重扫）。

用法: PYTHONPATH=/jasna python3 scan_worker.py --input x.mp4 --out /outputs/scan.json
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, "/jasna")

import cv2  # noqa: E402
import torch  # noqa: E402

from jasna.mosaic.detection_registry import (  # noqa: E402
    build_detection_model,
    coerce_detection_model_name,
    rfdetr_model_config,
    require_detection_model_weights,
)

MIN_GAP_SEC = 2.0   # 阳性采样点间隔超过此值断开 span
PAD_SEC = 0.6       # span 前后保护
# 检测器内置阈值压到地板：scores 从 logits 直接算出本就与阈值无关，
# 低阈值只影响内部掩码合并，保证低分样本也进 sample_scores 供重过滤
FLOOR_THRESHOLD = 0.05


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--detection-model", default="rfdetr-v6-large")
    ap.add_argument("--sample-fps", type=float, default=1.0)
    ap.add_argument("--score-threshold", type=float, default=0.30)
    args = ap.parse_args()

    t0 = time.time()
    det_name = coerce_detection_model_name(args.detection_model)
    weights = require_detection_model_weights(det_name)
    config = rfdetr_model_config(det_name)
    device = torch.device("cuda:0")
    # M2.2：新装机引擎缺失时自动编译（扫描 tail 实时显示编译进度；job 路径
    # 本就走 ensure_engines_compiled，这里补齐 scan 路径的首启体验）
    try:
        from jasna.engine_compiler import EngineCompilationRequest, ensure_engines_compiled
        ensure_engines_compiled(
            EngineCompilationRequest(
                device="cuda:0", fp16=True,
                detection=True, detection_model_name=det_name,
                detection_model_path=str(weights), detection_batch_size=4,
            ),
            log_callback=lambda line: print(f"[engine-compile] {line}", flush=True),
        )
    except Exception as exc:  # 编译失败留给 build_detection_model 报原始错
        print(f"[engine-compile] skipped: {exc}", flush=True)
    detector = build_detection_model(
        det_name, weights, batch_size=4, device=device,
        score_threshold=FLOOR_THRESHOLD, fp16=True,
    )

    cap = cv2.VideoCapture(args.input)
    if not cap.isOpened():
        print(json.dumps({"error": "cannot open input"}), flush=True)
        return 1
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    # cv2 的元数据帧数会虚高（nb_frames 不可靠），时长以 ffprobe 容器值为准
    import subprocess as _sp
    try:
        dur_out = _sp.run(["/opt/ff8/bin/ffprobe", "-v", "error", "-show_entries",
                           "format=duration", "-of", "default=nw=1:nokey=1", args.input],
                          capture_output=True, text=True, timeout=30, check=False)
        duration = float(dur_out.stdout.strip())
    except Exception:
        total = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        duration = total / fps if total else 0
    stride = max(1, int(round(fps / max(0.2, args.sample_fps))))
    res = config.resolution

    samples: list[list[float]] = []  # 每样本 [ts, 最优分, 掩码占比]
    frames_buf: list[tuple[float, object]] = []
    frame_idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_idx % stride == 0:
            ts = frame_idx / fps
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frames_buf.append((ts, rgb))
            if len(frames_buf) == 4:
                _flush(detector, frames_buf, samples)
        frame_idx += 1
    if frames_buf:
        _flush(detector, frames_buf, samples)
    cap.release()

    ratio_by_ts = {s[0]: s[2] for s in samples}
    positives = [s[0] for s in samples if s[1] >= args.score_threshold]
    spans = _aggregate(positives, ratio_by_ts, duration)
    thumbs = _thumbnails(args.input, spans, fps)
    result = {
        "input": args.input, "detection_model": det_name,
        "sample_stride_frames": stride, "duration": round(duration, 2),
        "threshold": args.score_threshold,
        "positive_samples": len(positives),
        "sample_scores": samples,
        "spans": [
            {**s, "thumb": thumbs.get(round(s["start"], 1), "")} for s in spans
        ],
        "scan_sec": round(time.time() - t0, 1),
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"done": True, "spans": len(spans), "out": args.out}), flush=True)
    return 0


def _flush(detector, buf, samples):
    batch = torch.stack([torch.from_numpy(f) for _, f in buf]).permute(0, 3, 1, 2).contiguous()
    scores, masks = detector.scan_scores_masks(batch, mask_hw=(90, 160))
    score_list = scores.detach().float().cpu().tolist()
    # 掩码占比替代旧版 box 占比：scan_scores_masks 不回传框，语义近似
    ratio_list = masks.detach().float().mean(dim=(1, 2)).cpu().tolist()
    for (ts, _), sc, ar in zip(buf, score_list, ratio_list):
        samples.append([round(ts, 2), round(float(sc), 4), round(float(ar), 4)])
    buf.clear()


def _aggregate(positives: list[float], ratio_by_ts: dict, duration: float) -> list[dict]:
    if not positives:
        return []
    positives = sorted(positives)
    groups: list[list[float]] = [[positives[0]]]
    for ts in positives[1:]:
        if ts - groups[-1][-1] > MIN_GAP_SEC:
            groups.append([ts])
        else:
            groups[-1].append(ts)
    out = []
    for g in groups:
        start = round(max(0.0, g[0] - PAD_SEC), 2)
        end = round(min(duration, g[-1] + PAD_SEC + 0.5), 2)
        if end - start < 1.0:
            end = min(duration, start + 1.0)
        ratio = max((ratio_by_ts.get(round(ts, 2), 0.0) for ts in g), default=0.0)
        end = min(end, duration - 0.15) if duration > 0.3 else end
        out.append({"start": start, "end": end, "max_box_ratio": ratio, "samples": len(g)})
    return out


def _thumbnails(path: str, spans: list[dict], fps: float) -> dict:
    if not spans:
        return {}
    cap = cv2.VideoCapture(path)
    thumbs = {}
    for span in spans:
        mid = int((span["start"] + span["end"]) / 2 * fps)
        cap.set(cv2.CAP_PROP_POS_FRAMES, mid)
        ok, frame = cap.read()
        if not ok:
            continue
        h, w = frame.shape[:2]
        scale = 480 / w
        small = cv2.resize(frame, (480, int(h * scale)))
        ok2, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok2:
            thumbs[round(span["start"], 1)] = base64.b64encode(buf).decode()
    cap.release()
    return thumbs


if __name__ == "__main__":
    sys.exit(main())
