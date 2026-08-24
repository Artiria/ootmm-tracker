"""The tracker's version, in one place.

It shows up in three: the console banner, `/state.json`, and the badge in the
director view. Bump it here and the three follow.

`0.1.0-beta` is a deliberate statement, not modesty. What works is measured and
written down in the POC, but two things have never run: multiworld (questions
P4 and P6) and a build on any machine other than the author's. Until those are
closed, whoever downloads it should know what they are picking up.
"""

__version__ = "0.1.3.1-beta"

# One line, shown next to the version wherever there is room for it.
STAGE_NOTE = "early beta - single player is tested, multiworld is not"
