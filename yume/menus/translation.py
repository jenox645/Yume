"""Translation menus: backend, address, model, and a test sentence."""

from __future__ import annotations

import json
import urllib.request

from yume.constants import BACKEND_INFO, GGUF_DIR, GiB, backend_info, use_backend
from yume.menus.hf_browser import browse_hf
from yume.network import check_ollama_models, check_translation_server
from yume.ports import find_free_port, get_port_process, is_port_free
from yume.ui import C, ask_arrow, ask_input, ask_yn, bullet, error, header, info, pause, success, warn
from yume.utils import find_gguf_models


def _show_install_steps(cur: str, bi: dict) -> None:
    from yume.installers import install_llamacpp_python, install_ollama

    header(f"Install {bi.get('name', cur)}")
    print(f"\n  {C.BOLD}{bi.get('name', cur)}{C.RESET}\n  {bi.get('desc', '')}\n\n  {C.BOLD}Installation:{C.RESET}")
    for line in bi.get("inst", "").split("\n"):
        print(f"  {line}")
    if cur == "llamacpp":
        print()
        if ask_yn("Install llama-cpp-python now?"):
            install_llamacpp_python()
    elif cur == "ollama":
        print()
        if ask_yn("Auto-install Ollama now?"):
            install_ollama()
    pause()


def _menu_backend(cfg: dict) -> None:
    from config import DEFAULT_TRANSLATION_PORT

    while True:
        header("Translation Backend")
        cur = cfg.get("translation_backend", "llamacpp")
        bi = backend_info(cur)
        info(f"Current: {C.BOLD}{bi.get('name', cur)}{C.RESET}")
        info(
            f"Address: {C.CYAN}{cfg.get('translation_host', '127.0.0.1')}:"
            f"{cfg.get('translation_port', DEFAULT_TRANSLATION_PORT)}{C.RESET}"
        )
        st = check_translation_server(
            cfg.get("translation_host", "127.0.0.1"),
            cfg.get("translation_port", DEFAULT_TRANSLATION_PORT),
            bi,
        )
        (success if st["up"] else warn)(f"Status: {'RUNNING' if st['up'] else 'Not running'}")

        ch = ask_arrow(
            "Options:",
            [
                ("Change backend", "Switch between llama.cpp/Ollama/LM Studio/WebUI/Custom"),
                ("Change address", f"Currently {cfg.get('translation_host')}:{cfg.get('translation_port')}"),
                ("Install instructions", f"How to set up {bi.get('name', cur)}"),
                ("Manage model", "Pull, change, browse, or download models"),
                ("Back", None),
            ],
            default=4,
        )
        if ch == -1 or ch == 4:
            return
        elif ch == 0:
            _select_backend(cfg)
        elif ch == 1:
            _change_addr(cfg, "translation")
        elif ch == 2:
            _show_install_steps(cur, bi)
        elif ch == 3:
            _manage_model(cfg)


def _select_backend(cfg: dict) -> None:
    from config import save_config

    header("Select Backend")
    keys = list(BACKEND_INFO.keys())
    opts = [(BACKEND_INFO[k]["name"], BACKEND_INFO[k]["desc"]) for k in keys] + [("Back", None)]
    cur = cfg.get("translation_backend", "llamacpp")
    di = keys.index(cur) if cur in keys else 0
    ch = ask_arrow("Choose:", opts, default=di)
    if ch == -1 or ch == len(keys):
        return
    bi = use_backend(cfg, keys[ch])
    save_config(cfg)
    success(f"Backend: {bi['name']}  ({bi['dh']}:{bi['dp']})")
    print(f"\n  {C.BOLD}Installation:{C.RESET}")
    for line in bi["inst"].split("\n"):
        print(f"  {line}")
    pause()


def _ask_host(prefix: str, current: str) -> str:
    from config import validate_host

    if prefix == "whisper":
        # The Whisper server binds 127.0.0.1 only and rejects any other Host
        # header (DNS-rebinding defence); the extension can only reach localhost.
        info(f"{C.DIM}The Whisper server always runs on this computer (127.0.0.1) — only the port can change.{C.RESET}")
        return "127.0.0.1"
    host = validate_host(ask_input("Host", current))
    if host is None:
        warn(f"Keeping current host: {current}")
        return current
    return host


