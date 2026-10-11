"""A benchmark of real coding agents under three conditions: no guidance, rules in CLAUDE.md, and Threefold.

Nothing here imports the Threefold engine to decide whether a violation landed.
The checkers in `checks.py` read the files an agent left behind with their own
parser, so the measurement does not grade Threefold with Threefold.
"""
