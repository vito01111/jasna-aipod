import numpy as np
from PIL import Image


def load(p):
    return np.asarray(Image.open(p).convert('RGB'), dtype=np.int16)


for t in (100, 140, 170):
    src = load(f'/tmp/atid_q/src_{t}.jpg')
    for tag in ('yolo', 'rfdetr'):
        out = load(f'/tmp/atid_q/{tag}_{t}.jpg')
        diff = np.abs(src - out).mean(axis=2)
        hot = diff > 60  # 强差异像素
        n = int(hot.sum())
        if n > 200:
            ys, xs = np.where(hot)
            print(f"t={t}s {tag}: hot_px={n} bbox=({xs.min()},{ys.min()})-({xs.max()},{ys.max()}) "
                  f"peak={diff.max():.0f} mean={diff.mean():.2f}")
        else:
            print(f"t={t}s {tag}: hot_px={n} peak={diff.max():.0f} mean={diff.mean():.2f} -> no local restore")
