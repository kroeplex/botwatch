"""A thin async client for the parts of the Slack Web API that botwatch reads."""

import asyncio
import collections
import http
import httpx
import logging
import pydantic
from . import _ts
from collections.abc import Iterable
from collections.abc import Mapping
from dataclasses import InitVar
from dataclasses import dataclass
from dataclasses import field
from datetime import timedelta
from typing import Self

_BASE_URL = 'https://slack.com/api/'

_PAGE_SIZE = 200

_TIMEOUT = httpx.Timeout(30.0)

_INITIAL_BACKOFF = 1.0
"""Seconds before the first retry of a transient failure; doubles per retry."""

_MAX_BACKOFF = 60.0

_logger = logging.getLogger(__name__)


class SlackError(Exception):
    """Slack answered a call with ``"ok": false``."""

    def __init__(
        self,
        method: str,
        error: str,
    ) -> None:
        super().__init__(f'{method}: {error}')
        self.method = method
        self.error = error


class Message(pydantic.BaseModel):
    """A message as ``conversations.history`` and ``conversations.replies``
    return it, reduced to the fields botwatch reads. See
    :class:`~botwatch.slack.MessagePosted` for their meanings."""

    model_config = pydantic.ConfigDict(frozen=True)

    ts: _ts.SlackTs
    thread_ts: _ts.SlackTs | None = None

    latest_reply: _ts.SlackTs | None = None
    """The ``ts`` of the thread's newest reply, on a parent with replies."""

    user: str | None = None
    bot_id: str | None = None
    username: str | None = None
    subtype: str | None = None
    text: str = ''

    @property
    def latest_ts(self) -> _ts.SlackTs:
        """The ``ts`` of its thread's newest reply, or its own when it has
        none."""
        return self.latest_reply or self.ts


class _ResponseMetadata(pydantic.BaseModel):
    next_cursor: str = ''


class _MessagesPage(pydantic.BaseModel):
    """One page of a ``conversations.history`` or ``conversations.replies``
    response."""

    ok: bool
    error: str = ''
    messages: tuple[Message, ...] = ()

    has_more: bool = True
    """Whether more messages remain. Assumed when Slack doesn't say, so a page
    is never mistaken for the whole history."""

    response_metadata: _ResponseMetadata = pydantic.Field(
        default_factory=_ResponseMetadata,
    )


@dataclass(frozen=True)
class RecentHistory:
    """A conversation's newest top-level messages, from one history page."""

    messages: tuple[Message, ...]
    """Oldest first."""

    is_complete: bool
    """Whether these are all of the conversation's top-level messages."""

    def covers(self, after: _ts.SlackTs) -> bool:
        """Whether every top-level message after ``after`` is here."""
        return self.is_complete or (
            bool(self.messages) and self.messages[0].ts <= after
        )

    def find(self, ts: _ts.SlackTs) -> Message | None:
        return next((message for message in self.messages if message.ts == ts), None)


@dataclass(frozen=True)
class _CachedHistory:
    fetched_at: float
    """The event loop's clock when the fetch started."""

    history: RecentHistory


type _Outcome = httpx.Response | httpx.TransportError
"""What one attempt at a call produced: a response, or a network error."""


def _combine_message_pages(pages: Iterable[_MessagesPage]) -> list[Message]:
    """Merges pages into one list, oldest first, without duplicates.

    Slack pages history newest first, and a message can appear on both sides of
    a page boundary.
    """
    by_ts = {message.ts: message for page in pages for message in page.messages}
    return [by_ts[ts] for ts in sorted(by_ts)]


def _is_transient(outcome: _Outcome) -> bool:
    """Whether an attempt failed in a way worth retrying: a network error, rate
    limiting, or a server error."""
    return isinstance(outcome, httpx.TransportError) or (
        outcome.status_code == http.HTTPStatus.TOO_MANY_REQUESTS
        or outcome.status_code >= http.HTTPStatus.INTERNAL_SERVER_ERROR
    )


def _get_retry_delay(
    outcome: _Outcome,
    backoff: float,
) -> float:
    """Slack's ``Retry-After`` when it gave one, else the current backoff."""
    retry_after = (
        outcome.headers.get('Retry-After')
        if isinstance(outcome, httpx.Response)
        else None
    )
    return float(retry_after) if retry_after is not None else backoff


def _describe(outcome: _Outcome) -> str:
    return (
        f'HTTP {outcome.status_code}'
        if isinstance(outcome, httpx.Response)
        else repr(outcome)
    )


