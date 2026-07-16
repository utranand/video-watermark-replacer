"""Video I/O helpers built on imageio's ffmpeg plugin (imageio-ffmpeg backend).

Only stdlib + numpy + imageio(-ffmpeg) are used here — no OpenCV, no moviepy.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

import imageio.v2 as imageio
import imageio.v3 as iio
import numpy as np

logger = logging.getLogger(__name__)

# The imageio plugin name for the ffmpeg-backed reader/writer (provided by
# the imageio-ffmpeg package). Pinning it explicitly avoids ambiguity should
# a different video plugin (e.g. pyav) ever become available/default.
_PLUGIN = "FFMPEG"


def probe(video_path: str) -> Dict[str, Any]:
    """Return basic metadata for a video file.

    Returns a dict with keys: width, height, fps, duration, has_audio, codec.
    """
    meta = iio.immeta(video_path, plugin=_PLUGIN)
    width, height = meta.get("size", (0, 0))
    return {
        "width": int(width),
        "height": int(height),
        "fps": float(meta.get("fps") or 0.0),
        "duration": float(meta.get("duration") or 0.0),
        "has_audio": bool(meta.get("audio_codec")),
        "codec": meta.get("codec"),
    }


def read_frame(video_path: str, t: float = 0.0) -> np.ndarray:
    """Read a single RGB frame from ``video_path`` at time ``t`` seconds.

    ``t`` is clamped to the video's playable duration. Returns an (H, W, 3)
    uint8 numpy array in RGB order.
    """
    info = probe(video_path)
    fps = info["fps"] or 1.0
    duration = info["duration"]

    t = max(0.0, float(t))
    if duration > 0:
        t = min(t, max(0.0, duration - (1.0 / fps)))

    target_index = max(0, int(round(t * fps)))

    reader = imageio.get_reader(video_path, _PLUGIN)
    try:
        index = target_index
        while True:
            try:
                return reader.get_data(index)
            except IndexError:
                if index <= 0:
                    raise
                index -= 1
    finally:
        reader.close()
