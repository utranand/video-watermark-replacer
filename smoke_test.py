#!/usr/bin/env python3
"""Standalone smoke test for the Video Watermark Replacer server.

Starts ``app.py`` as a subprocess on a free local port, drives it through
the frontend-facing API contract via ``urllib``, and verifies the results
against the real output files (via ``wmr.probe``). Exits 0 on success, or
prints a clear FAIL and exits non-zero on the first hard failure while
still running every step it can and reporting PASS/FAIL per step.

Run with the repo venv, from anywhere::

    .venv/bin/python src/smoke_test.py
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
SERVICE_DIR = SRC_DIR.parent


def _find_repo_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".sidekicks").is_dir():
            return candidate
    return current


REPO_ROOT = _find_repo_root(SRC_DIR)
RESOURCES_DIR = SERVICE_DIR / "resources"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import wmr  # noqa: E402

SAMPLE_VIDEOS = [
    "Woman_applying_lipstick_in_room_202607170153.mp4",
    "Woman_applying_lipstick_in_room_202607170154.mp4",
    "Woman_walking_on_street_202607170154.mp4",
]
SHORTEST_VIDEO = "Woman_walking_on_street_202607170154.mp4"
SAMPLE_IMAGE = "paper-sticker.png"

EXPECTED_CX = 603
EXPECTED_CY = 1160
TOLERANCE = 25


results: list[tuple[str, bool, str]] = []


def record(step: str, ok: bool, detail: str = "") -> None:
    tag = "PASS" if ok else "FAIL"
    line = f"[{tag}] {step}" + (f" — {detail}" if detail else "")
    print(line)
    results.append((step, ok, detail))


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def occupy_port() -> tuple[socket.socket, int]:
    """Bind and hold a free local port open (unlike ``find_free_port``, which
    releases it immediately) so a subprocess trying that exact port sees it
    as busy. Caller must ``close()`` the returned socket when done."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


def wait_for_exit(proc: subprocess.Popen, timeout: float) -> int | None:
    """Wait up to ``timeout`` seconds for ``proc`` to exit; ``None`` on timeout."""
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def terminate(proc: subprocess.Popen | None) -> None:
    """Best-effort, idempotent process cleanup for the startup-robustness steps."""
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def http_get(url: str, headers: dict | None = None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()) if exc.headers else {}, exc.read()


