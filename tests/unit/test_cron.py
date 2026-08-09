"""Cron expression parsing + next-fire computation (``synopticon/cron.py``)."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from synopticon import cron

AMS = ZoneInfo("Europe/Amsterdam")
UTC = timezone.utc


def _at(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _fire(expr: str, after: datetime, tz=AMS) -> datetime:
    return cron.next_fire(cron.parse(expr), after, tz)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_parses_wildcards_and_lists():
    expr = cron.parse("0,30 */6 * * *")
    assert expr.minutes == (0, 30)
    assert expr.hours == (0, 6, 12, 18)
    assert expr.days == tuple(range(1, 32))
    assert not expr.dom_restricted and not expr.dow_restricted


def test_parses_names_and_ranges():
    expr = cron.parse("0 4 * JAN-mar mon-FRI")
    assert expr.months == (1, 2, 3)
    assert expr.dows == (1, 2, 3, 4, 5)
    assert expr.dow_restricted


def test_sunday_accepts_both_zero_and_seven():
    assert cron.parse("0 0 * * 7").dows == (0,)
    assert cron.parse("0 0 * * 0").dows == (0,)


def test_step_from_bare_value_runs_to_the_top_of_the_range():
    # Vixie semantics: `5/10` in minutes is 5,15,25,... not just 5.
    assert cron.parse("5/10 * * * *").minutes == (5, 15, 25, 35, 45, 55)


def test_question_mark_is_a_wildcard():
    expr = cron.parse("0 0 ? * ?")
    assert not expr.dom_restricted and not expr.dow_restricted


def test_macros():
    assert cron.parse("@hourly").source == "@hourly"
    assert cron.parse("@hourly").minutes == (0,)
    assert cron.parse("@weekly").dows == (0,)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "  ",
        "* * * *",
        "* * * * * *",
        "60 * * * *",
        "* 24 * * *",
        "0 0 0 * *",
        "0 0 * 13 *",
        "0 0 * * 8",
        "abc * * * *",
        "10-5 * * * *",
        "*/0 * * * *",
        "@reboot",
        "@nope",
    ],
)
def test_rejects_bad_expressions(bad):
    with pytest.raises(cron.CronError):
        cron.parse(bad)


# --------------------------------------------------------------------------- #
# Next fire
# --------------------------------------------------------------------------- #
def test_daily_fire_is_in_local_time():
    # 23:17 UTC on Aug 9 is 01:17 local (CEST) on Aug 10.
    got = _fire("0 3 * * *", _at(2026, 8, 9, 23, 17))
    assert got.isoformat() == "2026-08-10T03:00:00+02:00"


def test_fire_is_strictly_after():
    exactly_on = datetime(2026, 8, 10, 3, 0, tzinfo=AMS)
    assert _fire("0 3 * * *", exactly_on).day == 11


def test_seconds_are_ignored_not_rounded_up():
    got = _fire("*/15 * * * *", datetime(2026, 8, 10, 1, 29, 59, tzinfo=AMS))
    assert got.isoformat() == "2026-08-10T01:30:00+02:00"


def test_day_of_week_and_day_of_month_union():
    # Both restricted -> either matches. Aug 10 2026 is a Monday; Sep 1 is a
    # Tuesday but inside 1-7.
    fires = cron.next_fires(
        cron.parse("0 12 1-7 * MON"), _at(2026, 8, 9, 23, 0), AMS, 3
    )
    assert [f.date().isoformat() for f in fires] == [
        "2026-08-10",
        "2026-08-17",
        "2026-08-24",
    ]


def test_only_day_of_month_restricted():
    fires = cron.next_fires(cron.parse("0 0 1 * *"), _at(2026, 8, 9), AMS, 2)
    assert [f.date().isoformat() for f in fires] == ["2026-09-01", "2026-10-01"]


def test_leap_day_schedule_is_reachable():
    got = _fire("0 0 29 2 *", _at(2026, 3, 1))
    assert got.date().isoformat() == "2028-02-29"


def test_impossible_date_raises_rather_than_looping():
    with pytest.raises(cron.CronError):
        _fire("0 0 30 2 *", _at(2026, 1, 1))


def test_timezone_is_honoured():
    after = _at(2026, 1, 10, 12, 0)
    assert _fire("0 3 * * *", after, ZoneInfo("UTC")).hour == 3
    assert _fire("0 3 * * *", after, ZoneInfo("Pacific/Auckland")).utcoffset() is not None
    # Same wall clock, different instants.
    assert _fire("0 3 * * *", after, ZoneInfo("UTC")) != _fire(
        "0 3 * * *", after, ZoneInfo("Pacific/Auckland")
    )


def test_dst_spring_forward_fires_late_rather_than_never():
    # 2026-03-29 02:30 local does not exist in Amsterdam (02:00 -> 03:00).
    got = _fire("30 2 * * *", datetime(2026, 3, 28, 12, 0, tzinfo=AMS))
    assert got.date().isoformat() == "2026-03-29"
    assert got > datetime(2026, 3, 29, 1, 0, tzinfo=AMS)


def test_dst_fall_back_fires_once():
    # 2026-10-25 02:30 happens twice; fold=0 picks the first (CEST, +02:00).
    got = _fire("30 2 * * *", datetime(2026, 10, 24, 12, 0, tzinfo=AMS))
    assert got.isoformat() == "2026-10-25T02:30:00+02:00"


def test_next_fires_are_strictly_increasing():
    fires = cron.next_fires(cron.parse("*/7 * * * *"), _at(2026, 8, 9, 23, 0), AMS, 20)
    assert fires == sorted(fires)
    assert len(set(fires)) == 20


def test_naive_after_is_rejected():
    with pytest.raises(ValueError):
        cron.next_fire(cron.parse("@daily"), datetime(2026, 8, 9, 12, 0), AMS)
