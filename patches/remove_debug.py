from pathlib import Path


def strip_block(path, start_marker, end_marker):
    p = Path(path)
    src = p.read_text()
    if start_marker not in src:
        print(f'{path}: marker absent (already clean)')
        return
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    src = src[:i] + src[j:]
    p.write_text(src)
    print(f'{path}: removed debug block')


strip_block(
    '/jasna/jasna/cpu_encoder_fallback.py',
    '        import os as _os\n        if _os.environ.get("JASNA_ENC_DEBUG"):',
    '        vf = av.VideoFrame.from_ndarray(array, format="rgb24")')

strip_block(
    '/jasna/jasna/pipeline_threads.py',
    '                import os as _os\n                if _os.environ.get("JASNA_BLEND_DEBUG"):',
    '                with timer.measure("blend"):')

strip_block(
    '/jasna/jasna/media/video_decoder.py',
    '            import os as _os\n            if _os.environ.get("JASNA_READER_DEBUG"):',
    '            group = next_group\n            yield batch, pts')

for f in ('/jasna/jasna/media/video_decoder.py',):
    src = Path(f).read_text()
    # remove the second (hardware) reader block if present
    m = '            import os as _os\n            if _os.environ.get("JASNA_READER_DEBUG"):'
    if m in src:
        i = src.index(m)
        j = src.index('            group = next_group', i)
        src = src[:i] + src[j:]
        Path(f).write_text(src)
        print(f'{f}: removed second debug block')

# verify
import subprocess
for f in ('/jasna/jasna/cpu_encoder_fallback.py', '/jasna/jasna/pipeline_threads.py',
          '/jasna/jasna/media/video_decoder.py'):
    r = subprocess.run(['grep', '-c', 'JASNA_.*_DEBUG', f], capture_output=True, text=True)
    print(f, 'remaining debug markers:', r.stdout.strip())
r = subprocess.run(['grep', '-c', 'JASNA_RING_PATCH', '/jasna/jasna/media/video_decoder.py'],
                   capture_output=True, text=True)
print('ring patch present:', r.stdout.strip())
