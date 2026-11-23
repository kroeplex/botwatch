"""One run: select over the configured sources and write their events.

Design spec: **where a run ends never changes what is emitted.** Given the same
observations from the sources, successive runs emit, per source, the same
sequence of events once repeats are dropped by ``(source, id)``, ``botwatch.*``
events aside, however each run ends: timeout, linger, or a crash. A crash may
repeat events but never skips one. The gap between runs observes nothing, so
several changes inside it can surface as one, as with a slower poll.

Checked by ``test_run_once__where_a_run_ends_never_changes_what_is_emitted``.
"""

import contextlib
import logging
import pydantic
import uuid
from . import _config
from . import _select
from . import _source
from . import _state
from . import slack
from datetime import datetime
from datetime import timezone
from typing import Annotated
from typing import Literal
from typing import TextIO

_logger = logging.getLogger(__name__)


class SourceFailed(_source.Event):
    """A source raised. It stays down for the rest of the run."""

    type: Literal['botwatch.source_failed'] = 'botwatch.source_failed'

    error: str
    """What the source raised."""


type OutputEvent = Annotated[
    slack.MessagePosted | SourceFailed,
    pydantic.Field(discriminator='type'),
]
"""Every event a run can write."""


class Record(pydantic.BaseModel):
    """One line of output."""

    model_config = pydantic.ConfigDict(frozen=True)

    source: _config.SourceKey

    event: OutputEvent


def _write_record(out: TextIO, record: Record) -> None:
    """Writes one JSON line and flushes it, so a reader on a pipe gets it now."""
    out.write(record.model_dump_json() + '\n')
    out.flush()


def _create_failure(error: BaseException) -> SourceFailed:
    return SourceFailed(
        id=uuid.uuid4().hex,
        time=datetime.now(timezone.utc),
        error=repr(error),
    )


async def run_once(
    config: _config.Config,
    state: _state.StateFile,
    *,
    slack_client: slack.SlackClient,
    out: TextIO,
) -> _state.StateFile:
    """Starts every configured source from its state in ``state``, or from now,
    selects over them until the run departs, and writes each event to ``out``
    as a JSON line.

    Returns the state to save: each configured source's state from its last
    update. A source that failed keeps the state of its last update before the
    failure.
    """
    sources = {
        source.key: source.watch(
            slack_client,
            state=state.sources.get(source.key),
        )
        for source in config.sources
    }
    latest = {key: state.sources[key] for key in sources if key in state.sources}
    updates = _select.select(
        sources,
        quiet_timeout=config.quiet_timeout,
        linger=config.linger,
    )
    async with contextlib.aclosing(updates):
        async for key, task in updates:
            error = _select.get_error(task)
            if error is None:
                update = task.result()
                for event in update.events:
                    _write_record(out, Record(source=key, event=event))
                latest[key] = update.state
            else:
                _logger.error('source %s failed', key, exc_info=error)
                _write_record(out, Record(source=key, event=_create_failure(error)))
    return _state.StateFile(sources=latest)
