import os
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else os.environ.get('JASNA_SRC', '/jasna'))
p = root / 'jasna/mosaic/yolo_tensorrt_compilation.py'
src = p.read_text()
if 'JASNA_PREEXISTING_ONNX' in src:
    print('already patched')
    raise SystemExit(0)

anchor = """    _prev_cuda_visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    try:
        model = YOLO(str(model_path), verbose=False, task="segment")"""
patch = """    _prev_cuda_visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    # JASNA_PREEXISTING_ONNX（DGX Spark 部署层补丁）：.pt 旁已有同名 .onnx 时
    # 直接复用，跳过 ultralytics 导出。ultralytics export 内部会临时置
    # CUDA_VISIBLE_DEVICES=""，即使外层恢复变量，torch 对失败初始化的缓存
    # 也会毒化同进程后续的 TensorRT builder（torch._C._cuda_init 持续报
    # No CUDA GPUs are available）。
    _preexisting_onnx = Path(str(model_path)).with_suffix(".onnx")
    if _preexisting_onnx.exists():
        print(f"Using pre-existing ONNX {_preexisting_onnx} (skipping ultralytics export)")
        exported = _preexisting_onnx
    else:
        try:
            model = YOLO(str(model_path), verbose=False, task="segment")"""
assert anchor in src, 'anchor A not found'
src = src.replace(anchor, patch)

# 收尾：原 try/finally 中的导出体与 del model 移入 else 分支
anchor2 = """    finally:
        YOLO_LOGGER.setLevel(_prev_yolo_level)
        if _prev_cuda_visible is None:
            os.environ.pop("CUDA_VISIBLE_DEVICES", None)
        else:
            os.environ["CUDA_VISIBLE_DEVICES"] = _prev_cuda_visible
    del model
"""
patch2 = """        finally:
            YOLO_LOGGER.setLevel(_prev_yolo_level)
            if _prev_cuda_visible is None:
                os.environ.pop("CUDA_VISIBLE_DEVICES", None)
            else:
                os.environ["CUDA_VISIBLE_DEVICES"] = _prev_cuda_visible
        del model
"""
assert anchor2 in src, 'anchor B not found'
src = src.replace(anchor2, patch2)

# 导出体的缩进整体 +4（原 try: 内的 null_stream..exported = model.export 块）
anchor3 = """        null_stream = io.StringIO()
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"To copy construct from a tensor, it is recommended to use sourceTensor\\.detach\\(\\)\\.clone\\(\\).*",
                category=UserWarning,
                module=r"ultralytics\\.engine\\.exporter",
            )
            with contextlib.redirect_stdout(null_stream), contextlib.redirect_stderr(null_stream):
                exported = model.export(
                    format="onnx",
                    imgsz=int(imgsz) if isinstance(imgsz, int) else tuple(int(x) for x in imgsz),
                    dynamic=False,
                    nms=False,
                    batch=int(batch),
                    half=bool(fp16),
                )
"""
patch3 = """            null_stream = io.StringIO()
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message=r"To copy construct from a tensor, it is recommended to use sourceTensor\\.detach\\(\\)\\.clone\\(\\).*",
                    category=UserWarning,
                    module=r"ultralytics\\.engine\\.exporter",
                )
                with contextlib.redirect_stdout(null_stream), contextlib.redirect_stderr(null_stream):
                    exported = model.export(
                        format="onnx",
                        imgsz=int(imgsz) if isinstance(imgsz, int) else tuple(int(x) for x in imgsz),
                        dynamic=False,
                        nms=False,
                        batch=int(batch),
                        half=bool(fp16),
                    )
"""
assert anchor3 in src, 'anchor C not found'
src = src.replace(anchor3, patch3)
p.write_text(src)
print('yolo onnx-skip patch ok')
