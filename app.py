#!/usr/bin/env python3
"""Local web server for the Video Watermark Replacer.

Wires the existing ``wmr`` engine package to the existing static frontend
(``src/static/``). Stdlib-only (plus ``wmr`` itself) — no web framework is
available in this environment, so this uses ``http.server`` /
``socketserver`` directly. ``cgi`` is NOT used (removed in Python 3.13) —
multipart uploads are parsed by hand.

Run with::

    .venv/bin/python src/app.py [--port 8765] [--host 127.0.0.1] [--no-browser]

Paths in the API may be REPO-RELATIVE (resolved against the repository
root — the directory containing ``.sidekicks``, found by walking up from
this file) OR ABSOLUTE (honored as-is, deliberately — the operator can
point at any file/directory their own OS account can read). A resolved
read path must exist and be a regular file; the OS's own permission model
is the only remaining gate (a ``PermissionError`` is translated to a clean
JSON 403). The server itself still only binds ``127.0.0.1`` by default —
this is a local-machine tool, not a network-exposed one.
"""

from __future__ import annotations

import argparse
import ctypes
import io
import json
import logging
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

logger = logging.getLogger("wmr.app")


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _find_repo_root(start: Path) -> Path:
    """Walk up from ``start`` until a directory containing ``.sidekicks`` is found."""
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".sidekicks").is_dir():
            return candidate
    # Fall back to the filesystem root's nearest ancestor if never found —
    # should not happen inside this repo, but never raise here.
    return current.parents[-1] if current.parents else current


REPO_ROOT = _find_repo_root(Path(__file__).parent)
SERVICE_DIR = Path(__file__).resolve().parent.parent  # parent of src/
SRC_DIR = Path(__file__).resolve().parent
RESOURCES_DIR = SERVICE_DIR / "resources"  # repo-mode bundled sample resources

# Make the engine package importable (src/ on sys.path).
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import wmr  # noqa: E402  (import after sys.path setup)

VIDEO_EXTS = {".mp4", ".mov", ".m4v"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

# Broader extension sets accepted by /api/browse (a general filesystem picker,
# not limited to what this engine can itself ingest/scan).
BROWSE_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
BROWSE_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}

_SAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")


# ---------------------------------------------------------------------------
# Frozen-mode (PyInstaller-bundled) support
# ---------------------------------------------------------------------------

def is_frozen() -> bool:
    """True when running from a PyInstaller-frozen bundle."""
    return bool(getattr(sys, "frozen", False))


def static_dir() -> Path:
    """Directory the frontend's static assets are served from."""
    if is_frozen():
        return Path(sys._MEIPASS) / "static"  # type: ignore[attr-defined]
    return SRC_DIR / "static"


def data_dir() -> Path:
    """Per-install directory for uploads/outputs.

    In repo mode this is the service's own ``artifacts/app-data`` (as
    before). In frozen mode there is no repo to anchor to, so this resolves
    to a per-user application-data directory instead:

    - macOS:   ``~/Library/Application Support/VideoWatermarkReplacer``
    - Windows: ``%LOCALAPPDATA%\\VideoWatermarkReplacer``
    - other:   ``~/.local/share/VideoWatermarkReplacer``
    """
    if not is_frozen():
        return SERVICE_DIR / "artifacts" / "app-data"
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / ".local" / "share"
    return base / "VideoWatermarkReplacer"


def default_resources() -> dict:
    """Bundled fallback resources available only in frozen mode.

    Returns ``{"images": [Path, ...], "videos": [Path, ...]}`` — empty lists
    when not frozen, or when the bundled asset is absent (referenced
    defensively since the packager places it, not this app).
    """
    videos: list[Path] = []
    images: list[Path] = []
    if is_frozen():
        sticker = Path(sys._MEIPASS) / "resources" / "paper-sticker.png"  # type: ignore[attr-defined]
        if sticker.is_file():
            images.append(sticker)
    return {"videos": videos, "images": images}


STATIC_DIR = static_dir()
APP_DATA_DIR = data_dir()
UPLOADS_DIR = APP_DATA_DIR / "uploads"
OUTPUTS_DIR = APP_DATA_DIR / "outputs"


