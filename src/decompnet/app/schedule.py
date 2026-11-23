"""When a helper may work: off-peak windows, so a cheaper rate is what its tokens are bought at.

A window is "HH:MM-HH:MM" in UTC - the clock providers price on (DeepSeek's discount runs 16:30-00:30 UTC) - and a
schedule is one or more of them, comma separated: "16:30-00:30" or "00:30-08:00,16:30-24:00". A window whose end is
before its start wraps past midnight, which is what the discount window does.

Nothing here sleeps: `next_start` says when the next window opens (None while one is open), and the session loop
waits on that.
"""

from __future__ import annotations

import re
import time

WINDOW = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse(hours: str | None) -> list[tuple[int, int]]:
    """The windows as (start minute, end minute) of the day; an empty or unreadable schedule is always open."""
    if not hours or not isinstance(hours, str) or not hours.strip():
        return []
    out = []
    for part in hours.split(","):
        if not part.strip():
            continue
        m = WINDOW.match(part)
        if m is None:
            raise ValueError(f"'{part.strip()}' is not a time window like 16:30-00:30")
        sh, sm, eh, em = (int(g) for g in m.groups())
        if sh > 24 or eh > 24 or sm > 59 or em > 59 or (sh, sm) == (eh, em):
            raise ValueError(f"'{part.strip()}' is not a time window like 16:30-00:30")
        out.append((sh * 60 + sm, eh * 60 + em))
    return out


def open_at(hours: str | None, when: float | None = None) -> bool:
    """Whether a helper scheduled like this may claim work now (no schedule: always)."""
    return next_start(hours, when) is None


def next_start(hours: str | None, when: float | None = None) -> float | None:
    """When this schedule next opens, or None while it is open now."""
    windows = parse(hours)
    if not windows:
        return None
    when = time.time() if when is None else when
    tm = time.gmtime(when)
    minute = tm.tm_hour * 60 + tm.tm_min
    for start, end in windows:
        inside = start <= minute < end if start < end else (minute >= start or minute < end)
        if inside:
            return None
    # the soonest opening among the windows (each today, or tomorrow if today's start has gone)
    wait: int | None = None
    for start, _end in windows:
        ahead = (start - minute) * 60 - tm.tm_sec
        if ahead <= 0:
            ahead += 24 * 3600
        wait = ahead if wait is None else min(wait, ahead)
    return when + wait


def describe(hours: str | None) -> str:
    """How a session says its schedule (the UI shows the same string)."""
    windows = parse(hours)
    if not windows:
        return "any time"
    return ", ".join(f"{s // 60:02d}:{s % 60:02d}-{e // 60:02d}:{e % 60:02d}" for s, e in windows) + " UTC"
