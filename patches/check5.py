def load(path, mean_i, std_i):
    rows = [l.rstrip('\n').split('\t') for l in open(path) if l.strip()]
    bad = [r for r in rows if float(r[std_i]) > 75 or float(r[mean_i]) < 100 or float(r[mean_i]) > 160]
    return rows, bad


brows, bbad = load('/tmp/encdbg/blend.tsv', 3, 4)
erows, ebad = load('/tmp/encdbg/stats.tsv', 2, 3)
print('blend rows:', len(brows), ' anomalies:', len(bbad))
for r in bbad[:15]:
    print('  idx=%s pts=%s effect=%s mean=%s std=%s' % tuple(r))
print('encoder rows:', len(erows), ' anomalies:', len(ebad))
for r in ebad[:15]:
    print('  idx=%s pts=%s mean=%s std=%s max=%s' % tuple(r))
print('=== encoder thumbs >3x median this run ===')
import os
files = [(int(f[1:5]), os.path.getsize('/tmp/encdbg/' + f)) for f in os.listdir('/tmp/encdbg') if f.endswith('.jpg')]
files.sort()
med = sorted(s for _, s in files)[len(files)//2]
print('median thumb:', med)
print('outliers:', [(i, s) for i, s in files if s > 3*med])
