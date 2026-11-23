"""The source contract: what every source yields."""

import pydantic
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import datetime


class Event(pydantic.BaseModel):
    """Something that happened at a source.

    Each kind of event subclasses this, adding a ``type`` literal namespaced by
    its source type, such as ``slack.message_posted``, and its own fields.
    """

    model_config = pydantic.ConfigDict(frozen=True)

    id: str
    """This occurrence, unique within its source and stable across replays.
    Callers dedupe on the source together with this."""

    time: datetime
    """When it happened at the source."""


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
and its consumer closes it, e.g. with :func:`contextlib.aclosing`. It fails by
raising.
"""
