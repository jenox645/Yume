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

# Whisper models offered by Yume — the single list every menu, the benchmark,
# the launcher's resource check and the recommendation use.
# (name, parameters, approx. VRAM in MB with float16 on GPU, description)
# VRAM figures are faster-whisper's (CTranslate2), roughly half of OpenAI's
# reference implementation; int8_float16 needs ~35% less. The translation LLM
# needs its own VRAM on top when both run on the same GPU.
# Only multilingual models: *.en and distil-* are English-only.
WHISPER_MODELS = [
    ("tiny", "39M", 1000, "Fastest, low accuracy"),
    ("base", "74M", 1000, "Fast, decent accuracy"),
    ("small", "244M", 1500, "Good balance of speed and accuracy"),
    ("medium", "769M", 3000, "High accuracy, slower"),
    ("large-v2", "1550M", 4500, "Very high accuracy"),
    ("large-v3", "1550M", 4500, "Best accuracy"),
    ("large-v3-turbo", "809M", 3000, "Near-v3 accuracy at ~2x speed (best mid-range)"),
]
WHISPER_MODEL_VRAM_MB = {name: vram for name, _p, vram, _d in WHISPER_MODELS}


# detect_gpu() shells out to nvidia-smi / rocm-smi / wmic — up to several seconds
# on machines without the tool. It's called on every menu render, so cache it;
# GPUs don't get swapped between keystrokes.
_GPU_CACHE_TTL = 60.0
_gpu_cache: dict | None = None
_gpu_cache_time: float = 0.0


def detect_gpu(refresh: bool = False) -> dict:
    global _gpu_cache, _gpu_cache_time
    import time

    if not refresh and _gpu_cache is not None and time.monotonic() - _gpu_cache_time < _GPU_CACHE_TTL:
        return _gpu_cache
    _gpu_cache = _detect_gpu_uncached()
    _gpu_cache_time = time.monotonic()
    return _gpu_cache


def _detect_gpu_uncached() -> dict:
    r: dict = {"has_nvidia": False, "has_amd": False, "name": None, "vram_mb": 0, "vendor": "none"}

    # ── NVIDIA ────────────────────────────────────────────────────────────────
    try:
        from yume.utils import _run

        out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            # One line per GPU — use the one with the most VRAM
            gpus = []
            for line in out.stdout.strip().splitlines():
                name, _, mem = line.rpartition(",")
                try:
                    gpus.append((int(mem.strip()), name.strip()))
                except ValueError:
                    continue
            if gpus:
                vram, name = max(gpus)
                r.update(has_nvidia=True, name=name, vendor="nvidia", vram_mb=vram)
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

    # ── Windows AMD via CIM ───────────────────────────────────────────────────
    # (wmic is deprecated and absent on current Windows 11 builds.)
    if IS_WIN:
        for line in _win_cim("Win32_VideoController", "Name,AdapterRAM"):
            name, _, ram = line.partition("|")
            if "radeon" in name.lower() or "amd" in name.lower():
                r.update(has_amd=True, vendor="amd", name=name.strip() or "AMD GPU")
                # AdapterRAM is a uint32: it caps at 4 GB, so treat it as a minimum
                r["vram_mb"] = int(ram) // MiB if ram.strip().isdigit() else 0
                return r

    return r


def _win_cim(cls: str, props: str) -> list[str]:
    """Query a WMI/CIM class via PowerShell; one 'a|b|...' line per instance."""
    from yume.utils import _run

    fields = ",".join(f"$_.{p}" for p in props.split(","))
    cmd = f"Get-CimInstance {cls} | ForEach-Object {{ @({fields}) -join '|' }}"
    try:
        out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd], timeout=15)
        return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()] if out.returncode == 0 else []
    except Exception as e:
        _log.debug("[_win_cim] %s failed: %s", cls, e)
        return []


def _detect_cpu_name() -> str:
    """Get the CPU brand string."""
    try:
        cpuinfo = Path("/proc/cpuinfo")
        if cpuinfo.exists():
            for line in cpuinfo.read_text().splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        if IS_WIN:
            lines = _win_cim("Win32_Processor", "Name")
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
        elif vram >= 5000:
            # Not distil-large-v3: distil models are English-only and would
            # output English for the JA/ZH/KO/RU/AR audio Yume is built for.
            return "large-v3-turbo", f"GPU with {vram} MB VRAM → turbo (near-v3 accuracy, 2x faster)"
        elif vram >= 4000:
            return "small", f"GPU with {vram} MB VRAM → small (recommended)"
        elif vram >= 2000:
            return "base", f"GPU with {vram} MB VRAM → base"
        else:
            return "tiny", f"GPU with {vram} MB VRAM → tiny"
