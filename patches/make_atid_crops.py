import numpy as np
from PIL import Image


def load(p):
    return np.asarray(Image.open(p).convert('RGB'), dtype=np.float32)


def lap_energy(a):
    g = a.mean(axis=2)
    lap = np.abs(4*g[1:-1,1:-1] - g[:-2,1:-1] - g[2:,1:-1] - g[1:-1,:-2] - g[1:-1,2:])
    return lap.mean(), lap.std()


# 170s bbox 区域（两家一致的修复区）
box = (753, 529, 1043, 763)
src170 = load('/tmp/atid_q/src_170.jpg')
yolo170 = load('/tmp/atid_q/yolo_170.jpg')
det170 = load('/tmp/atid_q/rfdetr_170.jpg')
for name, img in (('src', src170), ('yolo', yolo170), ('rfdetr', det170)):
    reg = img[box[1]:box[3], box[0]:box[2]]
    m, s = lap_energy(reg)
    print(f"170s bbox {name}: lap_mean={m:.2f} lap_std={s:.2f} px_std={reg.std():.1f}")

# 放大裁片（3x）供目检
for name, img in (('src', src170), ('yolo', yolo170), ('rfdetr', det170)):
    reg = Image.fromarray(img[box[1]:box[3], box[0]:box[2]].astype(np.uint8))
    reg.resize((reg.width*3, reg.height*3), Image.LANCZOS).save(f'/tmp/atid_q/crop170_{name}.png')

# 100s 同位置区域对比（源是否也有同款块）
src100 = load('/tmp/atid_q/src_100.jpg')
reg = src100[box[1]:box[3], box[0]:box[2]]
m, s = lap_energy(reg)
print(f"100s same-bbox src: lap_mean={m:.2f} lap_std={s:.2f} px_std={reg.std():.1f}")
Image.fromarray(src100[box[1]:box[3], box[0]:box[2]].astype(np.uint8)).resize(
    ((box[2]-box[0])*3, (box[3]-box[1])*3), Image.LANCZOS).save('/tmp/atid_q/crop100_src.png')
print('crops saved')