@dataclass
class SlackClient:
    """An async client for the Slack Web API, for one token.

    Use it as an async context manager. Transient failures, meaning rate
    limiting, server errors and network errors, are retried without limit,
    each logged as a warning. A Slack error response raises
    :class:`SlackError`.
    """

    token: InitVar[str]

    transport: InitVar[httpx.AsyncBaseTransport | None] = None
    """Replaces the network, e.g. with :class:`httpx.MockTransport` in tests."""

    _http: httpx.AsyncClient = field(init=False)

    _recent_histories: dict[str, _CachedHistory] = field(
        init=False,
        default_factory=dict,
    )

    _recent_history_locks: collections.defaultdict[str, asyncio.Lock] = field(
        init=False,
        default_factory=lambda: collections.defaultdict(asyncio.Lock),
    )

    def __post_init__(
        self,
        token: str,
        transport: httpx.AsyncBaseTransport | None,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={'Authorization': f'Bearer {token}'},
            timeout=_TIMEOUT,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._http.aclose()

    async def _try_get(
        self,
        method: str,
        params: Mapping[str, str],
    ) -> _Outcome:
        try:
            outcome: _Outcome = await self._http.get(method, params=params)
        except httpx.TransportError as error:
            outcome = error
        return outcome

    async def _get_messages_page(
        self,
        method: str,
        params: Mapping[str, str],
    ) -> _MessagesPage:
        """Calls ``method`` until it doesn't fail transiently, and parses the
        page."""
        backoff = _INITIAL_BACKOFF
        outcome = await self._try_get(method, params)
        while _is_transient(outcome):
            delay = _get_retry_delay(outcome, backoff)
            _logger.warning(
                'Slack %s: %s; retrying in %.1fs',
                method,
                _describe(outcome),
                delay,
            )
            await asyncio.sleep(delay)
            backoff = min(backoff * 2, _MAX_BACKOFF)
            outcome = await self._try_get(method, params)

        assert isinstance(outcome, httpx.Response)  # errors are transient
        outcome.raise_for_status()
        page = _MessagesPage.model_validate(outcome.json())
        if not page.ok:
            raise SlackError(method, page.error)
        return page

    async def _fetch_messages(
        self,
        method: str,
        **params: str,
    ) -> list[Message]:
        """Calls a paged messages method, following its cursor to the end."""
        first_page_params = {**params, 'limit': str(_PAGE_SIZE)}
        page = await self._get_messages_page(method, first_page_params)
        pages = [page]
        while page.response_metadata.next_cursor:
            page = await self._get_messages_page(
                method,
                {**first_page_params, 'cursor': page.response_metadata.next_cursor},
            )
            pages.append(page)
        return _combine_message_pages(pages)

    async def fetch_history(
        self,
        conversation: str,
        *,
        oldest: _ts.SlackTs,
    ) -> list[Message]:
        """A conversation's top-level messages after ``oldest``, oldest first."""
        return await self._fetch_messages(
            'conversations.history',
            channel=conversation,
            oldest=oldest,
        )

    async def fetch_replies(
        self,
        conversation: str,
        thread_ts: _ts.SlackTs,
        *,
        oldest: _ts.SlackTs,
    ) -> list[Message]:
        """A thread's replies after ``oldest``, oldest first, without the parent
        message Slack puts on every page."""
        messages = await self._fetch_messages(
            'conversations.replies',
            channel=conversation,
            ts=thread_ts,
            oldest=oldest,
        )
        return [message for message in messages if message.ts != thread_ts]

    async def fetch_recent_history(
        self,
        conversation: str,
        *,
        max_age: timedelta,
    ) -> RecentHistory:
        """The conversation's newest top-level messages, fetched at most once per
        ``max_age`` however many sources ask.

        Sources watching one conversation and its threads share this one call:
        a thread's parent carries ``latest_reply``, so a thread with no new
        replies costs no call of its own.
        """
        loop = asyncio.get_running_loop()
        # Judged from when the caller asked, so callers queued behind a slow
        # fetch share it rather than each fetching again.
        requested_at = loop.time()
        async with self._recent_history_locks[conversation]:
            cached = self._recent_histories.get(conversation)
            if (
                cached is None
                or requested_at - cached.fetched_at > max_age.total_seconds()
            ):
                fetched_at = loop.time()
                page = await self._get_messages_page(
                    'conversations.history',
                    {'channel': conversation, 'limit': str(_PAGE_SIZE)},
                )
                cached = _CachedHistory(
                    fetched_at=fetched_at,
                    history=RecentHistory(
                        messages=tuple(_combine_message_pages([page])),
                        is_complete=not page.has_more,
                    ),
                )
                self._recent_histories[conversation] = cached
        return cached.history

    async def fetch_latest_ts(self, conversation: str) -> _ts.SlackTs:
        """The ``ts`` of the conversation's newest top-level message, or
        ``EPOCH`` when it has none.

        Slack can return an empty page while more remain, so this follows the
        cursor until a page holds a message.
        """
        params = {'channel': conversation, 'limit': str(_PAGE_SIZE)}
        page = await self._get_messages_page('conversations.history', params)
        while not page.messages and page.response_metadata.next_cursor:
            page = await self._get_messages_page(
                'conversations.history',
                {**params, 'cursor': page.response_metadata.next_cursor},
            )
        return max((message.ts for message in page.messages), default=_ts.EPOCH)

    async def fetch_latest_reply_ts(
        self,
        conversation: str,
        thread_ts: _ts.SlackTs,
    ) -> _ts.SlackTs:
        """The ``ts`` of a thread's newest reply, or its parent's when it has
        none."""
        page = await self._get_messages_page(
            'conversations.replies',
            {'channel': conversation, 'ts': thread_ts, 'limit': '1'},
        )
        return page.messages[0].latest_ts
