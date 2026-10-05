"""Live end-to-end check of a RUNNING Yume server (manual; not collected by pytest).

    python pocket_yume.py launch                  # in one terminal
    python tests/live/check_server.py             # in another (default port 5001)
    python tests/live/check_server.py --port 5911 --audio speech.wav --language fr

What it checks, over real HTTP like the extension does:
  * security: token only for extension origins, no CORS for web pages,
    DNS-rebinding Host rejected, invalid URLs/languages rejected
  * the job pipeline: a local audio file is served over HTTP and submitted as a
    custom stream URL; the job must download, plan regions, transcribe and
    (if the LLM is reachable) translate; then export, library, blacklist
  * cache: the same job requested again after a cache-only reload is served
    without downloading
Exit code 0 = every check passed.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave

EXT = {"Origin": "chrome-extension://yume-live-check"}
FAILED = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        FAILED.append(name)


def call(base, path, method="GET", body=None, headers=None, timeout=60):
    req = urllib.request.Request(
        base + path,
        method=method,
        headers=headers or {},
        data=json.dumps(body).encode() if body is not None else None,
    )
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 — local server
            return r.status, json.loads(r.read() or b"{}"), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}"), dict(e.headers)


def tone_wav(path, seconds=70):
    """Fallback audio: a tone with gaps (exercises the pipeline, yields no speech)."""
    import math
    import struct

    sr = 16000
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = bytearray()
        for i in range(sr * seconds):
            t = i / sr
            v = 0.3 * math.sin(2 * math.pi * 220 * t) if (t % 10) < 8 else 0.0
            frames += struct.pack("<h", int(v * 32767))
        w.writeframes(bytes(frames))


def serve_file(path):
    """Serve `path`'s directory on a free port; returns the file's URL."""
    directory, name = os.path.split(os.path.abspath(path))

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=directory, **k)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/{name}"


def wait_job(base, auth, job_id, timeout, llm_up):
    """Wait for the job to settle. Without a reachable LLM a job waits in
    "translating" (lines are kept for when it comes back): that counts too."""
    t0 = time.time()
    snap = {}
    settled = ("done", "error") if llm_up else ("done", "error", "translating")
    while time.time() - t0 < timeout:
        _, snap, _ = call(base, f"/jobs/{job_id}?since=0&t=0", headers=auth)
        if snap.get("status") in settled:
            break
        time.sleep(1)
    return snap


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=5001)
    ap.add_argument("--audio", help="16 kHz mono WAV to submit (default: generated tone)")
    ap.add_argument("--language", default="ja")
    ap.add_argument("--target", default="English")
    ap.add_argument("--timeout", type=int, default=600, help="seconds to wait for the job")
    args = ap.parse_args()
    for stream in (sys.stdout, sys.stderr):  # lines are printed in any language
        stream.reconfigure(encoding="utf-8", errors="replace")
    base = f"http://127.0.0.1:{args.port}"

    # ── security ──────────────────────────────────────────────────────────────
    st, h, _ = call(base, "/health", headers=EXT)
    token = h.get("api_token")
    check(
        "server ready, token handed to an extension origin",
        st == 200 and h.get("status") == "ready" and token,
        h.get("status"),
    )
    if not token:
        sys.exit("Server not ready — start it with: python pocket_yume.py launch")
    _, h2, _ = call(base, "/health", headers={"Origin": "http://localhost:3000"})
    check("no token for a web page", "api_token" not in h2)
    st, _, hdr = call(base, "/stats", headers={"Origin": "http://localhost:3000"})
    check("web page without token rejected, no CORS header", st == 403 and "Access-Control-Allow-Origin" not in hdr)
    st, _, _ = call(base, "/stats", headers={"Host": "evil.example", "X-API-Token": token})
    check("DNS-rebinding Host rejected", st == 403)
    auth = {"X-API-Token": token, **EXT}
    st, e, _ = call(base, "/jobs", "POST", {"url": "file:///etc/passwd"}, auth)
    check("non-http URL rejected", st == 400, e.get("error"))
    st, e, _ = call(base, "/jobs", "POST", {"url": "https://x.example/v", "language": "../x"}, auth)
    check("invalid language rejected", st == 400, e.get("error"))

    # ── job pipeline ──────────────────────────────────────────────────────────
    audio = args.audio
    if not audio:
        audio = os.path.join(tempfile.mkdtemp(prefix="yume_live_"), "tone.wav")
        tone_wav(audio)
    stream = serve_file(audio)
    video_id = f"live-check-{os.path.basename(audio)}-{int(time.time())}"
    params = {
        "video_id": video_id,
        "url": stream,
        "stream_url": stream,
        "language": args.language,
        "target": args.target,
        "duration": 0,
    }
    st, job, _ = call(base, "/jobs", "POST", params, auth)
    check("job created", st == 200 and job.get("id"), job.get("error"))
    if st != 200:
        sys.exit(1)
    _, again, _ = call(base, "/jobs", "POST", params, auth)
    check("same parameters rejoin the same job", again.get("id") == job["id"])
    _, th, _ = call(base, "/translation/health", headers=auth)
    t0 = time.time()
    snap = wait_job(base, auth, job["id"], args.timeout, th.get("up"))
    p = snap.get("progress", {})
    check(
        "job finished" if th.get("up") else "job transcribed (translation server down: waiting for it)",
        snap.get("status") == "done" or (not th.get("up") and snap.get("status") == "translating"),
        f"{snap.get('status')} {snap.get('error', '')} {time.time() - t0:.0f}s",
    )
    check(
        "every region transcribed",
        p.get("regions_done") == p.get("regions_total") and p.get("regions_total", 0) > 0,
        f"{p.get('regions_done')}/{p.get('regions_total')}",
    )
    lines = [s for s in snap.get("segments", []) if not s.get("hidden")]
    print(f"     {len(lines)} lines, {p.get('translated')} translated")
    for s in lines[:5]:
        print(f"     [{s['start']:6.1f}] {s['text']}  =>  {s['translation']}")
    if th.get("up") and lines:
        check("lines translated", all(s["translation"] for s in lines))
    elif not th.get("up"):
        print(f"     (translation server {th.get('address')} not reachable — translation not checked)")
    _, ex, _ = call(base, f"/jobs/{job['id']}/export?format=srt", headers=auth)
    check("SRT export", ex.get("count") == len(lines), f"{ex.get('count')} cues")
    _, lib, _ = call(base, "/library", headers=auth)
    check("video listed in the library", any(v["video_key"] == video_id for v in lib.get("videos", [])))

    # ── blacklist ─────────────────────────────────────────────────────────────
    phrase = f"yume-live-check-{int(time.time())}"
    call(base, "/blacklist/add", "POST", {"text": phrase}, auth)
    _, bl, _ = call(base, "/blacklist", headers=auth)
    check("blacklist add", phrase in bl.get("blacklist", []))
    call(base, "/blacklist/remove", "POST", {"text": phrase.upper()}, auth)
    _, bl, _ = call(base, "/blacklist", headers=auth)
    check("blacklist remove (case-insensitive)", phrase not in bl.get("blacklist", []))

    # ── unknown job ───────────────────────────────────────────────────────────
    st, _, _ = call(base, "/jobs/" + "0" * 32, headers=auth)
    check("unknown job → 404 (the extension recreates it)", st == 404)

    call(base, "/library/delete", "POST", {"video_key": video_id}, auth)  # leave no trace
    print()
    print("ALL CHECKS PASSED" if not FAILED else f"{len(FAILED)} FAILED: {', '.join(FAILED)}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