class ApiError(Exception):
    """An error that should be reported to the client as JSON with a status code."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def ensure_data_dirs() -> None:
    UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)


def resolve_input_path(raw_path: str) -> Path:
    """Resolve a request-supplied path to an existing, readable file.

    ``raw_path`` may be repo-relative (resolved against ``REPO_ROOT``, as
    before) or absolute (honored as-is — the deliberate policy: any path
    the running OS account can read is fair game). Raises ApiError(400) if
    missing/empty/not-found/not-a-file. A ``PermissionError`` the OS itself
    raises while resolving/stat-ing is deliberately NOT caught here — it
    propagates to the request handler, which reports it as a clean 403.
    """
    if raw_path is None:
        raise ApiError(400, "missing path")
    raw_path = raw_path.strip()
    if not raw_path:
        raise ApiError(400, "empty path")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    candidate = candidate.resolve()
    if not candidate.exists():
        raise ApiError(400, f"path not found: {raw_path}")
    if not candidate.is_file():
        raise ApiError(400, f"not a file: {raw_path}")
    return candidate


def resolve_output_dir(raw_dir: str) -> Path:
    """Resolve a request-supplied output directory, creating it if needed.

    Empty/missing -> the default ``OUTPUTS_DIR``. Otherwise must be an
    absolute path; it is created (parents included) if it does not exist.
    """
    raw_dir = (raw_dir or "").strip()
    if not raw_dir:
        ensure_data_dirs()
        return OUTPUTS_DIR
    out_dir = Path(raw_dir)
    if not out_dir.is_absolute():
        raise ApiError(400, "output_dir must be an absolute path")
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def display_path(path: Path) -> str:
    """Render a path for API responses: repo-relative (posix) if inside
    ``REPO_ROOT``, else absolute with the OS's native separators."""
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def sanitize_filename(name: str) -> str:
    """Collapse a filename to safe characters, basename only."""
    base = Path(name).name  # strips any directory components
    base = base.replace("\\", "_").replace("/", "_")
    stem = Path(base).stem
    suffix = Path(base).suffix
    safe_stem = _SAFE_CHARS_RE.sub("_", stem).strip("_") or "file"
    safe_suffix = _SAFE_CHARS_RE.sub("", suffix.lower())
    return safe_stem + safe_suffix


def unique_path(directory: Path, filename: str) -> Path:
    """Return a non-colliding path in ``directory`` for ``filename``, adding a numeric suffix."""
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    n = 1
    while True:
        candidate = directory / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def kind_for_ext(ext: str) -> str | None:
    ext = ext.lower()
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    return None


# ---------------------------------------------------------------------------
# Multipart parsing (stdlib only — no cgi module, removed in Python 3.13)
# ---------------------------------------------------------------------------

def _parse_content_type(header_value: str) -> tuple[str, dict[str, str]]:
    parts = header_value.split(";")
    main = parts[0].strip()
    params: dict[str, str] = {}
    for part in parts[1:]:
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        value = value.strip().strip('"')
        params[key.strip().lower()] = value
    return main, params


def parse_multipart_form_file(body: bytes, content_type_header: str, field_name: str) -> tuple[str, bytes]:
    """Manually parse a multipart/form-data body for one file field.

    Returns (original_filename, file_bytes). Raises ApiError on failure.
    """
    main, params = _parse_content_type(content_type_header or "")
    if main.lower() != "multipart/form-data" or "boundary" not in params:
        raise ApiError(400, "expected multipart/form-data with a boundary")
    boundary = params["boundary"].encode("utf-8")
    delimiter = b"--" + boundary

    # Split the body on the boundary delimiter; the last part is the epilogue.
    segments = body.split(delimiter)
    for segment in segments:
        segment = segment.strip(b"\r\n")
        if not segment or segment in (b"--", b""):
            continue
        if b"\r\n\r\n" not in segment:
            continue
        header_blob, _, content = segment.partition(b"\r\n\r\n")
        # Trailing CRLF before the next boundary belongs to the delimiter, not content.
        if content.endswith(b"\r\n"):
            content = content[:-2]
        headers_text = header_blob.decode("utf-8", errors="replace")
        disposition_line = None
        for line in headers_text.split("\r\n"):
            if line.lower().startswith("content-disposition:"):
                disposition_line = line
                break
        if disposition_line is None:
            continue
        _, disp_params = _parse_content_type(disposition_line.split(":", 1)[1])
        if disp_params.get("name") != field_name:
            continue
        filename = disp_params.get("filename", "")
        if not filename:
            continue
        return filename, content

    raise ApiError(400, f"multipart field '{field_name}' not found")


