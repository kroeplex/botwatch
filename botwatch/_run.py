"""One run: select over the configured sources and write their events."""

import asyncio
import contextlib
import logging
import pydantic
import uuid
from . import _config
from . import _select
from . import _source
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


def _get_error[ResultT](task: asyncio.Task[ResultT]) -> BaseException | None:
    """What a source's pull raised, if anything, a cancellation of its own
    included."""
    return asyncio.CancelledError() if task.cancelled() else task.exception()


def _create_failure(error: BaseException) -> SourceFailed:
    return SourceFailed(
        id=uuid.uuid4().hex,
        time=datetime.now(timezone.utc),
        error=repr(error),
    )


async def run_once(
    config: _config.Config,
    *,
    slack_client: slack.SlackClient,
    out: TextIO,
) -> None:
    """Starts every configured source from now, selects over them until the run
    departs, and writes each event to ``out`` as a JSON line."""
    sources = {
        source.key: source.watch(slack_client, None) for source in config.sources
    }
    updates = _select.select(
        sources,
        quiet_timeout=config.quiet_timeout,
        linger=config.linger,
    )
    async with contextlib.aclosing(updates):
        async for key, task in updates:
            error = _get_error(task)
            if error is None:
                for event in task.result().events:
                    _write_record(out, Record(source=key, event=event))
            else:
                _logger.error('source %s failed', key, exc_info=error)
                _write_record(out, Record(source=key, event=_create_failure(error)))
