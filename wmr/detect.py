"""Static-watermark detection for Google Flow (Veo) videos.

The watermark is a single small, semi-transparent-white, 4-pointed sparkle
that never moves across the whole clip. Sampling frames at a low fps and
looking at each pixel's temporal minimum brightness (elevated under the
watermark, because white-over-anything never gets darker than the alpha
blend floor) and temporal standard deviation (damped under the watermark,
because the overlay is constant while the scene underneath keeps changing)
gives a simple, dependency-free way to find it — no OpenCV/scipy required,
just numpy + a small BFS-based connected-components pass.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import imageio.v2 as imageio
import numpy as np

logger = logging.getLogger(__name__)

_PLUGIN = "FFMPEG"

# Search the bottom-right corner of the frame first (this is where the
# watermark lives on every observed Flow/Veo export). Expressed as the
# fraction of width/height where the search box begins.
_QUADRANT_X_FRAC = 0.65
_QUADRANT_Y_FRAC = 0.75

# Sanity bounds for an accepted blob, relative to frame width / own shape.
_MIN_WIDTH_FRAC = 0.02
_MAX_WIDTH_FRAC = 0.20
_MIN_ASPECT = 0.5
_MAX_ASPECT = 2.0

# Percentile threshold used to binarize the temporal score map.
_SCORE_PERCENTILE = 99.0

# Padding applied to an accepted tight bbox before it is returned.
_PAD_FRAC = 0.15

# Known fallback geometry (measured across many Flow/Veo exports), used only
# when temporal detection fails to find a plausible blob anywhere.
_FALLBACK_CX_FRAC = 0.8375
_FALLBACK_CY_FRAC = 0.90625
_FALLBACK_W_FRAC = 0.076
_FALLBACK_H_FRAC = 0.047


def _sample_frames(video_path: str, sample_fps: float, max_seconds: float) -> Tuple[List[np.ndarray], Dict[str, Any]]:
    """Grab a handful of frames at a low, decoder-resampled fps.

    Uses imageio's ffmpeg reader with an output ``fps`` kwarg so ffmpeg does
    the resampling itself — far cheaper than decoding every frame and
    discarding most of them.
    """
    reader = imageio.get_reader(video_path, _PLUGIN, fps=sample_fps)
    frames: List[np.ndarray] = []
    try:
        meta = reader.get_meta_data()
        max_frames = max(2, int(round(sample_fps * max_seconds)))
        for i in range(max_frames):
            try:
                frames.append(reader.get_data(i))
            except (IndexError, StopIteration):
                break
    finally:
        reader.close()
    return frames, meta


def _to_gray(frames: List[np.ndarray]) -> np.ndarray:
    """Stack frames into an (N, H, W) float32 grayscale array."""
    arr = np.stack(frames).astype(np.float32)
    return arr[..., 0] * 0.2989 + arr[..., 1] * 0.5870 + arr[..., 2] * 0.1140


def _connected_components(mask: np.ndarray) -> List[List[Tuple[int, int]]]:
    """Label 4-connected components of a boolean 2D array via BFS.

    Pure numpy/stdlib — scipy's ``label`` is unavailable in this environment.
    Only the (typically few hundred to a few thousand) True pixels are
    visited, so this stays cheap even though it is plain Python.
    """
    h, w = mask.shape
    visited = np.zeros_like(mask, dtype=bool)
    ys, xs = np.nonzero(mask)
    components: List[List[Tuple[int, int]]] = []
    for y0, x0 in zip(ys.tolist(), xs.tolist()):
        if visited[y0, x0]:
            continue
        stack = [(y0, x0)]
        visited[y0, x0] = True
        comp: List[Tuple[int, int]] = []
        while stack:
            y, x = stack.pop()
            comp.append((y, x))
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                ny, nx = y + dy, x + dx
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not visited[ny, nx]:
                    visited[ny, nx] = True
                    stack.append((ny, nx))
        components.append(comp)
    return components


def _score_window(tmin: np.ndarray, tstd: np.ndarray) -> np.ndarray:
    """Temporal score: high stable-brightness minus damped variance."""
    return (tmin - np.median(tmin)) - (tstd - np.median(tstd))


def _best_blob(
    tmin: np.ndarray, tstd: np.ndarray, x0: int, y0: int
) -> Optional[Dict[str, Any]]:
    """Find the largest connected high-score blob within a (sub)window.

    ``x0``/``y0`` are the offsets of the window within the full frame, so the
    returned bbox is already in full-frame coordinates.
    """
    score = _score_window(tmin, tstd)
    threshold = np.percentile(score, _SCORE_PERCENTILE)
    mask = score >= threshold
    components = _connected_components(mask)
    if not components:
        return None
    components.sort(key=len, reverse=True)
    best = components[0]
    ys = [p[0] for p in best]
    xs = [p[1] for p in best]
    bx, by = min(xs) + x0, min(ys) + y0
    bw = max(xs) - min(xs) + 1
    bh = max(ys) - min(ys) + 1
    vals = np.array([score[p] for p in best])
    med = float(np.median(score))
    sd = float(np.std(score)) + 1e-6
    z = float((vals.mean() - med) / sd)
    confidence = float(min(0.99, max(0.05, z / 6.0)))
    return {"x": bx, "y": by, "w": bw, "h": bh, "confidence": confidence}


def _is_plausible(blob: Dict[str, Any], width: int) -> bool:
    w, h = blob["w"], blob["h"]
    if h <= 0:
        return False
    width_frac = w / float(width)
    if not (_MIN_WIDTH_FRAC <= width_frac <= _MAX_WIDTH_FRAC):
        return False
    aspect = w / float(h)
    return _MIN_ASPECT <= aspect <= _MAX_ASPECT


def _pad_box(blob: Dict[str, Any], width: int, height: int) -> Dict[str, Any]:
    x, y, w, h = blob["x"], blob["y"], blob["w"], blob["h"]
    pad_w, pad_h = w * _PAD_FRAC, h * _PAD_FRAC
    new_x = max(0, int(round(x - pad_w)))
    new_y = max(0, int(round(y - pad_h)))
    new_w = int(round(w + 2 * pad_w))
    new_h = int(round(h + 2 * pad_h))
    new_w = min(new_w, width - new_x)
    new_h = min(new_h, height - new_y)
    return {
        "x": new_x,
        "y": new_y,
        "w": new_w,
        "h": new_h,
        "confidence": blob["confidence"],
    }


def _fallback_box(width: int, height: int) -> Dict[str, Any]:
    w = int(round(_FALLBACK_W_FRAC * width))
    h = int(round(_FALLBACK_H_FRAC * height))
    cx = int(round(_FALLBACK_CX_FRAC * width))
    cy = int(round(_FALLBACK_CY_FRAC * height))
    x = max(0, cx - w // 2)
    y = max(0, cy - h // 2)
    return {
        "x": x,
        "y": y,
        "w": w,
        "h": h,
        "cx": cx,
        "cy": cy,
        "confidence": 0.0,
        "method": "fallback",
    }


def detect_watermark(video_path: str, sample_fps: float = 2.0, max_seconds: float = 10.0) -> Dict[str, Any]:
    """Locate the static Flow/Veo watermark in ``video_path``.

    Strategy: sample a handful of low-fps frames, compute per-pixel temporal
    min and std, and look for a compact blob of elevated-min/damped-std
    pixels — first in the bottom-right corner (where the watermark lives on
    every observed export), then across the whole frame if that comes up
    empty or implausible. Always returns a box; never raises on a readable
    video.
    """
    try:
        frames, meta = _sample_frames(video_path, sample_fps, max_seconds)
        width, height = meta.get("size", (0, 0))
        width, height = int(width), int(height)
        if width <= 0 or height <= 0 or len(frames) < 2:
            logger.warning("wmr.detect: insufficient frames/metadata for %s; using fallback", video_path)
            return _fallback_box(width or 1, height or 1)

        gray = _to_gray(frames)
        tmin_full = gray.min(axis=0)
        tstd_full = gray.std(axis=0)

        # 1) Bottom-right corner search.
        x0 = int(_QUADRANT_X_FRAC * width)
        y0 = int(_QUADRANT_Y_FRAC * height)
        blob = _best_blob(tmin_full[y0:height, x0:width], tstd_full[y0:height, x0:width], x0, y0)
        if blob is not None and _is_plausible(blob, width):
            box = _pad_box(blob, width, height)
            box["cx"] = box["x"] + box["w"] // 2
            box["cy"] = box["y"] + box["h"] // 2
            box["method"] = "temporal"
            return box

        # 2) Whole-frame search.
        blob = _best_blob(tmin_full, tstd_full, 0, 0)
        if blob is not None and _is_plausible(blob, width):
            box = _pad_box(blob, width, height)
            box["cx"] = box["x"] + box["w"] // 2
            box["cy"] = box["y"] + box["h"] // 2
            box["method"] = "temporal"
            return box

        # 3) Known fallback geometry.
        logger.info("wmr.detect: no plausible blob found for %s; using fallback box", video_path)
        return _fallback_box(width, height)
    except Exception:  # noqa: BLE001 - detection must never raise
        logger.exception("wmr.detect: detection failed for %s; using fallback", video_path)
        # Best-effort width/height even on failure, so the fallback box is
        # still meaningful (720x1280 portrait is the observed default).
        try:
            width, height = int(meta.get("size", (720, 1280))[0]), int(meta.get("size", (720, 1280))[1])  # type: ignore[possibly-undefined]
        except Exception:  # noqa: BLE001
            width, height = 720, 1280
        return _fallback_box(width, height)
