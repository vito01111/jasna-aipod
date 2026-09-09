from pathlib import Path

p = Path('/jasna/jasna/media/video_decoder.py')
src = p.read_text()
if 'JASNA_READER_DEBUG' in src:
    print('already patched')
    raise SystemExit(0)

anchor = """            next_group = self._read_group(decoded)
            stream.synchronize()
            group = next_group
            yield batch, pts"""
dbg = """            next_group = self._read_group(decoded)
            stream.synchronize()
            import os as _os
            if _os.environ.get("JASNA_READER_DEBUG"):
                try:
                    _stds = batch.float().std(dim=(1, 2, 3)).cpu().tolist()
                    _means = batch.float().mean(dim=(1, 2, 3)).cpu().tolist()
                    with open("/tmp/encdbg/reader_sw.tsv", "a") as _f:
                        for _i, (_m, _s) in enumerate(zip(_means, _stds)):
                            _f.write(f"{pts[_i]}\\t{_m:.2f}\\t{_s:.2f}\\n")
                except Exception as _e:
                    print("[readerdbg] err", _e)
            group = next_group
            yield batch, pts"""
assert anchor in src, 'anchor sw not found'
src = src.replace(anchor, dbg)

# hardware path too
anchor2 = """            next_group = self._read_group(decoded)
            self.stream.synchronize()
            group = next_group
            yield batch, pts"""
dbg2 = """            next_group = self._read_group(decoded)
            self.stream.synchronize()
            import os as _os
            if _os.environ.get("JASNA_READER_DEBUG"):
                try:
                    _stds = batch.float().std(dim=(1, 2, 3)).cpu().tolist()
                    _means = batch.float().mean(dim=(1, 2, 3)).cpu().tolist()
                    with open("/tmp/encdbg/reader_hw.tsv", "a") as _f:
                        for _i, (_m, _s) in enumerate(zip(_means, _stds)):
                            _f.write(f"{pts[_i]}\\t{_m:.2f}\\t{_s:.2f}\\n")
                except Exception as _e:
                    print("[readerdbg] err", _e)
            group = next_group
            yield batch, pts"""
if anchor2 in src:
    src = src.replace(anchor2, dbg2)
p.write_text(src)
print('patched ok')
