import asyncio
import botwatch
import contextlib
import pytest
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Literal

_QUIET = timedelta(seconds=0.3)

_LINGER = timedelta(seconds=0.1)


class _Ping(botwatch.Event):
    type: Literal['test.ping'] = 'test.ping'


type _Update = botwatch.Update[int, _Ping]


def _update(state: int, *event_ids: str) -> _Update:
    """An update whose state counts the events so far."""
    return botwatch.Update(
        events=tuple(
            _Ping(id=event_id, time=datetime.now(timezone.utc))
            for event_id in event_ids
        ),
        state=state,
    )


async def _play(
    script: list[tuple[float, _Update | Exception]],
    closed: list[str],
    name: str,
) -> botwatch.Source[int, _Ping]:
    """A source that yields (or raises) each scripted item after its delay, in
    seconds, then waits forever like a real one."""
    try:
        for delay, item in script:
            await asyncio.sleep(delay)
            if isinstance(item, Exception):
                raise item
            yield item
        await asyncio.Event().wait()
    finally:
        closed.append(name)


async def _run(
    sources: dict[str, botwatch.Source[int, _Ping]],
) -> tuple[list[tuple[str, _Update | BaseException]], float]:
    """Runs :func:`botwatch.select` to departure, returning what it yielded and
    how long it took."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    received: list[tuple[str, _Update | BaseException]] = []
    updates = botwatch.select(sources, quiet_timeout=_QUIET, linger=_LINGER)
    async with contextlib.aclosing(updates):
        async for key, task in updates:
            exception = task.exception()
            received.append((key, task.result() if exception is None else exception))
    return received, loop.time() - started


async def test_select__waits_out_the_quiet_timeout() -> None:
    closed: list[str] = []

    received, elapsed = await _run({'a': _play([(0, _update(0))], closed, 'a')})

    assert received == [('a', _update(0))]
    assert elapsed >= _QUIET.total_seconds()
    assert closed == ['a']


async def test_select__departs_a_linger_after_the_first_event() -> None:
    closed: list[str] = []
    early = _play([(0, _update(0)), (0.05, _update(1, 'a1'))], closed, 'a')
    within = _play([(0, _update(0)), (0.1, _update(1, 'b1'))], closed, 'b')
    late = _play([(0, _update(0)), (0.25, _update(1, 'c1'))], closed, 'c')

    received, elapsed = await _run({'a': early, 'b': within, 'c': late})

    assert [key for key, item in received if item != _update(0)] == ['a', 'b']
    assert elapsed < _QUIET.total_seconds()
    assert sorted(closed) == ['a', 'b', 'c']


async def test_select__failures_never_open_the_window() -> None:
    closed: list[str] = []
    failing = _play([(0, ValueError('boom'))], closed, 'a')

    received, elapsed = await _run({'a': failing})

    assert [(key, repr(item)) for key, item in received] == [
        ('a', "ValueError('boom')"),
    ]
    assert elapsed >= _QUIET.total_seconds()


async def test_select__checkpoints_never_open_the_window() -> None:
    closed: list[str] = []
    checkpoints = _play([(0, _update(0)), (0.05, _update(0))], closed, 'a')

    _received, elapsed = await _run({'a': checkpoints})

    assert elapsed >= _QUIET.total_seconds()


@pytest.mark.parametrize('with_event', [False, True])
async def test_select__when_every_source_ends(with_event: bool) -> None:
    """Ended sources pace the run unless an event already opened the window."""

    async def end(event_ids: tuple[str, ...]) -> botwatch.Source[int, _Ping]:
        yield _update(len(event_ids), *event_ids)

    _received, elapsed = await _run({'a': end(('a1',) if with_event else ())})

    assert (elapsed >= _QUIET.total_seconds()) is not with_event
