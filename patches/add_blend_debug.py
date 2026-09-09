from pathlib import Path

p = Path('/jasna/jasna/pipeline_threads.py')
src = p.read_text()
if 'JASNA_BLEND_DEBUG' in src:
    print('already patched')
    raise SystemExit(0)

anchor = """                with timer.measure("blend"):
                    if not meta.apply_effect:
                        blended = original_frame
                    else:
                        blended = blend_buffer.blend_frame(
                            meta.frame_idx,
                            original_frame,
                        )"""
dbg = """                import os as _os
                if _os.environ.get("JASNA_BLEND_DEBUG"):
                    try:
                        _of = original_frame.detach()
                        _ostd = float(_of.float().std().cpu())
                        _omean = float(_of.float().mean().cpu())
                        with open("/tmp/encdbg/blend.tsv", "a") as _f:
                            _f.write(f"{meta.frame_idx}\\t{meta.pts}\\t{int(bool(meta.apply_effect))}\\t{_omean:.2f}\\t{_ostd:.2f}\\n")
                    except Exception as _e:
                        print("[blenddbg] err", _e)
"""
assert anchor in src, 'anchor not found'
p.write_text(src.replace(anchor, dbg + anchor))
print('patched ok')
