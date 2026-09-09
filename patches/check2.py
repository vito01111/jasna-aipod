import os
import subprocess

print("=== thumbnail sizes f0050-f0080 (noise thumb >> real thumb) ===")
files = sorted(os.listdir('/tmp/encdbg'))
for fn in files:
    if fn.startswith('f0') and fn.endswith('.jpg'):
        idx = int(fn[1:5])
        if 50 <= idx <= 80:
            print("  %s  %d bytes" % (fn, os.path.getsize('/tmp/encdbg/' + fn)))

print("=== all thumbs size summary ===")
sizes = [(int(fn[1:5]), os.path.getsize('/tmp/encdbg/' + fn)) for fn in files if fn.endswith('.jpg')]
sizes.sort()
small = [s for _, s in sizes if _ <= 120]
print("thumbs: n=%d, min=%d, max=%d, median=%d" % (
    len(sizes), min(s for _, s in sizes), max(s for _, s in sizes),
    sorted(s for _, s in sizes)[len(sizes)//2]))
big = [(i, s) for i, s in sizes if s > 3 * sorted(x for _, x in sizes)[len(sizes)//2]]
print("outliers (>3x median):", big[:20])

print("=== rerun raw packets (first 8 + around pts base) ===")
out = subprocess.run(
    ["/opt/ff8/bin/ffprobe", "-v", "error", "-select_streams", "v:0",
     "-show_entries", "packet=pts_time,size,flags", "-of", "csv", "/outputs/seg3138-rerun.mkv"],
    capture_output=True, text=True).stdout
lines = out.splitlines()
print("total packets:", len(lines))
for ln in lines[:5]:
    print("  ", ln)
big_pkts = [ln for ln in lines if len(ln.split(',')) >= 3 and int(ln.split(',')[2]) > 300000]
print("packets >300KB:", len(big_pkts))
for ln in big_pkts[:15]:
    print("  ", ln)
