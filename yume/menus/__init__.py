"""Interactive menus and the CLI subcommands that talk to the server.

server.py      CLI commands (stats, blacklist, model) + blacklist / Whisper model menus
hf_browser.py  HuggingFace browser for GGUF translation models
tools.py       Tools & Fonts: installers, translation backend/engine, YouTube auth, one-click start
settings.py    Settings menu
"""

from yume.menus._shared import set_backend_info
from yume.menus.hf_browser import browse_hf
from yume.menus.server import _menu_blacklist, _menu_whisper_model, cli_blacklist, cli_model, cli_server_stats
from yume.menus.settings import settings_menu
from yume.menus.tools import _test_translation, cli_one_click, menu_one_click, tools_menu

__all__ = [
    "_menu_blacklist",
    "_menu_whisper_model",
    "_test_translation",
    "browse_hf",
    "cli_blacklist",
    "cli_model",
    "cli_one_click",
    "cli_server_stats",
    "menu_one_click",
    "set_backend_info",
    "settings_menu",
    "tools_menu",
]
