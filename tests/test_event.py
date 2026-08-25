import datetime as dt
import json

import pydantic
import pytest

from botwatch import _event

_TS = dt.datetime(2026, 8, 2, 1, 16, 16, tzinfo=dt.timezone.utc)


def _message() -> _event.Event:
    return _event.Event(
        channel='dev-slack',
        type='slack',
        kind='message',
        id='1785633376.000000',
        ts=_TS,
        payload={'text': 'hi'},
    )


def test_to_jsonl_is_compact_with_fields_in_envelope_order() -> None:
    line = _message().to_jsonl()
    assert '\n' not in line
    assert ', ' not in line and ': ' not in line
    assert list(json.loads(line).keys()) == [
        'channel',
        'type',
        'kind',
        'id',
        'ts',
        'meta',
        'payload',
    ]


def test_to_jsonl_renders_ts_as_utc_zulu_seconds() -> None:
    assert json.loads(_message().to_jsonl())['ts'] == '2026-08-02T01:16:16Z'


def _serialized_ts(ts: dt.datetime) -> str:
    line = _message().model_copy(update={'ts': ts}).to_jsonl()
    parsed: str = json.loads(line)['ts']
    return parsed


def test_a_naive_ts_serializes_as_utc() -> None:
    assert _serialized_ts(dt.datetime(2026, 8, 2, 1, 16, 16)) == '2026-08-02T01:16:16Z'


def test_an_aware_ts_is_converted_to_utc() -> None:
    central = dt.timezone(dt.timedelta(hours=-6))
    aware = dt.datetime(2026, 8, 1, 19, 16, 16, tzinfo=central)
    assert _serialized_ts(aware) == '2026-08-02T01:16:16Z'


def test_failure_carries_a_channel_and_instant_scoped_id() -> None:
    event = _event.Event.failure(
        channel='upstream-main', type='git', error='timed out', ts=_TS
    )
    assert event.kind == 'error'
    assert event.id == 'upstream-main@2026-08-02T01:16:16Z'
    assert event.payload == {'error': 'timed out'}


def test_id_is_stable_across_a_replay_so_a_consumer_can_dedup() -> None:
    assert _message().id == _message().id


def test_extra_fields_are_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        _event.Event(
            channel='c',
            type='t',
            kind='message',
            id='1',
            ts=_TS,
            surprise='nope',  # type: ignore[call-arg]
        )
