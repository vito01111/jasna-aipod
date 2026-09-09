#!/usr/bin/env bash
# x3 上构建 jasna-orin 镜像（幂等；由本机 scp 上去跑，勿拼内联命令）
set -e
BUILD_DIR=/tmp/jasna-orin-build
mkdir -p "$BUILD_DIR"
cp /tmp/Dockerfile.orin "$BUILD_DIR/Dockerfile"
cp /tmp/ffmpeg-n8.1-linuxarm64.tar.xz "$BUILD_DIR/"
cd "$BUILD_DIR"
docker build --progress=plain -t jasna-orin:0.1.0 . 2>&1
echo "BUILD_OK jasna-orin:0.1.0"