# ---------------------------------------------------------------------------
# Resource scanning
# ---------------------------------------------------------------------------

def scan_resources() -> dict:
    videos: list[dict] = []
    images: list[dict] = []

    def add_from(directory: Path) -> None:
        if not directory.is_dir():
            return
        for entry in sorted(directory.iterdir()):
            if not entry.is_file():
                continue
            kind = kind_for_ext(entry.suffix)
            if kind == "video":
                videos.append({"name": entry.name, "path": display_path(entry)})
            elif kind == "image":
                images.append({"name": entry.name, "path": display_path(entry)})

    if is_frozen():
        # Frozen mode: no bundled sample resources dir — videos come only
        # from uploads; images get the bundled default sticker (if present)
        # plus uploads.
        defaults = default_resources()
        for p in defaults.get("images", []):
            images.append({"name": p.name, "path": display_path(p)})
        for p in defaults.get("videos", []):
            videos.append({"name": p.name, "path": display_path(p)})
    else:
        add_from(RESOURCES_DIR)

    add_from(UPLOADS_DIR)
    return {"videos": videos, "images": images}


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def content_type_for(path: Path) -> str:
    ct = CONTENT_TYPES.get(path.suffix.lower())
    if ct:
        return ct
    guessed, _ = mimetypes.guess_type(str(path))
    return guessed or "application/octet-stream"


