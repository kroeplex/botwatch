"""Slack message timestamps."""

import pydantic
from datetime import datetime
from datetime import timezone
from typing import Annotated

type SlackTs = Annotated[str, pydantic.StringConstraints(pattern=r'^\d{10}\.\d{6}$')]
"""A Slack message timestamp, such as ``1785600000.000200``: seconds and
microseconds since the epoch.

Within a conversation it is a message's ID. Its fixed width makes string order
time order.
"""

EPOCH: SlackTs = '0000000000.000000'
"""Before every message."""


def ts_to_datetime(ts: SlackTs) -> datetime:
    """Parses a Slack timestamp into an aware UTC datetime, exactly."""
    seconds, microseconds = ts.split('.')
    return datetime.fromtimestamp(int(seconds), timezone.utc).replace(
        microsecond=int(microseconds),
    )
