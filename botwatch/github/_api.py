"""A thin async client for the parts of the GitHub REST API that botwatch reads."""

import email.utils
import http
import httpx
import logging
import pydantic
import time
from .. import _utils
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
    user: User | None
    body: str
    path: str
    line: int | None = None
    in_reply_to_id: int | None = None
    html_url: str


class _ErrorBody(pydantic.BaseModel):
    message: str = ''


_ISSUE_COMMENTS = pydantic.TypeAdapter(list[IssueComment])
_REVIEWS = pydantic.TypeAdapter(list[Review])
_REVIEW_COMMENTS = pydantic.TypeAdapter(list[ReviewComment])


def _is_rate_limited(response: httpx.Response) -> bool:
    """GitHub signals its primary and secondary rate limits with a 403 or 429
    and these headers."""
    return response.status_code in (
        http.HTTPStatus.FORBIDDEN,
        http.HTTPStatus.TOO_MANY_REQUESTS,
    ) and (
        response.headers.get('x-ratelimit-remaining') == '0'
        or 'retry-after' in response.headers
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
    now: float,
) -> float:
    """GitHub's ``Retry-After`` when it gave one, else the wait until the rate
    limit resets when it's exhausted, else the current backoff."""
    headers = outcome.headers if isinstance(outcome, httpx.Response) else {}
    if 'retry-after' in headers:
        delay = float(headers['retry-after'])
    elif headers.get('x-ratelimit-remaining') == '0' and 'x-ratelimit-reset' in headers:
        delay = max(0.0, float(headers['x-ratelimit-reset']) - now)
    else:
        delay = backoff
    return delay


@dataclass(frozen=True)
class _Page:
    content: bytes
    next_url: str | None


@dataclass
class GitHubClient:
    """An async client for the GitHub REST API, for one token.

    Use it as an async context manager. Transient failures, meaning rate
    limiting, server errors and network errors, are retried without limit,
    each logged as a warning. Any other error response raises
    :class:`GitHubError`.

    Every response's ``ETag`` is kept, and the same request is made again
    conditionally, so a poll that finds nothing changed is answered with 304s,
    which GitHub doesn't count against the rate limit.
    """

    token: InitVar[str]

    transport: InitVar[httpx.AsyncBaseTransport | None] = None
    """Replaces the network, e.g. with :class:`httpx.MockTransport` in tests."""

    _http: httpx.AsyncClient = field(init=False)

    _cached: dict[str, tuple[str, bytes]] = field(init=False, default_factory=dict)
    """Each URL's latest ``ETag`` and content."""

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
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._http.aclose()

    async def _get(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """GETs ``url`` until it doesn't fail transiently, conditionally on the
        cached ``ETag``."""
        request = self._http.build_request('GET', url, params=params)
        cached = self._cached.get(str(request.url))
        if cached is not None:
            request.headers['If-None-Match'] = cached[0]

        outcome = await _utils.http.send_until_settled(
            self._http,
            request,
            is_transient=_is_transient,
            get_retry_delay=lambda outcome, backoff: _get_retry_delay(
                outcome,
                backoff,
                time.time(),
            ),
            logger=_logger,
        )
        if outcome.is_error:
            body = _ErrorBody.model_validate_json(outcome.content or b'{}')
            raise GitHubError(request.url.path, outcome.status_code, body.message)
        return outcome

    async def _get_page(
        self,
        url: str,
        params: Mapping[str, str] | None = None,
    ) -> _Page:
        response = await self._get(url, params)
        key = str(response.request.url)
        if response.status_code == http.HTTPStatus.NOT_MODIFIED:
            content = self._cached[key][1]
        else:
            content = response.content
            if 'etag' in response.headers:
                self._cached[key] = (response.headers['etag'], content)
        next_link = response.links.get('next')
        return _Page(content=content, next_url=next_link['url'] if next_link else None)

    async def _fetch_all[ModelT: pydantic.BaseModel](
        self,
        adapter: pydantic.TypeAdapter[list[ModelT]],
        path: str,
        params: Mapping[str, str] | None = None,
    ) -> list[ModelT]:
        """GETs a paged list, following its ``next`` links to the end."""
        page = await self._get_page(
            path,
            {**(params or {}), 'per_page': str(_PAGE_SIZE)},
        )
        items = adapter.validate_json(page.content)
        while page.next_url is not None:
            page = await self._get_page(page.next_url)
            items.extend(adapter.validate_json(page.content))
        return items

    async def fetch_time(self) -> datetime:
        """GitHub's clock, from a call that doesn't count against the rate
        limit."""
        response = await self._get('rate_limit')
        return email.utils.parsedate_to_datetime(response.headers['date'])

    async def fetch_issue_comments(
        self,
        repo: str,
        number: int,
        *,
        since: datetime,
    ) -> list[IssueComment]:
        """A pull request's conversation comments updated since ``since``,
        oldest first."""
        return await self._fetch_all(
            _ISSUE_COMMENTS,
            f'repos/{repo}/issues/{number}/comments',
            {'since': since.strftime('%Y-%m-%dT%H:%M:%SZ')},
        )

    async def fetch_reviews(
        self,
        repo: str,
        number: int,
    ) -> list[Review]:
        """A pull request's reviews, oldest first."""
        return await self._fetch_all(_REVIEWS, f'repos/{repo}/pulls/{number}/reviews')

    async def fetch_review_comments(
        self,
        repo: str,
        number: int,
        review_id: int,
    ) -> list[ReviewComment]:
        """One review's comments on the diff."""
        return await self._fetch_all(
            _REVIEW_COMMENTS,
            f'repos/{repo}/pulls/{number}/reviews/{review_id}/comments',
        )
