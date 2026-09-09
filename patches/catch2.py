import os
import subprocess
import sys

os.chdir('/jasna')
os.environ['PATH'] = '/opt/ff8/bin:' + os.environ.get('PATH', '')
os.environ['JASNA_ENC_DEBUG'] = '1'
os.environ['JASNA_BLEND_DEBUG'] = '1'
os.environ['JASNA_READER_DEBUG'] = '1'

REPORT = open('/tmp/encdbg/report.txt', 'w', 1)

for attempt in range(1, 7):
    for f in ('stats.tsv', 'blend.tsv', 'reader_sw.tsv', 'reader_hw.tsv'):
        p = '/tmp/encdbg/' + f
        if os.path.exists(p):
            os.remove(p)
    for f in os.listdir('/tmp/encdbg'):
        if f.endswith('.jpg'):
            os.remove('/tmp/encdbg/' + f)
    out = f'/outputs/catch{attempt}.mkv'
    r = subprocess.run(
        ['python3', '-m', 'jasna', '--input', '/videos/dass-377-seg3138.mp4',
         '--output', out, '--codec', 'h264', '--detection-model', 'rfdetr-v6-large',
         '--log-level', 'warning'],
        capture_output=True, text=True, timeout=600)
    sizes = [(int(f[1:5]), os.path.getsize('/tmp/encdbg/' + f))
             for f in os.listdir('/tmp/encdbg') if f.endswith('.jpg')]
    if not sizes:
        REPORT.write(f'attempt {attempt}: no thumbs rc={r.returncode}\n')
        continue
    med = sorted(s for _, s in sizes)[len(sizes) // 2]
    outliers = [(i, s) for i, s in sizes if s > 2.5 * med]
    REPORT.write(f'attempt {attempt}: rc={r.returncode} median={med} outliers={outliers}\n')
    if outliers:
        want = sorted(i for i, _ in outliers)
        REPORT.write(f'>>> GLITCH frames {want}\n')
        reader = {}
        for name in ('reader_sw.tsv', 'reader_hw.tsv'):
            p = '/tmp/encdbg/' + name
            if os.path.exists(p):
                for line in open(p):
                    q = line.split('\t')
                    reader[int(q[0])] = (q[1], q[2].strip())
        blend = {}
        for line in open('/tmp/encdbg/blend.tsv'):
            q = line.rstrip('\n').split('\t')
            blend[int(q[0])] = q
        for i in want:
            b = blend.get(i)
            if b:
                pt = int(b[1])
                rr = reader.get(pt)
                REPORT.write(
                    f'frame {i}: blend(orig) mean/std={b[3]}/{b[4]} effect={b[2]} | '
                    f'reader-at-sync mean/std={rr[0]}/{rr[1] if rr else "N/A"}\n')
        break
    else:
        os.remove(out)
REPORT.write('DONE\n')
REPORT.close()
