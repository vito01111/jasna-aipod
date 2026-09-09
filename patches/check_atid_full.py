import numpy as np
from PIL import Image


def load(p):
    return np.asarray(Image.open(p).convert('RGB'), dtype=np.int16)


for t in (2500, 2540, 2570, 4800):
    src = load(f'/tmp/atid_full_q/src_{t}.jpg')
    out = load(f'/tmp/atid_full_q/out_{t}.jpg')
    diff = np.abs(src - out).mean(axis=2)
    hot = diff > 60
    n = int(hot.sum())
    if n > 200:
        ys, xs = np.where(hot)
        print(f"t={t}s: RESTORED hot_px={n} bbox=({xs.min()},{ys.min()})-({xs.max()},{ys.max()})")
    else:
        print(f"t={t}s: no local restore (hot_px={n})")
