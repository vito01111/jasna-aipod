import av
import numpy as np


def load(idx):
    f = av.open(f'/tmp/encdbg/f{idx:04d}.jpg')
    for fr in f.decode(video=0):
        return fr.to_ndarray(format='rgb24')


def blockmap(img, bs=16):
    h, w = img.shape[:2]
    hh, ww = h // bs * bs, w // bs * bs
    b = img[:hh, :ww].reshape(hh // bs, bs, ww // bs, bs, 3)
    return b.mean(axis=(1, 3, 2)), b.std(axis=(1, 3, 2))


base = load(47)
for idx in (48, 49, 50):
    img = load(idx)
    diff = np.abs(img.astype(int) - base.astype(int)).mean(axis=2)
    # 行级差异：若是行位移/撕裂，会呈现行条带模式
    rowdiff = diff.mean(axis=1)
    coldiff = diff.mean(axis=0)
    print(f"== f{idx:04d}: global_mean_diff={diff.mean():.1f}")
    print("   rowdiff profile (every 8th):", " ".join(f"{v:.0f}" for v in rowdiff[::8][:34]))
    # 与前一帧自身做行自相关位移检测：best shift
    a = img.astype(float).mean(axis=2)
    b0 = base.astype(float).mean(axis=2)
    best, best_s = 0, -1
    for s in range(-40, 41):
        if s >= 0:
            c = np.corrcoef(a[s:, :].ravel(), b0[:a.shape[0]-s, :].ravel())[0, 1]
        else:
            c = np.corrcoef(a[:s, :].ravel(), b0[-s:, :].ravel())[0, 1]
        if c > best_s:
            best_s, best = c, s
    print(f"   best row-shift vs f0047: {best:+d} rows, corr={best_s:.3f}")
    bm, bs_ = blockmap(img)
    # 块方差 top 区域分布
    top = np.dstack(np.unravel_index(np.argsort(bs_.ravel())[::-1][:20], bs_.shape))[0]
    print("   highest-var blocks (row,col):", top[:8].tolist())
    print(f"   block std: median={np.median(bs_):.1f} p95={np.percentile(bs_,95):.1f} max={bs_.max():.1f}")