def http_post_json(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = resp.read()
            return resp.status, json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return exc.code, {"ok": False, "error": body.decode("utf-8", errors="replace")}


def wait_for_ready(base_url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _, _ = http_get(base_url + "/")
            if status == 200:
                return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(0.25)
    return False


def main() -> int:
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    app_py = SRC_DIR / "app.py"

    proc = subprocess.Popen(
        [sys.executable, str(app_py), "--port", str(port), "--host", "127.0.0.1", "--no-browser"],
        cwd=str(SRC_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        ready = wait_for_ready(base_url)
        record("server startup", ready, "" if ready else "server did not become ready in time")
        if not ready:
            return 1

        # Step 1: GET / -> 200 + contains "<canvas"
        try:
            status, _, body = http_get(base_url + "/")
            ok = status == 200 and b"<canvas" in body
            record("GET / (index page)", ok, f"status={status}")
        except Exception as exc:  # noqa: BLE001
            record("GET / (index page)", False, str(exc))

        # Step 2: GET /static/app.js -> 200
        try:
            status, _, _ = http_get(base_url + "/static/app.js")
            record("GET /static/app.js", status == 200, f"status={status}")
        except Exception as exc:  # noqa: BLE001
            record("GET /static/app.js", False, str(exc))

        # Step 3: GET /api/resources -> ok, >=3 videos, >=1 image incl paper-sticker.png
        resources_ok = False
        try:
            status, _, body = http_get(base_url + "/api/resources")
            data = json.loads(body.decode("utf-8"))
            videos = data.get("videos", [])
            images = data.get("images", [])
            has_sticker = any(img.get("name") == SAMPLE_IMAGE for img in images)
            resources_ok = status == 200 and len(videos) >= 3 and len(images) >= 1 and has_sticker
            record(
                "GET /api/resources",
                resources_ok,
                f"status={status} videos={len(videos)} images={len(images)} has_sticker={has_sticker}",
            )
        except Exception as exc:  # noqa: BLE001
            record("GET /api/resources", False, str(exc))

        video_repo_paths: dict[str, str] = {}
        if resources_ok:
            for v in videos:
                video_repo_paths[Path(v["path"]).name] = v["path"]
            for img in images:
                if img.get("name") == SAMPLE_IMAGE:
                    image_repo_path = img["path"]
                    break
            else:
                image_repo_path = None
        else:
            image_repo_path = None

        # Step 4: POST /api/detect for EACH of the 3 sample videos
        for name in SAMPLE_VIDEOS:
            video_path = video_repo_paths.get(name)
            if video_path is None:
                record(f"POST /api/detect ({name})", False, "video not found in /api/resources")
                continue
            try:
                status, data = http_post_json(base_url + "/api/detect", {"video": video_path})
                box = data.get("box", {})
                cx, cy = box.get("cx"), box.get("cy")
                ok = (
                    status == 200
                    and data.get("ok") is True
                    and cx is not None
                    and cy is not None
                    and abs(cx - EXPECTED_CX) <= TOLERANCE
                    and abs(cy - EXPECTED_CY) <= TOLERANCE
                )
                record(
                    f"POST /api/detect ({name})",
                    ok,
                    f"status={status} cx={cx} cy={cy} (expected ~{EXPECTED_CX},{EXPECTED_CY} +/-{TOLERANCE})",
                )
            except Exception as exc:  # noqa: BLE001
                record(f"POST /api/detect ({name})", False, str(exc))

        # Step 5: GET /api/frame on one sample -> 200, image/jpeg magic bytes
        try:
            sample_path = video_repo_paths.get(SHORTEST_VIDEO)
            status, headers, body = http_get(
                base_url + f"/api/frame?video={urllib.parse.quote(sample_path)}&t=0"
                if sample_path
                else base_url + "/api/frame"
            )
            is_jpeg = body[:3] == b"\xff\xd8\xff"
            record(
                "GET /api/frame",
                status == 200 and is_jpeg,
                f"status={status} content-type={headers.get('Content-Type')} magic_ok={is_jpeg}",
            )
        except Exception as exc:  # noqa: BLE001
            record("GET /api/frame", False, str(exc))

        # Step 6: POST /api/process on the shortest sample, box=null, scale 1.5
        output_repo_path = None
        shortest_repo_path = video_repo_paths.get(SHORTEST_VIDEO)
        source_probe = None
        if shortest_repo_path:
            source_probe = wmr.probe(str(REPO_ROOT / shortest_repo_path))
        try:
            if not shortest_repo_path or not image_repo_path:
                raise RuntimeError("missing shortest video or sample image from /api/resources")
            status, data = http_post_json(
                base_url + "/api/process",
                {
                    "video": shortest_repo_path,
                    "image": image_repo_path,
                    "box": None,
                    "scale": 1.5,
                    "output_name": "",
                },
            )
            ok = status == 200 and data.get("ok") is True
            if ok:
                output_repo_path = data["output"]
            record("POST /api/process (auto-detect)", ok, f"status={status} data={data if not ok else data.get('output')}")
        except Exception as exc:  # noqa: BLE001
            record("POST /api/process (auto-detect)", False, str(exc))

        if output_repo_path:
            try:
                out_abs = REPO_ROOT / output_repo_path
                probe = wmr.probe(str(out_abs))
                fps_ok = abs(probe["fps"] - 24.0) <= 0.1
                dur_ok = (
                    source_probe is not None
                    and abs(probe["duration"] - source_probe["duration"]) <= 0.2
                )
                ok = (
                    probe["width"] == 720
                    and probe["height"] == 1280
                    and fps_ok
                    and dur_ok
                    and probe["has_audio"] is True
                )
                record(
                    "verify processed output (wmr.probe)",
                    ok,
                    f"probe={probe} source_duration={source_probe['duration'] if source_probe else None}",
                )
            except Exception as exc:  # noqa: BLE001
                record("verify processed output (wmr.probe)", False, str(exc))
        else:
            record("verify processed output (wmr.probe)", False, "no output produced by previous step")

        # Step 7: POST /api/process with an explicit manual box
        manual_box = {"x": 100, "y": 100, "w": 60, "h": 60}
        try:
            if not shortest_repo_path or not image_repo_path:
                raise RuntimeError("missing shortest video or sample image from /api/resources")
            status, data = http_post_json(
                base_url + "/api/process",
                {
                    "video": shortest_repo_path,
                    "image": image_repo_path,
                    "box": manual_box,
                    "scale": 1.5,
                    "output_name": "manual_box_test.mp4",
                },
            )
            box_used = data.get("box_used", {})
            box_matches = all(box_used.get(k) == manual_box[k] for k in manual_box)
            ok = status == 200 and data.get("ok") is True and box_matches
            manual_output_repo_path = data.get("output") if ok else None
            record(
                "POST /api/process (manual box)",
                ok,
                f"status={status} box_used={box_used} expected={manual_box}",
            )
        except Exception as exc:  # noqa: BLE001
            manual_output_repo_path = None
            record("POST /api/process (manual box)", False, str(exc))

        # Step 8: GET /media on the output with a Range header -> 206
        range_target = manual_output_repo_path or output_repo_path
        try:
            if not range_target:
                raise RuntimeError("no processed output available to range-request")
            status, headers, body = http_get(
                base_url + f"/media?path={urllib.parse.quote(range_target)}",
                headers={"Range": "bytes=0-1023"},
            )
            ok = status == 206 and "Content-Range" in headers
            record("GET /media with Range", ok, f"status={status} content-range={headers.get('Content-Range')}")
        except Exception as exc:  # noqa: BLE001
            record("GET /media with Range", False, str(exc))

        # Step 9: GET /api/browse with an empty dir -> home directory
        try:
            status, _, body = http_get(base_url + "/api/browse")
            data = json.loads(body.decode("utf-8"))
            expected_home = str(Path.home().resolve())
            ok = (
                status == 200
                and data.get("ok") is True
                and data.get("dir") == expected_home
                and data.get("parent") is not None
            )
            record(
                "GET /api/browse (default -> home)",
                ok,
                f"status={status} dir={data.get('dir')} parent={data.get('parent')}",
            )
        except Exception as exc:  # noqa: BLE001
            record("GET /api/browse (default -> home)", False, str(exc))

        # Step 10: GET /api/browse?kind=video pointed at resources/ (absolute) -> the 3 sample mp4s
        try:
            url = base_url + "/api/browse?" + urllib.parse.urlencode(
                {"dir": str(RESOURCES_DIR.resolve()), "kind": "video"}
            )
            status, _, body = http_get(url)
            data = json.loads(body.decode("utf-8"))
            names = {f.get("name") for f in data.get("files", [])}
            ok = status == 200 and data.get("ok") is True and set(SAMPLE_VIDEOS).issubset(names)
            record(
                "GET /api/browse (kind=video, resources dir)",
                ok,
                f"status={status} files={sorted(n for n in names if n)}",
            )
        except Exception as exc:  # noqa: BLE001
            record("GET /api/browse (kind=video, resources dir)", False, str(exc))

        # Step 11: GET /api/browse?kind=dir pointed at resources/ -> files empty
        try:
            url = base_url + "/api/browse?" + urllib.parse.urlencode(
                {"dir": str(RESOURCES_DIR.resolve()), "kind": "dir"}
            )
            status, _, body = http_get(url)
            data = json.loads(body.decode("utf-8"))
            ok = status == 200 and data.get("ok") is True and data.get("files") == []
            record("GET /api/browse (kind=dir)", ok, f"status={status} files={data.get('files')}")
        except Exception as exc:  # noqa: BLE001
            record("GET /api/browse (kind=dir)", False, str(exc))

        # Step 12: POST /api/process with fit=true + an explicit box -> ok, fit true
        try:
            if not shortest_repo_path or not image_repo_path:
                raise RuntimeError("missing shortest video or sample image from /api/resources")
            status, data = http_post_json(
                base_url + "/api/process",
                {
                    "video": shortest_repo_path,
                    "image": image_repo_path,
                    "box": {"x": 100, "y": 100, "w": 60, "h": 60},
                    "scale": 1.5,
                    "fit": True,
                    "output_name": "fit_test.mp4",
                    "output_dir": "",
                },
            )
            ok = status == 200 and data.get("ok") is True and data.get("fit") is True
            record("POST /api/process (fit=true)", ok, f"status={status} fit={data.get('fit')}")
        except Exception as exc:  # noqa: BLE001
            record("POST /api/process (fit=true)", False, str(exc))

        # Step 13: POST /api/process with an absolute output_dir (a fresh temp dir)
        tmp_out_dir = tempfile.mkdtemp(prefix="wmr_smoke_out_")
        abs_output_path = None
        try:
            try:
                if not shortest_repo_path or not image_repo_path:
                    raise RuntimeError("missing shortest video or sample image from /api/resources")
                status, data = http_post_json(
                    base_url + "/api/process",
                    {
                        "video": shortest_repo_path,
                        "image": image_repo_path,
                        "box": None,
                        "scale": 1.5,
                        "fit": False,
                        "output_name": "abs_out_test.mp4",
                        "output_dir": tmp_out_dir,
                    },
                )
                output_str = data.get("output") if status == 200 and data.get("ok") else None
                inside_tmp_dir = (
                    output_str is not None
                    and Path(tmp_out_dir).resolve() in Path(output_str).resolve().parents
                )
                ok = (
                    status == 200
                    and data.get("ok") is True
                    and output_str is not None
                    and Path(output_str).is_absolute()
                    and inside_tmp_dir
                    and Path(output_str).is_file()
                )
                if ok:
                    abs_output_path = output_str
                record(
                    "POST /api/process (absolute output_dir)",
                    ok,
                    f"status={status} output={output_str}",
                )
            except Exception as exc:  # noqa: BLE001
                record("POST /api/process (absolute output_dir)", False, str(exc))

            if abs_output_path:
                try:
                    out_probe = wmr.probe(abs_output_path)
                    fps_ok = abs(out_probe["fps"] - 24.0) <= 0.1
                    probe_ok = (
                        out_probe["width"] == 720
                        and out_probe["height"] == 1280
                        and fps_ok
                        and out_probe["has_audio"] is True
                    )
                    record("verify absolute-output-dir output (wmr.probe)", probe_ok, f"probe={out_probe}")
                except Exception as exc:  # noqa: BLE001
                    record("verify absolute-output-dir output (wmr.probe)", False, str(exc))

                try:
                    status, headers, body = http_get(
                        base_url + f"/media?path={urllib.parse.quote(abs_output_path)}",
                        headers={"Range": "bytes=0-1023"},
                    )
                    ok = status == 206 and "Content-Range" in headers
                    record(
                        "GET /media (absolute output_dir path) with Range",
                        ok,
                        f"status={status} content-range={headers.get('Content-Range')}",
                    )
                except Exception as exc:  # noqa: BLE001
                    record("GET /media (absolute output_dir path) with Range", False, str(exc))
            else:
                record("verify absolute-output-dir output (wmr.probe)", False, "no output produced")
                record("GET /media (absolute output_dir path) with Range", False, "no output produced")
        finally:
            shutil.rmtree(tmp_out_dir, ignore_errors=True)

        # Step 14: default-port fallback — occupy a port, run the app with
        # --default-port pinned to it (still counts as non-explicit), and
        # expect the app to auto-pick port+1 and announce the fallback.
        fallback_sock = None
        fallback_proc = None
        try:
            fallback_sock, occupied_port = occupy_port()
            fallback_port = occupied_port + 1
            fallback_proc = subprocess.Popen(
                [sys.executable, str(app_py), "--default-port", str(occupied_port), "--no-browser"],
                cwd=str(SRC_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            fallback_url = f"http://127.0.0.1:{fallback_port}"
            fallback_ready = wait_for_ready(fallback_url)
            fallback_status = None
            if fallback_ready:
                fallback_status, _, _ = http_get(fallback_url + "/")
            terminate(fallback_proc)
            fallback_output = fallback_proc.stdout.read() if fallback_proc.stdout else ""
            names_fallback = str(occupied_port) in fallback_output and str(fallback_port) in fallback_output
            ok = fallback_ready and fallback_status == 200 and names_fallback
            record(
                "default-port fallback (busy default -> port+1)",
                ok,
                f"occupied_port={occupied_port} fallback_port={fallback_port} "
                f"ready={fallback_ready} status={fallback_status} names_fallback={names_fallback}",
            )
        except Exception as exc:  # noqa: BLE001
            record("default-port fallback (busy default -> port+1)", False, str(exc))
        finally:
            terminate(fallback_proc)
            if fallback_sock is not None:
                fallback_sock.close()

        # Step 15/16: an EXPLICIT --port that is busy must fail loudly (exit
        # code 2, clear message) and append a traceback to the repo-mode
        # startup-error.log.
        busy_sock = None
        busy_proc = None
        try:
            busy_sock, busy_port = occupy_port()
            busy_proc = subprocess.Popen(
                [sys.executable, str(app_py), "--port", str(busy_port), "--no-browser"],
                cwd=str(SRC_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            exit_code = wait_for_exit(busy_proc, timeout=15)
            busy_output = busy_proc.stdout.read() if busy_proc.stdout else ""
            ok = exit_code == 2 and str(busy_port) in busy_output
            record(
                "explicit --port busy -> exit code 2",
                ok,
                f"port={busy_port} exit_code={exit_code} output_has_port={str(busy_port) in busy_output}",
            )

            log_path = SERVICE_DIR / "artifacts" / "app-data" / "logs" / "startup-error.log"
            log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-4000:] if log_path.is_file() else ""
            log_ok = log_path.is_file() and str(busy_port) in log_tail
            record(
                "startup-error.log appended on explicit-busy failure",
                log_ok,
                f"log_path={log_path} exists={log_path.is_file()}",
            )
        except Exception as exc:  # noqa: BLE001
            record("explicit --port busy -> exit code 2", False, str(exc))
            record("startup-error.log appended on explicit-busy failure", False, "prior step raised")
        finally:
            terminate(busy_proc)
            if busy_sock is not None:
                busy_sock.close()

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        if proc.stdout:
            leftover = proc.stdout.read()
            if leftover and any(not ok for _, ok, _ in results):
                print("---- server output ----")
                print(leftover)
                print("------------------------")

    failed = [s for s, ok, _ in results if not ok]
    print()
    if failed:
        print(f"FAIL: {len(failed)}/{len(results)} step(s) failed: {', '.join(failed)}")
        return 1
    print(f"PASS: all {len(results)} step(s) passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
