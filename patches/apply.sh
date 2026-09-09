#!/bin/sh
# jasna 部署补丁（幂等）：CPU 编码器回退 + web 后端文件就位。
# 用法: 在 Spark 宿主 sh apply.sh（源码目录默认 /tmp/jasna）
set -e
SRC="${1:-/tmp/jasna}"
PATCH_DIR="$(cd "$(dirname "$0")" && pwd)"

# 1) 回退模块 + web 后端
cp "$PATCH_DIR/cpu_encoder_fallback.py" "$SRC/jasna/cpu_encoder_fallback.py"
cp "$PATCH_DIR/jasna_web.py" "$SRC/jasna_web.py"
cp "$PATCH_DIR/scan_worker.py" "$SRC/scan_worker.py"

# 2) pipeline.py: import 工厂 + 两处实例化改工厂（幂等：先还原再套用）
cd "$SRC"
sed -i '/from jasna.cpu_encoder_fallback import make_encoder/d' jasna/pipeline.py
sed -i 's|^from jasna.media.video_encoder import NvidiaVideoEncoder$|from jasna.media.video_encoder import NvidiaVideoEncoder\nfrom jasna.cpu_encoder_fallback import make_encoder|' jasna/pipeline.py
sed -i 's|encoder_ctx = make_encoder(|encoder_ctx = NvidiaVideoEncoder(|g' jasna/pipeline.py
sed -i 's|encoder_ctx = NvidiaVideoEncoder(|encoder_ctx = make_encoder(|g' jasna/pipeline.py

# 3) smart render copy 片段绕 NUT（BtbN ffmpeg8 读 PyAV NUT 丢 B 帧）
#    copy 片段直出 TS（模式含 ", raw, codec" 参数名，替换后不再命中，天然幂等）
sed -i 's|create_copy_fragment(self.input_video, span, index, raw, codec=codec)|from jasna.cpu_encoder_fallback import create_copy_ts_fragment; create_copy_ts_fragment(self.input_video, span, index, normalized, codec=codec)|' "$SRC/jasna/pipeline.py"
#    normalize 只作用于 render 片段（幂等守卫防叠加包行）
grep -q 'if span.is_render: normalize_fragment' jasna/pipeline.py || \
sed -i 's|normalize_fragment(raw, normalized, codec=codec)|if span.is_render: normalize_fragment(raw, normalized, codec=codec)|' "$SRC/jasna/pipeline.py"
#    自愈历史双写行（曾因非幂等 sed 产生 "if span.is_render: if span.is_render: ..."）
sed -i 's|if span.is_render: if span.is_render: normalize_fragment|if span.is_render: normalize_fragment|' "$SRC/jasna/pipeline.py"

# 4) reader 批缓冲环形预分配（DGX Spark 分配器踩踏规避，幂等）
#    全管线并发下缓存分配器复用块被残留异步写命中 → 整帧均匀噪声花屏，
#    位置逐次漂移；PYTORCH_NO_CUDA_MEMORY_CACHING=1 对照 5/5 干净定位。
#    补丁后 6/6 复验干净。详见 add_ring_buffer.py 头注。
cp "$PATCH_DIR/add_ring_buffer.py" /tmp/add_ring_buffer.py
sed -i 's/\r$//' /tmp/add_ring_buffer.py
python3 /tmp/add_ring_buffer.py "$SRC"

# 5) NVENC 硬编输出通道（GB10 解耦加速模块）
#    CPU 编码是管线瓶颈（36s 段 write 40.9s）；nvenc_output.py 以子进程
#    ffmpeg 接管编码（rgb24 管道 + GPU NV12 快通道可选），工厂探测链
#    PyAV nvenc → 本模块 → CPU 回退，JASNA_ENCODER=nvenc|cpu|auto 可控。
#    检测用 lada-yolo-v4 时另有 onnx 复用补丁（见 6）。
cp "$PATCH_DIR/nvenc_output.py" "$SRC/jasna/nvenc_output.py"

# 6) yolo 引擎编译：.pt 旁已有同名 .onnx 时跳过 ultralytics 导出（幂等）
#    ultralytics export 内部置 CUDA_VISIBLE_DEVICES="" 且 torch 缓存失败
#    初始化，毒化同进程 TRT builder（No CUDA GPUs are available）。
cp "$PATCH_DIR/add_yolo_onnx_skip.py" /tmp/add_yolo_onnx_skip.py
sed -i 's/\r$//' /tmp/add_yolo_onnx_skip.py
python3 /tmp/add_yolo_onnx_skip.py "$SRC"

# 7) 段任务断点续跑（JASNA_WORKDIR_PATCH，幂等）：
#    working_dir 语义扩展为确切段目录（不自删）+ 完整片段跳过 + 成功后清理；
#    配套 jasna_web.py 的 .work-<id> 预生成 / 让位 SIGKILL / retry 复用。
cp "$PATCH_DIR/add_workdir_resume.py" /tmp/add_workdir_resume.py
sed -i 's/\r$//' /tmp/add_workdir_resume.py
python3 /tmp/add_workdir_resume.py "$SRC"


