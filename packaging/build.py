#!/usr/bin/env python3
"""PyInstaller build script for the Video Watermark Replacer desktop app.

Cross-platform (macOS + Windows) — run with the repo's shared venv python:

    <repo>/.venv/bin/python src/packaging/build.py        # macOS / Linux
    <repo>\\.venv\\Scripts\\python.exe src\\packaging\\build.py   # Windows

Produces a onedir PyInstaller bundle named ``VideoWatermarkReplacer``:

- macOS: a windowed (no console) ``.app`` bundle, ad-hoc code-signed, then
  packaged into a ``.dmg`` disk image (with an ``Applications`` symlink
  alongside it) via ``hdiutil``.
- Windows: a console app (own window, so ``Ctrl-C`` / close works normally),
  zipped into a single archive.

CLOUD-SYNCED-VOLUME SAFETY (important — read before changing this file)
-------------------------------------------------------------------------
This repository can live on a Google-Drive-synced (FileProvider) volume.
Executables and app bundles CANNOT reliably live there: macOS Launch
Services refuses to execute a `.app` synced this way (observed failure:
Launch Services error -10810, process killed with SIGKILL "Resource
deadlock avoided"), and even a plain `cp` of the frozen executable can fail
with I/O errors mid-copy. A `.dmg` (a single compressed file) or a `.zip`
IS safe on a synced volume — cloud sync just replicates the file's bytes,
it never has to execute or partially-materialize it.

So this script never lets PyInstaller build (`--workpath`/`--distpath`) or
sign anything on the repo volume. All of that happens in a LOCAL,
non-synced staging directory outside the repo (`~/Library/Caches/...` on
macOS, `%LOCALAPPDATA%\\...` on Windows). Only the final, single-file,
sync-safe deliverable (the `.dmg` or the `.zip`) is copied back into this
repo's ``artifacts/dist/`` — never a raw `.app` bundle or onedir folder.

Outputs:

- Local staging (transient, never committed, never synced): all PyInstaller
  work, the ad-hoc-signed `.app`, and the dmg-staging folder.
- ``artifacts/dist/`` (final, sync-safe, this repo): the `.dmg` (macOS) or
  `.zip` (Windows) only.

This script does not install anything itself — it only checks that
``pyinstaller`` is importable in the running interpreter and exits with a
clear message (and the exact install command) if it is not.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

APP_NAME = "VideoWatermarkReplacer"

# ---------------------------------------------------------------------------
# Paths — all resolved relative to this file, never the current working
# directory, so the script works no matter where it is invoked from.
# ---------------------------------------------------------------------------
PACKAGING_DIR = Path(__file__).resolve().parent  # src/packaging  (this dir)
SRC_DIR = PACKAGING_DIR.parent  # src/
SERVICE_DIR = SRC_DIR.parent  # service root (parent of src/)

ENTRY_SCRIPT = SRC_DIR / "app.py"
STATIC_DIR = SRC_DIR / "static"
RESOURCES_DIR = SERVICE_DIR / "resources"
STICKER_FILE = RESOURCES_DIR / "paper-sticker.png"

# artifacts/dist/ is now the home ONLY for the final, single-file, sync-safe
# deliverable (.dmg on macOS, .zip on Windows) — never a raw .app or onedir
# folder (see module docstring). artifacts/build-tmp/ is legacy: PyInstaller
# no longer writes there (its work happens in the local staging dir below);
# any stale content left by a previous build is cleaned up as part of this
# fix (it was itself a broken cloud-synced binary).
ARTIFACTS_DIR = SERVICE_DIR / "artifacts"
DIST_DIR = ARTIFACTS_DIR / "dist"
LEGACY_BUILD_TMP_DIR = ARTIFACTS_DIR / "build-tmp"


def _local_staging_base() -> Path:
    """Local, non-cloud-synced base directory for all transient PyInstaller
    build work — the fix for this app's core packaging defect (see module
    docstring). Resolved per-OS, always OUTSIDE the repo:

    - macOS:        ``~/Library/Caches/VideoWatermarkReplacer-build``
    - Windows:      ``%LOCALAPPDATA%\\VideoWatermarkReplacer-build``
    - other POSIX:  ``~/.cache/VideoWatermarkReplacer-build``
    """
    system = platform.system()
    if system == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    elif system == "Darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path.home() / ".cache"
    return base / f"{APP_NAME}-build"


STAGING_DIR = _local_staging_base()
STAGING_WORK_DIR = STAGING_DIR / "build-tmp"  # PyInstaller --workpath
STAGING_DIST_DIR = STAGING_DIR / "dist"  # PyInstaller --distpath
STAGING_DMG_ROOT = STAGING_DIR / "dmg-root"  # folder hdiutil packages into a dmg (macOS)


def _fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(1)


def _check_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        venv_hint = str((SERVICE_DIR / ".." / ".." / ".." / ".venv").resolve())
        _fail(
            "pyinstaller is not installed in this interpreter.\n"
            f"       Interpreter: {sys.executable}\n"
            "       Install it into the repo's shared venv first, e.g.:\n"
            "         <repo>/.venv/bin/pip install pyinstaller      (macOS/Linux)\n"
            "         <repo>\\.venv\\Scripts\\pip.exe install pyinstaller  (Windows)\n"
            f"       (repo venv guess: {venv_hint})"
        )


def _check_inputs() -> None:
    missing = [p for p in (ENTRY_SCRIPT, STATIC_DIR, STICKER_FILE) if not p.exists()]
    if missing:
        listed = "\n".join(f"  - {p}" for p in missing)
        _fail(f"required build input(s) not found:\n{listed}")


def _clean_stale_repo_artifacts() -> None:
    """Remove broken raw .app / onedir copies a previous build left directly
    on this cloud-synced repo volume, plus the legacy build-tmp workdir this
    script no longer writes to. These are exactly the kind of artifact that
    cannot reliably live on a synced volume (see module docstring) — cleaning
    them up is part of this fix, not an unrelated side effect."""
    stale_paths = [
        DIST_DIR / f"{APP_NAME}.app",
        DIST_DIR / APP_NAME,
        LEGACY_BUILD_TMP_DIR,
    ]
    for stale in stale_paths:
        if not stale.exists() and not stale.is_symlink():
            continue
        print(f"Removing stale cloud-synced build artifact: {stale}")
        if stale.is_symlink() or stale.is_file():
            stale.unlink()
        else:
            shutil.rmtree(stale)


def _run_pyinstaller() -> None:
    STAGING_DIST_DIR.mkdir(parents=True, exist_ok=True)
    STAGING_WORK_DIR.mkdir(parents=True, exist_ok=True)
    PACKAGING_DIR.mkdir(parents=True, exist_ok=True)
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    # PyInstaller's --add-data SOURCE<sep>DEST separator is the platform's
    # native path separator: ':' on macOS/Linux, ';' on Windows.
    sep = os.pathsep

    args = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(ENTRY_SCRIPT),
        "--name",
        APP_NAME,
        "--onedir",
        "--noconfirm",
        "--clean",
        "--paths",
        str(SRC_DIR),
        "--distpath",
        str(STAGING_DIST_DIR),
        "--workpath",
        str(STAGING_WORK_DIR),
        "--specpath",
        str(PACKAGING_DIR),
        "--add-data",
        f"{STATIC_DIR}{sep}static",
        "--add-data",
        f"{STICKER_FILE}{sep}resources",
        # imageio_ffmpeg ships its ffmpeg binary as package data looked up via
        # importlib.resources at runtime, not a static import PyInstaller's
        # analysis can see — --collect-all pulls in the binary, the
        # `binaries` sub-package data, and any submodules so the lookup
        # resolves inside the frozen bundle. (pyinstaller-hooks-contrib also
        # ships a hook for this package; --collect-all is defense-in-depth,
        # and the build below empirically verifies the binary lands.)
        "--collect-all",
        "imageio_ffmpeg",
        # imageio picks its plugin (e.g. the ffmpeg backend) via a lazy
        # import keyed by plugin name, which PyInstaller's static analysis
        # cannot follow — collect every plugin submodule so the lookup
        # resolves regardless of which one is requested at runtime.
        "--collect-submodules",
        "imageio.plugins",
        # imageio's __init__ reads its own installed version via
        # importlib.metadata.version("imageio") at import time; without its
        # dist-info bundled that raises PackageNotFoundError and the frozen
        # app crashes on startup before serving a single request.
        "--copy-metadata",
        "imageio",
        # The local `wmr` engine package is imported via sys.path.insert()
        # at runtime (app.py); --paths above lets PyInstaller's analysis
        # find it, --hidden-import is a defensive belt-and-suspenders.
        "--hidden-import",
        "wmr",
    ]

    if platform.system() == "Darwin":
        args.append("--windowed")
    else:
        args.append("--console")

    print("Running PyInstaller (staging in a local, non-synced directory):")
    print(f"  staging dir: {STAGING_DIR}")
    print("  " + " ".join(args))
    print()
    result = subprocess.run(args, cwd=str(SERVICE_DIR))
    if result.returncode != 0:
        _fail(f"PyInstaller exited with status {result.returncode}.")


def _onedir_root() -> Path:
    """The onedir bundle root to inspect for the collected ffmpeg binary.

    On macOS with --windowed, PyInstaller's .app layout only puts the launcher
    executable in Contents/MacOS/ — collected data and binaries (including
    imageio_ffmpeg's ffmpeg executable) land under Contents/Resources/ and
    Contents/Frameworks/, so the whole .app bundle must be searched. Elsewhere
    (including Windows) it is the flat dist/<APP_NAME>/ folder. Always looks
    in the local staging distpath — PyInstaller never writes into the repo.
    """
    app_bundle = STAGING_DIST_DIR / f"{APP_NAME}.app"
    if app_bundle.exists():
        return app_bundle
    return STAGING_DIST_DIR / APP_NAME


def _verify_ffmpeg_bundled() -> None:
    root = _onedir_root()
    if not root.exists():
        print(f"WARNING: expected onedir output not found at {root}; cannot verify ffmpeg binary.")
        return
    hits = [p for p in root.rglob("*") if p.is_file() and "ffmpeg" in p.name.lower()]
    if hits:
        print("Verified: ffmpeg binary present in bundle:")
        for p in hits:
            print(f"  - {p.relative_to(root)}")
    else:
        print(
            "WARNING: no ffmpeg-named file found under the bundle — "
            "imageio_ffmpeg's binary may not have been collected. "
            "Video processing will fail at runtime."
        )


def _codesign_adhoc(app_bundle: Path) -> None:
    """Ad-hoc code-sign the .app in staging (``codesign --sign -``). No
    Developer ID is involved — this only satisfies the local dynamic
    validation macOS performs before running/packaging a bundle; it is not
    notarization and does not clear Gatekeeper's quarantine check for a
    bundle that was downloaded/transferred from elsewhere (see BUILD.md)."""
    cmd = ["codesign", "--force", "--deep", "--sign", "-", str(app_bundle)]
    print("Running: " + " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        _fail(f"codesign failed (status {result.returncode}) on {app_bundle}")
    print(f"Ad-hoc signed: {app_bundle}")


def _make_dmg() -> Path | None:
    """Build the .dmg entirely in the local staging dir, from a small folder
    containing the (already ad-hoc-signed) .app plus an ``Applications``
    symlink, so the mounted dmg offers the familiar drag-to-install flow.
    Returns the staging .dmg path (the caller copies it into artifacts/dist/)
    or ``None`` if the step could not be completed (never fatal — the .app in
    staging is still usable directly)."""
    app_bundle = STAGING_DIST_DIR / f"{APP_NAME}.app"
    if not app_bundle.exists():
        print(f"WARNING: {app_bundle} not found; skipping .dmg creation.")
        return None
    if shutil.which("hdiutil") is None:
        print("WARNING: hdiutil not found on PATH; skipping .dmg creation.")
        return None

    if STAGING_DMG_ROOT.exists():
        shutil.rmtree(STAGING_DMG_ROOT)
    STAGING_DMG_ROOT.mkdir(parents=True)

    staged_app = STAGING_DMG_ROOT / f"{APP_NAME}.app"
    shutil.copytree(app_bundle, staged_app, symlinks=True)
    os.symlink("/Applications", STAGING_DMG_ROOT / "Applications")

    staging_dmg_path = STAGING_DIR / f"{APP_NAME}.dmg"
    if staging_dmg_path.exists():
        staging_dmg_path.unlink()

    cmd = [
        "hdiutil",
        "create",
        "-volname",
        APP_NAME,
        "-srcfolder",
        str(STAGING_DMG_ROOT),
        "-ov",
        "-format",
        "UDZO",
        str(staging_dmg_path),
    ]
    print("Running: " + " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"WARNING: hdiutil failed (status {result.returncode}):")
        print(result.stdout)
        print(result.stderr, file=sys.stderr)
        return None
    return staging_dmg_path


def _make_windows_zip() -> Path:
    """Zip the onedir folder in staging (single-archive = sync-safe) and
    return the staging .zip path; caller copies it into artifacts/dist/."""
    onedir = STAGING_DIST_DIR / APP_NAME
    archive_base = STAGING_DIR / APP_NAME  # shutil adds the .zip suffix
    if archive_base.with_suffix(".zip").exists():
        archive_base.with_suffix(".zip").unlink()
    archive_str = shutil.make_archive(
        base_name=str(archive_base), format="zip", root_dir=str(STAGING_DIST_DIR), base_dir=APP_NAME
    )
    return Path(archive_str)


def main() -> None:
    _check_pyinstaller()
    _check_inputs()
    _clean_stale_repo_artifacts()
    _run_pyinstaller()

    system = platform.system()
    print()
    _verify_ffmpeg_bundled()

    final_dist_path: Path | None = None
    staged_app_bundle: Path | None = None

    if system == "Darwin":
        staged_app_bundle = STAGING_DIST_DIR / f"{APP_NAME}.app"
        print()
        _codesign_adhoc(staged_app_bundle)
        print()
        staging_dmg_path = _make_dmg()
        if staging_dmg_path is not None:
            final_dist_path = DIST_DIR / f"{APP_NAME}.dmg"
            shutil.copy2(staging_dmg_path, final_dist_path)
    else:
        print()
        staging_zip_path = _make_windows_zip()
        final_dist_path = DIST_DIR / staging_zip_path.name
        shutil.copy2(staging_zip_path, final_dist_path)

    print()
    print("Build complete.")
    print(f"Local staging directory (PyInstaller work + signed .app): {STAGING_DIR}")
    print(f"Repo deliverable directory (sync-safe only): {DIST_DIR}")

    if system == "Darwin":
        exe = staged_app_bundle / "Contents" / "MacOS" / APP_NAME if staged_app_bundle else None
        print(f"  staging .app bundle: {staged_app_bundle}")
        if final_dist_path is not None:
            print(f"  .dmg (in artifacts/dist): {final_dist_path}")
        print()
        print("Install:")
        print(f"  1. Mount the dmg and drag {APP_NAME}.app onto the Applications shortcut inside it.")
        print("  2. In Applications, right-click the app and choose Open the first time (unsigned/ad-hoc build).")
        print()
        print("Local dev / verification (never run the .app from this synced repo path):")
        if exe is not None:
            print(f"  '{exe}' --no-browser --port 8971")
    else:
        exe = STAGING_DIST_DIR / APP_NAME / f"{APP_NAME}.exe"
        print(f"  staging onedir folder: {STAGING_DIST_DIR / APP_NAME}")
        if final_dist_path is not None:
            print(f"  .zip (in artifacts/dist): {final_dist_path}")
        print()
        print("Install:")
        print(f"  1. Unzip {final_dist_path.name if final_dist_path else APP_NAME + '.zip'} anywhere on local disk.")
        print(f"  2. Run \"{APP_NAME}.exe\" inside the unzipped folder.")


if __name__ == "__main__":
    main()
