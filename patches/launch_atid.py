import json
import urllib.request

BASE = 'http://localhost:18766'
SCAN_ID = '7f15f37c17bd'

scans = json.load(urllib.request.urlopen(BASE + '/scans/' + SCAN_ID, timeout=90))
spans = scans['spans']
assert scans['input'] == '/videos/ATID-571-C.mp4'


def jr(x):
    return int(x + 0.5)


segments = ','.join(f'{jr(s["start"])}-{jr(s["end"])}' for s in spans)
total = sum(s['end'] - s['start'] for s in spans)
print(f'spans: {len(spans)}, restore {total:.0f}s / 6226s = {total/6226*100:.1f}%')

payload = {
    'input': '/videos/ATID-571-C.mp4',
    'output_name': 'ATID-571-C-restored.mp4',
    'codec': 'h264',
    'segments': segments,
    'detection_model': 'rfdetr-v6-large',
    'detection_score_threshold': 0.30,
    'encoder': 'nvenc',
}
req = urllib.request.Request(
    BASE + '/jobs', data=json.dumps(payload).encode(),
    headers={'Content-Type': 'application/json'}, method='POST')
resp = json.load(urllib.request.urlopen(req))
print('job:', resp['id'], resp['state'])
print('cmd:', ' '.join(resp['command'][4:14]))
print('env:', resp.get('env'))
