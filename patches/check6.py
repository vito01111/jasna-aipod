"""Two concurrent NvidiaVideoReader instances on one GPU (mimics decode-detect
+ blend-encode), no models. Checks whether batches turn to noise under
concurrency alone."""
import threading

import torch
from jasna.media import get_video_meta_data
from jasna.media.video_decoder import NvidiaVideoReader

meta = get_video_meta_data("/videos/dass-377-seg3138.mp4")
dev = torch.device("cuda")
torch.cuda.init()

anomalies = {0: [], 1: []}
errors = []


def run_reader(tag):
    try:
        rdr = NvidiaVideoReader(
            "/videos/dass-377-seg3138.mp4", batch_size=4, device=dev, metadata=meta)
        with rdr:
            idx = 0
            for batch, pts in rdr.frames():
                cpu = batch.float().cpu()
                for i in range(cpu.shape[0]):
                    m, s = cpu[i].mean().item(), cpu[i].std().item()
                    if s > 76 or m < 95 or m > 165:
                        anomalies[tag].append((idx, pts[i], round(m, 1), round(s, 1)))
                    idx += 1
                if idx >= 200:
                    break
    except BaseException as e:
        errors.append((tag, repr(e)))


t0 = threading.Thread(target=run_reader, args=(0,))
t1 = threading.Thread(target=run_reader, args=(1,))
t0.start(); t1.start(); t0.join(); t1.join()
print("errors:", errors)
print("reader0 anomalies:", anomalies[0][:20] or "NONE")
print("reader1 anomalies:", anomalies[1][:20] or "NONE")
