"""The tracker's version, in one place.

It shows up in three: the console banner, `/state.json`, and the badge in the
director view. Bump it here and the three follow.

`0.1.0-beta` was a deliberate statement, not modesty: two things had never run,
multiworld and a build on any machine other than the author's. Both have since
-- a few multiworld sessions, two friends' PCs -- and what they turned up is
fixed and written down. It is still a beta: the measured list of what works is
in the README, and whoever downloads it should know what they are picking up.
"""

__version__ = "0.2.0-beta"

# One line, shown next to the version wherever there is room for it.
STAGE_NOTE = "early beta - single player is tested; multiworld has had a few sessions"
