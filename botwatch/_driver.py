"""The channel-driver contract: how a source is watched.

A driver is constructed once per configured channel per run, watches its source,
and reports the state to resume from next time. Events flow out and nothing
flows in: the runtime -- the only party that knows the run's timing -- decides
when to stop pulling and closes the stream, and that close is what makes the
driver's final state readable.

The runtime advances a driver only by asking for its next event, so a driver
that has yielded event *k* and is awaiting the request for *k+1* has, on close,
a state consistent with exactly *k* emitted. That is what keeps ``get_state``
from running ahead of what was emitted (which would drop events).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from . import _config
from ._event import Event
from collections.abc import AsyncGenerator
from collections.abc import Callable
from typing import Protocol


@dataclasses.dataclass(frozen=True)
class RunContext:
    """Run-wide settings a driver may read.

    Deliberately narrow -- widening it later breaks no driver, narrowing it
    later breaks every driver already written against the wide type.
    """

    now: dt.datetime
    """The instant the run started; a driver computes start windows from it."""


class ChannelDriver(Protocol):
    """Watches one source and reports its new events."""

    def watch(self) -> AsyncGenerator[Event, None]:
        """Yield each new event once, in source order within a sub-stream, and
        stay open until the stream is closed."""

    def get_state(self) -> dict[str, object]:
        """The opaque resume state to record -- consistent with what ``watch``
        yielded: never ahead of the yields (that drops events), at worst behind
        (that merely replays them)."""


DriverFactory = Callable[
    [
        str,  # the channel key
        _config.ChannelConfig,  # this channel's config this run
        _config.ChannelConfig | None,  # prev_config, or None if new
        dict[str, object] | None,  # prev_state, or None if never positioned
        RunContext,
    ],
    ChannelDriver,
]
"""How the runtime builds a driver for a channel. A driver type registers one of
these under its ``type`` name so the runtime can construct it from the config."""


Registry = dict[str, DriverFactory]
"""Maps a channel ``type`` to the factory that builds its driver."""
