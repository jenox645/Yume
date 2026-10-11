"""cutlet 0.5.2 — Japanese to romaji (https://github.com/polm/cutlet), vendored.

MIT License, Copyright (c) 2020 Paul O'Leary McCann — see LICENSE. Vendored so
Yume does not need mojimoji (see the note in cutlet.py); needs fugashi,
unidic-lite and jaconv (server/requirements.txt).
"""

from .cutlet import *  # noqa: F403

__all__ = ("Cutlet",)
