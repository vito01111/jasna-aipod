import numpy as np
from PIL import Image


def load(p):
    return np.asarray(Image.open(p).convert('RGB'), dtype=np.float32)


def blocks(img, bs=48):
    g = img.mean(axis=2)
    h, w = g.shape
    lap = np.abs(4*g[1:-1,1:-1] - g[:-2,1:-1] - g[2:,1:-1] - g[1:-1,:-2] - g[1:-1,2:])
    lh, lw = lap.shape
    hb, wb = lh//bs, lw//bs
    lb = lap[:hb*bs, :wb*bs].reshape(hb, bs, wb, bs)
    return lb.mean(axis=(1, 3)), lb.std(axis=(1, 3))


# 已确认有马赛克并已修复的 2570 源帧作参照
for t in (2570, 2500, 2540):
    src = load(f'/tmp/atid_full_q/src_{t}.jpg')
    bm, bs_ = blocks(src)
    i, j = np.unravel_index(np.argmax(bm), bm.shape)
    print(f"src t={t}s: max-lap block at ({j*48},{i*48}) val={bm.max():.2f} "
          f"p99={np.percentile(bm,99):.2f} median={np.median(bm):.2f}")
