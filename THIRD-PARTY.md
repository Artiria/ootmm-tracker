# Third-party code and data

This tracker is original code, but it **leans on files from the OoTMM
repository**, which ship inside `data/` and inside the `.exe` as well. OoTMM
is MIT licensed, which allows copying, modifying and redistributing them on a
single condition: keeping its copyright notice and license text. That is what
this file is for.

Nothing here is Nintendo's intellectual property: the tracker **distributes
neither a sprite nor a byte of the games**. Every user extracts the icons from
their own ROM the first time they run the program (see the "Distributing the
tracker" section of the README).

## OoTMM

- Repository: <https://github.com/OoTMM/OoTMM>
- License: MIT
- Copyright (c) 2020-2022 OoTMM Team

### Files that come from there

| File in this repo | Origin in OoTMM/OoTMM | What it is used for |
|---|---|---|
| `data/pool_oot.csv` | `data/pool/pool_oot.csv` | the pool's label dictionary (`mkchecks.py`) |
| `data/pool_mm.csv` | `data/pool/pool_mm.csv` | same |
| `data/scenes.yml` | `data/defs/scenes.yml` | scene name -> index |
| `data/npc.yml` | `data/defs/npc.yml` | npc symbol -> index |
| `data/gi.yml` | `data/defs/gi.yml` | symbolic id from the GI table (`placement.py`) |
| `data/entrances.yml` | `data/defs/entrances.yml` | entrance names and areas (`entrances.py`) |
| `data/ref/items.h` | `packages/generator/include/combo/data/items.h` | item ids, parsed at runtime |
| `data/ref/mark.c` | `packages/generator/src/common/mark.c` | reference for the mark format |
| `data/ref/xflags.c` | `packages/generator/src/common/xflags.c` | reference for the xflag format |

They are verbatim copies, unmodified. Checked against `master` on 14 August
2026: seven of the eight are identical line for line; `gi.yml` is a copy of an
earlier version and differs on 26 lines, all of them renames OoTMM made later
(the Rusty Key labels). It makes no difference: the tracker reads item names
from the ROM and only uses `gi.yml` for the symbolic id.

`data/ref/mark.c` and `data/ref/xflags.c` are not read at runtime; they are
kept because they document the format this tracker decodes, and it helps to
have them pinned to a specific version.

### OoTMM's license, in full

```
MIT License

Copyright (c) 2020-2022 OoTMM Team

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## What is not distributed

`adapter-tracker.lua`, used by `proxy` mode, is a verbatim copy of the OoTMM
MultiClient's `adapter.lua` with the port changed. It is **not in this
repository** and is not distributed: anyone who wants that mode can make it
from their own copy.
