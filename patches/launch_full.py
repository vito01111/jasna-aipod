import json
import urllib.request

BASE = 'http://localhost:18766'

# 1) fetch persisted scan for dass-377
scans = json.load(urllib.request.urlopen(BASE + '/scans'))['scans']
scan = next(s for s in scans if s['input'] == '/videos/dass-377.mp4')
spans = scan['spans']
print('scan', scan['id'], 'spans:', len(spans), 'state:', scan['state'])


def jr(x):  # JS Math.round semantics (round half up)
    return int(x + 0.5)


segments = ','.join(f'{jr(s["start"])}-{jr(s["end"])}' for s in spans)
total = sum(s['end'] - s['start'] for s in spans)
dur = 8251.0
print(f'selected: {len(spans)} spans, restore {total:.0f}s / {dur:.0f}s = {total/dur*100:.1f}%')
print('segments head:', segments[:120], '... tail:', segments[-60:])

# 2) submit job (same payload shape as the web UI task modal)
payload = {
    'input': '/videos/dass-377.mp4',
    'output_name': 'dass-377-restored.mkv',
    'codec': 'hevc',               # smart render 强制匹配源（h264），此值会被探测覆盖
    'segments': segments,
    'detection_model': 'rfdetr-v6-large',
}
req = urllib.request.Request(
    BASE + '/jobs', data=json.dumps(payload).encode(),
    headers={'Content-Type': 'application/json'}, method='POST')
resp = json.load(urllib.request.urlopen(req))
print('job:', json.dumps(resp)[:300])
