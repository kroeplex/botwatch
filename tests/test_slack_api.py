import httpx
import logging
import pytest
from botwatch import slack
from botwatch.slack import _api
from collections.abc import Iterator
from pytest_mock import MockerFixture

_CONVERSATION = 'C0123456789'

_OLDEST = '1785600000.000000'


def _page(*timestamps: str, next_cursor: str = '') -> httpx.Response:
    return httpx.Response(
        200,
        json={
            'ok': True,
            'messages': [{'ts': ts, 'text': f'at {ts}'} for ts in timestamps],
            'response_metadata': {'next_cursor': next_cursor},
        },
    )


def _create_client(
    responses: list[httpx.Response | httpx.TransportError],
    requests: list[httpx.Request],
) -> slack.SlackClient:
    """A client whose successive requests get ``responses``, raising the errors
    among them, and are recorded in ``requests``."""
    remaining: Iterator[httpx.Response | httpx.TransportError] = iter(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = next(remaining)
        if isinstance(response, httpx.TransportError):
            raise response
        return response

    return slack.SlackClient('xoxb-test', transport=httpx.MockTransport(handle))


@pytest.fixture(autouse=True)
def _no_backoff(mocker: MockerFixture) -> None:
    mocker.patch.object(_api, '_INITIAL_BACKOFF', 0.0)


class Test_SlackClient__fetch_history:
    async def test__merges_pages_oldest_first(self) -> None:
        requests: list[httpx.Request] = []
        responses: list[httpx.Response | httpx.TransportError] = [
            _page('1785600003.000000', '1785600002.000000', next_cursor='next'),
            _page('1785600002.000000', '1785600001.000000'),
        ]

        async with _create_client(responses, requests) as client:
            messages = await client.fetch_history(_CONVERSATION, oldest=_OLDEST)

        assert [message.ts for message in messages] == [
            '1785600001.000000',
            '1785600002.000000',
            '1785600003.000000',
        ]
        assert [dict(request.url.params) for request in requests] == [
            {'channel': _CONVERSATION, 'oldest': _OLDEST, 'limit': '200'},
            {
                'channel': _CONVERSATION,
                'oldest': _OLDEST,
                'limit': '200',
                'cursor': 'next',
            },
        ]
        assert requests[0].headers['Authorization'] == 'Bearer xoxb-test'

    async def test__retries_transient_failures(
        self,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        requests: list[httpx.Request] = []
        responses: list[httpx.Response | httpx.TransportError] = [
            httpx.Response(429, headers={'Retry-After': '0'}),
            httpx.Response(503),
            httpx.ConnectError('unreachable'),
            _page('1785600001.000000'),
        ]

        with caplog.at_level(logging.WARNING):
            async with _create_client(responses, requests) as client:
                messages = await client.fetch_history(_CONVERSATION, oldest=_OLDEST)

        assert [message.ts for message in messages] == ['1785600001.000000']
        assert len(requests) == 4
        assert any('retrying' in record.message for record in caplog.records)

    async def test__raises_slack_errors(self) -> None:
        responses: list[httpx.Response | httpx.TransportError] = [
            httpx.Response(200, json={'ok': False, 'error': 'channel_not_found'}),
        ]

        async with _create_client(responses, []) as client:
            with pytest.raises(slack.SlackError) as raised:
                await client.fetch_history(_CONVERSATION, oldest=_OLDEST)

        assert raised.value.method == 'conversations.history'
        assert raised.value.error == 'channel_not_found'


async def test_SlackClient__fetch_replies__omits_the_parent() -> None:
    requests: list[httpx.Request] = []
    responses: list[httpx.Response | httpx.TransportError] = [
        _page('1785600000.000100', '1785600001.000000'),
    ]

    async with _create_client(responses, requests) as client:
        messages = await client.fetch_replies(
            _CONVERSATION,
            '1785600000.000100',
            oldest=_OLDEST,
        )

    assert [message.ts for message in messages] == ['1785600001.000000']
    assert requests[0].url.path == '/api/conversations.replies'
    assert requests[0].url.params['ts'] == '1785600000.000100'
