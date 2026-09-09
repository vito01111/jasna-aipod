def rows(path):
    return {int(p[0]): p for p in (l.rstrip('\n').split('\t') for l in open(path) if l.strip())}

b = rows('/tmp/encdbg/blend.tsv')
e = rows('/tmp/encdbg/stats.tsv')
print(f"{'idx':>4} {'effect':>6} | orig mean/std | enc mean/std")
for i in range(74, 90):
    bb, ee = b.get(i), e.get(i)
    if bb and ee:
        print(f"{i:>4} {bb[2]:>6} | {bb[3]:>7}/{bb[4]:<7} | {ee[2]:>7}/{ee[3]:<7}")
