# pylint: disable=protected-access

import httpx
import logging
import pytest
from . import _fake_github
from botwatch import _utils
from botwatch import github
from botwatch.github import _api
from collections.abc import Iterator
from pytest_mock import MockerFixture

_REPO = _fake_github.REPO

_PULL = _fake_github.PULL


@pytest.fixture(autouse=True)
def _no_backoff(mocker: MockerFixture) -> None:
    mocker.patch.object(_utils.http, 'INITIAL_BACKOFF', 0.0)


async def test_GitHubClient__fetch_issue_comments__follows_pages() -> None:
    fake = _fake_github.FakeGitHub(page_size=2)
    for second in [1, 2, 3, 4, 5]:
        fake.comment(second, body=f'at {second}')

    async with fake.create_client() as client:
        comments = await client.fetch_issue_comments(
            _REPO,
            _PULL,
            since=_fake_github.at(2),
        )

    assert [comment.body for comment in comments] == ['at 2', 'at 3', 'at 4', 'at 5']
    assert len(fake.requests) == 2


async def test_GitHubClient__asks_again_conditionally() -> None:
    fake = _fake_github.FakeGitHub()
    fake.review(1)

    async with fake.create_client() as client:
        first = await client.fetch_reviews(_REPO, _PULL)
        second = await client.fetch_reviews(_REPO, _PULL)

    assert first == second
    assert 'If-None-Match' not in fake.requests[0].headers
    assert 'If-None-Match' in fake.requests[1].headers


async def test_GitHubClient__fetch_time() -> None:
    fake = _fake_github.FakeGitHub(now=_fake_github.at(7))

    async with fake.create_client() as client:
        now = await client.fetch_time()

    assert now == _fake_github.at(7)


async def test_GitHubClient__raises_errors() -> None:
    fake = _fake_github.FakeGitHub()

    async with fake.create_client() as client:
        with pytest.raises(github.GitHubError, match='HTTP 404: Not Found'):
            await client.fetch_reviews(_REPO, _PULL + 1)


async def test_GitHubClient__retries_transient_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    outcomes: Iterator[httpx.Response | httpx.TransportError] = iter(
        [
            httpx.Response(
                403,
                headers={'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '0'},
            ),
            httpx.Response(429, headers={'retry-after': '0'}),
            httpx.Response(502),
            httpx.ConnectError('refused'),
            httpx.Response(200, json=[]),
        ],
    )

    def handle(_request: httpx.Request) -> httpx.Response:
        outcome = next(outcomes)
        if isinstance(outcome, httpx.TransportError):
            raise outcome
        return outcome

    transport = httpx.MockTransport(handle)
    with caplog.at_level(logging.WARNING):
        async with github.GitHubClient('ghp-test', transport=transport) as client:
            reviews = await client.fetch_reviews(_REPO, _PULL)

    assert not reviews
    assert caplog.text.count('retrying') == 4


@pytest.mark.parametrize(
    ('headers', 'delay'),
    [
        ({'retry-after': '7'}, 7.0),
        ({'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '1060'}, 60.0),
        ({'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '900'}, 0.0),
        ({}, 4.0),
    ],
)
def test_get_retry_delay(headers: dict[str, str], delay: float) -> None:
    response = httpx.Response(403, headers=headers)

    assert _api._get_retry_delay(response, 4.0, now=1000.0) == delay


def test_is_transient__not_a_plain_forbidden() -> None:
    assert not _api._is_transient(httpx.Response(403))
