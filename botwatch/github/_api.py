"""A thin async client for the parts of the GitHub REST API that botwatch reads."""

import email.utils
import http
import httpx
import logging
import pydantic
import time
from .. import _utils
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import InitVar
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from typing import Self

_BASE_URL = 'https://api.github.com/'

_PAGE_SIZE = 100

_TIMEOUT = httpx.Timeout(30.0)

_logger = logging.getLogger(__name__)


class GitHubError(Exception):
    """GitHub answered a call with an error that retrying won't fix, such as a
    missing pull request or a bad token."""

    def __init__(
        self,
        path: str,
        status: int,
        message: str,
    ) -> None:
        super().__init__(f'{path}: HTTP {status}: {message}')
        self.path = path
        self.status = status
        self.message = message


class User(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True)

    login: str


class IssueComment(pydantic.BaseModel):
    """A comment on a pull request's conversation, as GitHub returns it, reduced
    to the fields botwatch reads."""

    model_config = pydantic.ConfigDict(frozen=True)

    id: int
    user: User | None
    body: str | None = None
    created_at: datetime
    html_url: str


class Review(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True)

    id: int
    user: User | None
    body: str | None = None
    state: str
    """``APPROVED``, ``CHANGES_REQUESTED``, ``COMMENTED``, ``DISMISSED`` or
    ``PENDING``."""

    submitted_at: datetime | None = None
    """When it was submitted; ``None`` while pending."""

    html_url: str


class ReviewComment(pydantic.BaseModel):
    """A comment on a pull request's diff. Each belongs to a review, even a
    lone reply in a thread."""

    model_config = pydantic.ConfigDict(frozen=True)

    id: int
    pull_request_review_id: int
    user: User | None
    body: str
    path: str
    line: int | None
    """``None`` once the diff has moved on."""

    in_reply_to_id: int | None = None
    html_url: str


class IssueEvent(pydantic.BaseModel):
    """Something done to a pull request, such as closing it, as its issue
    events list it."""

    model_config = pydantic.ConfigDict(frozen=True)

    id: int
    event: str
    """Such as ``closed``, ``merged``, ``reopened`` or ``labeled``."""

    actor: User | None
    created_at: datetime


class _CommitDetails(pydantic.BaseModel):
    message: str


class Commit(pydantic.BaseModel):
    """A commit, reduced to the fields botwatch reads."""

    model_config = pydantic.ConfigDict(frozen=True)

    commit: _CommitDetails

    committer: User | None
    """The GitHub account of whoever committed it, when GitHub can match
    one. A commit GitHub made, such as one from its web editor, is
    ``web-flow``'s."""

    @property
    def message(self) -> str:
        return self.commit.message


_COMMITS = pydantic.TypeAdapter(list[Commit])


class _ErrorBody(pydantic.BaseModel):
    message: str = ''


_ISSUE_COMMENTS = pydantic.TypeAdapter(list[IssueComment])
_REVIEWS = pydantic.TypeAdapter(list[Review])
_REVIEW_COMMENTS = pydantic.TypeAdapter(list[ReviewComment])
_ISSUE_EVENTS = pydantic.TypeAdapter(list[IssueEvent])


class CheckRun(pydantic.BaseModel):
    """One check on a commit, such as a CI job, reduced to the fields botwatch
    reads."""

    model_config = pydantic.ConfigDict(frozen=True)

    name: str
    status: str
    """``queued``, ``in_progress`` or ``completed``, among others."""

    conclusion: str | None = None
    """Once completed: ``success``, ``failure``, ``cancelled``, ``skipped``,
    ``neutral``, ``timed_out`` or ``action_required``, among others."""


class CheckSuite(pydantic.BaseModel):
    """One app's checks on a commit, such as a GitHub Actions workflow run. It
    stays incomplete until every one of its checks has run, including those
    not created yet."""

    model_config = pydantic.ConfigDict(frozen=True)

    status: str
    latest_check_runs_count: int
    created_at: datetime


class Status(pydantic.BaseModel):
    """A commit status, as older CI integrations report results."""

    model_config = pydantic.ConfigDict(frozen=True)

    context: str
    state: str
    """``pending``, ``success``, ``failure`` or ``error``."""


class _CheckRunsPage(pydantic.BaseModel):
    check_runs: list[CheckRun]


class _CheckSuitesPage(pydantic.BaseModel):
    check_suites: list[CheckSuite]


class _CombinedStatus(pydantic.BaseModel):
    statuses: list[Status]


_SECONDARY_LIMIT_WAIT = 60.0
"""Seconds to wait at least on a secondary rate limit that names no wait, as
GitHub asks."""


