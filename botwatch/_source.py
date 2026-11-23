"""The source contract: what every source yields."""

import pydantic
from . import _utils
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime


class Event(pydantic.BaseModel):
    """Something that happened at a source.

    Each kind of event subclasses this, narrowing ``type`` to a literal
    namespaced by its source type, such as ``slack.message_posted``, and adding
    its own fields.
    """

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    type: str

    id: str
    """This occurrence, unique within its source and stable across replays.
    Callers dedupe on the source together with this."""

    time: datetime
    """When it happened at the source."""

    @pydantic.field_serializer('time', when_used='json')
    def _serialize_time(self, time: datetime) -> str:
        return _utils.datetime.to_rfc3339(time)


@dataclass(frozen=True)
class Update[StateT, EventT: Event]:
    """What a source yields: zero or more events, and its full state after them.

    An update without events is a checkpoint. A consumer handles an update
    whole: it writes the events, then keeps the state, so the state it keeps
    always accounts for exactly the events it has written.
    """

    events: tuple[EventT, ...]
    """Zero or more events, in source order."""

    state: StateT
    """The source's full state after these events."""


type Source[StateT, EventT: Event] = AsyncGenerator[Update[StateT, EventT], None]
"""A source's stream of updates.

A source is created from its previous state, or from none to start from now,
and its first update is a checkpoint of its starting state. It yields forever,
and it fails by raising.

Its consumer stops it by cancelling the pull in progress, or by closing it
(e.g. with :func:`contextlib.aclosing`) when none is. Either way its cleanup
runs, so a source must tolerate cancellation at any ``await``.
"""
