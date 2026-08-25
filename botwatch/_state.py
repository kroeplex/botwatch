"""The state file: botwatch's entire memory between runs.

One file the caller names, holding a block per channel. botwatch owns every
field except ``driver``, which is opaque -- the driver puts whatever it needs to
resume there (for the Slack driver, a per-thread cursor map). The write is
atomic (temp file renamed over the target) and happens at exit, after events are
emitted.

The lifecycle that "survives reconfiguration" turns on distinguishing five cases
for a channel, and it is ``prev_state`` -- a recorded *position* -- not
``prev_config`` that separates a channel that has run from one merely configured:

    new                          not in the state file at all
    configured, never positioned  block exists, driver never recorded (a first
                                  run whose driver failed)
    returning (resume)            block exists, driver recorded, config unchanged
    reconfigured                  block exists, driver recorded, config differs
    removed -> retired            in the state file, absent from this config
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import json
import os
import pydantic
import tempfile
from . import _config
from pathlib import Path


class Case(enum.Enum):
    """Which of the reconfiguration cases a channel is in this run."""

    NEW = 'new'
    UNPOSITIONED = 'configured-never-positioned'
    RETURNING = 'returning'
    RECONFIGURED = 'reconfigured'


class ChannelState(pydantic.BaseModel):
    """One channel's block. Every field is botwatch's except ``driver``."""

    model_config = pydantic.ConfigDict(extra='forbid')

    created: dt.datetime
    """When this key was first seen; written once, never rewritten."""

    updated: dt.datetime
    """When this run wrote the block."""

    last_event: dt.datetime | None = None
    """Source time of the most recent event emitted for this channel."""

    last_error: str | None = None
    """The most recent failure's message, cleared on the next configured run
    that does not fail."""

    last_error_at: dt.datetime | None = None
    """When the most recent failure happened; refreshed each failure."""

    failing_since: dt.datetime | None = None
    """Start of the current failure streak; set on the first failure of a streak
    and cleared on success, so the two error timestamps bracket the streak."""

    retired: dt.datetime | None = None
    """Set when the key leaves the config; cleared on its return."""

    meta: dict[str, object] = pydantic.Field(default_factory=dict)
    """The channel's ``meta`` from config, copied verbatim."""

    config: dict[str, object] | None = None
    """The channel's own config as last run -- the source of ``prev_config``.
    Kept per-block (not only a whole-config embed) so it survives retirement,
    when the channel is no longer in the run config at all."""

    driver: dict[str, object] | None = None
    """The driver's opaque resume state. ``None`` means "never positioned" -- the
    discriminator between a channel with a resume point and one without."""