def _is_rate_limited(response: httpx.Response) -> bool:
    """GitHub signals its rate limits with a 429, or a 403 that says so in its
    headers or its message."""
    return response.status_code == http.HTTPStatus.TOO_MANY_REQUESTS or (
        response.status_code == http.HTTPStatus.FORBIDDEN
        and (
            response.headers.get('x-ratelimit-remaining') == '0'
            or 'retry-after' in response.headers
            or 'rate limit' in response.text.lower()
        )
    )


def _is_transient(outcome: _utils.http.Outcome) -> bool:
    """Whether an attempt failed in a way worth retrying: a network error, rate
    limiting, or a server error."""
    return isinstance(outcome, httpx.TransportError) or (
        _is_rate_limited(outcome)
        or outcome.status_code >= http.HTTPStatus.INTERNAL_SERVER_ERROR
    )


def _get_retry_delay(
    outcome: _utils.http.Outcome,
    backoff: float,
) -> float:
    """GitHub's ``Retry-After`` when it gave one; else at least the backoff, and
    until the rate limit resets by GitHub's clock when it's exhausted, or a
    minute on any other rate limit."""
    response = outcome if isinstance(outcome, httpx.Response) else None
    headers = response.headers if response is not None else httpx.Headers()
    if 'retry-after' in headers:
        delay = float(headers['retry-after'])
    elif headers.get('x-ratelimit-remaining') == '0' and 'x-ratelimit-reset' in headers:
        now = (
            email.utils.parsedate_to_datetime(headers['date']).timestamp()
            if 'date' in headers
            else time.time()
        )
        delay = max(backoff, float(headers['x-ratelimit-reset']) - now)
    elif response is not None and _is_rate_limited(response):
        delay = max(backoff, _SECONDARY_LIMIT_WAIT)
    else:
        delay = backoff
    return delay


@dataclass(frozen=True)
class _Cached:
    url: str
    etag: str
    content: bytes


