"""Small, dependency-free parser for standard five-field Linux cron schedules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re


class CronExpressionError(ValueError):
    """Raised when a schedule is not a supported five-field cron expression."""


@dataclass(frozen=True)
class CronExpression:
    expression: str
    minute: frozenset[int]
    hour: frozenset[int]
    day_of_month: frozenset[int]
    month: frozenset[int]
    day_of_week: frozenset[int]
    day_of_month_wildcard: bool
    day_of_week_wildcard: bool

    def matches(self, timestamp: datetime) -> bool:
        """Return whether this cron expression is due for the supplied minute."""

        if timestamp.minute not in self.minute or timestamp.hour not in self.hour:
            return False
        if timestamp.month not in self.month:
            return False

        day_of_month_matches = timestamp.day in self.day_of_month
        # Python begins weekdays at Monday=0; Linux cron treats Sunday as 0 or 7.
        day_of_week_matches = ((timestamp.weekday() + 1) % 7) in self.day_of_week
        if self.day_of_month_wildcard and self.day_of_week_wildcard:
            return True
        if self.day_of_month_wildcard:
            return day_of_week_matches
        if self.day_of_week_wildcard:
            return day_of_month_matches
        return day_of_month_matches or day_of_week_matches


_FIELD_SPECS = (
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day of month", 1, 31, {}),
    ("month", 1, 12, {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}),
    ("day of week", 0, 7, {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6}),
)
_DAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _replace_names(value: str, names: dict[str, int]) -> str:
    normalized = value.lower()
    for name, number in names.items():
        normalized = re.sub(rf"\b{name}\b", str(number), normalized)
    return normalized


def _number(value: str, *, minimum: int, maximum: int, field_name: str) -> int:
    if not value.isdecimal():
        raise CronExpressionError(f"The {field_name} field contains an invalid value.")
    number = int(value)
    if number < minimum or number > maximum:
        raise CronExpressionError(f"The {field_name} field must be between {minimum} and {maximum}.")
    return number


def _field_values(value: str, *, minimum: int, maximum: int, field_name: str, names: dict[str, int]) -> tuple[frozenset[int], bool]:
    normalized = _replace_names(value, names)
    wildcard = normalized == "*"
    values: set[int] = set()
    for item in normalized.split(","):
        if not item:
            raise CronExpressionError(f"The {field_name} field contains an empty list item.")
        pieces = item.split("/")
        if len(pieces) > 2:
            raise CronExpressionError(f"The {field_name} field has an invalid step value.")
        base = pieces[0]
        step = 1
        if len(pieces) == 2:
            step = _number(pieces[1], minimum=1, maximum=maximum - minimum + 1, field_name=field_name)
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            range_parts = base.split("-")
            if len(range_parts) != 2:
                raise CronExpressionError(f"The {field_name} field has an invalid range.")
            start = _number(range_parts[0], minimum=minimum, maximum=maximum, field_name=field_name)
            end = _number(range_parts[1], minimum=minimum, maximum=maximum, field_name=field_name)
            if start > end:
                raise CronExpressionError(f"The {field_name} field range must be ascending.")
        else:
            start = end = _number(base, minimum=minimum, maximum=maximum, field_name=field_name)
        values.update(range(start, end + 1, step))
    if field_name == "day of week" and 7 in values:
        values.remove(7)
        values.add(0)
    return frozenset(values), wildcard


def parse_cron_expression(value: str) -> CronExpression:
    """Parse a POSIX-style five-field cron expression without shell commands."""

    if not isinstance(value, str):
        raise CronExpressionError("A five-field Linux cron expression is required.")
    parts = value.split()
    if len(parts) != 5:
        raise CronExpressionError("Use five cron fields: minute hour day-of-month month day-of-week.")
    parsed = [
        _field_values(part, minimum=minimum, maximum=maximum, field_name=name, names=names)
        for part, (name, minimum, maximum, names) in zip(parts, _FIELD_SPECS, strict=True)
    ]
    return CronExpression(
        expression=" ".join(parts),
        minute=parsed[0][0],
        hour=parsed[1][0],
        day_of_month=parsed[2][0],
        month=parsed[3][0],
        day_of_week=parsed[4][0],
        day_of_month_wildcard=parsed[2][1],
        day_of_week_wildcard=parsed[4][1],
    )


def _simple_step(values: frozenset[int], minimum: int, maximum: int) -> int | None:
    for step in range(1, maximum - minimum + 2):
        if values == frozenset(range(minimum, maximum + 1, step)):
            return step
    return None


def _list_label(values: frozenset[int], labels: tuple[str, ...] | None = None) -> str:
    rendered = [labels[value] if labels else str(value) for value in sorted(values)]
    if len(rendered) == 1:
        return rendered[0]
    if len(rendered) == 2:
        return " and ".join(rendered)
    return ", ".join(rendered[:-1]) + ", and " + rendered[-1]


def describe_cron_expression(value: str | CronExpression) -> str:
    """Return a concise readable schedule description for the collection UI."""

    schedule = parse_cron_expression(value) if isinstance(value, str) else value
    minute_step = _simple_step(schedule.minute, 0, 59)
    hour_step = _simple_step(schedule.hour, 0, 23)
    if minute_step == 1 and hour_step == 1:
        description = "Every minute"
    elif len(schedule.minute) == 1 and len(schedule.hour) == 1:
        description = f"At {next(iter(schedule.hour)):02d}:{next(iter(schedule.minute)):02d}"
    elif len(schedule.minute) == 1 and hour_step == 1:
        description = f"At minute {next(iter(schedule.minute)):02d} past every hour"
    elif len(schedule.minute) == 1 and hour_step and hour_step > 1:
        description = f"At minute {next(iter(schedule.minute)):02d} past every {hour_step}th hour"
    elif minute_step and minute_step > 1 and hour_step == 1:
        description = f"Every {minute_step} minutes"
    else:
        description = f"At minutes {_list_label(schedule.minute)} during hours {_list_label(schedule.hour)}"

    qualifiers: list[str] = []
    if not schedule.day_of_month_wildcard:
        qualifiers.append(f"on day {_list_label(schedule.day_of_month)}")
    if not schedule.month == frozenset(range(1, 13)):
        qualifiers.append(f"in {_list_label(schedule.month, (None,) + _MONTH_NAMES)}")
    if not schedule.day_of_week_wildcard:
        qualifiers.append(f"on {_list_label(schedule.day_of_week, _DAY_NAMES)}")
    return description + (" " + " ".join(qualifiers) if qualifiers else "") + "."
