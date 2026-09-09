import csv
import subprocess

print("=== encoder-input stats: anomalies (std>75 or max<200) ===")
with open('/tmp/encdbg/stats.tsv') as f:
    rows = [line.rstrip('\n').split('\t') for line in f if line.strip()]
bad = [r for r in rows if float(r[3]) > 75 or int(r[4]) < 200]
print(f"total frames: {len(rows)}, anomalies: {len(bad)}")
for r in bad[:20]:
    print("  idx=%s pts=%s mean=%s std=%s max=%s" % tuple(r))

print("=== stats frames 55-75 ===")
for r in rows:
    if 55 <= int(r[0]) <= 75:
        print("  idx=%s pts=%s mean=%s std=%s max=%s" % tuple(r))

print("=== rerun output packets 1.6-2.6s ===")
out = subprocess.run(
    ["/opt/ff8/bin/ffprobe", "-v", "error", "-select_streams", "v:0",
     "-show_entries", "packet=pts,size,flags", "-of", "csv", "/outputs/seg3138-rerun.mkv"],
    capture_output=True, text=True).stdout
for line in out.splitlines():
    p = line.split(',')
    if len(p) >= 2 and p[1] and 1.6 < float(p[1]) < 2.6:
        print("  pts=%s size=%s flags=%s" % (p[1], p[2] if len(p) > 2 else '?', p[3] if len(p) > 3 else '?'))
