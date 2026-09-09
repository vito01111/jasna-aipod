import sys

sys.path.insert(0, "/jasna")

import av
from pathlib import Path

src_path = "/videos/VDD-207-20min-bench.mp4"
dst_nut = "/tmp/copy-frag.nut"
dst_ts = "/tmp/copy-frag.ts"

# create_copy_fragment 核心：seek 起点，remux [start, end) 的包到 NUT
START_PTS = 0
END_PTS = 160000  # ~5.3s @30k timescale 附近，宽松即可

with av.open(src_path) as src, av.open(dst_nut, "w", format="nut") as dst:
    vs = src.streams.video[0]
    out = dst.add_stream_from_template(vs)
    print("in time_base:", vs.time_base, "start_pts:", vs.start_time)
    src.seek(START_PTS, stream=vs, backward=True)
    n = 0
    for packet in src.demux(vs):
        if packet.pts is None or not (START_PTS <= packet.pts < END_PTS):
            continue
        packet.pts -= START_PTS
        if packet.dts is not None:
            packet.dts -= START_PTS
        packet.stream = out
        dst.mux(packet)
        n += 1
    print("packets muxed:", n)

from jasna.media.splice import normalize_fragment

normalize_fragment(Path(dst_nut), Path(dst_ts), codec="h264")
print("normalized")
