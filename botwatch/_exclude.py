"""A stage that drops unwanted events but keeps their source's place."""

import contextlib
from . import _source
from collections.abc import Callable


async def exclude_events[StateT, EventT: _source.Event](
    source: _source.Source[StateT, EventT],
    is_excluded: Callable[[EventT], bool],
) -> _source.Source[StateT, EventT]:
    """Wraps ``source``, dropping the events ``is_excluded`` matches.

    Each update keeps its state, so excluded events still advance the source,
    and an update whose events are all excluded passes on as a checkpoint. Takes
    ownership of ``source`` and closes it.
    """
    async with contextlib.aclosing(source):
        async for update in source:
            yield _source.Update(
                events=tuple(
                    event for event in update.events if not is_excluded(event)
                ),
                state=update.state,
            )
