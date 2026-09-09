"""ATID 质量客观判定：源 vs 成品帧像素差。
命中修复 = 差异集中在局部块（马赛克区），其余 ~0；
未命中 = 差异≈0（仅编码噪声）。"""
import numpy as np
from PIL import Image


def load(p):
    return np.asarray(Image.open(p).convert('RGB'), dtype=np.int16)


for t in (100, 140, 170):
    src = load(f'/tmp/atid_q/src_{t}.jpg')
    for tag in ('yolo', 'rfdetr'):
        out = load(f'/tmp/atid_q/{tag}_{t}.jpg')
        diff = np.abs(src - out).mean(axis=2)
        # 差异图分块统计：找强差异集中区
        h, w = diff.shape
        bs = 32
        blocks = diff[:h//bs*bs, :w//bs*bs].reshape(h//bs, bs, w//bs, bs).mean(axis=(1, 3))
        thr = 25.0
        hot = np.argwhere(blocks > thr)
        if len(hot):
            y0, x0 = hot.min(axis=0) * bs
            y1, x1 = (hot.max(axis=0) + 1) * bs
            area = (y1 - y0) * (x1 - x0) / (h * w) * 100
            peak = blocks.max()
            print(f"t={t}s {tag}: RESTORED region ~bbox=({x0},{y0})-({x1},{y1}) "
                  f"area={area:.1f}% peak_block_diff={peak:.0f} global_mean={diff.mean():.1f}")
        else:
            print(f"t={t}s {tag}: NOT restored? max_block={blocks.max():.1f} "
                  f"global_mean={diff.mean():.2f}")
