# Video Watermark Replacer

A local, single-user web app that finds the Google Flow / Veo sparkle
watermark in a video and replaces it with an image of your choosing —
select a video, auto-detect (or hand-adjust) the watermark box, pick a
replacement image, and render a new video with the overlay composited
frame-by-frame.

## What it does

Google Flow (Veo) video exports carry a small, semi-transparent white
4-pointed sparkle watermark in a fixed screen position for the whole
clip. This app:

1. Detects that watermark's bounding box (or lets you draw/adjust one by
   hand).
2. Alpha-composites a replacement image over that region on every frame,
   scaled and centered on the box.
3. Muxes the original audio back in (when present) and writes a new MP4.

## Requirements

- The repository's own virtualenv — **no installs, no new dependencies**.
  Everything used (`numpy`, `PIL`/Pillow, `imageio`, `imageio-ffmpeg`) is
  already present in `.venv`.
- Nothing beyond the Python standard library is used for the web server
  itself (`http.server` / `socketserver`) — there is no Flask/FastAPI/etc.
  in this environment, so none is used.
- Local-only: the server binds `127.0.0.1` by default and is meant to run
  on your own machine.

## How to run

From the repo root (or the service directory — the app resolves paths
from its own file location, so either works):

```sh
.venv/bin/python projects/gdrive-sk/services/video-watermark-remover/src/app.py
```

or, from the service directory:

```sh
../../../../.venv/bin/python src/app.py
```

On startup it prints the URL it is listening on (default
`http://127.0.0.1:8765/`) and opens it in your default browser. Flags:

- `--port <N>` — listen on this **exact** port. If it is already in use the
  server fails loudly (a one-line error naming the port, exit code `2`) —
  it never dies silently.
- `--host <addr>` — bind address (default `127.0.0.1`; keep this local-only
  unless you understand the exposure).
- `--no-browser` — don't auto-open a browser tab (used by the smoke test).
- `--default-port <N>` — **internal/test-support only**: overrides what
  "the default port" means (still non-explicit, so a busy value auto-falls
  back rather than exiting `2`); lets the smoke test pin a known-busy port
  deterministically instead of racing a real `8765`.

Stop the server with `Ctrl+C` — it shuts down cleanly.

### Port auto-pick

