"""Selecting over many sources at once, until the run departs."""

import asyncio
import contextlib
import turbopipes
from . import _source
from collections.abc import AsyncGenerator
from collections.abc import Callable
from collections.abc import Mapping
from datetime import timedelta


async def _linger[ItemT](
    items: AsyncGenerator[ItemT, None],
    *,
    quiet_timeout: timedelta,
    linger: timedelta,
    opens: Callable[[ItemT], bool],
) -> AsyncGenerator[ItemT, None]:
    """Passes ``items`` through until ``linger`` after the first item that
    ``opens`` the window, or until ``quiet_timeout`` if none does, then closes
    ``items``.

    If ``items`` ends before the window opens, waits out the quiet timeout
    anyway, so a caller looping on this is paced by it rather than spinning.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + quiet_timeout.total_seconds()
    is_open = False

    async with contextlib.aclosing(items):
        try:
            while True:
                # Cancelling the pull on timeout is what closes the sources:
                # the cancellation reaches each one where it waits.
                item = await asyncio.wait_for(anext(items), deadline - loop.time())
                if not is_open and opens(item):
                    is_open = True
                    deadline = loop.time() + linger.total_seconds()
                yield item
        except TimeoutError:
            pass  # departure
        except StopAsyncIteration:
            if not is_open:
                await asyncio.sleep(max(0.0, deadline - loop.time()))


def _has_events[KeyT, StateT, EventT: _source.Event](
    item: tuple[KeyT, asyncio.Task[_source.Update[StateT, EventT]]],
) -> bool:
    """Whether an item from :func:`turbopipes.aselect` is an update with
    events, as opposed to a checkpoint or a failure."""
    _key, task = item
    return task.exception() is None and bool(task.result().events)


def select[KeyT, StateT, EventT: _source.Event](
    sources: Mapping[KeyT, _source.Source[StateT, EventT]],
    *,
    quiet_timeout: timedelta,
    linger: timedelta,
) -> AsyncGenerator[tuple[KeyT, asyncio.Task[_source.Update[StateT, EventT]]], None]:
    """Selects over ``sources`` until the run departs, yielding each update as
    it arrives, as a ``(key, task)`` pair.

    The pairs are :func:`turbopipes.aselect`'s: the task has completed, and
    awaiting it gives the update, or raises what the source raised.

    Waits up to ``quiet_timeout`` for an update with events, then exactly
    ``linger`` more, counted from that update, and then closes every source.
    Checkpoints and failures never open that window. If every source ends or
    fails before it opens, the run still lasts the quiet timeout.

    Example::

        sources = {
            'top': slack.watch(client, 'C0123456789', None),
            'thread': slack.watch(client, 'C0123456789', None, thread_ts='…'),
        }
        updates = botwatch.select(
            sources,
            quiet_timeout=timedelta(minutes=55),
            linger=timedelta(seconds=5),
        )
        async with contextlib.aclosing(updates):
            async for key, task in updates:
                update = await task
                ...
    """
    return _linger(
        turbopipes.aselect(sources),
        quiet_timeout=quiet_timeout,
        linger=linger,
        opens=_has_events,
    )