def _settle_busy_port(port: int, current: int, host: str) -> int:
    """The port to use when `port` may be taken: it, a free one, or the current one."""
    if is_port_free(port, host):
        return port
    pid, name = get_port_process(port)
    warn(f"Port {port} is in use by {name or 'unknown'} (PID {pid or '?'})")
    if ask_yn(f"Use port {port} anyway?", False):
        return port
    free = find_free_port(port + 1)
    if not free:
        return current
    info(f"Suggestion: port {free} is available")
    return free if ask_yn(f"Use {free} instead?") else current


def _change_addr(cfg: dict, prefix: str) -> None:
    from config import save_config, validate_port

    ch = cfg.get(f"{prefix}_host", "127.0.0.1")
    cp = cfg.get(f"{prefix}_port", 5000)
    print(f"\n  Current: {C.CYAN}{ch}:{cp}{C.RESET}\n")
    host = _ask_host(prefix, ch)
    raw_port = ask_input("Port", str(cp))
    port = validate_port(raw_port, f"{prefix.title()} port")
    # A busy port only matters for servers Yume starts itself (Whisper, llama.cpp);
    # Ollama / LM Studio are SUPPOSED to be listening on theirs.
    launches_it = prefix == "whisper" or cfg.get("translation_backend") == "llamacpp"
    if port is None:
        warn(f"Keeping current port: {cp}")
        port = cp
    elif port != cp and launches_it:
        port = _settle_busy_port(port, cp, host)
    cfg[f"{prefix}_host"] = host
    cfg[f"{prefix}_port"] = port
    save_config(cfg)
    success(f"Set to {host}:{port}")
    if prefix == "whisper" and port != cp:
        warn(f"Also set the Whisper port to {port} in the browser extension popup (Server Settings).")
    pause()


def _show_ollama_models(cfg: dict, active: str) -> None:
    from config import DEFAULT_OLLAMA_PORT

    models = check_ollama_models(
        cfg.get("translation_host", "127.0.0.1"), cfg.get("translation_port", DEFAULT_OLLAMA_PORT)
    )
    if models:
        print()
        info("Ollama models:")
        family = active.split(":")[0]
        for m in models:
            bullet(f"{m}{f' {C.GREEN}<- active{C.RESET}' if m == active or m.startswith(family) else ''}")


def _show_model_status(cfg: dict, ggufs: list) -> None:
    bk = cfg.get("translation_backend", "llamacpp")
    mdl = cfg.get("translation_model", "")
    gp = cfg.get("gguf_model_path", "")
    info(f"Backend: {C.BOLD}{BACKEND_INFO.get(bk, {}).get('name', bk)}{C.RESET}")
    if mdl:
        info(f"Model:   {C.BOLD}{mdl}{C.RESET}")
    if gp:
        info(f"GGUF:    {gp}")
    if ggufs:
        print()
        info(f"GGUF files in {GGUF_DIR}:")
        for f in ggufs:
            act = f" {C.GREEN}<- active{C.RESET}" if str(f) == gp else ""
            bullet(f"{f.name}  ({f.stat().st_size / GiB:.2f} GB){act}")
    if bk == "ollama":
        _show_ollama_models(cfg, mdl)


def _rename_model(cfg: dict, ggufs: list) -> None:
    from config import save_config

    name = ask_input("Model name", cfg.get("translation_model", ""))
    if name:
        cfg["translation_model"] = name
        save_config(cfg)
        success(f"Model: {name}")
    pause()


def _pull_model(cfg: dict, ggufs: list) -> None:
    from config import save_config
    from yume.installers import pull_ollama_model

    name = ask_input("Ollama model to pull", cfg.get("translation_model", "") or "qwen2.5:7b")
    if name and pull_ollama_model(name):  # starts Ollama if needed
        cfg["translation_model"] = name
        save_config(cfg)
    pause()


def _browse_hf(cfg: dict, ggufs: list) -> None:
    browse_hf(cfg)


