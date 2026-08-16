"""Switches for work that is built but not yet seen working in game.

A switch here means the code exists, is exercised by the guards on the dumps,
and is OFF by default: with it off nothing that ships changes, byte for byte
-- not checks.json, not /state.json, not the page. Once the feature has been
watched live and is good, the switch goes away and every `if` that reads it
goes with it (grep the name; each site is marked `FEATURE:`).

To try one without editing this file, set the environment variable of the
same name to 1 (`set ENABLE_SOULS=1`) before starting the tracker: the .exe
honours it too, so a build can be tested without a rebuild.

    ENABLE_SOULS   the souls panel (soul shuffle), 16 Aug 2026. Everything it
                   needs comes out of the ROM -- the catalogue and each soul's
                   bit from kAddItemParams/kAddItemFuncs, the arrays' offsets
                   from the payload's references (souls.py, payload.py) -- and
                   it is verified against the dumps, but no live session has
                   picked up a soul with it on yet.
"""

import os


def _switch(name, default=False):
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


ENABLE_SOULS = _switch("ENABLE_SOULS", False)
