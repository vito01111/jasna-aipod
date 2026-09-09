import os
import subprocess
import sys

os.chdir('/jasna')
os.environ['PATH'] = '/opt/ff8/bin:' + os.environ.get('PATH', '')
os.environ['JASNA_ENC_DEBUG'] = '1'
os.environ['JASNA_BLEND_DEBUG'] = '1'

for attempt in range(1, int(sys.argv[1]) + 1 if len(sys.argv) > 1 else 5):
    for f in ('/tmp/encdbg/stats.tsv', '/tmp/encdbg/blend.tsv'):
        if os.path.exists(f):
            os.remove(f)
    for f in os.listdir('/tmp/encdbg'):
        if f.endswith('.jpg'):
            os.remove('/tmp/encdbg/' + f)
    out = f'/outputs/catch{attempt}.mkv'
    r = subprocess.run(
        ['python3', '-m', 'jasna', '--input', '/videos/dass-377-seg3138.mp4',
         '--output', out, '--codec', 'h264', '--detection-model', 'rfdetr-v6-large',
         '--log-level', 'warning'],
        capture_output=True, text=True, timeout=600)
    # detect glitch: thumbnail size outlier
    sizes = [(int(f[1:5]), os.path.getsize('/tmp/encdbg/' + f))
             for f in os.listdir('/tmp/encdbg') if f.endswith('.jpg')]
    if not sizes:
        print(f'attempt {attempt}: no thumbs, rc={r.returncode}')
        continue
    med = sorted(s for _, s in sizes)[len(sizes) // 2]
    outliers = [(i, s) for i, s in sizes if s > 2.5 * med]
    print(f'attempt {attempt}: rc={r.returncode} thumbs={len(sizes)} median={med} outliers={outliers}')
    if outliers:
        print(f'>>> GLITCH CAUGHT at frames {[i for i, _ in outliers]} (attempt {attempt})')
        print('=== blend.tsv at those frames ===')
        want = {i for i, _ in outliers}
        for line in open('/tmp/encdbg/blend.tsv'):
            p = line.rstrip('\n').split('\t')
            if int(p[0]) in want or abs(int(p[0]) - min(want)) <= 2:
                print('  idx=%s pts=%s effect=%s mean=%s std=%s' % tuple(p))
        break
    else:
        os.remove(out)
