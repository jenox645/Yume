"""Whisper model benchmarking."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from yume.hardware import IS_WIN, WHISPER_MODELS, detect_gpu, recommend_whisper_model
from yume.ui import C, ask_input, ask_yn, error, header, info, pause, section, success, table, warn
from yume.utils import LOGS_DIR, find_tool, _run

_log = logging.getLogger("pocket_yume")

WHISPER_SAMPLE_RATE = 16000
# Whisper always processes 30 s windows: a 5 s clip costs a whole window, so
# timing 5 s of audio made every model look ~6x slower than it is.
TEST_SECONDS = 30

# Accuracy order (most accurate first) for the post-benchmark suggestion
_ACCURACY_RANK = ["large-v3", "large-v2", "large-v3-turbo", "medium", "small", "base", "tiny"]

# (name, params, vram label, description) — derived from the shared table
WHISPER_MODELS_INFO = [(n, f"{p} params", f"~{v / 1000:g} GB", d) for n, p, v, d in WHISPER_MODELS]


def _is_whisper_model_cached(model_name: str) -> bool:
    """Check if a Whisper model is already downloaded in the HuggingFace cache."""
    try:
        from yume.utils import hf_hub_dir

        cache_dir = hf_hub_dir()
        if not cache_dir.exists():
            return False
        # Exact names: a substring test reported "large-v3" as downloaded when
        # only "large-v3-turbo" was
        names = {
            n.lower()
            for n in (
                f"models--Systran--faster-whisper-{model_name}",
                f"models--guillaumekln--faster-whisper-{model_name}",
                f"models--mobiuslabsgmbh--faster-whisper-{model_name}",
            )
        }
        if any(entry.name.lower() in names for entry in cache_dir.iterdir()):
            return True
        ct2_cache = Path.home() / ".cache" / "faster_whisper"
        return ct2_cache.exists() and any(entry.name == model_name for entry in ct2_cache.iterdir())
    except Exception:
        return False


# Error text that means CUDA's libraries are missing or broken, not the model
_CUDA_ERROR_MARKERS = (
    "cublas",
    "cudnn",
    "cudart",
    "cufft",
    "cusolver",
    "cusparse",
    "cuda",
    "is not found or cannot be loaded",
)


def _make_test_audio(path: Path) -> bool:
    """Write TEST_SECONDS of tone to `path` (ffmpeg, else the wave module)."""
    # Always regenerate: a leftover file from an older version may have a different length
    path.unlink(missing_ok=True)
    ffmpeg = find_tool("ffmpeg")
    if ffmpeg:
        tone = f"sine=frequency=300:duration={TEST_SECONDS}"
        cmd = [ffmpeg, "-y", "-f", "lavfi", "-i", tone, "-ar", str(WHISPER_SAMPLE_RATE), "-ac", "1", str(path)]
        try:
            _run(cmd, timeout=10)
        except Exception as e:
            _log.debug("[benchmark_whisper] test-audio-gen failed: %s", e)
    if path.exists():
        return True
    try:
        import math
        import struct
        import wave

        sr = WHISPER_SAMPLE_RATE
        samples = [
            int((0.5 * math.sin(2 * math.pi * 300 * i / sr) + 0.3 * math.sin(2 * math.pi * 600 * i / sr)) * 32767)
            for i in range(sr * TEST_SECONDS)
        ]
        with wave.open(str(path), "w") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))
        success(f"Generated test audio ({TEST_SECONDS}s)")
        return True
    except Exception as e:
        error(f"Could not create test audio: {e}")
        return False


def _fits(gpu: dict, req_mb: int) -> bool:
    """Whether a model needing `req_mb` should run here (GPU memory, or ≤ 5 GB on CPU)."""
    if gpu.get("has_nvidia") or gpu.get("has_amd"):
        vram = gpu.get("vram_mb", 0)
        return not (vram > 0 and req_mb > vram)
    return req_mb <= 5120


def _parse_selection(selection: str, models: list[tuple[str, bool]], rec: str) -> list[str]:
    """'all' (those that fit), 'rec', or comma-separated 1-based numbers into `models` (name, fits)."""
    if selection == "all":
        return [name for name, fits in models if fits]
    if selection == "rec":
        return [rec]
    picked = []
    for part in selection.split(","):
        part = part.strip()
        if part.isdigit() and 0 <= int(part) - 1 < len(models):
            picked.append(models[int(part) - 1][0])
    return picked


def _choose_models(cfg: dict, gpu: dict, rec: str) -> list[str]:
    """List the models with fit/download status and ask which to benchmark."""
    models = [(name, _fits(gpu, req_mb)) for name, _p, req_mb, _d in WHISPER_MODELS]
    info("Select models to benchmark:")
    info("  'distil' models are distilled on English only — they cannot transcribe JA/ZH/KO/RU/AR")
    info("  'turbo' = optimized large-v3 (near-full accuracy at much higher speed)")
    info("  VRAM = your GPU's video memory — models marked [fits] will work on your hardware")
    print()
    for i, ((name, _params, vr, desc), (_n, fits)) in enumerate(zip(WHISPER_MODELS_INFO, models)):
        tag = f"{C.GREEN}fits{C.RESET}" if fits else f"{C.RED}may OOM{C.RESET}"
        cached = _is_whisper_model_cached(name)
        dl_tag = f"{C.GREEN}downloaded{C.RESET}" if cached else f"{C.DIM}not downloaded{C.RESET}"
        cur = f" {C.GOLD}<- current{C.RESET}" if name == cfg.get("whisper_model") else ""
        print(f"    {i + 1:2d}. {name:22s} {vr:8s} [{tag}] [{dl_tag}]{cur}")
        print(f"        {C.DIM}{desc}{C.RESET}")
    print()
    selection = ask_input(
        "Enter model numbers (comma-separated) or 'rec' for recommended, 'all' for all that fit", "rec"
    )
    return _parse_selection(selection.strip().lower(), models, rec)


def _device_and_precision(gpu: dict) -> tuple[str, str]:
    if gpu.get("has_nvidia"):
        device = "cuda"
    elif gpu.get("has_amd") and not IS_WIN:
        device = "auto"
        info("AMD GPU detected — benchmarking via ROCm/auto")
    else:
        return "cpu", "int8"
    vram_mb = gpu.get("vram_mb", 0)
    return device, "float16" if vram_mb >= 8000 else "int8_float16" if vram_mb >= 4000 else "int8"


def _time_model(model_name: str, device: str, compute: str, test_wav: Path, where: str = "") -> dict:
    """Load a model and time 3 transcriptions of the test audio; raises on failure."""
    import gc
    import warnings

    from faster_whisper import WhisperModel

    warnings.filterwarnings("ignore", message=".*huggingface.*")
    warnings.filterwarnings("ignore", message=".*symlinks.*")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

    t0 = time.time()
    model = WhisperModel(model_name, device=device, compute_type=compute)
    load_time = time.time() - t0
    success(f"Loaded{where} in {load_time:.1f}s")

    info(f"{C.DIM}Running 3 transcription passes ({TEST_SECONDS}s test audio each)...{C.RESET}")
    times = []
    for _ in range(3):
        t0 = time.time()
        segs, _info = model.transcribe(str(test_wav), language="ja", vad_filter=False, word_timestamps=False)
        list(segs)
        times.append(time.time() - t0)
    median_time = sorted(times)[len(times) // 2]
    speed = TEST_SECONDS / median_time if median_time > 0 else 0
    success(f"Median{where}: {median_time:.3f}s for {TEST_SECONDS}s audio ({speed:.1f}x realtime)")

    del model
    try:
        gc.collect()
        if device == "cuda":
            import torch

            torch.cuda.empty_cache()
    except Exception as e:
        _log.debug("[benchmark_whisper] cuda-cleanup failed: %s", e)
    return {"load_s": round(load_time, 1), "median_s": round(median_time, 3), "speed_x": round(speed, 1)}


def _benchmark_one(model_name: str, device: str, compute: str, test_wav: Path) -> dict:
    """One model's result row; offers a CPU retry when CUDA's libraries are missing."""
    try:
        return {"model": model_name, "status": "OK", **_time_model(model_name, device, compute, test_wav)}
    except Exception as e:
        err_msg = str(e)
        error(f"Failed: {err_msg[:80]}")
    failed = {"model": model_name, "status": f"FAIL: {err_msg[:80]}"}
    if device not in ("cuda", "auto") or not any(m in err_msg.lower() for m in _CUDA_ERROR_MARKERS):
        return failed

    print()
    warn("CUDA libraries are missing or incomplete.")
    info("This usually means the NVIDIA CUDA Toolkit isn't fully installed.")
    if IS_WIN:
        info("Fix: Install CUDA Toolkit from https://developer.nvidia.com/cuda-downloads")
        info("     Or install cuBLAS via: pip install nvidia-cublas-cu12")
    else:
        info("Fix: Install CUDA Toolkit for your distro, or:")
        info("     pip install nvidia-cublas-cu12")
    if not ask_yn("Retry this model on CPU instead?", default=True):
        return failed
    info(f"Retrying {model_name} on CPU...")
    try:
        timing = _time_model(model_name, "cpu", "int8", test_wav, " on CPU")
        return {"model": f"{model_name} (CPU)", "status": "OK", **timing}
    except Exception as e2:
        error(f"CPU fallback also failed: {str(e2)[:60]}")
        return failed


def _show_results(cfg: dict, results: list[dict], device: str, compute: str, gpu: dict) -> None:
    """Results table, how to read it, and an offer to switch to the best model."""
    from config import save_config

    print()
    section("Benchmark Results")
    info(f"Device: {device} | Precision: {compute} | Test: {TEST_SECONDS}s audio, 3 runs (median)")
    if gpu.get("has_nvidia"):
        info(f"GPU: {gpu['name']} ({gpu['vram_mb']} MB VRAM)")
    print()

    ok_results = [r for r in results if r["status"] == "OK"]
    best_speed = max((r["speed_x"] for r in ok_results), default=0)
    rows = []
    for r in results:
        if r["status"] != "OK":
            rows.append([r["model"], "-", "-", "-", f"{C.RED}{r['status']}{C.RESET}"])
            continue
        bar_len = min(25, int(r["speed_x"] / max(best_speed, 1) * 25))
        speed_bar = f"{C.GREEN}{'█' * bar_len}{C.DIM}{'░' * (25 - bar_len)}{C.RESET}"
        is_best = f" {C.GOLD}★{C.RESET}" if r["speed_x"] == best_speed and len(results) > 1 else ""
        rows.append([r["model"], f"{r['load_s']}s", f"{r['median_s']:.3f}s", f"{r['speed_x']}x{is_best}", speed_bar])
    table(
        ["Model", "Load", f"{TEST_SECONDS}s Audio", "Speed", ""],
        rows,
        col_styles=[C.CYAN, C.RESET, C.RESET, C.GREEN, C.RESET],
        title="Whisper Benchmark",
    )
    if not ok_results:
        return

    fastest = min(ok_results, key=lambda r: r["median_s"])
    print()
    info("How to read the results:")
    info(f"  {C.DIM}Speed = how many times faster than real-time. Higher is better.{C.RESET}")
    info(f"  {C.DIM}10x = 1 minute of audio transcribed in 6 seconds{C.RESET}")
    info(f"  {C.DIM}50x = 1 minute of audio transcribed in ~1 second{C.RESET}")
    print()
    success(f"Fastest: {C.BOLD}{fastest['model']}{C.RESET} at {C.GREEN}{fastest['speed_x']}x{C.RESET} realtime")
    if fastest["speed_x"] > 0:
        info(f"  {C.DIM}→ A 4-minute song would be transcribed in ~{240 / fastest['speed_x']:.1f} seconds{C.RESET}")

    # Suggest the MOST ACCURATE model that is still comfortably real-time,
    # not the fastest one (that is always tiny, the least accurate).
    usable = [r["model"] for r in ok_results if r["speed_x"] >= 5 and r["model"] in _ACCURACY_RANK]
    pick = min(usable, key=_ACCURACY_RANK.index) if usable else None
    if pick and pick != cfg.get("whisper_model"):
        print()
        info(f"Most accurate model running at 5x+ realtime here: {C.BOLD}{pick}{C.RESET}")
        if ask_yn(f"Switch to {pick}?", default=False):
            cfg["whisper_model"] = pick
            save_config(cfg)
            success(f"Config updated to {pick}")


def _confirm_start(models: list[str]) -> bool:
    info(f"Benchmarking {len(models)} model(s): {', '.join(models)}")
    needs_download = [m for m in models if not _is_whisper_model_cached(m)]
    if needs_download:
        warn(f"These models will be downloaded first: {', '.join(needs_download)}")
        return ask_yn("Download and benchmark?", True)
    info("All selected models are already downloaded (cached)")
    return ask_yn("Start benchmark?", True)


def benchmark_whisper(cfg: dict) -> None:
    """Compare Whisper model speeds on this hardware."""
    header("Whisper Benchmark")
    info("This measures how fast each speech recognition model runs on your hardware.")
    info(f"{C.DIM}Yume processes audio in chunks. Faster models = subtitles appear sooner.{C.RESET}")
    info(f"{C.DIM}A model running at '10x realtime' transcribes 1 minute of audio in 6 seconds.{C.RESET}")
    print()

    try:
        import faster_whisper  # noqa: F401

        success("faster-whisper found")
    except ImportError:
        error("faster-whisper not installed. Run: pip install faster-whisper")
        pause()
        return

    gpu = detect_gpu()
    if gpu["has_nvidia"]:
        info(f"GPU: {gpu['name']} ({gpu['vram_mb']} MB VRAM)")
    elif gpu.get("has_amd"):
        info(f"GPU: {gpu['name']} ({gpu.get('vram_mb', '?')} MB VRAM, ROCm)")
    else:
        info("No GPU detected — benchmarking in CPU mode")
    rec, reason = recommend_whisper_model(gpu)
    info(f"Recommended: {rec} ({reason})")
    print()

    test_wav = LOGS_DIR / "_benchmark_test.wav"
    if not _make_test_audio(test_wav):
        pause()
        return
    try:
        models = _choose_models(cfg, gpu, rec)
        if not models:
            warn("No models selected")
            pause()
            return
        if not _confirm_start(models):
            return
        device, compute = _device_and_precision(gpu)
        results = []
        for idx, model_name in enumerate(models):
            section(f"Testing {idx + 1}/{len(models)}: {model_name}")
            if not _is_whisper_model_cached(model_name):
                info(f"{C.DIM}Downloading model... (this may take a few minutes the first time){C.RESET}")
            info(f"Device: {device} | Precision: {compute}")
            results.append(_benchmark_one(model_name, device, compute, test_wav))
    finally:
        test_wav.unlink(missing_ok=True)
    _show_results(cfg, results, device, compute, gpu)
    pause()
