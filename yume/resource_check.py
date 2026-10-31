"""Pre-launch check: will Whisper and the translation model fit in free VRAM and RAM?"""

from __future__ import annotations

from pathlib import Path

from yume.hardware import detect_gpu
from yume.ui import C, ask_arrow, info, panel, warn
from yume.utils import _run


def _get_available_ram_mb() -> int:
    """Return available RAM in MB. Returns 0 if detection fails."""
    try:
        import psutil  # type: ignore[import]

        return int(psutil.virtual_memory().available / (1024 * 1024))
    except ImportError:
        pass
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def _get_available_vram_mb() -> int:
    """Return free VRAM in MB for the primary NVIDIA GPU. Returns 0 if detection fails."""
    try:
        import pynvml  # type: ignore[import]

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        pynvml.nvmlShutdown()
        return int(mem.free / (1024 * 1024))
    except Exception:
        pass
    try:
        out = _run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"], timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return int(out.stdout.strip().split("\n")[0].strip())
    except Exception:
        pass
    return 0


def _whisper_vram_mb(cfg: dict, gpu: dict) -> int:
    """VRAM the Whisper model needs on an NVIDIA GPU; 0 when it does not run on one."""
    if not gpu.get("has_nvidia") or cfg.get("whisper_device", "auto") not in ("auto", "cuda"):
        return 0
    from yume.hardware import WHISPER_MODEL_VRAM_MB

    needed = WHISPER_MODEL_VRAM_MB.get(cfg.get("whisper_model", "large-v3-turbo"), 4_500)  # custom: assume large
    if cfg.get("whisper_compute_type", "auto") in ("int8", "int8_float16"):
        needed = int(needed * 0.65)
    return needed


def _resource_issues(cfg: dict) -> list[str]:
    """What will not fit: Whisper in free VRAM, and the GGUF in VRAM (with Whisper) or RAM."""
    gpu = detect_gpu()
    issues = []
    whisper_mb = _whisper_vram_mb(cfg, gpu)
    free_vram = _get_available_vram_mb() if gpu.get("has_nvidia") or gpu.get("has_amd") else 0
    if whisper_mb and 0 < free_vram < whisper_mb:
        issues.append(
            f"Whisper '{cfg.get('whisper_model', 'large-v3-turbo')}' needs ~{whisper_mb / 1024:.1f} GB VRAM, "
            f"but only {free_vram / 1024:.1f} GB is free."
        )

    gp = cfg.get("gguf_model_path", "")
    if cfg.get("translation_backend", "llamacpp") != "llamacpp" or not gp or not Path(gp).exists():
        return issues
    # llama.cpp offloads every layer when there is a GPU, so the model lives in
    # VRAM next to Whisper; only without a GPU does it need system RAM.
    gguf_mb = int(Path(gp).stat().st_size / (1024 * 1024) * 1.2)
    if gpu.get("has_nvidia") or gpu.get("has_amd"):
        if 0 < free_vram < gguf_mb + whisper_mb:
            both = f" (plus ~{whisper_mb / 1024:.1f} GB for Whisper)" if whisper_mb else ""
            issues.append(
                f"Translation model ({Path(gp).name}) needs ~{gguf_mb / 1024:.1f} GB VRAM{both}, "
                f"but only {free_vram / 1024:.1f} GB is free."
            )
    else:
        free_ram = _get_available_ram_mb()
        if 0 < free_ram < gguf_mb:
            issues.append(
                f"Translation model ({Path(gp).name}) needs ~{gguf_mb / 1024:.1f} GB RAM, "
                f"but only {free_ram / 1024:.1f} GB is available."
            )
    return issues


def check_resources(cfg: dict) -> bool:
    """Pre-launch RAM/VRAM check. Returns True to proceed, False to cancel.

    Never hard-blocks — the user can always choose 'Launch anyway'.
    """
    while True:
        issues = _resource_issues(cfg)
        if not issues:
            return True
        print()
        panel(
            "\n".join(f"  • {msg}" for msg in issues),
            title=f"{C.YELLOW}Low Resources Warning{C.RESET}",
            style=C.YELLOW,
        )
        print()
        info("Launching with low resources may cause crashes or heavy swap usage.")
        print()
        ch = ask_arrow(
            "How would you like to proceed?",
            [
                ("Launch anyway", "Override — proceed despite the warning"),
                ("Retry check", "Close other apps to free resources, then re-check"),
                ("Switch Whisper model", "Pick a smaller Whisper model that fits available VRAM/RAM"),
                ("Cancel", "Return to main menu without launching"),
            ],
            default=0,
            allow_back=False,
        )
        if ch == 0:
            warn("Launching with low resources — watch logs if servers crash.")
            return True
        if ch == 1:
            info("Re-checking resources — close unused apps first, then press Enter.")
            try:
                input(f"  {C.DIM}Press Enter when ready...{C.RESET}")
            except (EOFError, KeyboardInterrupt):
                print()
        elif ch == 2:
            from yume.menus import _menu_whisper_model

            _menu_whisper_model(cfg)
        else:
            return False