def _select_gguf(cfg: dict, ggufs: list) -> None:
    from config import save_config

    if not ggufs:
        warn(f"No .gguf files in {GGUF_DIR}")
        pause()
        return
    opts = [(f"{f.name} ({f.stat().st_size / GiB:.2f} GB)", None) for f in ggufs] + [("Back", None)]
    ch = ask_arrow("Select:", opts, default=len(opts) - 1)
    if 0 <= ch < len(ggufs):
        cfg["gguf_model_path"] = str(ggufs[ch])
        success(f"Selected: {ggufs[ch].name}")
        if cfg["translation_backend"] != "llamacpp" and ask_yn("Switch to llama.cpp backend?"):
            use_backend(cfg, "llamacpp")
        # Other backends send translation_model with every request: it must stay theirs
        if cfg["translation_backend"] == "llamacpp":
            cfg["translation_model"] = ggufs[ch].stem
        save_config(cfg)
    pause()


def _manage_model(cfg: dict) -> None:
    while True:
        header("Manage Translation Model")
        ggufs = find_gguf_models()
        _show_model_status(cfg, ggufs)
        entries = [
            ("Change model name", "Enter model name manually", _rename_model),
            ("Pull Ollama model", "Download via ollama pull", _pull_model),
            ("Download GGUF from HuggingFace", "Browse repos and pick files", _browse_hf),
            ("Select local GGUF file", f"{len(ggufs)} file(s) in models/translation/", _select_gguf),
        ]
        ch = ask_arrow(
            "Options:", [(label, desc) for label, desc, _ in entries] + [("Back", None)], default=len(entries)
        )
        if not 0 <= ch < len(entries):
            return
        entries[ch][2](cfg, ggufs)


def _ask_llm(url: str, model: str, text: str) -> str:
    """One test sentence to the backend: the reply's text ("" when it has none)."""
    body = {
        "messages": [
            {"role": "system", "content": "You are a translation system. Output ONLY the English translation."},
            {"role": "user", "content": text},
        ],
        "max_tokens": 200,
        "temperature": 0.1,
        "stream": False,
    }
    if model:
        body["model"] = model
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Yume"},
        method="POST",
    )
    info("Waiting...")
    with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310
        result = json.loads(resp.read())
    if result.get("choices"):
        return result["choices"][0].get("message", {}).get("content", "")
    return result.get("message", {}).get("content", "")


def _test_translation(cfg: dict) -> None:
    from config import DEFAULT_TRANSLATION_PORT

    header("Test Translation")
    bk = cfg.get("translation_backend", "llamacpp")
    h = cfg.get("translation_host", "127.0.0.1")
    p = cfg.get("translation_port", DEFAULT_TRANSLATION_PORT)
    m = cfg.get("translation_model", "")
    bi = backend_info(bk)

    info(f"Backend: {bi.get('name', bk)} ({h}:{p})")
    if m:
        info(f"Model: {m}")
    print()

    info("Checking server connectivity...")
    st = check_translation_server(h, p, bi)
    if not st["up"]:
        error(f"Server not reachable at {h}:{p}")
        if bk == "llamacpp":
            warn("Make sure you launched Yume first (main menu → Launch Yume)")
            info(f"{C.DIM}The translation server starts automatically when you launch.{C.RESET}")
        pause()
        return
    success("Server reachable!")

    txt = ask_input("Test sentence (Japanese)", "\u4eca\u65e5\u306f\u3044\u3044\u5929\u6c17\u3067\u3059\u306d")
    info(f"Sending: {txt}")
    print()
    url = f"http://{h}:{p}{bi.get('ap', '/v1/chat/completions')}"  # noqa: S5332 — local LLM backend
    try:
        # llama-cpp-python serves one model; every other backend needs the name
        tr = _ask_llm(url, "" if bk == "llamacpp" else m, txt)
        if tr:
            print()
            success(f"Translation: {C.BOLD}{tr.strip()}{C.RESET}")
            print()
            success("Pipeline working!")
        else:
            warn("Got response but no translation text")
    except Exception as e:
        error(f"Failed: {e}")
    pause()
