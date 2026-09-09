"""x3 首编 TRT 引擎（M1.3）：basicvsrpp 子引擎 + rfdetr 检测引擎，fp16。

容器内跑（detached）：
  cd /tmp && docker compose -f docker-compose.orin.yml run -d --name jasna-compile \
    jasna sh -c "cd /jasna && python3 compile_engines_x3.py"
日志：docker logs jasna-compile
"""
import sys
import time

sys.path.insert(0, "/jasna")

from jasna.engine_compiler import EngineCompilationRequest, _subprocess_compile

req = EngineCompilationRequest(
    device="cuda:0",
    fp16=True,
    basicvsrpp=True,
    basicvsrpp_model_path="model_weights/lada_mosaic_restoration_model_generic_v1.2.pth",
    detection=True,
    detection_model_name="rfdetr-v6-large",
    detection_model_path="model_weights/rfdetr-v6-large.onnx",
    detection_batch_size=4,
)

t0 = time.time()
print("compiling:", req, flush=True)
_subprocess_compile(req)
print(f"COMPILE_DONE in {time.time() - t0:.0f}s", flush=True)
