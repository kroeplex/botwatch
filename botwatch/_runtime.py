"""The runtime: hold every channel open at once, and depart on the two clocks.

One run instantiates a driver per configured channel, pulls their events onto one
stream, and returns when either clock says so: the **quiet timeout** if nothing
happens at all, or the **boarding window** measured from the first event. Events
are written as JSONL as they arrive; a driver failure is localised to its own
channel and reported as an ``error`` event on the same stream.

The pull is lookahead-free: a driver is advanced only by requesting its next
event, so on departure the outstanding requests are cancelled with nothing
produced beyond what was already emitted -- which is what lets ``get_state``
stay consistent with the emitted stream (never ahead, so never dropping).
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
from . import _config
from . import _driver
from . import _state
from ._event import Event
from collections.abc import AsyncGenerator
from collections.abc import Mapping
from typing import TextIO

_STOP = object()
"""Returned by :func:`_next_event` when a driver's stream is exhausted."""

_Active = Mapping[str, tuple['_config.ChannelConfig', '_driver.ChannelDriver']]


def _emit(out: TextIO, event: Event) -> None:
    """Write one event as a JSONL line and flush, so a pipe reader sees it now."""
    out.write(event.to_jsonl() + '\n')
    out.flush()


async def _next_event(source: AsyncGenerator[Event, None]) -> object:
    """Pull the next event, or ``_STOP`` when the stream ends.

    Any other exception propagates to the caller, which localises it to the
    channel (driver obligation 4). ``StopAsyncIteration`` must not escape a
    coroutine, which is why it becomes the sentinel here.
    """
    try:
        result: object = await anext(source)
    except StopAsyncIteration:
        result = _STOP
    return result


@dataclasses.dataclass
class _Clocks:
    """The two departure clocks: the quiet timeout until the first event, then a
    boarding window measured from it."""

    quiet: float
    boarding: float
    deadline: float | None = None

    @classmethod
    def from_defaults(cls, defaults: _config.Defaults) -> _Clocks:
        return cls(
            quiet=defaults.quiet_timeout.total_seconds(),
            boarding=defaults.boarding_window.total_seconds(),
        )

    def timeout(self) -> float:
        """How long to wait next: the quiet timeout until the first event, then
        whatever remains of the boarding window."""
        if self.deadline is None:
            result = self.quiet
        else:
            result = max(0.0, self.deadline - asyncio.get_running_loop().time())
        return result

    def board(self) -> None:
        """Start the boarding window when the first event arrives."""
        if self.deadline is None:
            self.deadline = asyncio.get_running_loop().time() + self.boarding


def _consume(
    task: asyncio.Task[object],
    channel: _config.ChannelConfig,
    *,
    out: TextIO,
    now: dt.datetime,
    last_event: dict[str, dt.datetime],
    errors: dict[str, str],
) -> bool:
    """Handle one completed pull. Returns whether the channel should be pulled
    again -- true only when a real event was emitted (a failure or an exhausted
    stream is not rescheduled)."""
    reschedule = False
    try:
        result = task.result()
    except Exception as exc:  # pylint: disable=broad-exception-caught
        # Obligation 4: any driver error fails *its* channel, not the run.
        errors[channel.key] = str(exc) or exc.__class__.__name__
        _emit(
            out,
            Event.failure(
                channel=channel.key,
                type=channel.type,
                error=errors[channel.key],
                ts=now,
                meta=dict(channel.meta),
            ),
        )
    else:
        if result is not _STOP:
            assert isinstance(result, Event)  # narrows the sentinel union
            _emit(out, result)
            last_event[channel.key] = result.ts
            reschedule = True
    return reschedule


async def _shutdown(
    pending: Mapping[asyncio.Task[object], str],
    streams: Mapping[str, AsyncGenerator[Event, None]],
) -> None:
    """Cancel the outstanding pulls and close every stream, running each
    driver's cleanup (obligation 2)."""
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    for stream in streams.values():
        await stream.aclose()


async def _select(
    active: _Active,
    *,
    defaults: _config.Defaults,
    out: TextIO,
    now: dt.datetime,
) -> tuple[dict[str, dt.datetime], dict[str, str]]:
    """Multiplex the drivers onto one stream under the two clocks.

    Returns the source time of the last event emitted per channel, and the error
    message per channel that failed -- what the caller records.
    """
    streams = {key: driver.watch() for key, (_cfg, driver) in active.items()}
    pending: dict[asyncio.Task[object], str] = {
        asyncio.ensure_future(_next_event(stream)): key
        for key, stream in streams.items()
    }
    last_event: dict[str, dt.datetime] = {}
    errors: dict[str, str] = {}
    clocks = _Clocks.from_defaults(defaults)

    while pending:
        done, _ = await asyncio.wait(
            pending, timeout=clocks.timeout(), return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            break  # a clock elapsed with nothing produced -- depart
        for task in done:
            key = pending.pop(task)
            reschedule = _consume(
                task,
                active[key][0],
                out=out,
                now=now,
                last_event=last_event,
                errors=errors,
            )
            if reschedule:
                clocks.board()
                pending[asyncio.ensure_future(_next_event(streams[key]))] = key

    await _shutdown(pending, streams)
    return last_event, errors


async def run_once(
    config: _config.Config,
    state: _state.State,
    registry: _driver.Registry,
    *,
    out: TextIO,
    now: dt.datetime,
) -> None:
    """Do one run: build the drivers, select over them, record the outcomes.

    Mutates ``state`` in place -- the caller saves it. Emits events (and channel
    failures) to ``out`` as they arrive.
    """
    context = _driver.RunContext(now=now)
    active: dict[str, tuple[_config.ChannelConfig, _driver.ChannelDriver]] = {}
    for channel in config.channels:
        factory = registry.get(channel.type)
        if factory is None:
            error = f'no driver registered for type {channel.type!r}'
            _emit(
                out,
                Event.failure(
                    channel=channel.key,
                    type=channel.type,
                    error=error,
                    ts=now,
                    meta=dict(channel.meta),
                ),
            )
            state.record(channel, now=now, error=error)
            continue
        resume = state.resume(channel)
        active[channel.key] = (
            channel,
            factory(
                channel.key,
                channel,
                resume.prev_config,
                resume.prev_state,
                context,
            ),
        )

    last_event, errors = await _select(
        active, defaults=config.defaults, out=out, now=now
    )

    for key, (channel, driver) in active.items():
        if key in errors:
            state.record(channel, now=now, error=errors[key])
        else:
            state.record(
                channel,
                now=now,
                driver=driver.get_state(),
                last_event=last_event.get(key),
            )
    state.retire_absent({channel.key for channel in config.channels}, now=now)
