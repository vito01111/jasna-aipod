import torch
from jasna.media import get_video_meta_data
from jasna.media.video_decoder import NvidiaVideoReader

meta = get_video_meta_data("/videos/dass-377-seg3138.mp4")
torch.cuda.init()
dev = torch.device("cuda")

reader = NvidiaVideoReader(
    "/videos/dass-377-seg3138.mp4",
    batch_size=4,
    device=dev,
    metadata=meta,
)
with reader as rdr:
    print("vendor:", getattr(rdr, "vendor", "?"), "vali:", getattr(rdr, "_vali_source", None) is not None)
    idx = 0
    anomalies = []
    for batch, pts in rdr.frames():
        cpu = batch.float().cpu()
        for i in range(cpu.shape[0]):
            std = cpu[i].std().item()
            mean = cpu[i].mean().item()
            if std > 78 or mean < 90 or mean > 165:
                anomalies.append((idx, pts[i], round(mean, 1), round(std, 1)))
            idx += 1
        if idx >= 110:
            break
    print("frames read:", idx)
    print("anomalies:", anomalies if anomalies else "NONE")
