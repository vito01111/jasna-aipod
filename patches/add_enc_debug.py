from pathlib import Path

p = Path('/jasna/jasna/cpu_encoder_fallback.py')
src = p.read_text()
if 'JASNA_ENC_DEBUG' in src:
    print('already patched')
    raise SystemExit(0)
anchor = '        vf = av.VideoFrame.from_ndarray(array, format="rgb24")'
dbg = (
    '        import os as _os\n'
    '        if _os.environ.get("JASNA_ENC_DEBUG"):\n'
    '            _idx = self._frame_index\n'
    '            try:\n'
    '                with open("/tmp/encdbg/stats.tsv", "a") as _f:\n'
    '                    _f.write(f"{_idx}\\t{pts}\\t{array.mean():.2f}\\t{array.std():.2f}\\t{int(array.max())}\\n")\n'
    '                if 0 <= _idx <= 140:\n'
    '                    _sm = array[::8, ::8]\n'
    '                    av.VideoFrame.from_ndarray(_sm, format="rgb24").to_image().save(f"/tmp/encdbg/f{_idx:04d}.jpg")\n'
    '            except Exception as _e:\n'
    '                print("[encdbg] err", _e)\n'
)
assert anchor in src, 'anchor not found'
p.write_text(src.replace(anchor, dbg + anchor))
print('patched ok')
