"""`pocket_yume.py status`: hardware, tools, packages, servers, models, ports."""

from __future__ import annotations

from pathlib import Path

from yume.constants import ARCH, GiB, HEALTH_PATH_OPENAI, PLAT, backend_info
from yume.hardware import detect_gpu, detect_ram_gb, disk_free_gb
from yume.health import tools_menu_path
from yume.network import check_server
from yume.ports import port_owners
from yume.ui import C, bullet, header, info, section, table, warn
from yume.utils import _try_import, find_gguf_models, find_tool


def _status_hardware() -> None:
    gpu, ram, disk = detect_gpu(), detect_ram_gb(), disk_free_gb()
    if gpu["has_nvidia"]:
        gpu_txt = f"{C.GREEN}{gpu['name']} ({gpu['vram_mb']} MB){C.RESET}"
    elif gpu["has_amd"]:
        vr = f" ({gpu['vram_mb']} MB)" if gpu["vram_mb"] else ""
        gpu_txt = f"{C.CYAN}{gpu['name']}{vr}{C.RESET}"
    else:
        gpu_txt = f"{C.YELLOW}None (CPU mode){C.RESET}"
    table(
        ["Component", "Status"],
        [
            ["GPU", gpu_txt],
            ["RAM", f"{ram:.1f} GB" + (f"  {C.GREEN}OK{C.RESET}" if ram >= 8 else f"  {C.YELLOW}low{C.RESET}")],
            ["Disk", f"{disk:.1f} GB free" + (f"  {C.GREEN}OK{C.RESET}" if disk >= 10 else f"  {C.RED}low!{C.RESET}")],
            ["OS", f"{PLAT} ({ARCH})"],
        ],
        col_styles=[C.BOLD, C.RESET],
        title="Hardware",
    )


def _status_tools(cfg: dict) -> None:
    rows = []
    for t in ["yt-dlp", "ffmpeg", "deno"]:
        p = find_tool(t)
        if p:
            rows.append([t, f"{C.GREEN}installed{C.RESET}", p])
        elif t == "deno" and cfg.get("youtube_auth_method") == "cookies":
            rows.append([t, f"{C.DIM}skipped{C.RESET}", "using cookies"])
        else:
            rows.append([t, f"{C.RED}MISSING{C.RESET}", "run Tools menu to install"])
    table(["Tool", "Status", "Path"], rows, col_styles=[C.CYAN, C.RESET, C.DIM], title="Tools")


def _status_packages(cfg: dict) -> None:
    from yume import llama_server

    packages = [("faster_whisper", "faster-whisper"), ("flask", "Flask"), ("waitress", "Waitress")]
    # llama-cpp-python only matters when llama.cpp translates without llama-server
    if cfg.get("translation_backend", "llamacpp") == "llamacpp" and not llama_server.server_path():
        packages.append(("llama_cpp", "llama-cpp-python"))
    rows = [
        [display, f"{C.GREEN}✓{C.RESET}" if _try_import(pkg) else f"{C.RED}✗  not installed{C.RESET}"]
        for pkg, display in packages
    ]
    table(["Package", "Status"], rows, col_styles=[C.CYAN, C.RESET], title="Python Packages")


def _status_servers(cfg: dict) -> None:
    bi = backend_info(cfg.get("translation_backend", "llamacpp"))
    ws = check_server(cfg["whisper_host"], cfg["whisper_port"], "/health")
    ts = check_server(cfg["translation_host"], cfg["translation_port"], bi["hp"])
    if not ts["up"]:
        ts = check_server(cfg["translation_host"], cfg["translation_port"], HEALTH_PATH_OPENAI)

    def state(up: bool) -> str:
        return f"{C.GREEN}● running{C.RESET}" if up else f"{C.DIM}○ stopped{C.RESET}"

    table(
        ["Server", "Address", "Status"],
        [
            ["Whisper", f"{cfg['whisper_host']}:{cfg['whisper_port']}", state(ws["up"])],
            [bi["name"], f"{cfg['translation_host']}:{cfg['translation_port']}", state(ts["up"])],
        ],
        col_styles=[C.CYAN, C.DIM, C.RESET],
        title="Servers",
    )


def _status_models(cfg: dict) -> None:
    from yume.benchmark import _is_whisper_model_cached
    from yume.utils import hf_hub_dir

    section("Models")
    wm = cfg.get("whisper_model", "large-v3")
    if Path(wm).is_dir():
        info(f"Whisper:      {C.BOLD}{Path(wm).name}{C.RESET}  {C.DIM}(custom: {wm}){C.RESET}")
    elif _is_whisper_model_cached(wm):
        info(f"Whisper:      {C.BOLD}{wm}{C.RESET}")
        info(f"              {C.GREEN}downloaded{C.RESET}  {C.DIM}{hf_hub_dir()}{C.RESET}")
    else:
        info(f"Whisper:      {C.BOLD}{wm}{C.RESET}")
        info(f"              {C.YELLOW}not downloaded{C.RESET}  (will download on first use)")
        info(f"              {C.DIM}Cache: {hf_hub_dir()}{C.RESET}")

    gp = cfg.get("gguf_model_path", "")
    ggufs = find_gguf_models()
    if gp and Path(gp).exists():
        info(f"Translation:  {C.BOLD}{Path(gp).name}{C.RESET}  {C.DIM}({gp}){C.RESET}")
    elif ggufs:
        info(f"Translation:  {len(ggufs)} GGUF file(s) in models/translation/")
        for f in ggufs:
            bullet(f"{f.name} ({f.stat().st_size / GiB:.2f} GB)")
    else:
        info(f"Translation:  {C.DIM}no GGUF models found{C.RESET}")
        if cfg.get("translation_backend", "llamacpp") == "llamacpp":
            info(f"              Download one: {tools_menu_path()} → Download Translation Model")


def _status_ports(cfg: dict) -> None:
    section("Ports")
    for label, host, port, who in port_owners(cfg):
        if who is None:
            info(f"{label:12s} {host}:{port}  {C.GREEN}available{C.RESET}")
        else:
            warn(f"{label:12s} {host}:{port}  {C.RED}in use{C.RESET} by {who}")


def show_status(cfg: dict) -> None:
    """Display system status overview."""
    header("System Status")
    _status_hardware()
    _status_tools(cfg)
    _status_packages(cfg)
    _status_servers(cfg)
    _status_models(cfg)
    _status_ports(cfg)
