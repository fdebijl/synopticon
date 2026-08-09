"""Cron expression parsing and next-fire computation. Dependency-free leaf.

Used by the web GUI's scheduler (``web/scheduler.py``) so a container running
only ``synopticon web`` can run recurring jobs without a cron daemon. Kept as a
top-level leaf module — stdlib only, no project imports — so anything may import
it without dragging the web/pipeline stacks along (same rule as ``cpu.py`` and
``progress.py``).

Syntax is standard 5-field Vixie cron::

    minute  hour  day-of-month  month  day-of-week
    0-59    0-23  1-31          1-12   0-6 (0 = Sunday, 7 also accepted)

with ``*``, ``a``, ``a-b``, ``a-b/n``, ``*/n``, comma lists, and three-letter
month/day names (``JAN``…``DEC``, ``SUN``…``SAT``). ``?`` is accepted as a
synonym for ``*`` because people paste Quartz expressions. The ``@hourly`` /
``@daily`` / ``@midnight`` / ``@weekly`` / ``@monthly`` / ``@yearly`` /
``@annually`` macros are supported; ``@reboot`` deliberately is not (there is no
sensible meaning for it here — the scheduler starts with the server).

Day-of-month and day-of-week follow the traditional union rule: when *both* are
restricted, a day matches if *either* field matches; when only one is
restricted, that field alone decides.

Times are computed in a caller-supplied :class:`~zoneinfo.ZoneInfo` (the
container's local zone by default). DST is handled pragmatically: a wall-clock
time that does not exist on a spring-forward day fires at the shifted instant
rather than being skipped — for batch work, late beats never — and an ambiguous
time on a fall-back day fires once, on its first occurrence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import Iterable

__all__ = ["CronError", "CronExpr", "parse", "next_fire", "next_fires"]

#: How far ahead the search will look before giving up. An expression like
#: ``0 0 30 2 *`` (February 30th) never matches, and the search must terminate.
#: The longest *legitimate* gap is February 29th's four years (1461 days), so the
#: bound has to clear that with room to spare or a leap-day schedule would be
#: rejected as impossible.
_MAX_DAYS = 6 * 366

_MONTHS = {
    name: i
    for i, name in enumerate(
        "jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1
    )
}
_DOWS = {name: i for i, name in enumerate("sun mon tue wed thu fri sat".split())}

_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}


class CronError(ValueError):
    """A cron expression could not be parsed."""


@dataclass(frozen=True)
class CronExpr:
    """A parsed 5-field cron expression."""

    source: str
    minutes: tuple[int, ...]
    hours: tuple[int, ...]
    days: tuple[int, ...]
    months: tuple[int, ...]
    dows: tuple[int, ...]
    #: Whether the corresponding field was something other than ``*``. The
    #: day-of-month / day-of-week union rule needs to know this, and it cannot be
    #: recovered from the expanded sets (``* * * * 0-6`` is not ``* * * * *``).
    dom_restricted: bool
    dow_restricted: bool

    def matches_day(self, day: datetime) -> bool:
        if day.month not in self.months:
            return False
        dom_hit = day.day in self.days
        # Python's weekday() is Monday=0; cron's is Sunday=0.
        dow_hit = ((day.weekday() + 1) % 7) in self.dows
        if self.dom_restricted and self.dow_restricted:
            return dom_hit or dow_hit
        if self.dom_restricted:
            return dom_hit
        if self.dow_restricted:
            return dow_hit
        return True


def parse(expr: str) -> CronExpr:
    """Parse a cron expression (5 fields or a ``@macro``).

    Raises :class:`CronError` with a message aimed at the person who typed it.
    """
    if not isinstance(expr, str):
        raise CronError("cron expression must be a string")
    text = " ".join(expr.strip().split())
    if not text:
        raise CronError("cron expression is empty")
    if text.startswith("@"):
        macro = _MACROS.get(text.lower())
        if macro is None:
            raise CronError(
                f"unknown macro {text!r} (supported: "
                + ", ".join(sorted(_MACROS))
                + ")"
            )
        text = macro
    fields = text.split(" ")
    if len(fields) != 5:
        raise CronError(
            f"expected 5 fields (minute hour day-of-month month day-of-week), "
            f"got {len(fields)}"
        )
    minute, hour, dom, month, dow = fields
    return CronExpr(
        source=" ".join(expr.strip().split()),
        minutes=_field(minute, 0, 59, "minute"),
        hours=_field(hour, 0, 23, "hour"),
        days=_field(dom, 1, 31, "day-of-month"),
        months=_field(month, 1, 12, "month", names=_MONTHS),
        dows=_field(dow, 0, 6, "day-of-week", names=_DOWS, wrap={7: 0}),
        dom_restricted=not _is_wildcard(dom),
        dow_restricted=not _is_wildcard(dow),
    )


def _is_wildcard(field: str) -> bool:
    return field in ("*", "?")


def _field(
    field: str,
    lo: int,
    hi: int,
    label: str,
    *,
    names: dict[str, int] | None = None,
    wrap: dict[int, int] | None = None,
) -> tuple[int, ...]:
    """Expand one cron field to the sorted set of values it matches."""
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"{label}: empty list entry in {field!r}")
        values.update(_part(part, lo, hi, label, names, wrap))
    if not values:
        raise CronError(f"{label}: {field!r} matches nothing")
    return tuple(sorted(values))


def _part(
    part: str,
    lo: int,
    hi: int,
    label: str,
    names: dict[str, int] | None,
    wrap: dict[int, int] | None,
) -> Iterable[int]:
    body, _, step_text = part.partition("/")
    step = 1
    if step_text:
        if not step_text.isdigit() or int(step_text) < 1:
            raise CronError(f"{label}: step must be a positive integer in {part!r}")
        step = int(step_text)
    if _is_wildcard(body):
        start, end = lo, hi
    else:
        start_text, dash, end_text = body.partition("-")
        start = _value(start_text, lo, hi, label, names, wrap)
        if dash:
            end = _value(end_text, lo, hi, label, names, wrap)
        else:
            # A bare value with a step means "from here to the top of the range"
            # (`5/10` in the minute field is 5,15,25,...), matching Vixie cron.
            end = hi if step_text else start
        if end < start:
            raise CronError(f"{label}: range {body!r} runs backwards")
    return range(start, end + 1, step)


def _value(
    text: str,
    lo: int,
    hi: int,
    label: str,
    names: dict[str, int] | None,
    wrap: dict[int, int] | None,
) -> int:
    text = text.strip()
    if names is not None and text.lower() in names:
        return names[text.lower()]
    try:
        value = int(text)
    except ValueError:
        raise CronError(f"{label}: {text!r} is not a number") from None
    if wrap and value in wrap:
        value = wrap[value]
    if not lo <= value <= hi:
        raise CronError(f"{label}: {value} is outside {lo}-{hi}")
    return value


def next_fire(expr: CronExpr, after: datetime, tz: tzinfo) -> datetime:
    """The first firing strictly after ``after``, as an aware datetime in ``tz``.

    ``after`` must be timezone-aware (the scheduler passes an epoch-derived UTC
    instant). Raises :class:`CronError` if the expression cannot match within
    four years — the only way that happens is an impossible date such as
    ``0 0 30 2 *``.
    """
    if after.tzinfo is None:
        raise ValueError("`after` must be timezone-aware")
    local = after.astimezone(tz).replace(second=0, microsecond=0, tzinfo=None)
    naive = _next_naive(expr, local)
    # fold=0 picks the first occurrence of an ambiguous wall time (fall-back), so
    # the job runs once rather than twice. A wall time that does not exist
    # (spring-forward) resolves to the instant one offset-step later, which is
    # still strictly after `after` — the run happens late rather than not at all.
    return naive.replace(tzinfo=tz, fold=0)


def next_fires(expr: CronExpr, after: datetime, tz: tzinfo, count: int) -> list[datetime]:
    """The next ``count`` firings after ``after`` (for the UI's preview)."""
    out: list[datetime] = []
    cursor = after
    for _ in range(max(0, count)):
        cursor = next_fire(expr, cursor, tz)
        out.append(cursor)
    return out


def _next_naive(expr: CronExpr, start: datetime) -> datetime:
    """Next matching naive wall-clock minute strictly after ``start``."""
    cursor = start + timedelta(minutes=1)
    for _ in range(_MAX_DAYS):
        if expr.matches_day(cursor):
            for hour in expr.hours:
                if hour < cursor.hour:
                    continue
                minutes = (
                    expr.minutes
                    if hour > cursor.hour
                    else [m for m in expr.minutes if m >= cursor.minute]
                )
                if minutes:
                    return cursor.replace(hour=hour, minute=min(minutes))
        # Nothing left today: jump to midnight tomorrow. Stepping by day rather
        # than by minute keeps a yearly expression cheap (~1400 iterations
        # worst case instead of two million).
        cursor = (cursor + timedelta(days=1)).replace(hour=0, minute=0)
    raise CronError(f"{expr.source!r} has no matching time within four years")
