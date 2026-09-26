"""The launch logo: a wooden Trojan horse on wheels, drawn in half-block pixels.

Each character cell holds two vertical pixels (▀ with a foreground colour for
the top pixel and a background colour for the bottom one), so a 24 x 18 pixel
drawing takes 24 x 9 cells.
"""
from __future__ import annotations

from rich.style import Style
from rich.text import Text

PALETTE = {
    "L": "#e9b872",  # sunlit wood
    "M": "#c98a45",  # wood
    "D": "#8a5a2b",  # shaded wood
    "K": "#2b1d12",  # eye
    "G": "#f2c14e",  # bronze trim
    "H": "#3a2414",  # the hatch, where the soldiers wait
    "P": "#a0703a",  # platform
    "W": "#7a5230",  # wheel
    "O": "#f2c14e",  # wheel hub
}

ART = """
.....DD.................
....DLLD................
...DLLLLD...............
..LKLLLLLD..............
.LLLLLLLLD..............
LLLLLLLLLD..............
.DDD.LLLLLD.............
.....LLLLLDDDDDDDDDDDD..
.....LLLLLMMMMMMMMMMMMD.
.....MMMMMMMMMMMMMMMMMMDD
.....MMMMMMGGGGGGMMMMMMD.D
.....MMMMMMGHHHHGMMMMMMD.D
.....DDDDDDDDDDDDDDDDDDD..
......MD.MD......MD.MD....
......MD.MD......MD.MD....
...PPPPPPPPPPPPPPPPPPPPP..
....WOW.....WOW.....WOW...
....WWW.....WWW.....WWW...
"""


def horse() -> Text:
    rows = [r for r in ART.strip("\n").split("\n")]
    width = max(len(r) for r in rows)
    rows = [r.ljust(width, ".") for r in rows]
    if len(rows) % 2:
        rows.append("." * width)
    out = Text()
    for top, bottom in zip(rows[0::2], rows[1::2]):
        for a, b in zip(top, bottom):
            ca, cb = PALETTE.get(a), PALETTE.get(b)
            if ca and cb:
                out.append("▀", Style(color=ca, bgcolor=cb))
            elif ca:
                out.append("▀", Style(color=ca))
            elif cb:
                out.append("▄", Style(color=cb))
            else:
                out.append(" ")
        out.append("\n")
    out.rstrip()
    return out
