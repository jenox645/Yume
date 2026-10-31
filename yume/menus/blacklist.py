"""Subtitle filter: the `blacklist` command and its menu, with the server up or down."""

from __future__ import annotations

import json
import logging

from yume.network import server_get as _server_get, server_post as _server_post
from yume.ui import (
    C,
    ask_arrow,
    ask_input,
    ask_yn,
    bullet,
    error,
    header,
    info,
    pause,
    success,
    warn,
)

_log = logging.getLogger("pocket_yume")


def _bl_file():
    """Path of the persisted blacklist (shared with the whisper server)."""
    from config import CONFIG_DIR

    return CONFIG_DIR / "blacklist.json"


def _bl_read_offline() -> list:
    try:
        items = json.loads(_bl_file().read_text(encoding="utf-8"))
        return [str(i).strip() for i in items if str(i).strip()] if isinstance(items, list) else []
    except FileNotFoundError:
        return []
    except Exception as e:
        _log.debug("[_bl_read_offline] %s", e)
        return []


def _bl_write_offline(items: list) -> bool:
    try:
        _bl_file().parent.mkdir(parents=True, exist_ok=True)
        _bl_file().write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        error(f"Could not save: {e}")
        return False


class _Blacklist:
    """The blacklist on the running server, or config/blacklist.json (loaded at
    server startup) when the server is down."""

    def __init__(self, cfg: dict):
        self.h, self.p = cfg["whisper_host"], cfg["whisper_port"]
        data = _server_get(self.h, self.p, "/blacklist")
        self.online = bool(data)
        self.items = data.get("blacklist", []) if data else _bl_read_offline()

    def save(self, items: list) -> bool:
        if self.online:
            r = _server_post(self.h, self.p, "/blacklist/update", {"blacklist": items})
            ok = bool(r.get("success"))
        else:
            ok = _bl_write_offline(items)
        if ok:
            self.items = items
        return ok

    def has(self, text: str) -> bool:
        return text.lower() in (b.lower() for b in self.items)

    def add(self, text: str) -> bool | None:
        """None: already there (any case); else whether it was saved."""
        return None if self.has(text) else self.save([*self.items, text])

    def remove(self, text: str) -> bool | None:
        """None: not there (any case); else whether it was saved."""
        keep = [b for b in self.items if b.lower() != text.lower()]
        return None if len(keep) == len(self.items) else self.save(keep)


def _said(result: bool | None, done: str, unchanged: str = "", failed: str = "Failed to save") -> None:
    """Report a blacklist edit: done, nothing to do (None), or not saved."""
    if result is None:
        warn(unchanged)
    else:
        (success if result else error)(done if result else failed)


def _bl_add(bl: _Blacklist) -> None:
    text = ask_input("Phrase to block", "")
    if text:
        _said(bl.add(text), f"Added: {text}", "Already blocked", failed="Failed")
    pause()


def _bl_remove(bl: _Blacklist) -> None:
    if not bl.items:
        info("Empty")
        pause()
        return
    shown = bl.items[:20]
    if len(bl.items) > 20:
        info(f"Showing first 20 of {len(bl.items)} — remove others with:")
        info(f"{C.DIM}  python pocket_yume.py blacklist remove <text>{C.RESET}")
    ch = ask_arrow("Remove which?", [(item, None) for item in shown] + [("Back", None)], default=len(shown))
    # Against len(shown), not len(items): with >20 items "Back" is index 20, a valid item index
    if 0 <= ch < len(shown):
        removed = bl.items[ch]
        _said(bl.save(bl.items[:ch] + bl.items[ch + 1 :]), f"Removed: {removed}")
    pause()


def _bl_clear(bl: _Blacklist) -> None:
    if ask_yn("Clear ALL?", False):
        _said(bl.save([]), "Cleared")
    pause()


def _menu_blacklist(cfg: dict) -> None:
    """Blacklist management menu. Works offline too — the list lives in
    config/blacklist.json, which the server loads at startup."""
    entries = [
        ("Add entry", "Block a phrase from subtitles", _bl_add),
        ("Remove entry", "Unblock a phrase", _bl_remove),
        ("Clear all", "Remove all entries", _bl_clear),
    ]
    while True:
        header("Subtitle Filter (Blacklist)")
        info("Whisper sometimes generates fake text that wasn't actually spoken —")
        info("things like 'Thank you for watching' or 'Subscribe'. These are called")
        info("'hallucinations'. Yume blocks known patterns automatically, but you can")
        info("add your own phrases to block here.")
        print()
        bl = _Blacklist(cfg)
        if not bl.online:
            warn(f"Server not running — editing the saved list ({_bl_file().name}).")
            info(f"{C.DIM}Changes apply automatically the next time the server starts.{C.RESET}")
            print()
        info(f"Blacklist: {len(bl.items)} items")
        for item in bl.items[:15]:
            bullet(item)
        if len(bl.items) > 15:
            info(f"  ... and {len(bl.items) - 15} more")
        ch = ask_arrow("Options:", [(label, desc) for label, desc, _ in entries] + [("Back", None)], default=3)
        if not 0 <= ch < len(entries):
            return
        entries[ch][2](bl)


def _bl_list(bl: _Blacklist, where: str) -> None:
    info(f"Blacklist ({len(bl.items)} items):{where}" if bl.items else f"Blacklist is empty{where}")
    for item in bl.items:
        bullet(item)


def cli_blacklist(cfg: dict, args: list) -> None:
    """CLI blacklist management."""
    if not args:
        _menu_blacklist(cfg)
        return
    bl = _Blacklist(cfg)
    where = "" if bl.online else f"  {C.DIM}(server not running — saved list){C.RESET}"
    subcmd, text = args[0].lower(), " ".join(args[1:]).strip()
    if subcmd == "list":
        _bl_list(bl, where)
    elif subcmd == "add" and text:
        _said(bl.add(text), f"Added: {text}{where}", f"Already blocked: {text}")
    elif subcmd in ("remove", "rm") and text:
        _said(bl.remove(text), f"Removed: {text}{where}", f"Not in blacklist: {text}")
    elif subcmd == "clear":
        _said(bl.save([]), f"Blacklist cleared{where}")
    else:
        print("  Usage: pocket_yume.py blacklist [list|add <text>|remove <text>|clear]")
