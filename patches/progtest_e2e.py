import json, subprocess, time, urllib.request

API = "http://127.0.0.1:18766/api/jobs"
CLIP = "/videos/__progtest_5994.mp4"

def api(path="", data=None, method=None):
    req = urllib.request.Request(API + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json"}, method=method)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

subprocess.run(["docker", "exec", "lada-jasna-1", "ffmpeg", "-y", "-v", "error",
    "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=59.94:duration=20",
    "-pix_fmt", "yuv420p", CLIP], check=True)

job = api("", {"input": CLIP, "output_name": "__progtest-out.mp4",
               "detection_model": "rfdetr-v6-large"}, "POST")
jid = job["id"]
print(f"job={jid} total_frames={job['total_frames']} (期望≈1199=20s*59.94)")
print("old-bug check: 59.94fps 片若仍按 30fps 估 total 应为 600；此处取实际 fps")

traj = []
t0 = time.time()
while time.time() - t0 < 420:
    js = api()
    cur = next(j for j in (js if isinstance(js, list) else js.get("jobs", [])) if j["id"] == jid)
    point = (round(time.time() - t0), cur["state"], cur["progress_pct"], cur["fps"])
    if not traj or traj[-1][1:] != point[1:]:
        traj.append(point)
        print(f"t={point[0]:>3}s state={point[1]:<8} pct={point[2]:>5} fps={point[3]}", flush=True)
    if cur["state"] in {"done", "failed", "cancelled", "interrupted"}:
        break
    time.sleep(2)

mid = [p for p in traj if p[1] == "running" and 0 < p[2] < 100]
print("VERDICT:", "PASS" if (mid and cur["state"] == "done" and cur["progress_pct"] >= 99)
      else "CHECK_MANUALLY", f"(mid-run 0<pct<100 样本数={len(mid)})")
