import asyncio
import botwatch
import contextlib
import pytest
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Literal

_QUIET = timedelta(seconds=0.5)

_LINGER = timedelta(seconds=0.2)


class _Ping(botwatch.Event):
    type: Literal['test.ping'] = 'test.ping'


type _Update = botwatch.Update[int, _Ping]

type _Outcome = _Update | BaseException | Literal['cancelled']
"""What a pull produced: an update, an exception, or a cancellation."""


def _create_update(state: int, *event_ids: str) -> _Update:
    return botwatch.Update(
        events=tuple(
            _Ping(id=event_id, time=datetime.now(timezone.utc))
            for event_id in event_ids
        ),
        state=state,
    )


async def _play(
    script: Sequence[tuple[float, _Update | BaseException]],
    closed: list[str],
    name: str,
) -> botwatch.Source[int, _Ping]:
    """A source that yields (or raises) each scripted item after its delay, in
    seconds, then waits forever like a real one, and records when it's
    closed."""
    try:
        for delay, item in script:
            await asyncio.sleep(delay)
            if isinstance(item, BaseException):
                raise item
            yield item
        await asyncio.Event().wait()
    finally:
        closed.append(name)


def _read_outcome(task: asyncio.Task[_Update]) -> _Outcome:
    outcome: _Outcome
    if task.cancelled():
        outcome = 'cancelled'
    elif (exception := task.exception()) is not None:
        outcome = exception
    else:
        outcome = task.result()
    return outcome


async def _run(
    sources: dict[str, botwatch.Source[int, _Ping]],
    linger: timedelta = _LINGER,
) -> tuple[list[tuple[str, _Outcome]], float]:
    """Runs :func:`botwatch.select` to departure, returning what it yielded and
    how long it took."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    received: list[tuple[str, _Outcome]] = []
    updates = botwatch.select(sources, quiet_timeout=_QUIET, linger=linger)
    async with contextlib.aclosing(updates):
        async for key, task in updates:
            received.append((key, _read_outcome(task)))
    return received, loop.time() - started


def _create_waiting_sources(
    closed: list[str],
) -> dict[str, botwatch.Source[int, _Ping]]:
    """Two sources: one with an event at once, one that never has any."""
    return {
        'a': _play([(0, _create_update(1, 'a1'))], closed, 'a'),
        'b': _play([(0, _create_update(0))], closed, 'b'),
    }


class Test_select:
    async def test__waits_out_the_quiet_timeout(self) -> None:
        closed: list[str] = []

        received, elapsed = await _run(
            {'a': _play([(0, _create_update(0))], closed, 'a')},
        )

        assert received == [('a', _create_update(0))]
        assert elapsed >= _QUIET.total_seconds()
        assert closed == ['a']

    async def test__departs_a_linger_after_the_first_event(self) -> None:
        closed: list[str] = []
        sources = {
            'a': _play([(0.05, _create_update(1, 'a1'))], closed, 'a'),
            'b': _play([(0.1, _create_update(1, 'b1'))], closed, 'b'),
            'c': _play([(0.4, _create_update(1, 'c1'))], closed, 'c'),
        }

        received, elapsed = await _run(sources)

        assert [key for key, _ in received] == ['a', 'b']
        assert elapsed < _QUIET.total_seconds()
        assert sorted(closed) == ['a', 'b', 'c']

    async def test__departs_at_once_without_a_linger(self) -> None:
        closed: list[str] = []

        received, elapsed = await _run(
            _create_waiting_sources(closed),
            linger=timedelta(0),
        )

        assert received[0][0] == 'a'
        assert elapsed < _LINGER.total_seconds()
        assert sorted(closed) == ['a', 'b']

    async def test__failures_never_open_the_window(self) -> None:
        closed: list[str] = []
        sources = {
            'a': _play([(0, ValueError('boom'))], closed, 'a'),
            'b': _play([(0, asyncio.CancelledError())], closed, 'b'),
        }

        received, elapsed = await _run(sources)

        assert sorted((key, repr(outcome)) for key, outcome in received) == [
            ('a', "ValueError('boom')"),
            ('b', "'cancelled'"),
        ]
        assert elapsed >= _QUIET.total_seconds()
        assert sorted(closed) == ['a', 'b']

    async def test__checkpoints_never_open_the_window(self) -> None:
        closed: list[str] = []
        checkpoints = [(0, _create_update(0)), (0.05, _create_update(0))]

        _received, elapsed = await _run({'a': _play(checkpoints, closed, 'a')})

        assert elapsed >= _QUIET.total_seconds()
        assert closed == ['a']

    @pytest.mark.parametrize('with_event', [False, True])
    async def test__when_every_source_ends(self, with_event: bool) -> None:
        """Ended sources pace the run unless an event already opened the
        window."""

        async def end(event_ids: tuple[str, ...]) -> botwatch.Source[int, _Ping]:
            yield _create_update(len(event_ids), *event_ids)

        _received, elapsed = await _run({'a': end(('a1',) if with_event else ())})

        assert (elapsed >= _QUIET.total_seconds()) is not with_event

    async def test__closes_every_source_when_the_consumer_stops_early(
        self,
    ) -> None:
        closed: list[str] = []
        updates = botwatch.select(
            _create_waiting_sources(closed),
            quiet_timeout=_QUIET,
            linger=_LINGER,
        )

        async with contextlib.aclosing(updates):
            async for _ in updates:
                break

        assert sorted(closed) == ['a', 'b']

    async def test__closes_every_source_when_the_consumer_raises(self) -> None:
        closed: list[str] = []
        updates = botwatch.select(
            _create_waiting_sources(closed),
            quiet_timeout=_QUIET,
            linger=_LINGER,
        )

        with pytest.raises(ValueError):
            async with contextlib.aclosing(updates):
                async for _ in updates:
                    raise ValueError('consumer failed')

        assert sorted(closed) == ['a', 'b']

    async def test__closes_every_source_when_the_consumer_is_cancelled(
        self,
    ) -> None:
        closed: list[str] = []
        sources = {
            'a': _play([], closed, 'a'),
            'b': _play([], closed, 'b'),
        }

        async def consume() -> None:
            updates = botwatch.select(sources, quiet_timeout=_QUIET, linger=_LINGER)
            async with contextlib.aclosing(updates):
                async for _ in updates:
                    pass

        consumer = asyncio.create_task(consume())
        await asyncio.sleep(0.05)
        consumer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await consumer

        assert sorted(closed) == ['a', 'b']
