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
def _small_pages_and_no_backoff(mocker: MockerFixture) -> None:
    mocker.patch.object(_api, '_PAGE_SIZE', 2)
    mocker.patch.object(_utils.http, 'INITIAL_BACKOFF', 0.0)


async def test_GitHubClient__fetch_issue_comments__reads_every_page() -> None:
    fake = _fake_github.FakeGitHub()
    for second in [1, 2, 3, 4, 5]:
        fake.comment(second, body=f'at {second}')

    async with fake.create_client() as client:
        comments = await client.fetch_issue_comments(
            _REPO,
            _PULL,
            since=_fake_github.at(2),
        )

    assert [comment.body for comment in comments] == ['at 2', 'at 3', 'at 4', 'at 5']
    assert [request.url.params['page'] for request in fake.requests] == ['1', '2', '3']


async def test_GitHubClient__asks_again_conditionally() -> None:
    fake = _fake_github.FakeGitHub()
    for second in [1, 2, 3]:
        fake.review(second, body=f'at {second}')

    async with fake.create_client() as client:
        await client.fetch_reviews(_REPO, _PULL)
        fake.review(4, body='at 4')  # lands on the last page
        reviews = await client.fetch_reviews(_REPO, _PULL)

    assert [review.body for review in reviews] == ['at 1', 'at 2', 'at 3', 'at 4']
    # The second listing: page 1 unchanged, page 2 grown, then an empty page 3.
    assert [
        request.headers.get('If-None-Match') is not None for request in fake.requests
    ] == [False, False, True, True, False]
    assert fake.statuses == [200, 200, 304, 200, 200]


async def test_GitHubClient__asks_unconditionally_after_new_parameters() -> None:
    fake = _fake_github.FakeGitHub()
    fake.comment(1)

    async with fake.create_client() as client:
        await client.fetch_issue_comments(_REPO, _PULL, since=_fake_github.at(0))
        await client.fetch_issue_comments(_REPO, _PULL, since=_fake_github.at(1))

    assert 'If-None-Match' not in fake.requests[1].headers


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


_DATE = 'Thu, 01 Jan 1970 00:16:40 GMT'
"""1000 seconds after the epoch."""


@pytest.mark.parametrize(
    ('response', 'delay'),
    [
        (httpx.Response(403, headers={'retry-after': '7'}), 7.0),
        (
            httpx.Response(
                403,
                headers={
                    'date': _DATE,
                    'x-ratelimit-remaining': '0',
                    'x-ratelimit-reset': '1060',
                },
            ),
            60.0,
        ),
        (
            httpx.Response(
                403,
                headers={
                    'date': _DATE,
                    'x-ratelimit-remaining': '0',
                    'x-ratelimit-reset': '900',
                },
            ),
            4.0,
        ),
        (httpx.Response(429), 60.0),
        (
            httpx.Response(
                403, json={'message': 'You have exceeded a secondary rate limit.'}
            ),
            60.0,
        ),
        (httpx.Response(502), 4.0),
    ],
)
def test_get_retry_delay(response: httpx.Response, delay: float) -> None:
    assert _api._get_retry_delay(response, 4.0) == delay


@pytest.mark.parametrize(
    ('response', 'is_transient'),
    [
        (httpx.Response(403, json={'message': 'Resource not accessible'}), False),
        (httpx.Response(403, json={'message': 'API rate limit exceeded'}), True),
        (httpx.Response(429), True),
        (httpx.Response(404), False),
    ],
)
def test_is_transient(response: httpx.Response, is_transient: bool) -> None:
    assert _api._is_transient(response) is is_transient


async def test_GitHubClient__follows_redirects() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return (
            httpx.Response(200, json=[])
            if request.url.path.startswith('/repositories/')
            else httpx.Response(
                301,
                headers={
                    'Location': 'https://api.github.com/repositories/1/pulls/26/reviews'
                },
            )
        )

    transport = httpx.MockTransport(handle)
    async with github.GitHubClient('ghp-test', transport=transport) as client:
        reviews = await client.fetch_reviews(_REPO, _PULL)

    assert not reviews
