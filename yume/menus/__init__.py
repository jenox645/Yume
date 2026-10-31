"""Interactive menus and the CLI subcommands that talk to the server.

server.py      `stats` command
blacklist.py   `blacklist` command and the subtitle filter menu
whisper_model.py `model` command and the Whisper model menu
hf_browser.py  HuggingFace browser for GGUF translation models
tools.py       Tools & Fonts: installers, the translation engine, Python dependencies
translation.py Translation backend, address, model, test sentence
youtube_auth.py Browser cookies or Deno
one_click.py   One-click start
settings.py    Settings menu
"""

from yume.menus.blacklist import _menu_blacklist, cli_blacklist
from yume.menus.hf_browser import browse_hf
from yume.menus.one_click import cli_one_click, menu_one_click
from yume.menus.server import cli_server_stats
from yume.menus.settings import settings_menu
from yume.menus.tools import tools_menu
from yume.menus.translation import _test_translation
from yume.menus.whisper_model import _menu_whisper_model, cli_model

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
    "settings_menu",
    "tools_menu",
]