class Resume(pydantic.BaseModel):
    """What a driver is handed to resume: the case, its prior config, position."""

    model_config = pydantic.ConfigDict(frozen=True)

    case: Case
    """Which reconfiguration case the channel is in this run."""

    prev_config: _config.ChannelConfig | None
    """The channel's config as last run, or ``None`` if it is new."""

    prev_state: dict[str, object] | None
    """The driver's recorded position, or ``None`` if never positioned."""


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file in the same directory renamed
    over the target, so a crash mid-write never leaves a torn file."""
    descriptor, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f'.{path.name}.', suffix='.tmp'
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _iso(value: object) -> str:
    """Render an aware (or assumed-UTC) datetime as an ISO-8601 string."""
    if not isinstance(value, dt.datetime):
        raise TypeError(f'not a datetime: {type(value).__name__}')
    aware = value if value.tzinfo is not None else value.replace(tzinfo=dt.timezone.utc)
    return aware.isoformat()


@dataclasses.dataclass
class State:
    """The in-memory state for one run: load it, resume from it, record into it,
    write it back.

    Non-frozen: it accumulates per-channel outcomes across a run before the
    single write at exit.
    """

    channels: dict[str, ChannelState]
    created: dt.datetime
    version: int = 1

    @classmethod
    def load(cls, path: Path | str, *, now: dt.datetime) -> State:
        """Load the state file, or start empty if it does not exist yet.

        A first run against a nonexistent path is the normal startup, not an
        error -- losing the file costs a re-read of each channel's start window,
        never correctness.
        """
        path = Path(path)
        if not path.exists():
            state = cls(channels={}, created=now)
        else:
            raw = json.loads(path.read_text(encoding='utf-8'))
            state = cls(
                channels={
                    key: ChannelState.model_validate(block)
                    for key, block in raw.get('channels', {}).items()
                },
                created=dt.datetime.fromisoformat(raw['created']),
                version=raw.get('version', 1),
            )
        return state

    def resume(self, config: _config.ChannelConfig) -> Resume:
        """Resolve how ``config``'s channel resumes, from what the file holds."""
        block = self.channels.get(config.key)
        if block is None:
            result = Resume(case=Case.NEW, prev_config=None, prev_state=None)
        else:
            prev_config = (
                _config.ChannelConfig.model_validate(block.config)
                if block.config is not None
                else None
            )
            if block.driver is None:
                case = Case.UNPOSITIONED
            elif prev_config == config:
                case = Case.RETURNING
            else:
                case = Case.RECONFIGURED
            result = Resume(case=case, prev_config=prev_config, prev_state=block.driver)
        return result

    def record(
        self,
        config: _config.ChannelConfig,
        *,
        now: dt.datetime,
        driver: dict[str, object] | None = None,
        last_event: dt.datetime | None = None,
        error: str | None = None,
    ) -> None:
        """Write a channel's outcome back into the in-memory state.

        ``created`` is stamped once and never rewritten; a channel returning from
        retirement keeps ``created`` and its retained ``driver`` cursor but starts
        its health fresh; a failure keeps the prior position untouched and only
        brackets the error streak; a success clears the error and the streak.
        """
        prior = self.channels.get(config.key)
        returning_from_retirement = prior is not None and prior.retired is not None

        if prior is None:
            base = ChannelState(created=now, updated=now)
        elif returning_from_retirement:
            base = ChannelState(created=prior.created, updated=now, driver=prior.driver)
        else:
            base = prior.model_copy()

        common: dict[str, object] = {
            'updated': now,
            'retired': None,
            'meta': dict(config.meta),
            'config': config.model_dump(mode='json'),
        }
        if error is not None:
            self.channels[config.key] = base.model_copy(
                update={
                    **common,
                    'last_error': error,
                    'last_error_at': now,
                    'failing_since': base.failing_since or now,
                    # obligation 4: a failure leaves the recorded position alone.
                    'driver': base.driver,
                }
            )
        else:
            self.channels[config.key] = base.model_copy(
                update={
                    **common,
                    'last_event': last_event or base.last_event,
                    'last_error': None,
                    'last_error_at': None,
                    'failing_since': None,
                    'driver': driver if driver is not None else base.driver,
                }
            )

    def retire_absent(self, live_keys: set[str], *, now: dt.datetime) -> None:
        """Retire -- retain, do not delete -- every channel absent from this run.

        Its ``driver`` cursor is kept so a return resumes; ``retired`` is stamped
        once; health fields freeze as they are.
        """
        for key, block in self.channels.items():
            if key not in live_keys and block.retired is None:
                self.channels[key] = block.model_copy(update={'retired': now})

    def to_document(self, *, now: dt.datetime) -> dict[str, object]:
        """Build the serialisable state document -- pure, no IO, so it can be
        checked without touching disk."""
        return {
            'version': self.version,
            'created': _iso(self.created),
            'updated': _iso(now),
            'channels': {
                key: block.model_dump(mode='json')
                for key, block in self.channels.items()
            },
        }

    def save(self, path: Path | str, *, now: dt.datetime) -> None:
        """Serialise (via :meth:`to_document`) and write atomically."""
        text = json.dumps(self.to_document(now=now), indent=2) + '\n'
        _atomic_write(Path(path), text)
