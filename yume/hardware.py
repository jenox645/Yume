"""Hardware detection — GPU, CPU, RAM, disk."""

from __future__ import annotations

import logging
import platform
import shutil
from pathlib import Path

_log = logging.getLogger("pocket_yume")

PLAT = platform.system()
IS_WIN = PLAT == "Windows"
IS_MAC = PLAT == "Darwin"
IS_LIN = PLAT == "Linux"

KiB = 1024
MiB = 1024**2
GiB = 1024**3


def detect_gpu() -> dict:
    r: dict = {"has_nvidia": False, "has_amd": False, "name": None, "vram_mb": 0, "vendor": "none"}

    # ── NVIDIA ────────────────────────────────────────────────────────────────
    try:
        from yume.utils import _run

        out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            p = out.stdout.strip().split(",")
            r["has_nvidia"] = True
            r["name"] = p[0].strip()
            r["vendor"] = "nvidia"
            r["vram_mb"] = int(p[1].strip()) if len(p) > 1 else 0
            return r
    except Exception as e:
        _log.debug("[detect_gpu] nvidia-smi failed: %s", e)

    # ── AMD via rocm-smi ──────────────────────────────────────────────────────
    try:
        from yume.utils import _run

        out = _run(["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--csv"], timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            lines = out.stdout.strip().split("\n")
            for line in lines[1:]:
                if line.strip():
                    r["has_amd"] = True
                    r["vendor"] = "amd"
                    fields = [f.strip() for f in line.split(",")]
                    name = fields[-1] if len(fields) > 1 else fields[0]
                    if not name or name.startswith("card") or name.startswith("GPU"):
                        name = fields[1] if len(fields) > 1 else "AMD GPU"
                    r["name"] = name if (name and not name.startswith("card")) else "AMD GPU"
                    break
            try:
                out2 = _run(["rocm-smi", "--showmeminfo", "vram"], timeout=10)
                for line2 in out2.stdout.split("\n"):
                    if "Total" in line2:
                        nums = [int(s) for s in line2.split() if s.isdigit()]
                        if nums:
                            r["vram_mb"] = nums[0] // MiB if nums[0] > 1_000_000 else nums[0]
            except Exception as e:
                _log.debug("[detect_gpu] rocm-vram-parse failed: %s", e)

            if r["name"] != "AMD GPU":
                return r
    except Exception as e:
        _log.debug("[detect_gpu] rocm-smi failed: %s", e)

    # ── AMD via rocminfo ──────────────────────────────────────────────────────
    try:
        from yume.utils import _run

        out = _run(["rocminfo"], timeout=10)
        if out.returncode == 0 and "gfx" in out.stdout.lower():
            r["has_amd"] = True
            r["vendor"] = "amd"
            for line in out.stdout.split("\n"):
                if "Marketing Name" in line:
                    r["name"] = line.split(":")[-1].strip()
                    break
            if not r["name"]:
                r["name"] = "AMD GPU (ROCm)"
            return r
    except Exception as e:
        _log.debug("[detect_gpu] rocminfo failed: %s", e)

    # ── Windows AMD via WMI ───────────────────────────────────────────────────
    if IS_WIN:
        try:
            from yume.utils import _run

            out = _run(["wmic", "path", "win32_videocontroller", "get", "name,adapterram", "/format:csv"], timeout=10)
            for line in out.stdout.strip().split("\n"):
                ll = line.lower()
                if "radeon" in ll or "amd" in ll:
                    parts = line.split(",")
                    r["has_amd"] = True
                    r["vendor"] = "amd"
                    r["name"] = parts[2].strip() if len(parts) > 2 else "AMD GPU"
                    try:
                        r["vram_mb"] = (
                            int(parts[1].strip()) // MiB if len(parts) > 1 and parts[1].strip().isdigit() else 0
                        )
                    except Exception as e:
                        _log.debug("[detect_gpu] wmic-amd-vram-parse failed: %s", e)
                    return r
        except Exception as e:
            _log.debug("[detect_gpu] wmic-amd failed: %s", e)

    return r


def _detect_cpu_name() -> str:
    """Get the CPU brand string."""
    try:
        cpuinfo = Path("/proc/cpuinfo")
        if cpuinfo.exists():
            for line in cpuinfo.read_text().splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        if IS_WIN:
            from yume.utils import _run

            r = _run(["wmic", "cpu", "get", "name"], timeout=5)
            lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip() and ln.strip() != "Name"]
            if lines:
                return lines[0]
        if IS_MAC:
            from yume.utils import _run

            r = _run(["sysctl", "-n", "machdep.cpu.brand_string"], timeout=5)
            if r.stdout.strip():
                return r.stdout.strip()
    except Exception:
        pass
    return platform.processor() or "Unknown CPU"


def detect_ram_gb() -> float:
    try:
        if IS_WIN:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                ] + [("_" + str(i), ctypes.c_ulonglong) for i in range(6)]

            s = MS()
            s.dwLength = ctypes.sizeof(s)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
            return s.ullTotalPhys / GiB
        elif IS_MAC:
            import subprocess as _sp
            r = _sp.run(["sysctl", "hw.memsize"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return int(r.stdout.split(":")[1].strip()) / GiB
        else:
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) / MiB
    except Exception as e:
        _log.debug("[detect_ram_gb] failed: %s", e)
    return 0.0


def disk_free_gb(p=None) -> float:
    try:
        from pathlib import Path as _Path

        base = _Path(__file__).parent.parent
        return shutil.disk_usage(p or base).free / GiB
    except Exception:
        return 0.0


def recommend_whisper_model(gpu_info: dict | None = None) -> tuple[str, str]:
    """Return (model_name, reason) for the given hardware."""
    if gpu_info is None:
        gpu_info = detect_gpu()
    vram = gpu_info.get("vram_mb", 0)
    has_gpu = gpu_info.get("has_nvidia") or gpu_info.get("has_amd")
    ram = detect_ram_gb()

    if not has_gpu:
        if ram >= 16:
            return "small", "CPU with 16+ GB RAM → small model (best CPU balance)"
        elif ram >= 8:
            return "base", "CPU with 8-16 GB RAM → base model"
        else:
            return "tiny", "CPU with <8 GB RAM → tiny model (fastest)"
    else:
        if vram >= 10000:
            return "large-v3", f"GPU with {vram} MB VRAM → large-v3 (best accuracy)"
        elif vram >= 7000:
            return "large-v3-turbo", f"GPU with {vram} MB VRAM → turbo (near-v3 accuracy, 2x faster)"
        elif vram >= 5000:
            return "distil-large-v3", f"GPU with {vram} MB VRAM → distil-large-v3 (fast + accurate)"
        elif vram >= 4000:
            return "small", f"GPU with {vram} MB VRAM → small (recommended)"
        elif vram >= 2000:
            return "base", f"GPU with {vram} MB VRAM → base"
        else:
            return "tiny", f"GPU with {vram} MB VRAM → tiny"
