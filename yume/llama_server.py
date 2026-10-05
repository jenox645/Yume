"""llama.cpp's own prebuilt server (llama-server) as the translation engine.

llama-cpp-python only runs on the GPU when it was compiled with CUDA, and
prebuilt CUDA wheels lag behind new Python versions — users end up with a
CPU-only build (~9 tokens/s on a 7B model, ~25 s per batch of subtitle lines).
llama.cpp publishes ready-to-run GPU builds for every platform with each
release; this module installs one into tools/llama.cpp/ and Yume prefers it
over llama-cpp-python. Same GGUF files, same OpenAI-compatible API.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

from yume.utils import TOOLS_DIR, _run

_log = logging.getLogger("pocket_yume")

LLAMA_DIR = TOOLS_DIR / "llama.cpp"
RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=15"
EXE_NAME = "llama-server.exe" if sys.platform == "win32" else "llama-server"
BUILD_FILE = LLAMA_DIR / "build.json"  # {"tag", "variant"} of the installed build


def server_path() -> Path | None:
    """The installed llama-server executable, or None."""
    if not LLAMA_DIR.is_dir():
        return None
    for p in sorted(LLAMA_DIR.rglob(EXE_NAME)):
        if p.is_file():
            return p
    return None


def installed_build() -> dict:
    try:
        return json.loads(BUILD_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def driver_cuda_version() -> tuple[int, int] | None:
    """Highest CUDA version the NVIDIA driver supports, from nvidia-smi's header
    ("CUDA Version: 12.8", or "CUDA UMD Version: 13.3" on newer drivers)."""
    try:
        r = _run(["nvidia-smi"], timeout=15)
    except Exception:
        return None
    m = re.search(r"CUDA (?:UMD )?Version:\s*(\d+)\.(\d+)", r.stdout or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def _os_arch() -> tuple[str, str]:
    os_name = {"win32": "win", "darwin": "macos"}.get(sys.platform, "ubuntu")
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    return os_name, arch


def pick_assets(assets: list[dict], gpu: dict, cuda: tuple[int, int] | None) -> tuple[str, list[dict]]:
    """(variant, [assets to download]) for this machine from one release's assets.

    NVIDIA: the newest CUDA build the driver supports (a build newer than the
    driver fails to start) plus its CUDA runtime. AMD and Intel GPUs: Vulkan.
    macOS: the Metal build. Otherwise the CPU build."""
    os_name, arch = _os_arch()
    by_name = {a["name"]: a for a in assets}

    def find(pattern):
        rx = re.compile(pattern)
        return [a for n, a in by_name.items() if rx.fullmatch(n)]

    ext = r"\.zip" if os_name == "win" else r"\.tar\.gz"
    if os_name == "macos":
        found = find(rf"llama-b\d+-bin-macos-{arch}{ext}")
        return ("metal" if arch == "arm64" else "cpu"), found[:1]

    if gpu.get("has_nvidia") and cuda:
        builds = []
        for a in find(rf"llama-b\d+-bin-{os_name}-cuda-(\d+)\.(\d+)-{arch}{ext}"):
            m = re.search(r"cuda-(\d+)\.(\d+)", a["name"])
            ver = (int(m.group(1)), int(m.group(2)))
            if ver <= cuda:
                builds.append((ver, a))
        if builds:
            ver, main = max(builds, key=lambda b: b[0])
            tag = f"{ver[0]}.{ver[1]}"
            runtime = find(rf"cudart-llama-(?:b\d+-)?bin-{os_name}-cuda-{re.escape(tag)}-{arch}{ext}")
            if runtime:
                return f"cuda-{tag}", [main, runtime[0]]
    if gpu.get("has_nvidia") or gpu.get("has_amd") or gpu.get("has_intel"):
        found = find(rf"llama-b\d+-bin-{os_name}-vulkan-{arch}{ext}")
        if found:
            return "vulkan", found[:1]
    cpu = rf"llama-b\d+-bin-{os_name}-cpu-{arch}{ext}" if os_name == "win" else rf"llama-b\d+-bin-{os_name}-{arch}{ext}"
    return "cpu", find(cpu)[:1]


def _releases() -> list[dict]:
    req = urllib.request.Request(RELEASES_API, headers={"Accept": "application/vnd.github+json", "User-Agent": "Yume"})
    with urllib.request.urlopen(req, timeout=30) as r:  # nosec B310 — fixed GitHub API URL
        return json.loads(r.read())


def _extract(archive: Path, dest: Path) -> None:
    """Extract a zip or tar.gz, refusing members that would land outside dest."""
    root = dest.resolve()

    def inside(name):
        target = (root / name).resolve()
        return target == root or root in target.parents

    if archive.suffix == ".zip":
        from yume.installers import _safe_extractall

        with zipfile.ZipFile(archive) as zf:
            _safe_extractall(zf, dest)  # nosec B202 — zip-slip checked inside
    else:
        with tarfile.open(archive, "r:gz") as tf:
            members = [m for m in tf.getmembers() if (m.isfile() or m.isdir() or m.issym()) and inside(m.name)]
            for m in members:
                if m.issym() and not inside(os.path.join(os.path.dirname(m.name), m.linkname)):
                    raise ValueError(f"unsafe link in archive: {m.name!r}")
            tf.extractall(dest, members=members)  # nosec B202 — members filtered above


def install(gpu: dict | None = None) -> bool:
    """Download the right llama-server build into tools/llama.cpp/."""
    from yume.hardware import detect_gpu
    from yume.network import download_file
    from yume.ui import error, info, success, warn

    gpu = gpu or detect_gpu()
    cuda = driver_cuda_version() if gpu.get("has_nvidia") else None
    try:
        releases = _releases()
    except Exception as e:
        error(f"Could not reach GitHub to find llama.cpp builds: {e}")
        return False
    # Numbered builds (bNNNN) are published as pre-releases; take the newest
    # one that has a build for this machine.
    for rel in releases:
        if not re.fullmatch(r"b\d+", rel.get("tag_name", "")):
            continue
        variant, assets = pick_assets(rel.get("assets", []), gpu, cuda)
        if assets:
            break
    else:
        error("No llama.cpp build found for this system.")
        return False

    size_mb = sum(a["size"] for a in assets) / 1e6
    info(f"llama.cpp {rel['tag_name']} ({variant}) — {size_mb:.0f} MB to download")
    if variant == "cpu" and (gpu.get("has_nvidia") or gpu.get("has_amd")):
        warn("No GPU build matches this system — installing the CPU build.")
    tmp = LLAMA_DIR.with_name("llama.cpp.new")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        for a in assets:
            archive = TOOLS_DIR / a["name"]
            digest = (a.get("digest") or "").removeprefix("sha256:") or None
            if not download_file(a["browser_download_url"], archive, a["name"], sha256=digest):
                return False
            try:
                _extract(archive, tmp)
            finally:
                archive.unlink(missing_ok=True)
        # Linux/macOS archives put everything under a build/bin folder
        exe = next((p for p in tmp.rglob(EXE_NAME) if p.is_file()), None)
        if exe is None:
            error(f"{EXE_NAME} not found in the downloaded build.")
            return False
        if sys.platform != "win32":
            for p in exe.parent.iterdir():
                if p.is_file():
                    p.chmod(p.stat().st_mode | 0o111)
        # The CUDA runtime DLLs must sit next to llama-server.exe
        if variant.startswith("cuda") and exe.parent != tmp:
            for p in tmp.glob("*.dll"):
                shutil.move(str(p), exe.parent / p.name)
        (tmp / "build.json").write_text(json.dumps({"tag": rel["tag_name"], "variant": variant}), encoding="utf-8")
        # Swap folders: move the old build aside first, so a failure (Windows
        # locks the files of a running llama-server) leaves it untouched
        old = LLAMA_DIR.with_name("llama.cpp.old")
        shutil.rmtree(old, ignore_errors=True)
        try:
            if LLAMA_DIR.exists():
                LLAMA_DIR.replace(old)
            tmp.replace(LLAMA_DIR)
        except OSError as e:
            if old.exists() and not LLAMA_DIR.exists():
                old.replace(LLAMA_DIR)
            error(f"Could not replace the installed build ({e}) — stop Yume first, then try again.")
            return False
        shutil.rmtree(old, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    r = _run([str(server_path()), "--version"], timeout=30)
    version = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
    if r.returncode != 0:
        error(f"llama-server does not start: {version[-1] if version else r.returncode}")
        return False
    success(f"llama-server installed ({variant}): {next((v for v in version if 'version' in v), rel['tag_name'])}")
    return True


def command(gguf_path: str, host: str, port: int, gpu_layers: bool) -> list[str]:
    exe = server_path()
    cmd = [
        str(exe), "--model", gguf_path, "--host", host, "--port", str(port),
        # one request at a time with the whole context (the default splits the
        # context across 4 parallel slots); prompt caching is on by default
        "--ctx-size", "4096", "--parallel", "1",
        "--no-webui",
    ]  # fmt: skip
    if gpu_layers:
        cmd += ["--n-gpu-layers", "999"]
    return cmd