When `--port` is **omitted**, the port is not pinned: if the default
(`8765`) is already held by another process, the server automatically tries
`8766`, `8767`, ... up to `8785`, binds the first free one, logs which port
it chose, and opens the browser at that **actual** bound port (never a URL
that isn't really serving). When `--port` **is** given explicitly and it is
busy, that is treated as a hard configuration error, not something to route
around — see the `--port` flag above.

### Startup error logging (never-silent failures)

Any fatal startup failure (bad port, permission error, anything else during
arg-parsing-through-bind) is always appended, with a full traceback, to a
`startup-error.log` file — so a failure is diagnosable even when nobody was
watching the terminal:

- **repo mode**: `artifacts/app-data/logs/startup-error.log` (relative to
  this service directory).
- **frozen/packaged mode**: under the same per-OS app-data root as
  uploads/outputs (see *Frozen/packaged app* below) — i.e.
  `<data_dir>/logs/startup-error.log`:
  - macOS: `~/Library/Application Support/VideoWatermarkReplacer/logs/startup-error.log`
  - Windows: `%LOCALAPPDATA%\VideoWatermarkReplacer\logs\startup-error.log`
  - other (Linux, …): `~/.local/share/VideoWatermarkReplacer/logs/startup-error.log`

In a **frozen, windowed** build with no usable console attached (the case
that used to fail silently), the same failure additionally pops a short
native OS error dialog (macOS `osascript`/Windows `MessageBoxW`) naming the
one-line error and the log path above, so the user is never left staring at
nothing. In normal (un-frozen) runs there is no dialog — stderr plus the log
file is enough since a terminal is presumably attached.

## UI walkthrough

1. **Source** — pick an existing video from the dropdown (seeded from
   `resources/`), or upload your own. Pick a replacement image the same
   way (`paper-sticker.png` is pre-selected when present).
2. **Preview & watermark box** — scrub the time slider to preview any
   frame. Click **Auto-detect watermark** to have the engine find the
   sparkle's bounding box, or skip straight to adjusting it by hand:
   drag inside the box to move it, drag a corner handle to resize it, or
   type exact `x`/`y`/`w`/`h` values. Any manual edit marks the box
   "Manual override" and is used instead of auto-detect at process time
   (uncheck "Use auto-detect at process time" to keep your manual box
   even after clicking Detect again).
3. **Process** — set the overlay **scale factor** (the replacement
   image's longest side becomes `scale × max(box.w, box.h)`, keeping its
   own aspect ratio), or switch on **fit mode** to stretch the replacement
   image to exactly cover the box rect instead (aspect ratio not
   preserved; `scale` is ignored while fit is on). Optionally pick an
   **output folder** (browse the filesystem, or leave it as the default
   `outputs/`) and an output filename. Click **Process video** to render.
4. **Result** — once processing finishes, the rendered video plays back
   inline with a **Download output** link.

## Path policy — repo-relative or absolute, localhost-only

Every path accepted by the API (`video`, `image`, `/api/frame?video=`,
`/media?path=`, `output_dir`, and `/api/browse?dir=`) may be either:

- **repo-relative** (forward slashes, resolved against the repository
  root), as before, or
- **absolute** — any path the account running the server can read. This
  is a deliberate policy: the app is a local, single-user tool, so an
  absolute path is honored as-is rather than confined to the repo.

A path used for reading must exist and be a regular file; the OS's own
permission model is the only remaining gate — a `PermissionError` (e.g. a
file you can't read) comes back as a clean `403` JSON response rather
than a stack trace. There is no repo-root containment check anymore (it
was intentionally dropped along with this change). This absolute-path
reach makes the server binding matter: **it still binds `127.0.0.1` by
default and is not meant to be exposed to a network** — anyone who can
reach the port can read/write anything your OS account can.

## Filesystem browsing

`GET /api/browse` lets the frontend offer a native-feeling folder/file
picker (for choosing a video, an image, or an output folder) without a
browser `<input type=file>` round-trip. Given `dir` (absolute or
repo-relative; empty/omitted defaults to your home directory) and `kind`
(`video`, `image`, or `dir`), it lists that directory's immediate
subdirectories and, for `video`/`image`, its matching files (dotfiles and
dot-directories are skipped; `kind=dir` returns no files, only
directories — for picking an output folder). Video matches:
`mp4/mov/m4v/webm/avi/mkv`; image matches: `png/jpg/jpeg/webp/bmp` (a
broader set than `/api/resources`' own scan, since this is a general
picker). Response paths are always absolute with the OS's native
separators.

## API reference

Paths in requests/responses may be **repo-relative** (forward slashes,
resolved against the repository root) or **absolute** — see *Path
policy* above.

| Method & path | Purpose |
|---|---|
| `GET /` | serves `src/static/index.html` |
| `GET /static/<name>` | serves a static asset (`app.js`, `style.css`, …) |
| `GET /api/resources` | `{"videos":[{name,path}...], "images":[{name,path}...]}` — scans the service's `resources/` dir plus any uploaded files in the app-data `uploads/` dir (frozen mode: videos come only from uploads; images get the bundled default sticker, if present, plus uploads) |
| `GET /api/browse?dir=<path>&kind=video\|image\|dir` | `{"ok":true,"dir","parent","dirs":[{name,path}...],"files":[{name,path,size}...]}` — see *Filesystem browsing* above; `{"ok":false,"error"}` with `400` (not found/not a directory) or `403` (permission denied) |
| `POST /api/upload` | `multipart/form-data` field `file`; saves into `uploads/` (sanitized, de-duplicated filename); `{"ok":true,"path","kind":"video"\|"image"}` |
| `POST /api/detect` | JSON `{"video"}`; runs `wmr.detect_watermark`; `{"ok":true,"box":{x,y,w,h,cx,cy,confidence,method}}` |
| `GET /api/frame?video=<path>&t=<seconds>` | a single decoded frame as a JPEG image |
| `POST /api/process` | JSON `{"video","image","box"\|null,"scale","fit":bool,"output_name","output_dir"}`; `box:null` runs detection first; `fit:true` stretches the overlay to exactly the box rect and ignores `scale`; `output_dir` empty uses the default `outputs/`, or an absolute path (created if missing) to write elsewhere; `{"ok":true,"output","frames","fps","box_used","fit"}` — `output` is absolute when `output_dir` was absolute, else repo-relative |
| `GET /media?path=<path>` | serves a file with HTTP Range support (`206 Partial Content`) — required for `<video>` seeking/playback |

Every error response is `{"ok": false, "error": "<message>"}` with an
appropriate `400`/`403`/`404`/`500` status; a raw traceback is never sent
to the client (exceptions are logged server-side via the `logging`
module instead).

## How detection works

`wmr.detect_watermark` (in `src/wmr/detect.py`, not modified by this app)
samples a handful of low-fps frames and computes, per pixel, the temporal
minimum brightness and temporal standard deviation across those samples.
The watermark is white-over-anything, so it never gets darker than the
alpha-blend floor (elevated `min`) while the constant overlay damps the
variance the underlying scene would otherwise show (`std`). A compact
blob of high-`min`/low-`std` pixels is searched for first in the
bottom-right quadrant (where the watermark is always observed to live),
then across the whole frame, and finally falls back to a known fixed
geometry if no plausible blob is found — so it always returns a usable
box.

## Smoke test

```sh
.venv/bin/python projects/gdrive-sk/services/video-watermark-remover/src/smoke_test.py
```

It starts the server as a subprocess on a free port (`--no-browser`),
drives the full API surface against the sample videos in `resources/`
(index/static pages, resource listing, detection on all three samples,
frame capture, a full auto-detect process run verified with `wmr.probe`,
a manual-box process run, a ranged `/media` fetch, `/api/browse` in its
three modes, a `fit=true` process run, and a process run with an
absolute `output_dir` verified end-to-end including a ranged `/media`
fetch of the absolute output path), prints `[PASS]`/`[FAIL]` per step,
and exits non-zero if anything failed. The server subprocess is always
terminated in a `finally` block.

It also covers the startup-robustness behavior above: occupying a port and
launching the app with the internal `--default-port` flag pinned to it
(asserting the app auto-picks port+1 and announces the fallback), occupying
a port and passing it via the real `--port` (asserting exit code `2` and a
clear message), and asserting that failure appended a traceback to the
repo-mode `startup-error.log`. Every socket/subprocess these steps use is
cleaned up in its own `finally` block.

## Output / upload locations

Runtime data lives under a per-install app-data directory (created on
demand, outside `src/`), or wherever an explicit `output_dir` points for
a given `/api/process` call:

- `uploads/` — files uploaded via `/api/upload`
- `outputs/` — rendered videos from `/api/process` (the default when
  `output_dir` is empty)

In normal (repo) mode this app-data directory is the service's own
`artifacts/app-data/`. In a packaged/frozen build it moves to a per-user
location — see *Frozen/packaged app* below.

## Frozen/packaged app

This app is bundle-ready for PyInstaller. When run from a frozen bundle
(`getattr(sys, "frozen", False)` is true), three things resolve
differently from repo mode:

- **Static assets** are served from `sys._MEIPASS/static` instead of
  `src/static`.
- **Uploads/outputs** move out of the repo entirely, to a per-user
  application-data directory (there is no repo to anchor `artifacts/` to
  once packaged):
  - macOS: `~/Library/Application Support/VideoWatermarkReplacer`
  - Windows: `%LOCALAPPDATA%\VideoWatermarkReplacer`
  - other (Linux, …): `~/.local/share/VideoWatermarkReplacer`
- **Default resources** (`/api/resources`) drop the bundled sample
  `resources/` dir: videos come only from what the user uploads; images
  offer a bundled default sticker at `sys._MEIPASS/resources/paper-sticker.png`
  (if the packager placed one there — referenced defensively, silently
  skipped if absent) plus uploads.

The resolution logic lives in small, independently testable helpers in
`app.py`: `is_frozen()`, `static_dir()`, `data_dir()`, and
`default_resources()` — each behaves exactly as it does today when not
frozen.

## Limitations

- The overlay **covers** the watermark region (alpha-composited on top);
  it does not inpaint/reconstruct the pixels underneath. A larger `scale`
  hides more of the surrounding frame along with the watermark.
- Detection is tuned to the Flow/Veo sparkle's typical size, aspect
  ratio, and screen position — an unusual watermark placement may need a
  manual box.
- Designed for local, single-user use only (binds `127.0.0.1` by
  default); there is no auth, and it should not be exposed to a network.
