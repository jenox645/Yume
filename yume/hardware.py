"""Hardware detection — GPU, CPU, RAM, disk."""

from __future__ import annotations

import logging
import platform
import shutil
from pathlib import Path

from yume.constants import BASE_DIR, IS_WIN, IS_MAC, MiB, GiB

_log = logging.getLogger("pocket_yume")


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
    ("large-v3", "1550M", 4500, "Slower than turbo, no more accurate on songs"),
    ("large-v3-turbo", "809M", 3000, "Best for any GPU: large-v3 accuracy at ~2x speed"),
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


_NO_GPU = {"has_nvidia": False, "has_amd": False, "name": None, "vram_mb": 0, "vendor": "none"}


def _probe(cmd: list[str]) -> str:
    """A detection tool's output; empty when it is missing or fails."""
    from yume.utils import _run

    try:
        out = _run(cmd, timeout=10)
    except Exception as e:
        _log.debug("[detect_gpu] %s failed: %s", cmd[0], e)
        return ""
    return out.stdout if out.returncode == 0 else ""


def _amd(name: str, vram_mb: int = 0) -> dict:
    return {**_NO_GPU, "has_amd": True, "vendor": "amd", "name": name, "vram_mb": vram_mb}


def _nvidia() -> dict | None:
    out = _probe(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.strip().splitlines():  # one line per GPU
        name, _, mem = line.rpartition(",")
        try:
            gpus.append((int(mem.strip()), name.strip()))
        except ValueError:
            continue
    if not gpus:
        return None
    vram, name = max(gpus)  # the one with the most VRAM
    return {**_NO_GPU, "has_nvidia": True, "vendor": "nvidia", "name": name, "vram_mb": vram}


def _rocm_vram_mb() -> int:
    # "VRAM Total Memory (B): 17163091968" — not the "VRAM Total Used Memory"
    # line after it, which the old `"Total" in line` test picked
    for line in _probe(["rocm-smi", "--showmeminfo", "vram"]).splitlines():
        if "Total Memory" in line:
            nums = [int(s) for s in line.split() if s.isdigit()]
            if nums:
                return nums[0] // MiB if nums[0] > 1_000_000 else nums[0]
    return 0


def _rocm_name(csv_row: str) -> str:
    fields = [f.strip() for f in csv_row.split(",")]
    name = fields[-1] if len(fields) > 1 else fields[0]
    if not name or name.startswith(("card", "GPU")):
        name = fields[1] if len(fields) > 1 else ""
    return "AMD GPU" if not name or name.startswith("card") else name


def _rocm_smi() -> dict | None:
    """AMD on Linux; the name is "AMD GPU" when rocm-smi does not give one."""
    out = _probe(["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--csv"])
    rows = [ln for ln in out.strip().split("\n")[1:] if ln.strip()]
    return _amd(_rocm_name(rows[0]), _rocm_vram_mb()) if rows else None


def _rocminfo_name() -> str | None:
    """The ROCm GPU's marketing name; "" without one, None without a ROCm GPU."""
    out = _probe(["rocminfo"])
    if "gfx" not in out.lower():
        return None
    return next((ln.split(":")[-1].strip() for ln in out.splitlines() if "Marketing Name" in ln), "")


def _windows_amd() -> dict | None:
    # CIM, not wmic: wmic is deprecated and absent on current Windows 11 builds
    if not IS_WIN:
        return None
    for line in _win_cim("Win32_VideoController", "Name,AdapterRAM"):
        name, _, ram = line.partition("|")
        if "radeon" in name.lower() or "amd" in name.lower():
            # AdapterRAM is a uint32: it caps at 4 GB, so treat it as a minimum
            return _amd(name.strip() or "AMD GPU", int(ram) // MiB if ram.strip().isdigit() else 0)
    return None


def _detect_gpu_uncached() -> dict:
    nvidia = _nvidia()
    if nvidia:
        return nvidia
    smi = _rocm_smi()
    if smi and smi["name"] != "AMD GPU":
        return smi
    info_name = _rocminfo_name()
    if info_name is not None:
        return _amd(info_name or (smi["name"] if smi else "AMD GPU (ROCm)"), smi["vram_mb"] if smi else 0)
    return _windows_amd() or smi or dict(_NO_GPU)


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
            from yume.utils import _run

            r = _run(["sysctl", "hw.memsize"], timeout=5)
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
        return shutil.disk_usage(p or BASE_DIR).free / GiB
    except Exception:
        return 0.0


def whisper_compute_type(vram_mb: int) -> str:
    """Whisper precision that fits in the GPU's memory."""
    if vram_mb >= 8000:
        return "float16"
    return "int8_float16" if vram_mb >= 4000 else "int8"


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
        if vram >= 5000:
            # Even with VRAM for large-v3: on two Japanese songs measured against
            # their lyrics, turbo had 23% / 21% character errors and large-v3
            # 22% / 32%, at twice the time per region. And not distil-large-v3:
            # distil models are English-only and would output English for the
            # JA/ZH/KO/RU/AR audio Yume is built for.
            return "large-v3-turbo", f"GPU with {vram} MB VRAM → turbo (large-v3 accuracy, 2x faster)"
        elif vram >= 4000:
            return "small", f"GPU with {vram} MB VRAM → small (recommended)"
        elif vram >= 2000:
            return "base", f"GPU with {vram} MB VRAM → base"
        else:
            return "tiny", f"GPU with {vram} MB VRAM → tiny"
