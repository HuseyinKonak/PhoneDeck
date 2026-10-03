"""Writes the phone's H.264 stream into an MP4 file without re-encoding."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import av

MICROSECONDS = Fraction(1, 1_000_000)
NOMINAL_FRAME_RATE = 60


class Mp4Recorder:
    """Muxes already-encoded packets, so recording costs almost no CPU.

    Not thread-safe: the owner serializes `write` and `close`.
    """

    def __init__(self, path: Path, width: int, height: int, config: bytes) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._container = av.open(str(path), mode="w", format="mp4")
        self._stream = self._container.add_stream("h264", rate=NOMINAL_FRAME_RATE)
        self._stream.width = width
        self._stream.height = height
        self._stream.time_base = MICROSECONDS
        # SPS/PPS from the stream's config packet. The MP4 muxer builds its header from
        # them, so they are also prepended to the first packet (see `write`).
        self._stream.codec_context.extradata = config
        self._config = config
        self._first_pts: int | None = None
        self._last_pts = -1
        self.packet_count = 0

    def write(self, data: bytes, pts_us: int, is_key_frame: bool) -> None:
        if self._first_pts is None:
            self._first_pts = pts_us
            data = self._config + data
        # Timestamps must strictly increase or the muxer rejects the packet.
        pts = max(pts_us - self._first_pts, self._last_pts + 1)
        self._last_pts = pts
        packet = av.Packet(data)
        packet.stream = self._stream
        packet.time_base = MICROSECONDS
        packet.pts = pts
        packet.dts = pts
        packet.is_keyframe = is_key_frame
        self._container.mux(packet)
        self.packet_count += 1

    def close(self) -> None:
        self._container.close()
