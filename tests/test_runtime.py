import asyncio
import datetime as dt
import io
import json
from botwatch import _config
from botwatch import _driver
from botwatch import _runtime
from botwatch import _state
from botwatch._event import Event
from collections.abc import AsyncGenerator

_NOW = dt.datetime(2026, 8, 2, 1, 0, 0, tzinfo=dt.timezone.utc)


def _event(channel: str, ident: str) -> Event:
    return Event(
        channel=channel,
        type='fake',
        kind='message',
        id=ident,
        ts=_NOW,
        payload={'n': ident},
    )


class _FakeDriver:
    """A driver whose events are a fixed list; optionally stays open after them
    (like a real poller) or fails on first pull."""

    def __init__(
        self,
        events: list[Event],
        *,
        stay_open: bool = False,
        fail: str | None = None,
    ) -> None:
        self._events = events
        self._stay_open = stay_open
        self._fail = fail
        self._cursor: str | None = None

    async def watch(self) -> AsyncGenerator[Event, None]:
        if self._fail is not None:
            raise RuntimeError(self._fail)
        for event in self._events:
            self._cursor = event.id  # mark before yield (obligation 3)
            yield event
        if self._stay_open:
            await asyncio.Event().wait()  # stay open until the runtime closes us

    def get_state(self) -> dict[str, object]:
        return {'cursor': self._cursor}


def _channel(key: str, chan_type: str = 'fake') -> _config.ChannelConfig:
    return _config.ChannelConfig(key=key, type=chan_type)


def _fast_config(*keys: str) -> _config.Config:
    return _config.Config(
        defaults=_config.Defaults(
            quiet_timeout=dt.timedelta(milliseconds=200),
            boarding_window=dt.timedelta(milliseconds=20),
        ),
        channels=tuple(_channel(k) for k in keys),
    )


def _registry_of(**drivers: _FakeDriver) -> _driver.Registry:
    return {'fake': lambda key, cfg, prev_cfg, prev_state, ctx: drivers[key]}


async def _run(
    config: _config.Config, registry: _driver.Registry
) -> tuple[_state.State, list[dict[str, object]]]:
    state = _state.State.load('/nonexistent', now=_NOW)
    out = io.StringIO()
    await _runtime.run_once(config, state, registry, out=out, now=_NOW)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    return state, lines


async def test_events_are_emitted_and_state_records_the_cursor() -> None:
    driver = _FakeDriver([_event('c', '1'), _event('c', '2')])
    state, lines = await _run(_fast_config('c'), _registry_of(c=driver))
    assert [line['id'] for line in lines] == ['1', '2']
    block = state.channels['c']
    assert block.driver == {'cursor': '2'}
    assert block.last_event == _NOW


async def test_an_unknown_type_fails_that_channel() -> None:
    config = _config.Config(
        defaults=_config.Defaults(
            quiet_timeout=dt.timedelta(milliseconds=50),
            boarding_window=dt.timedelta(milliseconds=10),
        ),
        channels=(_channel('c', 'nosuchtype'),),
    )
    state, lines = await _run(config, {})
    assert lines[0]['kind'] == 'error'
    payload = lines[0]['payload']
    assert isinstance(payload, dict)
    assert 'no driver registered' in str(payload['error'])
    assert state.channels['c'].last_error is not None


async def test_a_driver_failure_is_localised_to_its_channel() -> None:
    good = _FakeDriver([_event('good', '1')])
    bad = _FakeDriver([], fail='boom')
    registry: _driver.Registry = {
        'fake': lambda key, cfg, pc, ps, ctx: {'good': good, 'bad': bad}[key]
    }
    state, lines = await _run(_fast_config('good', 'bad'), registry)
    kinds = {line['channel']: line['kind'] for line in lines}
    assert kinds['good'] == 'message'
    assert kinds['bad'] == 'error'
    assert state.channels['good'].driver == {'cursor': '1'}
    assert state.channels['bad'].last_error == 'boom'


async def test_a_quiet_run_emits_nothing_and_returns() -> None:
    driver = _FakeDriver([], stay_open=True)
    state, lines = await _run(_fast_config('c'), _registry_of(c=driver))
    assert lines == []
    assert state.channels['c'].driver == {'cursor': None}


async def test_a_stay_open_driver_departs_after_the_boarding_window() -> None:
    driver = _FakeDriver([_event('c', '1')], stay_open=True)
    state, lines = await _run(_fast_config('c'), _registry_of(c=driver))
    assert [line['id'] for line in lines] == ['1']
    # get_state reflects exactly what was emitted -- not a lookahead
    assert state.channels['c'].driver == {'cursor': '1'}