class Handler(BaseHTTPRequestHandler):
    server_version = "WMRApp/0.1"
    protocol_version = "HTTP/1.1"

    # -- logging ------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        logger.info("%s - %s", self.address_string(), fmt % args)

    # -- helpers --------------------------------------------------------
    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str) -> None:
        logger.error("request failed (%s): %s", status, message)
        self._send_json(status, {"ok": False, "error": message})

    def _send_bytes(self, status: int, content_type: str, data: bytes, extra_headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _read_json_body(self) -> dict:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, f"invalid JSON body: {exc}") from None

    def _serve_file(self, path: Path, download_name: str | None = None) -> None:
        if not path.is_file():
            raise ApiError(404, f"file not found: {path}")
        data = path.read_bytes()
        headers = {}
        if download_name:
            headers["Content-Disposition"] = f'attachment; filename="{download_name}"'
        self._send_bytes(200, content_type_for(path), data, headers)

    def _serve_file_with_range(self, path: Path) -> None:
        """Serve ``path`` honoring a single-range Range header (for <video>)."""
        if not path.is_file():
            raise ApiError(404, f"file not found: {path}")
        file_size = path.stat().st_size
        content_type = content_type_for(path)
        range_header = self.headers.get("Range")

        if not range_header:
            data = path.read_bytes()
            self._send_bytes(200, content_type, data, {"Accept-Ranges": "bytes"})
            return

        match = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
        if not match:
            raise ApiError(400, "invalid Range header")
        start_s, end_s = match.groups()
        if start_s == "" and end_s == "":
            raise ApiError(400, "invalid Range header")
        if start_s == "":
            # suffix range: last N bytes
            length = int(end_s)
            start = max(0, file_size - length)
            end = file_size - 1
        else:
            start = int(start_s)
            end = int(end_s) if end_s != "" else file_size - 1
        end = min(end, file_size - 1)
        if start > end or start >= file_size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{file_size}")
            self.end_headers()
            return

        chunk_len = end - start + 1
        with path.open("rb") as f:
            f.seek(start)
            data = f.read(chunk_len)
        self._send_bytes(
            206,
            content_type,
            data,
            {
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
            },
        )

    # -- routing --------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        try:
            self._route_get()
        except ApiError as exc:
            self._send_error_json(exc.status, exc.message)
        except PermissionError as exc:
            self._send_error_json(403, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("unhandled error handling GET %s", self.path)
            self._send_error_json(500, str(exc))

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._route_post()
        except ApiError as exc:
            self._send_error_json(exc.status, exc.message)
        except PermissionError as exc:
            self._send_error_json(403, str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.exception("unhandled error handling POST %s", self.path)
            self._send_error_json(500, str(exc))

    def _route_get(self) -> None:
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if path == "/":
            self._serve_file(STATIC_DIR / "index.html")
            return

        if path.startswith("/static/"):
            name = path[len("/static/"):]
            candidate = (STATIC_DIR / name).resolve()
            try:
                candidate.relative_to(STATIC_DIR.resolve())
            except ValueError:
                raise ApiError(400, "invalid static path") from None
            self._serve_file(candidate)
            return

        if path == "/api/resources":
            ensure_data_dirs()
            self._send_json(200, scan_resources())
            return

        if path == "/api/frame":
            video = query.get("video", [None])[0]
            t = query.get("t", ["0"])[0]
            video_path = resolve_input_path(video)
            try:
                t_val = float(t)
            except (TypeError, ValueError):
                raise ApiError(400, "invalid t") from None
            self._serve_frame(video_path, t_val)
            return

        if path == "/media":
            media = query.get("path", [None])[0]
            media_path = resolve_input_path(media)
            self._serve_file_with_range(media_path)
            return

        if path == "/api/browse":
            self._handle_browse(query)
            return

        raise ApiError(404, f"not found: {path}")

    def _route_post(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path

        if path == "/api/upload":
            self._handle_upload()
            return
        if path == "/api/detect":
            self._handle_detect()
            return
        if path == "/api/process":
            self._handle_process()
            return

        raise ApiError(404, f"not found: {path}")

    # -- endpoint implementations ----------------------------------------
    def _serve_frame(self, video_path: Path, t: float) -> None:
        try:
            from PIL import Image
        except ImportError as exc:  # pragma: no cover - PIL is a verified dependency
            raise ApiError(500, f"PIL unavailable: {exc}") from None

        frame = wmr.read_frame(str(video_path), t=t)
        img = Image.fromarray(frame)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        self._send_bytes(200, "image/jpeg", buf.getvalue())

    def _handle_upload(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        body = self._read_body()
        filename, file_bytes = parse_multipart_form_file(body, content_type, "file")

        ext = Path(filename).suffix.lower()
        kind = kind_for_ext(ext)
        if kind is None:
            raise ApiError(400, f"unsupported file extension: {ext}")

        ensure_data_dirs()
        safe_name = sanitize_filename(filename)
        dest = unique_path(UPLOADS_DIR, safe_name)
        dest.write_bytes(file_bytes)

        self._send_json(200, {"ok": True, "path": display_path(dest), "kind": kind})

    def _handle_detect(self) -> None:
        payload = self._read_json_body()
        video = payload.get("video")
        video_path = resolve_input_path(video)
        box = wmr.detect_watermark(str(video_path))
        self._send_json(200, {"ok": True, "box": box})

    def _handle_process(self) -> None:
        payload = self._read_json_body()
        video = payload.get("video")
        image = payload.get("image")
        box = payload.get("box")
        scale = payload.get("scale", 1.5)
        fit = bool(payload.get("fit", False))
        output_name = (payload.get("output_name") or "").strip()
        output_dir_raw = payload.get("output_dir") or ""

        video_path = resolve_input_path(video)
        image_path = resolve_input_path(image)

        try:
            scale = float(scale)
        except (TypeError, ValueError):
            raise ApiError(400, "invalid scale") from None

        if box is None:
            box = wmr.detect_watermark(str(video_path))
        else:
            required = {"x", "y", "w", "h"}
            if not isinstance(box, dict) or not required.issubset(box.keys()):
                raise ApiError(400, "box must contain x, y, w, h")

        out_dir = resolve_output_dir(output_dir_raw)
        if output_name:
            output_name = sanitize_filename(output_name)
            if not output_name.lower().endswith(".mp4"):
                output_name += ".mp4"
        else:
            output_name = f"{video_path.stem}__replaced.mp4"
        out_path = unique_path(out_dir, output_name)

        result = wmr.replace_watermark(
            str(video_path), str(image_path), box, str(out_path), scale=scale, fit=fit
        )

        self._send_json(
            200,
            {
                "ok": True,
                "output": display_path(Path(result["output"])),
                "frames": result["frames"],
                "fps": result["fps"],
                "box_used": result["box_used"],
                "fit": result.get("fit", fit),
            },
        )

    def _handle_browse(self, query: dict) -> None:
        dir_param = (query.get("dir", [""])[0] or "").strip()
        kind = (query.get("kind", ["dir"])[0] or "dir").strip()
        if kind not in ("video", "image", "dir"):
            kind = "dir"

        if not dir_param:
            target = Path.home()
        else:
            candidate = Path(dir_param)
            target = candidate if candidate.is_absolute() else (REPO_ROOT / candidate)

        # Let PermissionError bubble to the do_GET wrapper -> clean 403.
        target = target.resolve()
        if not target.exists() or not target.is_dir():
            self._send_json(400, {"ok": False, "error": f"not a directory: {target}"})
            return

        exts = {"video": BROWSE_VIDEO_EXTS, "image": BROWSE_IMAGE_EXTS}.get(kind)

        dirs: list[dict] = []
        files: list[dict] = []
        for entry in target.iterdir():
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir():
                    dirs.append({"name": entry.name, "path": str(entry)})
                elif exts is not None and entry.is_file() and entry.suffix.lower() in exts:
                    files.append({"name": entry.name, "path": str(entry), "size": entry.stat().st_size})
            except OSError:
                continue  # entries that raise on stat are skipped silently

        dirs.sort(key=lambda d: d["name"].lower())
        files.sort(key=lambda f: f["name"].lower())

        parent = target.parent
        parent_str = str(parent) if parent != target else None

        self._send_json(
            200,
            {
                "ok": True,
                "dir": str(target),
                "parent": parent_str,
                "dirs": dirs,
                "files": files,
            },
        )


# ---------------------------------------------------------------------------
# Startup robustness — port auto-pick + never-silent frozen failures
#
# DEFECT this section fixes: if the default port is already held by another
# process, the server used to die instantly on bind — and in the frozen
# windowed macOS build that death was SILENT (no console, no dialog), so the
# user just saw "the app doesn't open". The policy now is:
#
#   - an EXPLICITLY passed --port that is busy is a loud, immediate failure
#     (clear one-line message, exit code 2) — never silently swallowed;
#   - the DEFAULT port (no --port given) auto-picks the next free port in
#     8766..8785 and logs which one was chosen, only opening the browser at
#     the port actually bound;
#   - ANY fatal startup exception (arg parsing onward) is appended with a
#     traceback to <data_dir>/logs/startup-error.log, and — only when frozen
#     AND no usable console is attached — additionally shown via a native
#     OS error dialog, so a windowed build never just vanishes.
# ---------------------------------------------------------------------------

# How many ports past the (non-explicit) default to try when it is busy —
# i.e. 8766..8785 when the default is 8765.
PORT_FALLBACK_ATTEMPTS = 20


class StartupError(Exception):
    """A fatal, expected startup failure (e.g. an explicitly-requested port
    already in use). Routed through the same never-silent logging/dialog
    path as any other startup exception, but carries its own exit code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _bind_server(host: str, base_port: int, explicit_port: bool) -> tuple[ThreadingHTTPServer, int]:
    """Bind the HTTP server, applying the explicit-vs-default port policy.

    Returns ``(server, bound_port)`` on success. An EXPLICIT busy port raises
    ``StartupError(exit_code=2)`` immediately. A DEFAULT (non-explicit) busy
    port falls through to trying ``base_port + 1 .. base_port +
    PORT_FALLBACK_ATTEMPTS``; exhausting that range also raises
    ``StartupError``. The server is fully bound before this returns — the
    caller only opens a browser tab afterward, at the real bound port.
    """
    try:
        return ThreadingHTTPServer((host, base_port), Handler), base_port
    except OSError as exc:
        if explicit_port:
            raise StartupError(
                f"port {base_port} is already in use (--port was explicit): {exc}",
                exit_code=2,
            ) from exc
        logger.info(
            "default port %s is busy (%s); trying fallback ports %s-%s",
            base_port, exc, base_port + 1, base_port + PORT_FALLBACK_ATTEMPTS,
        )

    for candidate in range(base_port + 1, base_port + 1 + PORT_FALLBACK_ATTEMPTS):
        try:
            server = ThreadingHTTPServer((host, candidate), Handler)
        except OSError:
            continue
        print(f"Note: default port {base_port} was busy; using port {candidate} instead", flush=True)
        logger.info("bound to fallback port %s (default %s was busy)", candidate, base_port)
        return server, candidate

    raise StartupError(
        f"could not bind any port in range {base_port}-{base_port + PORT_FALLBACK_ATTEMPTS} on {host}"
    )


def _startup_log_path() -> Path:
    return APP_DATA_DIR / "logs" / "startup-error.log"


def _log_startup_failure(exc: BaseException) -> Path:
    """Append a timestamped traceback for a fatal startup failure to the
    startup-error log (creating parent dirs as needed). Returns the log path
    so the caller can reference it (e.g. in a dialog message)."""
    log_path = _startup_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(f"\n---- {time.strftime('%Y-%m-%d %H:%M:%S')} ----\n")
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=f)
    return log_path


def _has_usable_console() -> bool:
    """Best-effort check for an attached, interactive console. A frozen
    windowed build (macOS .app launched from Finder, PyInstaller
    ``--windowed`` on Windows) has none, so stdout/stderr writes are
    invisible to the user — that is exactly when the native dialog
    fallback below is needed."""
    stream = sys.stderr
    if stream is None:
        return False
    try:
        return bool(stream.isatty())
    except Exception:
        return False


def _show_native_error_dialog(message: str, log_path: Path) -> None:
    """Show a short native OS error dialog. Best-effort only — the log file
    written by ``_log_startup_failure`` is the real guarantee, so any
    failure here (missing ``osascript``, no ``user32``, headless CI, ...) is
    swallowed rather than raised."""
    title = "Video Watermark Replacer"
    short_message = f"{message}\nDetails: {log_path}"
    try:
        if sys.platform == "darwin":
            script = (
                f"display dialog {json.dumps(short_message)} "
                f'with title {json.dumps(title)} buttons {{"OK"}} with icon stop'
            )
            subprocess.run(["osascript", "-e", script], check=False, timeout=30)
        elif os.name == "nt":
            ctypes.windll.user32.MessageBoxW(0, short_message, title, 0x10)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - dialog is best-effort, never fatal
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description="Video Watermark Replacer local server")
    parser.add_argument(
        "--port", type=int, default=None,
        help="listen on this exact port; fails loudly (exit 2) if it is busy. "
             "Omit to auto-pick starting at 8765.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true", help="do not auto-open a browser tab")
    # Internal/test-support only: overrides what "the default port" means
    # (still counts as non-explicit, i.e. busy -> auto-fallback, not exit 2).
    # Lets the smoke test pin a known-busy port without racing a real 8765.
    parser.add_argument("--default-port", type=int, default=8765, help=argparse.SUPPRESS)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    ensure_data_dirs()
    logger.info("data dir: %s", APP_DATA_DIR)

    explicit_port = args.port is not None
    base_port = args.port if explicit_port else args.default_port

    server, bound_port = _bind_server(args.host, base_port, explicit_port)
    logger.info("resolved port: %s", bound_port)

    url = f"http://{args.host}:{bound_port}/"
    print(f"Video Watermark Replacer running at {url}", flush=True)

    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        server.shutdown()
        server.server_close()


def run() -> int:
    """Guarded entry point. Wraps ``main()`` (arg parsing onward) so that ANY
    fatal startup exception is never silent:

    - always logged (stderr, via the ``logging`` module once configured) and
      appended with a traceback to ``<data_dir>/logs/startup-error.log``;
    - when frozen AND no usable console is attached, additionally shown via a
      short native OS error dialog naming the log path;
    - always exits non-zero.

    A ``KeyboardInterrupt`` during ``server.serve_forever()`` is handled
    inside ``main()`` itself (unchanged, clean-shutdown message) and never
    reaches this wrapper.
    """
    try:
        main()
        return 0
    except StartupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        logger.error(str(exc))
        log_path = _log_startup_failure(exc)
        if is_frozen() and not _has_usable_console():
            _show_native_error_dialog(str(exc), log_path)
        return exc.exit_code
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - top-level guard must catch everything
        logger.exception("fatal startup error")
        log_path = _log_startup_failure(exc)
        message = str(exc) or type(exc).__name__
        if is_frozen() and not _has_usable_console():
            _show_native_error_dialog(message, log_path)
        return 1


if __name__ == "__main__":
    sys.exit(run())
