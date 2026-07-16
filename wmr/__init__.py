"""wmr: core engine for the Google Flow (Veo) video-watermark replacer."""

from .composite import replace_watermark
from .detect import detect_watermark
from .video import probe, read_frame

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "probe",
    "read_frame",
    "detect_watermark",
    "replace_watermark",
]
