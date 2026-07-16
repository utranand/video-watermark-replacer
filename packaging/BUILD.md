# Building VideoWatermarkReplacer

PyInstaller packaging for the Video Watermark Replacer desktop app. One
cross-platform build script (`build.py`) drives both macOS and Windows
builds — it detects the OS itself and adjusts flags accordingly.

## Why this build never writes an executable into the repo

This repo can live on a **Google-Drive-synced (FileProvider) volume**. A
raw `.app` bundle or PyInstaller onedir folder **cannot reliably live or run
there**:

- macOS Launch Services refuses to execute a `.app` synced this way —
  observed failure: Launch Services error `-10810`, the process killed with
  `SIGKILL` ("Resource deadlock avoided").
- Even a plain `cp` of the frozen executable can fail mid-copy with I/O
  errors — cloud-sync file providers don't materialize executable files
  reliably.
- A **single compressed file** (a `.dmg` or a `.zip`) does not have this
  problem — cloud sync just replicates its bytes; it never has to execute or
  partially-materialize it.

So `build.py`:

1. Runs **all** PyInstaller work (`--workpath`, `--distpath`) and the
   ad-hoc `codesign` step in a **local, non-synced staging directory**
   outside the repo:
   - macOS: `~/Library/Caches/VideoWatermarkReplacer-build/`
   - Windows: `%LOCALAPPDATA%\VideoWatermarkReplacer-build\`
   - other POSIX: `~/.cache/VideoWatermarkReplacer-build/`
2. Only copies the **final, single-file, sync-safe deliverable** — the
   `.dmg` (macOS) or `.zip` (Windows) — back into this repo's
   `artifacts/dist/`.
3. Never leaves a raw `.app` bundle or onedir folder in `artifacts/dist/` —
   a previous build's stale copies there (broken by the exact failure mode
   above) are deleted automatically at the start of every build, along with
   the now-unused legacy `artifacts/build-tmp/` workdir (PyInstaller no
   longer writes there).

**Never run the `.app` or onedir executable directly from a path inside this
repo** — always run it from the local staging directory, or from wherever
you installed it from the `.dmg`/`.zip`.

## Prerequisites

- The repo's single shared virtualenv, `.venv`, at the repo root (see the
  repo's `CLAUDE.md` — never a per-project/per-skill venv).
- `pyinstaller` installed into that venv (see below). `build.py` does **not**
  install it for you — it only checks it is importable and tells you the
  exact command to run if not.

## Build on macOS

```sh
# from the repo root
.venv/bin/pip install pyinstaller
.venv/bin/python projects/gdrive-sk/services/video-watermark-remover/src/packaging/build.py
```

What happens:

1. PyInstaller builds the onedir bundle and the `.app` in the local staging
   directory (`~/Library/Caches/VideoWatermarkReplacer-build/dist/`) — never
   under the repo.
2. The staged `.app` is **ad-hoc code-signed**
   (`codesign --force --deep --sign - <app>`) — required for the app to
   behave correctly once it's packaged and moved.
3. A small staging folder is built containing the signed `.app` **and an
   `Applications` symlink**, and `hdiutil create` packages it into a `.dmg`
   (also in staging) — so mounting the dmg offers the familiar
   drag-`.app`-onto-`Applications` install flow.
4. Only the `.dmg` is copied into `artifacts/dist/VideoWatermarkReplacer.dmg`
   (overwriting any previous one). Any stale raw `.app`/onedir folder
   previously left in `artifacts/dist/` is deleted first.

Install it:

1. Open `artifacts/dist/VideoWatermarkReplacer.dmg`, drag
   `VideoWatermarkReplacer.app` onto the `Applications` shortcut inside the
   mounted volume, then eject.
2. In `/Applications`, **right-click the app → Open** the first time (not a
   plain double-click) — see *Known limitations* below.

For a quick local/headless check without installing, run the staged `.app`
directly from the **local staging directory** (never from the repo path):

```sh
~/Library/Caches/VideoWatermarkReplacer-build/dist/VideoWatermarkReplacer.app/Contents/MacOS/VideoWatermarkReplacer --no-browser --port 8971
```

## Build on Windows

```powershell
# from the repo root, in PowerShell or cmd
.venv\Scripts\python.exe -m pip install pyinstaller
.venv\Scripts\python.exe projects\gdrive-sk\services\video-watermark-remover\src\packaging\build.py
```

(Create the venv first with `python -m venv .venv` at the repo root if it
does not already exist there — see the repo's Python-environment convention
in `CLAUDE.md`.)

What happens:

1. PyInstaller builds the onedir bundle in the local staging directory
   (`%LOCALAPPDATA%\VideoWatermarkReplacer-build\dist\`) — never under the
   repo.
2. The onedir folder is zipped (`shutil.make_archive`) in staging — a single
   archive file is sync-safe, a raw `.exe` + DLLs folder is not.
3. Only the `.zip` is copied into
   `artifacts\dist\VideoWatermarkReplacer.zip` (overwriting any previous
   one).

Install it: unzip `artifacts\dist\VideoWatermarkReplacer.zip` anywhere on
local disk, then run `VideoWatermarkReplacer.exe` inside the unzipped
folder.

There is **no** `.dmg`-equivalent step on Windows — the zip is the
deliverable. `build.py` only attempts the `.dmg`/codesign steps when running
on macOS.

## What gets bundled

- The app entry point `src/app.py` and the local `wmr` engine package
  (found via `--paths src`, since `app.py` puts `src/` on `sys.path` itself
  at runtime).
- `src/static/` → `static/` inside the bundle (the frontend).
- `resources/paper-sticker.png` → `resources/paper-sticker.png` inside the
  bundle (the bundled default sticker image; `app.py` already treats this
  defensively — it works fine if absent, so this is a convenience, not a
  hard dependency).
- The `imageio_ffmpeg` package's ffmpeg binary and `imageio`'s plugin
  submodules, collected via `--collect-all imageio_ffmpeg` /
  `--collect-submodules imageio.plugins` (pyinstaller-hooks-contrib also
  ships hooks for both, so this is defense-in-depth). `build.py` empirically
  verifies after every build that an `ffmpeg*` binary actually landed in the
  bundle and prints its path, or a loud warning if it did not.
- `imageio`'s own package metadata via `--copy-metadata imageio` — imageio's
  `__init__.py` looks up its own installed version via
  `importlib.metadata.version("imageio")` at import time; without this flag
  the frozen app crashes on startup with `PackageNotFoundError`.

## How the frozen app behaves

- **Port / host**: same CLI flags as the source script —
  `--port` (fails loudly, exit code 2, if the exact port is busy),
  `--host` (default `127.0.0.1`), `--no-browser`. Omit `--port` and the app
  auto-picks the next free port starting at 8765 (tries up to 20 fallback
  ports) rather than dying silently — this matters most on macOS, where a
  windowed `.app` launched from Finder has no console to show a crash.
- **Browser**: auto-opens the default browser on startup (at whichever port
  was actually bound) unless `--no-browser` is passed.
- **Startup failures are never silent**: any fatal startup exception is
  logged with a traceback to `<data_dir>/logs/startup-error.log`, and — only
  when frozen and running with no usable console attached (the normal
  windowed-`.app`-from-Finder case) — also shown via a native OS error
  dialog naming that log path.
- **Data directory** (uploads/outputs — see `app.py`'s `data_dir()`):
  - macOS: `~/Library/Application Support/VideoWatermarkReplacer`
  - Windows: `%LOCALAPPDATA%\VideoWatermarkReplacer`
  - other POSIX: `~/.local/share/VideoWatermarkReplacer`
- **Quitting**:
  - macOS: Dock right-click → Quit (it is a windowed app with no console —
    closing a browser tab does not stop the server process).
  - Windows: it is a console app — `Ctrl-C` in its window, or close the
    window.

## Known limitations

- **Ad-hoc signed only — not a Developer ID / notarized build**: `build.py`
  ad-hoc signs the `.app` (`codesign --sign -`), which is enough for the
  bundle to run and be packaged correctly on the machine that built it, but
  it is **not** notarization and carries no Developer ID. If the `.dmg`
  reaches another machine via a path that applies Apple's quarantine
  attribute (downloaded through a browser, AirDropped, etc.), Gatekeeper
  will still refuse a plain double-click the first time —
  **right-click → Open** once (or `xattr -d com.apple.quarantine
  VideoWatermarkReplacer.app`) clears it. A full Developer ID + notarization
  pipeline is out of scope here.
- **No cross-compilation**: PyInstaller bundles the interpreter and native
  extensions for the OS/architecture it runs on. The macOS build must be
  built on a Mac; the Windows build must be built on Windows (or a Windows
  CI runner) — `build.py` cannot produce a Windows artifact from macOS or
  vice versa.
- **Onedir, not onefile**: chosen deliberately for faster startup and
  simpler handling of the bundled ffmpeg binary (onefile would extract
  everything to a temp dir on every launch). The trade-off is a folder of
  files instead of a single executable — the `.app`/`.dmg` wrapping on
  macOS, and the `.zip` on Windows, already hide this from end users.
- **Local staging directory is transient and machine-local**: it is not
  cleaned up automatically between builds (so repeated builds are fast), and
  it is not portable — it exists only on the machine that ran `build.py`. It
  is safe to delete by hand at any time; the next build recreates it.
