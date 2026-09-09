import os
import sys
from pathlib import Path

# 路径：优先命令行/环境变量指定的仓库根（apply.sh 场景），否则容器默认 /jasna
root = Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get('JASNA_SRC', '/jasna'))
p = root / 'jasna/media/video_decoder.py'
src = p.read_text()
if 'JASNA_RING_PATCH' in src:
    print('already patched')
    raise SystemExit(0)

anchor = """        pinned = torch.empty((self.batch_size, H + H // 2, W), dtype=dtype, pin_memory=True)
        staging = torch.empty((H + H // 2, W), dtype=dtype, device=self.device)
        stream = new_stream(self.device)

        while group:
            batch = torch.empty((len(group), 3, H, W), device=self.device, dtype=torch.uint8)"""
new = """        pinned = torch.empty((self.batch_size, H + H // 2, W), dtype=dtype, pin_memory=True)
        staging = torch.empty((H + H // 2, W), dtype=dtype, device=self.device)
        stream = new_stream(self.device)

        # JASNA_RING_PATCH (DGX Spark 部署层补丁): 批缓冲改为环形预分配。
        # 全管线并发（检测 TRT + BasicVSR++ + 双 reader）下，缓存分配器
        # 复用的块会被残留异步写命中，输出整帧均匀噪声（花屏），位置逐次
        # 漂移；PYTORCH_NO_CUDA_MEMORY_CACHING=1 时连续 5 轮干净。环形块
        # 常驻 reader 生命周期、永不回池，规避踩踏。crop 消费端为 clone，
        # reader 按消费节奏拉取，深度 8 有充足余量。
        ring_depth = 8
        ring = [
            torch.empty((self.batch_size, 3, H, W), device=self.device, dtype=torch.uint8)
            for _ in range(ring_depth)
        ]
        ring_i = 0

        while group:
            batch = ring[ring_i % ring_depth][:len(group)]
            ring_i += 1"""
assert anchor in src, 'anchor not found'
p.write_text(src.replace(anchor, new))
print('ring patch ok')
