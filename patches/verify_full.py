import subprocess

FF = '/opt/ff8/bin/ffprobe'
out = subprocess.run(
    [FF, '-v', 'error', '-select_streams', 'v:0',
     '-show_entries', 'packet=pts_time,size', '-of', 'csv', '--', '/outputs/dass-377-restored.mkv'],
    capture_output=True, text=True).stdout
pkts = [l.split(',') for l in out.splitlines() if l.strip()]
sizes = [int(p[2]) for p in pkts if len(p) > 2]
big = [(p[1], p[2]) for p in pkts if len(p) > 2 and int(p[2]) > 400000]
sizes_sorted = sorted(sizes)
print('packets:', len(pkts))
print('size: median=%dKB p99=%dKB max=%dKB (%.1fMB)' % (
    sizes_sorted[len(sizes)//2]//1024, sizes_sorted[int(len(sizes)*0.99)]//1024,
    sizes_sorted[-1]//1024, sizes_sorted[-1]/1048576))
print('>400KB packets (glitch-class check):', len(big))
for b in big[:10]:
    print('  ', b)
