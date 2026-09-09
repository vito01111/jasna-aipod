import subprocess

out = subprocess.run(
    ['/opt/ff8/bin/ffprobe', '-v', 'error', '-select_streams', 'v:0',
     '-show_entries', 'packet=pts_time,size', '-of', 'csv',
     '/outputs/dass-377-seg3138-restored.mkv'],
    capture_output=True, text=True).stdout
pkts = [l.split(',') for l in out.splitlines() if l.strip()]
big = [(p[1], p[2]) for p in pkts if len(p) > 2 and int(p[2]) > 300000]
print('packets:', len(pkts))
print('>300KB packets:', big if big else 'NONE')
