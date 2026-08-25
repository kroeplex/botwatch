# The driver's seams are internal by design (PBP 2.2): the _Api HTTP layer, the
# _Driver, and the pure helpers all carry a leading underscore, and the tests
# reach them directly rather than only through the registry.
# pylint: disable=protected-access

import datetime as dt
import httpx
import pytest
from botwatch import _config
from botwatch import _driver
from botwatch import _registry
from botwatch import _slack
from botwatch._event import Event
from collections.abc import AsyncGenerator
from pytest_mock import MockerFixture

_NOW = dt.datetime(2026, 8, 2, 1, 0, 0, tzinfo=dt.timezone.utc)


def _channel(**options: object) -> _config.ChannelConfig:
    return _config.ChannelConfig.model_validate(
        {'key': 'fam', 'type': 'slack', **options}
    )


class _FakeApi:
    """An :class:`_slack._ApiClient` whose polls are fixed lists of batches -- one
    stream of history batches, and a stream of reply batches per thread parent.
    The driver stays open past them, so a test pulls exactly as many events as it
    laid out."""

    def __init__(
        self,
        batches: list[list[dict[str, object]]],
        replies: dict[str, list[list[dict[str, object]]]] | None = None,
    ) -> None:
        self._batches = list(batches)
        self._replies = {key: list(value) for key, value in (replies or {}).items()}
        self.calls: list[tuple[str, str]] = []
        self.replies_calls: list[tuple[str, str, str]] = []
        self.closed = False

    async def history_since(self, channel: str, oldest: str) -> list[dict[str, object]]:
        self.calls.append((channel, oldest))
        return self._batches.pop(0) if self._batches else []

    async def replies_since(
        self, channel: str, parent: str, oldest: str
    ) -> list[dict[str, object]]:
        self.replies_calls.append((channel, parent, oldest))
        batches = self._replies.get(parent)
        return batches.pop(0) if batches else []

    async def aclose(self) -> None:
        self.closed = True


async def _take(gen: AsyncGenerator[Event, None], count: int) -> list[Event]:
    return [await anext(gen) for _ in range(count)]


# -- pure helpers -------------------------------------------------------------


def test_combine_dedups_and_sorts_ascending() -> None:
    pages: list[list[object]] = [
        [{'ts': '1787000000.000010'}, {'ts': '1787000000.000002'}],
        [{'ts': '1787000000.000002'}, {'ts': '1787000000.000001'}],
    ]
    result = _slack._combine(pages)
    assert [message['ts'] for message in result] == [
        '1787000000.000001',
        '1787000000.000002',
        '1787000000.000010',
    ]


def test_combine_skips_messages_without_a_string_ts() -> None:
    pages: list[list[object]] = [[{'text': 'no ts'}, {'ts': 12345}, 'junk']]
    assert _slack._combine(pages) == []


def test_next_cursor_reads_metadata_or_empty() -> None:
    assert _slack._next_cursor({'response_metadata': {'next_cursor': 'X'}}) == 'X'
    assert _slack._next_cursor({'response_metadata': {'next_cursor': ''}}) == ''
    assert _slack._next_cursor({'response_metadata': None}) == ''
    assert _slack._next_cursor({}) == ''


def test_slack_ts_round_trips_through_to_datetime() -> None:
    assert _slack._to_datetime(_slack._slack_ts(_NOW)) == _NOW


# -- the _Api HTTP layer, over a mock transport -------------------------------


def _ok(messages: list[dict[str, object]], *, next_cursor: str = '') -> httpx.Response:
    body: dict[str, object] = {'ok': True, 'messages': messages}
    if next_cursor:
        body['response_metadata'] = {'next_cursor': next_cursor}
    return httpx.Response(200, json=body)


async def test_api_follows_pagination_and_returns_ascending() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get('cursor'):
            response = _ok([{'ts': '1'}])
        else:
            response = _ok([{'ts': '3'}, {'ts': '2'}], next_cursor='PAGE2')
        return response

    api = _slack._Api('xoxb-test', transport=httpx.MockTransport(handler))
    result = await api.history_since('C1', '0')
    await api.aclose()
    assert [message['ts'] for message in result] == ['1', '2', '3']


async def test_api_retries_only_a_rate_limit(mocker: MockerFixture) -> None:
    mocker.patch('botwatch._slack.asyncio.sleep')
    attempts = {'n': 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        attempts['n'] += 1
        if attempts['n'] == 1:
            response = httpx.Response(200, json={'ok': False, 'error': 'ratelimited'})
        else:
            response = _ok([{'ts': '1'}])
        return response

    api = _slack._Api('xoxb-test', transport=httpx.MockTransport(handler))
    result = await api.history_since('C1', '0')
    await api.aclose()
    assert [message['ts'] for message in result] == ['1']
    assert attempts['n'] == 2


async def test_api_raises_on_a_terminal_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={'ok': False, 'error': 'channel_not_found'})

    api = _slack._Api('xoxb-test', transport=httpx.MockTransport(handler))
    with pytest.raises(_slack.SlackError, match='channel_not_found'):
        await api.history_since('C1', '0')
    await api.aclose()


# -- the driver ---------------------------------------------------------------


