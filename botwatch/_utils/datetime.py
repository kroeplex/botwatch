from datetime import datetime
from datetime import timezone
from typing import overload


def _to_naive_utc(date_time: datetime) -> datetime:
    return (
        date_time.astimezone(timezone.utc).replace(tzinfo=None)
        if date_time.tzinfo is not None
        else date_time
    )


@overload
def to_rfc3339(date_time: datetime) -> str: ...


@overload
def to_rfc3339(date_time: None) -> None: ...


def to_rfc3339(
    date_time: datetime | None,
) -> str | None:
    """Format :obj:`datetime` object into RFC-3339 / ISO-8601 format relative to UTC
    (i.e.  a ``Z`` suffix).

    If the input datetime object has timezone information attached to it, then
    the datetime is converted to UTC; otherwise, it's assumed that the (naive)
    datetime object is already relative to UTC.

    Note:
        * Unlike Python's default strftime formatting, this method retains only three
          decimals of microsecond precision for better compatibility with various
          systems/APIs/libraries/etc.
    """
    return (
        (_to_naive_utc(date_time).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z')
        if date_time is not None
        else None
    )


def from_rfc3339(text: str) -> datetime:
    """Parse datetime text from RFC-3339 / ISO-8601 format with ``Z`` suffix (UTC) into
    a :obj:`datetime.datetime` object.
    """
    date_format = '%Y-%m-%dT%H:%M:%SZ' if '.' not in text else '%Y-%m-%dT%H:%M:%S.%fZ'
    return datetime.strptime(text, date_format).replace(tzinfo=timezone.utc)
