import asyncio
import botwatch
import contextlib
from datetime import datetime
from datetime import timezone
from typing import Literal


class _Note(botwatch.Event):
    type: Literal['test.note'] = 'test.note'

    text: str


def _create_note(text: str) -> _Note:
    return _Note(id=text, time=datetime.now(timezone.utc), text=text)


async def _play(
    updates: list[botwatch.Update[int, _Note]],
    closed: list[bool],
) -> botwatch.Source[int, _Note]:
    """A source that yields ``updates``, then waits forever like a real one,
    and records when it's closed."""
    try:
        for update in updates:
            yield update
        await asyncio.Event().wait()
    finally:
        closed.append(True)


def _is_mine(note: _Note) -> bool:
    return note.text.startswith('[op 1]')


class Test_exclude_events:
    async def test__drops_events_but_keeps_state(self) -> None:
        closed: list[bool] = []
        source = _play(
            [
                botwatch.Update(events=(), state=0),
                botwatch.Update(
                    events=(_create_note('[op 1] mine'), _create_note('kept')),
                    state=1,
                ),
                botwatch.Update(events=(_create_note('[op 1] mine'),), state=2),
            ],
            closed,
        )
        updates = botwatch.exclude_events(source, _is_mine)

        async with contextlib.aclosing(updates):
            received = [
                ([event.text for event in update.events], update.state)
                for update in [await anext(updates) for _ in range(3)]
            ]

        assert received == [([], 0), (['kept'], 1), ([], 2)]
        assert closed == [True]

    async def test__closes_the_source_when_closed(self) -> None:
        closed: list[bool] = []
        updates = botwatch.exclude_events(
            _play([botwatch.Update(events=(), state=0)], closed),
            _is_mine,
        )

        await anext(updates)
        await updates.aclose()

        assert closed == [True]

    async def test__closes_the_source_when_its_pull_is_cancelled(self) -> None:
        closed: list[bool] = []
        updates = botwatch.exclude_events(_play([], closed), _is_mine)

        pull = asyncio.ensure_future(anext(updates))
        await asyncio.sleep(0)
        pull.cancel()
        await asyncio.gather(pull, return_exceptions=True)

        assert closed == [True]
