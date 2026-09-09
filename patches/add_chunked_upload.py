#!/usr/bin/env python3
"""给 jasna_web.py 注入分片上传端点（幂等）。"""
import ast

path = "jasna_web.py"
t = open(path, encoding="utf-8").read()
if "upload-session" in t:
    print("already patched")
    raise SystemExit

# 1) 会话注册表 + 持久化恢复（挂在 _load_persisted_scans() 调用行之后）
call_line = "\n_load_persisted_scans()\n"
assert t.count(call_line) == 1
block = call_line + '''

UPLOAD_PART_SIZE = 32 * 1024 * 1024
_upload_sessions: dict[str, dict] = {}


def _upload_dir(upload_id: str) -> Path:
    d = OUTPUTS_DIR / "uploads" / upload_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_upload_sessions() -> None:
    root = OUTPUTS_DIR / "uploads"
    if not root.is_dir():
        return
    for meta_file in root.glob("*/meta.json"):
        try:
            payload = json.loads(meta_file.read_text(encoding="utf-8"))
            upload_id = meta_file.parent.name
            parts = {int(p.name) for p in meta_file.parent.iterdir() if p.name.isdigit()}
            _upload_sessions[upload_id] = {
                "id": upload_id, **payload,
                "parts": parts, "created_at": meta_file.stat().st_mtime,
            }
        except Exception:
            continue


_load_upload_sessions()
'''
t = t.replace(call_line, block, 1)

# 2) PUT part handler（_json 方法前插）
handlers = '''
    def _handle_upload_part(self, upload_id, part_number):
        sess = _upload_sessions.get(upload_id)
        if not sess:
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        expected = min(UPLOAD_PART_SIZE, sess["size"] - part_number * UPLOAD_PART_SIZE)
        if expected <= 0 or length != expected:
            return self._json({"error": f"bad part size {length} != {expected}"}, 400)
        part_path = _upload_dir(upload_id) / str(part_number)
        received = 0
        with open(part_path, "wb") as fh:
            while received < length:
                chunk = self.rfile.read(min(8 * 1024 * 1024, length - received))
                if not chunk:
                    break
                fh.write(chunk)
                received += len(chunk)
        if received != length:
            part_path.unlink(missing_ok=True)
            return self._json({"error": "truncated"}, 400)
        sess["parts"].add(part_number)
        return self._json({"ok": True, "part": part_number, "received_parts": len(sess["parts"])})

'''
anchor = "    def _json(self, obj, status=200):"
assert t.count(anchor) == 1
t = t.replace(anchor, handlers + anchor, 1)

# 3) GET 会话状态
get_anchor = '''        m = re.match(r"^/scans/([\\w]+)$", path)'''
assert t.count(get_anchor) == 1
get_block = '''        m = re.match(r"^/upload-session/([\\w]+)$", path)
        if m:
            sess = _upload_sessions.get(m.group(1))
            if not sess:
                return self._json({"error": "no such session"}, 404)
            return self._json({"upload_id": sess["id"], "name": sess["name"], "size": sess["size"],
                               "received_parts": sorted(sess["parts"]),
                               "total_parts": (sess["size"] + UPLOAD_PART_SIZE - 1) // UPLOAD_PART_SIZE})
''' + get_anchor
t = t.replace(get_anchor, get_block, 1)

# 4) POST：创建会话 + complete（挂在 scan POST 之后）
post_anchor = '''            threading.Thread(target=_run_scan, args=(scan_id,), daemon=True).start()
            return self._json(sc)'''
assert t.count(post_anchor) == 1
post_block = post_anchor + '''
        m = re.match(r"^/upload-session/([\\w]+)/complete$", path)
        if m:
            sess = _upload_sessions.get(m.group(1))
            if not sess:
                return self._json({"error": "no such session"}, 404)
            total_parts = (sess["size"] + UPLOAD_PART_SIZE - 1) // UPLOAD_PART_SIZE
            missing = [i for i in range(total_parts) if i not in sess["parts"]]
            if missing:
                return self._json({"error": "missing parts", "missing": missing[:20]}, 400)
            dest = VIDEOS_DIR / sess["name"]
            received = 0
            with open(dest, "wb") as out:
                for i in range(total_parts):
                    data = (_upload_dir(sess["id"]) / str(i)).read_bytes()
                    out.write(data)
                    received += len(data)
            if received != sess["size"]:
                dest.unlink(missing_ok=True)
                return self._json({"error": f"assembled {received}/{sess['size']}"}, 400)
            import shutil as _sh
            _sh.rmtree(_upload_dir(sess["id"]), ignore_errors=True)
            del _upload_sessions[sess["id"]]
            return self._json({"ok": True, "name": sess["name"], "size_mb": round(received / 1048576, 1)})
        if path == "/upload-session":
            name = Path(str(body.get("name", ""))).name
            size = int(body.get("size") or 0)
            if not name or Path(name).suffix.lower() not in VIDEO_EXTS or size <= 0 or size > 50 * 1024 ** 3:
                return self._json({"error": "invalid name/size"}, 400)
            upload_id = uuid.uuid4().hex[:12]
            meta = {"name": name, "size": size}
            _upload_sessions[upload_id] = {"id": upload_id, **meta, "parts": set(), "created_at": time.time()}
            _upload_dir(upload_id).joinpath("meta.json").write_text(json.dumps(meta), encoding="utf-8")
            return self._json({"upload_id": upload_id,
                               "part_size": UPLOAD_PART_SIZE,
                               "total_parts": (size + UPLOAD_PART_SIZE - 1) // UPLOAD_PART_SIZE})'''
t = t.replace(post_anchor, post_block, 1)

# 5) PUT 路由分派（do_PUT 顶部，原单文件上传保留为回退）
put_anchor = '''    def do_PUT(self):
        """本地上传：PUT /upload/{name} 原始 body 落 /videos。"""
        path = self._route()'''
assert t.count(put_anchor) == 1
t = t.replace(put_anchor, '''    def do_PUT(self):
        """分片块上传：PUT /upload-session/{id}/{n}；旧式单文件 PUT /upload/{name} 保留。"""
        path = self._route()
        m = re.match(r"^/upload-session/([\\w]+)/(\\d+)$", path)
        if m:
            return self._handle_upload_part(m.group(1), int(m.group(2)))
        path = self._route()''', 1)

ast.parse(t)
open(path, "w", encoding="utf-8").write(t)
print("chunked upload injected + syntax ok")