@dataclass
class GitHubClient:
    """An async client for the GitHub REST API, for one token.

    Use it as an async context manager. Transient failures, meaning rate
    limiting, server errors and network errors, are retried without limit,
    each logged as a warning. Any other error response raises
    :class:`GitHubError`.

    Each list page's ``ETag`` is kept, and the same page is asked for again
    conditionally, so a poll that finds nothing changed is answered with 304s,
    which GitHub doesn't count against the rate limit.
    """

    token: InitVar[str]

    transport: InitVar[httpx.AsyncBaseTransport | None] = None
    """Replaces the network, e.g. with :class:`httpx.MockTransport` in tests."""

    _http: httpx.AsyncClient = field(init=False)

    _cached: dict[tuple[str, int | None], _Cached] = field(
        init=False,
        default_factory=dict,
    )
    """The latest response to each object, and each list's pages, by path and
    page number. A page asked for with other parameters, such as a later
    ``since``, replaces its entry."""

    def __post_init__(
        self,
        token: str,
        transport: httpx.AsyncBaseTransport | None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={
                'Accept': 'application/vnd.github+json',
                'Authorization': f'Bearer {token}',
                'X-GitHub-Api-Version': '2022-11-28',
            },
            timeout=_TIMEOUT,
            transport=transport,
            follow_redirects=True,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._http.aclose()

    async def _send(self, request: httpx.Request) -> httpx.Response:
        """Sends ``request`` until it doesn't fail transiently."""
        response = await _utils.http.send_until_settled(
            self._http,
            request,
            is_transient=_is_transient,
            get_retry_delay=_get_retry_delay,
            logger=_logger,
        )
        if response.is_error:
            body = _ErrorBody.model_validate_json(response.content or b'{}')
            raise GitHubError(request.url.path, response.status_code, body.message)
        return response

    async def _get_conditionally(
        self,
        path: str,
        params: Mapping[str, str],
        page: int | None = None,
        *,
        accept: str | None = None,
    ) -> bytes:
        """An object, or one page of a list, asked for conditionally on its
        cached ``ETag``, in the media type ``accept`` names if given."""
        paging = (
            {'per_page': str(_PAGE_SIZE), 'page': str(page)} if page is not None else {}
        )
        request = self._http.build_request(
            'GET',
            path,
            params={**params, **paging},
            headers={'Accept': accept} if accept is not None else None,
        )
        url = str(request.url)
        cached = self._cached.get((path, page))
        if cached is not None and cached.url == url:
            request.headers['If-None-Match'] = cached.etag
        response = await self._send(request)
        if response.status_code == http.HTTPStatus.NOT_MODIFIED:
            assert cached is not None  # only a conditional request gets a 304
            content = cached.content
        else:
            content = response.content
            if 'etag' in response.headers:
                self._cached[(path, page)] = _Cached(
                    url=url,
                    etag=response.headers['etag'],
                    content=content,
                )
        return content

    async def _fetch_pages[ItemT](
        self,
        path: str,
        read: Callable[[bytes], list[ItemT]],
        params: Mapping[str, str] | None = None,
    ) -> list[ItemT]:
        """GETs a paged list page by page, up to its first short page, reading
        each page's items with ``read``.

        Pages are counted rather than followed by their ``Link`` headers, which
        GitHub leaves out of a 304.
        """
        page = 1
        batch = read(await self._get_conditionally(path, params or {}, page))
        items = list(batch)
        while len(batch) == _PAGE_SIZE:
            page += 1
            batch = read(await self._get_conditionally(path, params or {}, page))
            items.extend(batch)
        return items

    async def _fetch_all[ModelT: pydantic.BaseModel](
        self,
        adapter: pydantic.TypeAdapter[list[ModelT]],
        path: str,
        params: Mapping[str, str] | None = None,
    ) -> list[ModelT]:
        """GETs a paged list of ``adapter``'s items."""
        return await self._fetch_pages(path, adapter.validate_json, params)

    async def fetch_time(self) -> datetime:
        """GitHub's clock, from a call that doesn't count against the rate
        limit."""
        response = await self._send(self._http.build_request('GET', 'rate_limit'))
        return email.utils.parsedate_to_datetime(response.headers['date'])

    async def fetch_issue_comments(
        self,
        repo: str,
        number: int,
        *,
        since: datetime,
    ) -> list[IssueComment]:
        """A pull request's conversation comments updated at or after
        ``since``, oldest first."""
        return await self._fetch_all(
            _ISSUE_COMMENTS,
            f'repos/{repo}/issues/{number}/comments',
            {'since': _utils.datetime.to_rfc3339(since)},
        )

    async def fetch_reviews(
        self,
        repo: str,
        number: int,
    ) -> list[Review]:
        """A pull request's reviews, oldest first."""
        return await self._fetch_all(_REVIEWS, f'repos/{repo}/pulls/{number}/reviews')

    async def fetch_head(self, repo: str, number: int) -> str:
        """The SHA of a pull request's head commit, asked for in a form whose
        ``ETag`` changes only when the head does, so an unchanged head costs
        a free 304."""
        content = await self._get_conditionally(
            f'repos/{repo}/commits/refs/pull/{number}/head',
            {},
            accept='application/vnd.github.sha',
        )
        return content.decode().strip()

    async def fetch_issue_events(
        self,
        repo: str,
        number: int,
    ) -> list[IssueEvent]:
        """What has been done to a pull request, such as closing it, oldest
        first."""
        return await self._fetch_all(
            _ISSUE_EVENTS,
            f'repos/{repo}/issues/{number}/events',
        )

    async def fetch_check_runs(self, repo: str, sha: str) -> list[CheckRun]:
        """The latest run of each check on a commit."""
        return await self._fetch_pages(
            f'repos/{repo}/commits/{sha}/check-runs',
            lambda content: _CheckRunsPage.model_validate_json(content).check_runs,
        )

    async def fetch_check_suites(self, repo: str, sha: str) -> list[CheckSuite]:
        return await self._fetch_pages(
            f'repos/{repo}/commits/{sha}/check-suites',
            lambda content: _CheckSuitesPage.model_validate_json(
                content,
            ).check_suites,
        )

    async def fetch_statuses(self, repo: str, sha: str) -> list[Status]:
        """The latest status of each context on a commit, up to 100."""
        content = await self._get_conditionally(
            f'repos/{repo}/commits/{sha}/status',
            {'per_page': str(_PAGE_SIZE)},
        )
        return _CombinedStatus.model_validate_json(content).statuses

    async def fetch_commit(self, repo: str, sha: str) -> Commit:
        """A commit, listed on its own so as to leave out its diff."""
        response = await self._send(
            self._http.build_request(
                'GET',
                f'repos/{repo}/commits',
                params={'sha': sha, 'per_page': '1'},
            ),
        )
        return _COMMITS.validate_json(response.content)[0]

    async def fetch_review_comments(
        self,
        repo: str,
        number: int,
    ) -> list[ReviewComment]:
        """Every comment on a pull request's diff, oldest first."""
        return await self._fetch_all(
            _REVIEW_COMMENTS,
            f'repos/{repo}/pulls/{number}/comments',
        )
