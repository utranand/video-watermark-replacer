"""Replace a detected watermark region with a supplied image, frame-by-frame.

Uses PIL for the overlay image and imageio's ffmpeg plugin for video I/O.
Audio is preserved by muxing the original audio stream back in via a direct
ffmpeg subprocess call (the imageio-ffmpeg-bundled binary), since the
frame-by-frame rewrite only carries video.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict

import imageio.v2 as imageio
import imageio_ffmpeg
import numpy as np
from PIL import Image

from .video import probe

logger = logging.getLogger(__name__)

_PLUGIN = "FFMPEG"


def _load_overlay(image_path: str, target_long_side: float) -> tuple[np.ndarray, np.ndarray]:
    """Load ``image_path`` as RGBA, resized so its longest side == target.

    Returns (rgb float32 array, alpha float32 array in [0, 1]), both (H, W, ...).
    """
    img = Image.open(image_path).convert("RGBA")
    orig_w, orig_h = img.size
    long_side = max(orig_w, orig_h)
    ratio = target_long_side / float(long_side) if long_side > 0 else 1.0
    new_w = max(1, int(round(orig_w * ratio)))
    new_h = max(1, int(round(orig_h * ratio)))
    img = img.resize((new_w, new_h), Image.LANCZOS)

    arr = np.asarray(img).astype(np.float32)
    rgb = arr[..., :3]
    alpha = arr[..., 3] / 255.0
    return rgb, alpha


def _load_overlay_exact(image_path: str, target_w: int, target_h: int) -> tuple[np.ndarray, np.ndarray]:
    """Load ``image_path`` as RGBA, stretched to EXACTLY (target_w, target_h).

    Aspect ratio is NOT preserved. Returns (rgb float32 array, alpha float32
    array in [0, 1]), both (H, W, ...).
    """
    img = Image.open(image_path).convert("RGBA")
    new_w = max(1, int(round(target_w)))
    new_h = max(1, int(round(target_h)))
    img = img.resize((new_w, new_h), Image.LANCZOS)

    arr = np.asarray(img).astype(np.float32)
    rgb = arr[..., :3]
    alpha = arr[..., 3] / 255.0
    return rgb, alpha


def _blend_region(frame: np.ndarray, overlay_rgb: np.ndarray, overlay_alpha: np.ndarray, ox: int, oy: int) -> None:
    """Alpha-composite the overlay onto ``frame`` in place, centered at (ox, oy)
    top-left, clipped to the frame's bounds."""
    frame_h, frame_w = frame.shape[:2]
    ov_h, ov_w = overlay_rgb.shape[:2]

    # Destination region on the frame, clipped to frame bounds.
    dst_x0, dst_y0 = max(0, ox), max(0, oy)
    dst_x1, dst_y1 = min(frame_w, ox + ov_w), min(frame_h, oy + ov_h)
    if dst_x1 <= dst_x0 or dst_y1 <= dst_y0:
        return  # overlay entirely off-frame

    # Corresponding region on the overlay itself.
    src_x0, src_y0 = dst_x0 - ox, dst_y0 - oy
    src_x1, src_y1 = dst_x1 - ox, dst_y1 - oy

    dst = frame[dst_y0:dst_y1, dst_x0:dst_x1].astype(np.float32)
    src_rgb = overlay_rgb[src_y0:src_y1, src_x0:src_x1]
    src_alpha = overlay_alpha[src_y0:src_y1, src_x0:src_x1][..., np.newaxis]

    blended = dst * (1.0 - src_alpha) + src_rgb * src_alpha
    frame[dst_y0:dst_y1, dst_x0:dst_x1] = np.clip(blended, 0, 255).astype(np.uint8)


def replace_watermark(
    video_path: str,
    image_path: str,
    box: Dict[str, Any],
    out_path: str,
    scale: float = 1.5,
    fit: bool = False,
) -> Dict[str, Any]:
    """Replace the watermark region ``box`` in ``video_path`` with ``image_path``.

    ``box`` is {"x", "y", "w", "h"} in source-frame pixels.

    When ``fit`` is False (default), ``scale`` is used: the overlay is
    resized (preserving its own aspect ratio) so its longest side equals
    ``scale * max(box.w, box.h)``, then alpha-composited centered on the
    box's center onto every frame.

    When ``fit`` is True, ``scale`` is ignored: the overlay is resized to
    EXACTLY (box.w, box.h) — stretched, aspect ratio NOT preserved — and
    composited so it covers exactly the box rect (box.x..box.x+box.w,
    box.y..box.y+box.h), clipped at the frame edges.

    Audio is muxed back in from the source when present.
    """
    info = probe(video_path)
    fps = info["fps"] or 24.0

    if fit:
        overlay_rgb, overlay_alpha = _load_overlay_exact(image_path, box["w"], box["h"])
        ov_h, ov_w = overlay_rgb.shape[:2]
        ox = box["x"]
        oy = box["y"]
    else:
        target_long_side = scale * max(box["w"], box["h"])
        overlay_rgb, overlay_alpha = _load_overlay(image_path, target_long_side)
        ov_h, ov_w = overlay_rgb.shape[:2]

        cx = box["x"] + box["w"] / 2.0
        cy = box["y"] + box["h"] / 2.0
        ox = int(round(cx - ov_w / 2.0))
        oy = int(round(cy - ov_h / 2.0))

    out_dir = os.path.dirname(os.path.abspath(out_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    fd, silent_path = tempfile.mkstemp(suffix=".mp4", prefix="wmr_silent_", dir=out_dir)
    os.close(fd)

    n_frames = 0
    try:
        reader = imageio.get_reader(video_path, _PLUGIN)
        writer = imageio.get_writer(
            silent_path,
            _PLUGIN,
            fps=fps,
            codec="libx264",
            pixelformat="yuv420p",
            quality=8,
        )
        try:
            for frame in reader:
                frame = np.array(frame, copy=True)
                _blend_region(frame, overlay_rgb, overlay_alpha, ox, oy)
                writer.append_data(frame)
                n_frames += 1
        finally:
            writer.close()
            reader.close()

        if info["has_audio"]:
            ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            cmd = [
                ffmpeg_exe,
                "-y",
                "-i",
                silent_path,
                "-i",
                video_path,
                "-map",
                "0:v",
                "-map",
                "1:a",
                "-c",
                "copy",
                "-shortest",
                out_path,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                logger.error("wmr.composite: audio mux failed (%s); falling back to silent output. stderr=%s", out_path, result.stderr)
                shutil.move(silent_path, out_path)
            else:
                os.remove(silent_path)
        else:
            shutil.move(silent_path, out_path)
    finally:
        if os.path.exists(silent_path):
            try:
                os.remove(silent_path)
            except OSError:
                pass

    return {
        "output": out_path,
        "frames": n_frames,
        "fps": fps,
        "box_used": box,
        "fit": fit,
        "overlay_size": [ov_w, ov_h],
    }
