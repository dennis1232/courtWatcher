"""Watch loop: stops on the first free court, backs off when throttled, gives up when the window closes."""

import asyncio
from datetime import datetime, timedelta

from find_court import TZ, quiet_until, watch

FREE = {"club_id": 1, "club": "A", "phone": None, "address": "", "slots": {"20:00": ["Court 1"]}}
EMPTY = {"club_id": 1, "club": "A", "phone": None, "address": "", "slots": {}}
THROTTLED = {**EMPTY, "error": "HTTP 429", "retry_after": 3000.0}


NOON = datetime(2026, 11, 16, 12, 0, tzinfo=TZ)


def run(cycles, closed_after=99, start=NOON):
    """Feed `cycles` one per check on a fake clock; return (hits, sleeps)."""
    it, sleeps, checks, now = iter(cycles), [], [], [start]

    async def cycle():
        checks.append(1)
        return next(it)

    async def sleep(sec):
        sleeps.append(sec)
        now[0] += timedelta(seconds=sec)

    hits = asyncio.run(watch(cycle, lambda: len(checks) >= closed_after, sleep=sleep, log=lambda *_: None,
                             clock=lambda: now[0]))
    return hits, sleeps


def test_stops_on_first_free_court():
    hits, sleeps = run([[EMPTY], [EMPTY], [FREE]])
    assert hits == [FREE] and len(sleeps) == 2


def test_wait_is_random_between_10_and_15_minutes():
    _, sleeps = run([[EMPTY]] * 30 + [[FREE]])  # 30 checks from noon stay in daytime
    assert all(600 <= s <= 900 for s in sleeps)
    assert min(sleeps) < 690 and max(sleeps) > 810  # spread across the range, not clustered


def test_backs_off_on_throttle_and_honours_retry_after():
    _, sleeps = run([[THROTTLED], [EMPTY], [FREE]])
    assert sleeps[0] >= 3000  # Retry-After wins over the 10-15 min draw
    assert 600 <= sleeps[1] <= 900  # clean cycle resets the backoff


def test_backoff_doubles_on_failures_up_to_4x():
    failing = {**EMPTY, "error": "timed out"}
    _, sleeps = run([[failing]] * 4 + [[FREE]])
    assert 1200 <= sleeps[0] <= 1800 and all(2400 <= s <= 3600 for s in sleeps[1:])


def test_gives_up_when_window_closes():
    hits, _ = run([[EMPTY]] * 10, closed_after=3)
    assert hits is None


def test_no_checks_at_night():
    """From 22:55, the next check is pushed to 07:00-07:15 the next morning, never into the night."""
    start = datetime(2026, 11, 16, 22, 55, tzinfo=TZ)
    _, sleeps = run([[EMPTY], [FREE]], start=start)
    nxt = start + timedelta(seconds=sleeps[0])
    assert nxt.date() == start.date() + timedelta(days=1) and 7 * 60 <= nxt.hour * 60 + nxt.minute <= 7 * 60 + 15


def test_quiet_until():
    d = datetime(2026, 11, 16, tzinfo=TZ)
    assert quiet_until(d.replace(hour=12)) is None and quiet_until(d.replace(hour=22, minute=59)) is None
    assert quiet_until(d.replace(hour=23, minute=30)) == d.replace(hour=7) + timedelta(days=1)
    assert quiet_until(d.replace(hour=3)) == d.replace(hour=7)