# 8) 流式播放器相对路径（JASNA_STREAM_RELPATH，幂等）：
#    上游播放器 API 是根绝对路径，子路径反代（/jasna/）下会打到域名根被 HTML 响应。
cp "$PATCH_DIR/add_stream_relpaths.py" /tmp/add_stream_relpaths.py
sed -i 's/$//' /tmp/add_stream_relpaths.py
python3 /tmp/add_stream_relpaths.py "$SRC"

# 10) PyAV17 兼容（JASNA_AV17_COMPAT，幂等）：
#     av 18.x 全系 requires-python>=3.11，JP6（Orin AIPod）生态只有 cp310+av17；
#     shim 掉 Colorspace 缺失成员（BT2020），BT.709 主路径零变化，GB10（av18）透传。
cp "$PATCH_DIR/add_av17_compat.py" /tmp/add_av17_compat.py
sed -i 's/\r$//' /tmp/add_av17_compat.py
python3 /tmp/add_av17_compat.py "$SRC"

# 11) L4T 驱动版本门放行（JASNA_L4T_DRIVER_PATCH，幂等）：
#     Jetson L4T 驱动版本号体系与桌面驱动无关（Orin JP6 报 540.5.0 vs 门槛 580），
#     /etc/nv_tegra_release 存在即放行；GB10 Spark 驱动 ≥580 走原路径。
cp "$PATCH_DIR/add_l4t_driver_gate.py" /tmp/add_l4t_driver_gate.py
sed -i 's/\r$//' /tmp/add_l4t_driver_gate.py
python3 /tmp/add_l4t_driver_gate.py "$SRC"

# 12) Jetson gst 硬编码 lane（JASNA_GST_ENC，幂等）：
#     L4T 无 NVENC SDK，硬编码走 gst nvv4l2（模块 + 工厂链由 cpu_encoder_fallback.py
#     整文件覆盖携带）；render 片段 gst 直出 TS 时经 maybe_normalized_fragment 落位。
cp "$PATCH_DIR/jetson_gst_encoder.py" "$SRC/jasna/jetson_gst_encoder.py"
grep -q 'maybe_normalized_fragment' "$SRC/jasna/pipeline.py" || \
sed -i 's|if span.is_render: normalize_fragment(raw, normalized, codec=codec)|if span.is_render: from jasna.jetson_gst_encoder import maybe_normalized_fragment as _mnf; _mnf(raw, normalized, codec=codec)|' "$SRC/jasna/pipeline.py"

# 13) jetson-gst NVDEC 解码后端（JASNA_GST_DEC，幂等）：
#     DECODE_BACKEND 环境变量化 + "jetson-gst" 显式后端（auto 不自动选）；
#     ffmpeg 解封装+seek | nvv4l2decoder | nvvidconv RGB，pts 锚点+CFR 重建。
cp "$PATCH_DIR/jetson_gst_decoder.py" "$SRC/jasna/jetson_gst_decoder.py"
cp "$PATCH_DIR/add_jetson_gst_decode.py" /tmp/add_jetson_gst_decode.py
sed -i 's/\r$//' /tmp/add_jetson_gst_decode.py
python3 /tmp/add_jetson_gst_decode.py "$SRC"

# 14) 编码卡死看门狗升级（JASNA_STALL_ESCALATION_PATCH，幂等）：
#     nvenc 子进程挂死 → BlendEncode 崩溃 → SecondaryRestore 堵死在
#     encode_queue.put → 任务假 running（2026-09-05 390JAC-068，卡 61% 3.3h）。
#     原看门狗只告警；升级版 900s 无编码产出先杀子进程，再宽限 180s 仍卡死
#     os._exit(2) → web 标 failed → retry 复用 work_dir 断点续跑。
#     不能用 SIGTERM：上游优雅清理会删片段目录（见 add_workdir_resume.py）。
cp "$PATCH_DIR/add_encode_stall_escalation.py" /tmp/add_encode_stall_escalation.py
sed -i 's/\r$//' /tmp/add_encode_stall_escalation.py
python3 /tmp/add_encode_stall_escalation.py "$SRC"

echo "patched: make_encoder count = $(grep -c 'make_encoder(' jasna/pipeline.py), web = $(test -f jasna_web.py && echo yes), ring = $(grep -c JASNA_RING_PATCH jasna/media/video_decoder.py), nvenc = $(test -f jasna/nvenc_output.py && echo yes), yolo-onnx-skip = $(grep -c JASNA_PREEXISTING_ONNX jasna/mosaic/yolo_tensorrt_compilation.py), workdir-resume = $(grep -c JASNA_WORKDIR_PATCH jasna/pipeline.py), stream-relpath = $(grep -c "fetch('open'" jasna/streaming.py), av17-compat = $(grep -c 'jasna.av17_compat' jasna/media/video_encoder.py), py310-strenum = $(grep -c 'py310_compat' jasna/accelerator.py), stall-escalation = $(grep -c JASNA_STALL_ESCALATION_PATCH jasna/vram_offloader.py)"
