#!/usr/bin/env bash
# x3 上构建 jasna-aipod 商店镜像并推送（M2.5）。
# 上下文 = 打好补丁的源码树（选择性拷贝）+ ffmpeg8 tarball + Dockerfile.agxorin。
set -e
SRC=/home/nvidia/jasna
PATCH_DIR=/home/nvidia/jasna-patches
CTX=/tmp/jasna-store-build
IMAGE_TAG=${1:-0.1.1}
ACR="${JASNA_ACR:?请 export JASNA_ACR=<你的镜像仓库>/jasna-aipod-agxorin}"
IMAGE=$ACR:$IMAGE_TAG

echo "== 准备构建上下文 =="
rm -rf "$CTX" && mkdir -p "$CTX/jasna"
( cd "$SRC" && tar cf - \
    --exclude='*.engine' --exclude='*_sub_engines' --exclude='*.bak-gb10' \
    --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' \
    --exclude='smoke_m12.py' --exclude='test_gst_encoder.py' --exclude='test_gst_decoder.py' \
    --exclude='diag*.py' --exclude='bench_decode.py' --exclude='compile_engines_x3.py' \
    jasna jasna_web.py scan_worker.py model_weights scripts pyproject.toml README.md ) \
  | ( cd "$CTX/jasna" && tar xf - )
ls "$CTX/jasna" | head -8
du -sh "$CTX/jasna"

FF=/tmp/ffmpeg-n8.1-linuxarm64.tar.xz
[ -f "$FF" ] || { echo "缺少 $FF（先从本机 scp 上来）"; exit 1; }
cp "$FF" "$CTX/"
cp "$PATCH_DIR/../aipod-pkg/Dockerfile.agxorin" "$CTX/Dockerfile" 2>/dev/null \
  || cp /tmp/Dockerfile.agxorin "$CTX/Dockerfile"

echo "== docker build =="
docker build --progress=plain -t "$IMAGE" "$CTX" 2>&1 | tail -5
echo "== docker push ==（本地已 tag，ACR 推送）"
docker push "$IMAGE" 2>&1 | tail -3
echo "STORE_IMAGE_DONE $IMAGE"
