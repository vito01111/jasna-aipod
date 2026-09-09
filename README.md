# Jasna AIPod · 视频修复工作台

<p><img src="pkg/lzc-icon.png" width="96" align="left" style="border-radius:20px;margin-right:12px"/></p>

把 [Jasna](https://github.com/Kruk2/jasna) 神经网络视频修复引擎（RF-DETR 马赛克检测 + BasicVSR++ 修复）
移植到 **懒猫微服 AI 算力舱**（NVIDIA Jetson AGX Orin）的开源打包与移植层：
上传 → 自动扫描定位 → TensorRT 推理修复 → 成品库 / 保存到懒猫网盘，一条龙 Web 工作台。

本仓库 = **AIPod 移植层**（补丁集 + LPK 打包 + 设备镜像构建），不含上游引擎本体；
上游源码请从 [Kruk2/jasna](https://github.com/Kruk2/jasna) 获取（AGPL-3.0）。

## 功能特性

- **全链 Web 工作台**：分片上传 / 断点续传、后台扫描（马赛克段自动定位与缩略图）、
  任务队列（smart-render 段级渲染 / 全片渲染）、成品库版本链管理、对比预览、
  回传懒猫网盘（lzc-file-picker / WebDAV 平台通道）
- **Jetson 深度优化**：NVDEC/NVENC 硬解硬编（jetson-gst 通道）、TensorRT fp16 引擎、
  显存卸载（VramOffloader）、编码卡死看门狗升级（900s 无产出自动转 failed 可续跑）
- **预烤引擎（0.1.28+）**：镜像内置按 L4T/TensorRT 环境指纹预编译的推理引擎，
  新设备首启 15 秒装配完成、零编译开箱；固件 OTA 后指纹失配自动回落现场编译
- **首启演示（0.1.27+）**：空库首访弹欢迎层，一键体验内置 10s 官方测试片全链
  （上传→扫描→全片修复→成品入库约 30 秒）
- **断点续跑**：smart-render 段任务确定性工作目录，中断后可续

## 仓库结构

| 目录 | 内容 |
|---|---|
| `patches/` | 移植补丁全集：`jasna_web.py`（web 编排层，HTTP API + 任务/扫描/上传会话管理）、
| | `apply.sh`（幂等套用到上游源码树）、jetson gst 编解码、看门狗升级、断点续跑、环形缓冲等 |
| `pkg/` | LPK 打包：`content/`（前端 + 内置演示片 + 平台文件选择组件）、`aipod-resources/`
| | （AIPod 机型 compose + aipod.yml）、`Dockerfile.agxorin`（商店镜像）、manifest / package.yml |
| `ops/` | 设备侧工具：`build_store.sh`（镜像构建推送）、`prebake_setup.py`（预烤引擎首启装配）、
| | `compile_engines_x3.py`（引擎编译）、`probe_x3.sh`（设备只读侦察）、gst 通道自测脚本 |

## 构建与部署

### 1. 准备源码树（上游 + 本仓库补丁）

```sh
git clone https://github.com/Kruk2/jasna /tmp/jasna
cd /tmp/jasna && sh <本仓库>/patches/apply.sh /tmp/jasna   # 幂等套用全部补丁
```

### 2. 构建设备镜像（Orin）

```sh
# 在 Jetson 设备上（需要 ffmpeg8 tarball 等构建素材，见脚本内注释）
export JASNA_ACR=<你的镜像仓库>/jasna-aipod-agxorin
bash <本仓库>/ops/build_store.sh <版本tag>
```

新设备首启时 `ops/prebake_setup.py` 会按环境指纹装配镜像内预烤引擎（`l4tR36.5.2-trt10.3.0`），
无预烤引擎则回落为首个任务现场编译（约 15-60 分钟，一次性）。

### 3. 打包并安装 LPK

```sh
cd pkg && lzc-cli project release .
lzc-cli lpk install cloud.lazycat.aipod.jasna-<版本>.lpk
```

> `pkg/aipod-resources/agxorin/docker-compose.yml` 的镜像地址为占位符，
> 自行构建时请替换为你推送的镜像（或 export `JASNA_IMAGE`）。

## 版本速览

- 0.1.22 — 去设备绑定、网盘平台通道（上架整改）
- 0.1.26/27 — 镜像与 LPK 版本对齐（微烤）、首启使用演示
- 0.1.28 — 预烤引擎指纹化预装配（新设备零编译）
- 0.1.29 — 演示强制全片任务 + 双触发竞态修复
- 0.1.31 — 正式图标
- 0.1.32/33 — 回传显式双入口（picker 目录选择 + 流式 WebDAV 写，域名链路）

## 许可与致谢

- 本仓库补丁与打包层按 **AGPL-3.0** 发布（随上游引擎许可），见 [LICENSE](LICENSE)
- 上游引擎：[Kruk2/jasna](https://github.com/Kruk2/jasna)
- 本应用运行于[懒猫微服](https://lazycat.cloud) AI 算力舱（AIPod runtime）

## 免责声明

本工具用于视频画质的神经网络上色/修复用途，请遵守所在地区法律法规使用；
对处理内容的合法性与版权由使用者自行负责。
