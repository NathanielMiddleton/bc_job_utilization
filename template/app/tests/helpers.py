"""Shared by the unit tests and the fake Slurm binaries in tests/bin."""
import os
import re
from datetime import datetime, timedelta

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
_PLACEHOLDER = re.compile(r"T([+-]\d+)([mhd])")
_UNITS = {"m": 60, "h": 3600, "d": 86400}


def render_fixture(name, now):
    """Replace T-3h style placeholders with timestamps relative to `now`."""
    with open(os.path.join(FIXTURES, name)) as fh:
        text = fh.read()

    def sub(m):
        t = now + timedelta(seconds=int(m.group(1)) * _UNITS[m.group(2)])
        return t.strftime("%Y-%m-%dT%H:%M:%S")

    return _PLACEHOLDER.sub(sub, text)
