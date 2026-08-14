# Hand-placed icons

Anything you drop in here overrides the icon extracted from the ROM.

**It is no longer needed for Majora's masks**: those are extracted from your
own ROM and come out with their real art. This folder is for taste — swapping
an icon for one you like better, or putting something where the game has
nothing.

Nothing is downloaded. You supply the images, from wherever you decide.

## Where each thing goes

```
icons/mm/deku-mask.png      Majora's Mask only
icons/oot/kokiri-sword.png  Ocarina of Time only
icons/something.png         used by both
```

## Naming the file

Names are matched **normalised** — lowercased, and anything that is not a
letter or a digit becomes a hyphen — so two forms work and you do not have to
get capitals or apostrophes right:

| Works | Because |
|---|---|
| `deku-mask.png` | it is the name the overlay shows on hover |
| `mask-deku.png` | it is `items.h`'s name for that id |
| `Deku Mask.png` | it normalises to the same as the first |
| `garos-mask.png` | "Garo's Mask" without the apostrophe |

If the slot is filled, both names are tried; if it is empty, only the first.
The game's folder is checked before the shared one.

Formats: `.png`, `.gif`, `.webp`, `.jpg`.

## Practical details

- **No restart needed**: the overlay re-reads the folder when it changes, so
  you drop the image in and it shows up on the next refresh.
- Square with a transparent background looks best; they are scaled to the
  cell size automatically.
- They are drawn greyed out and sunken when you do not have the item, and in
  colour when you do, exactly like the ROM's icons.
- The server only serves files that were present when the folder was scanned,
  so nothing outside this folder can be requested by URL.

## Finding out a name

Hover over any cell in the overlay: the full name appears, and that is the one
to use for the file.