async def test_a_message_becomes_a_full_event_envelope() -> None:
    message: dict[str, object] = {'ts': '100.000001', 'text': 'hi', 'user': 'U1'}
    fake = _FakeApi([[message]])
    driver = _slack._Driver(
        _channel(channel='C1', meta={'label': 'family'}),
        cursor='0',
        api_factory=lambda: fake,
    )
    gen = driver.watch()
    (event,) = await _take(gen, 1)
    await gen.aclose()
    assert event.channel == 'fam'
    assert event.type == 'slack'
    assert event.kind == 'message'
    assert event.id == '100.000001'
    assert event.ts == _slack._to_datetime('100.000001')
    assert event.meta == {'label': 'family'}
    assert event.payload == message
    assert fake.calls == [('C1', '0')]  # polled from the initial cursor
    assert fake.closed  # the generator's close ran the driver's cleanup


async def test_the_cursor_is_marked_before_the_yield() -> None:
    fake = _FakeApi([[{'ts': '100.000001'}, {'ts': '100.000002'}]])
    driver = _slack._Driver(
        _channel(channel='C1'), cursor='0', api_factory=lambda: fake
    )
    gen = driver.watch()
    await _take(gen, 1)
    # exactly one event pulled -> state is at that event, never ahead of it
    assert driver.get_state() == {'cursor': '100.000001', 'threads': {}}
    await gen.aclose()


async def test_a_later_poll_resumes_from_the_advanced_cursor() -> None:
    fake = _FakeApi([[{'ts': '100.000001'}], [{'ts': '100.000002'}]])
    driver = _slack._Driver(
        _channel(channel='C1', poll_interval='0s'),
        cursor='50.0',
        api_factory=lambda: fake,
    )
    gen = driver.watch()
    events = await _take(gen, 2)
    await gen.aclose()
    assert [event.id for event in events] == ['100.000001', '100.000002']
    assert fake.calls == [('C1', '50.0'), ('C1', '100.000001')]


async def test_missing_channel_option_fails_the_channel() -> None:
    driver = _slack._Driver(_channel(), cursor='0', api_factory=lambda: _FakeApi([]))
    gen = driver.watch()
    with pytest.raises(_slack.SlackError, match='invalid slack options'):
        await anext(gen)


async def test_an_unknown_option_is_rejected() -> None:
    driver = _slack._Driver(
        _channel(channel='C1', typo=True), cursor='0', api_factory=lambda: _FakeApi([])
    )
    gen = driver.watch()
    with pytest.raises(_slack.SlackError):
        await anext(gen)


# -- threads ------------------------------------------------------------------


async def test_a_thread_with_replies_is_followed_and_its_replies_emitted() -> None:
    parent: dict[str, object] = {'ts': '100.000001', 'text': 'parent', 'reply_count': 1}
    reply: dict[str, object] = {'ts': '100.000005', 'thread_ts': '100.000001'}
    fake = _FakeApi([[parent]], replies={'100.000001': [[reply]]})
    driver = _slack._Driver(
        _channel(channel='C1'), cursor='0', api_factory=lambda: fake
    )
    gen = driver.watch()
    events = await _take(gen, 2)
    await gen.aclose()
    assert [(e.kind, e.id) for e in events] == [
        ('message', '100.000001'),
        ('reply', '100.000005'),
    ]
    assert events[1].payload['thread_ts'] == '100.000001'
    # the thread was discovered and polled from the parent ts, cursor advanced
    assert fake.replies_calls == [('C1', '100.000001', '100.000001')]
    assert driver.get_state() == {
        'cursor': '100.000001',
        'threads': {'100.000001': '100.000005'},
    }


async def test_a_message_without_replies_is_not_followed() -> None:
    fake = _FakeApi([[{'ts': '100.000001', 'reply_count': 0}]])
    driver = _slack._Driver(
        _channel(channel='C1'), cursor='0', api_factory=lambda: fake
    )
    gen = driver.watch()
    await _take(gen, 1)
    assert not driver.get_state()['threads']  # nothing followed
    assert not fake.replies_calls  # so no thread was polled
    await gen.aclose()


async def test_a_followed_thread_resumes_from_its_reply_cursor() -> None:
    reply: dict[str, object] = {'ts': '200.000009', 'thread_ts': '100.000001'}
    fake = _FakeApi([[]], replies={'100.000001': [[reply]]})
    driver = _slack._Driver(
        _channel(channel='C1'),
        cursor='150.0',
        threads={'100.000001': '200.000000'},
        api_factory=lambda: fake,
    )
    gen = driver.watch()
    (event,) = await _take(gen, 1)
    await gen.aclose()
    assert event.kind == 'reply'
    assert event.id == '200.000009'
    assert fake.replies_calls == [('C1', '100.000001', '200.000000')]
    assert driver.get_state()['threads'] == {'100.000001': '200.000009'}


# -- the factory and registry -------------------------------------------------


def test_build_resumes_a_recorded_cursor() -> None:
    driver = _slack.build(
        'fam',
        _channel(channel='C1'),
        None,
        {'cursor': '555.5'},
        _driver.RunContext(now=_NOW),
    )
    assert driver.get_state() == {'cursor': '555.5', 'threads': {}}


def test_build_starts_a_new_channel_at_the_run_clock() -> None:
    driver = _slack.build(
        'fam', _channel(channel='C1'), None, None, _driver.RunContext(now=_NOW)
    )
    assert driver.get_state() == {'cursor': _slack._slack_ts(_NOW), 'threads': {}}


def test_build_resumes_recorded_threads() -> None:
    driver = _slack.build(
        'fam',
        _channel(channel='C1'),
        None,
        {'cursor': '555.5', 'threads': {'100.1': '100.9'}},
        _driver.RunContext(now=_NOW),
    )
    assert driver.get_state() == {'cursor': '555.5', 'threads': {'100.1': '100.9'}}


def test_the_registry_wires_the_slack_type() -> None:
    assert set(_registry.build_registry()) == {'slack'}
