#!/usr/bin/env bash
# AIPod 设备只读侦察（M1.1 / 移植前画像）。所有命令只读，可反复跑。
# 用法: bash probe_x3.sh [--docker]   （--docker 额外探测 docker，需 sudo）
set -u
DO_DOCKER=0
[ "${1:-}" = "--docker" ] && DO_DOCKER=1

sec() { echo; echo "===== $1 ====="; }

sec "系统/L4T"
hostname; uname -a
cat /etc/nv_tegra_release 2>/dev/null || echo "no nv_tegra_release"
grep -m1 MODEL /proc/device-tree/model 2>/dev/null || cat /proc/device-tree/model 2>/dev/null; echo
cat /etc/os-release | grep -E 'PRETTY_NAME|VERSION='

sec "内存/存储"
free -h
df -h / /home 2>/dev/null | grep -v tmpfs

sec "Python"
for p in python3 python3.10 python3.12; do command -v $p >/dev/null && echo "$p: $($p --version 2>&1)"; done

sec "CUDA/驱动可见性"
command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader || echo "no nvidia-smi (L4T 正常)"
ls /usr/local/cuda*/version* 2>/dev/null && cat /usr/local/cuda/version.json 2>/dev/null | head -5
cat /usr/local/cuda/include/cuda.h 2>/dev/null | grep -m1 'CUDA_VERSION'

sec "Tegra 设备节点"
ls -1 /dev/ | grep -E 'nv|v4l2|media' | head -20

sec "GStreamer nvv4l2/nv 元素"
gst-inspect-1.0 2>/dev/null | grep -E 'nvv4l2|nvvidconv|nvjpeg' | head -15 || echo "gst-inspect 不可用"
gst-inspect-1.0 --version 2>/dev/null | head -1

sec "ffmpeg"
for f in ffmpeg /usr/bin/ffmpeg; do command -v $f >/dev/null && $f -version 2>/dev/null | head -1 && break; done
echo "nvenc encoders:"; command -v ffmpeg >/dev/null && ffmpeg -hide_banner -encoders 2>/dev/null | grep -iE 'nvenc|v4l2' || echo "(无)"

sec "jetson 工具"
command -v jetson_release >/dev/null && jetson_release 2>/dev/null | head -25 || echo "no jetson_release"
command -v tegrastats >/dev/null && echo "tegrastats: 有" || echo "tegrastats: 无"

sec "TRT/CUDA 库与 python 包（宿主）"
ls -d /usr/lib/python3*/dist-packages/tensorrt* 2>/dev/null || echo "宿主无 tensorrt python 包"
ls /usr/src/tensorrt/bin/trtexec 2>/dev/null || echo "无 /usr/src/tensorrt/bin/trtexec"
ls -d /usr/lib/aarch64-linux-gnu/nvidia /usr/lib/aarch64-linux-gnu/tegra 2>/dev/null

if [ "$DO_DOCKER" = 1 ]; then
  sec "Docker（sudo）"
  SUDO="printf 'nvidia\n' | sudo -S -p ''"
  eval "$SUDO docker version --format '{{.Server.Version}}'" 2>/dev/null
  eval "$SUDO docker info" 2>/dev/null | grep -E 'Runtimes|nvidia|Docker Root'
  sec "运行中容器"
  eval "$SUDO docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'" 2>/dev/null | head -15
fi

echo; echo "===== 侦察完成 ====="
