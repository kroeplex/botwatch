"""The event envelope: what botwatch writes to stdout, one JSON object per line.

Every driver, whatever it watches, reports through this one shape, so a consumer
can read a mixed stream without knowing which driver produced which line. A
channel *failure* is reported as an ordinary event with ``kind='error'`` on the
same stream, so a consumer that reads events also sees failures rather than
having to watch a second channel for them.
"""

from __future__ import annotations

import datetime as dt
import json
import pydantic


def _format_ts(value: dt.datetime) -> str:
    """Format an instant as ``YYYY-MM-DDTHH:MM:SSZ`` in UTC.

    A naive datetime is taken to be UTC; an aware one is converted. The ``Z``
    (Zulu) suffix is used rather than ``+00:00`` so the on-the-wire form is the
    same across drivers and matches the ISO-8601 form Slack itself emits.
    """
    aware = value.replace(tzinfo=dt.timezone.utc) if value.tzinfo is None else value
    return aware.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Event(pydantic.BaseModel):
    """One thing that happened on one watched channel."""

    model_config = pydantic.ConfigDict(frozen=True, extra='forbid')

    channel: str
    """The botwatch channel key this event came from."""

    type: str
    """The driver type, e.g. ``slack`` — the envelope's documented field name,
    which is why it shadows the builtin."""

    kind: str
    """What kind of event this is, e.g. ``message`` or ``error``."""

    id: str
    """The source-native, stable identity of the event, and the dedup key: the
    same occurrence reported on two runs — a replay after a run that failed
    before recording its position — carries the same ``id`` both times, so a
    consumer deduplicating on ``id`` never processes it twice."""

    ts: dt.datetime
    """When the event happened *at the source*, never when botwatch noticed it,
    so it is stable across a replay too."""

    meta: dict[str, object] = pydantic.Field(default_factory=dict)
    """The channel's ``meta`` from config, carried through verbatim."""

    payload: dict[str, object] = pydantic.Field(default_factory=dict)
    """The driver-specific body of the event."""

    @pydantic.field_serializer('ts')
    def _serialize_ts(self, value: dt.datetime) -> str:
        return _format_ts(value)

    def to_jsonl(self) -> str:
        """Render as a single compact JSON line, without a trailing newline.

        Field order is the model's declaration order, which is the documented
        envelope order (channel, type, kind, id, ts, meta, payload); separators
        are compact so a line is cheap to pipe.
        """
        return json.dumps(
            self.model_dump(mode='json'),
            separators=(',', ':'),
            ensure_ascii=False,
        )

    @classmethod
    def failure(
        cls,
        *,
        channel: str,
        type: str,  # pylint: disable=redefined-builtin
        error: str,
        ts: dt.datetime,
        meta: dict[str, object] | None = None,
    ) -> Event:
        """Build a channel-failure event.

        Its ``id`` is ``<channel>@<ts>`` rather than a source id, because a
        failure has no source event behind it to borrow an id from — while still
        being stable for a given channel and instant, so a replay of the same
        failure deduplicates like any other event.
        """
        return cls(
            channel=channel,
            type=type,
            kind='error',
            id=f'{channel}@{_format_ts(ts)}',
            ts=ts,
            meta=meta or {},
            payload={'error': error},
        )
