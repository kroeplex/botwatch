"""The Slack channel driver: watch one conversation, top level and threads.

Polls ``conversations.history`` for the messages posted since a stored cursor and
``conversations.replies`` for each thread it is following, reports each as an
event, and advances the cursors so the next run resumes where this one stopped.
A conversation's own messages are ``message`` events; a thread's replies are
``reply`` events. The driver's whole opaque state is those cursors -- one for the
top level, one per followed thread -- keyed however the driver likes; the core
never looks inside.

A thread is discovered when its parent is seen in the top level *already bearing
replies*. That fits the burst the watcher is built to catch -- it wakes into a
conversation already in progress, so the parents it reads already carry their
reply counts. It has one known blind spot: a parent first seen with no replies,
whose first reply lands only after the top-level cursor has moved past it, is not
picked up -- no basic Slack call resurfaces such a parent. And the followed-thread
set only grows (there is no pruning yet). Both are left to a later refinement;
neither drops an event from a thread already being followed.

Everything that can fail -- resolving the token, validating the channel's
options, and every network call -- happens inside :meth:`_Driver.watch`, not at
construction, so a problem with any of them is reported as a channel failure
(driver obligation 4) rather than aborting the whole run.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import httpx
import os
import pydantic
from . import _config
from . import _driver
from ._event import Event
from collections.abc import AsyncGenerator
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

_API_ROOT = 'https://slack.com/api'

_PAGE_LIMIT = 200
"""Messages per API page. The pagination cursor is followed to the end of the
window regardless, so this only trades page count for size."""

_MAX_ATTEMPTS = 6
_BACKOFF_START = 2.0
_BACKOFF_CAP = 32.0


class SlackError(Exception):
    """A Slack API or transport failure. Raised inside the driver's poll loop so
    the runtime records it as a channel failure rather than failing the run."""


def _resolve_token() -> str:
    """Locate the bot token: ``$SLACK_BOT_TOKEN`` if set, else the
    ``slack-bot-token`` file in the kroebot config dir
    (``$XDG_CONFIG_HOME`` or ``~/.config`` under ``kroebot/``).

    A fixed config-dir location means the token is found the same way from any
    checkout, matching how the kroebot Slack scripts resolve it -- one identity
    for all of the family's Slack I/O. The value is never logged.
    """
    env = os.environ.get('SLACK_BOT_TOKEN')
    if env:
        token = env.strip()
    else:
        config_home = os.environ.get('XDG_CONFIG_HOME') or str(Path.home() / '.config')
        token_file = Path(config_home) / 'kroebot' / 'slack-bot-token'
        if not token_file.is_file():
            raise SlackError(
                'no Slack bot token: set $SLACK_BOT_TOKEN, or write the xoxb- token '
                f'to {token_file}'
            )
        token = token_file.read_text(encoding='utf-8').strip()
    return token


def _slack_ts(when: dt.datetime) -> str:
    """Render an instant as a Slack ``ts`` (``<seconds>.<6-digit-micros>``).

    Used for the initial cursor of a channel that has never been positioned, so
    it watches from the run's clock forward rather than replaying history.
    """
    aware = when if when.tzinfo is not None else when.replace(tzinfo=dt.timezone.utc)
    return f'{aware.timestamp():.6f}'


def _to_datetime(ts: str) -> dt.datetime:
    """Parse a Slack ``ts`` into an aware UTC datetime for the event envelope."""
    return dt.datetime.fromtimestamp(float(ts), tz=dt.timezone.utc)


def _next_cursor(page: dict[str, object]) -> str:
    """The pagination cursor for the following page, or ``''`` at the end."""
    meta = page.get('response_metadata')
    cursor = meta.get('next_cursor') if isinstance(meta, dict) else None
    return cursor if isinstance(cursor, str) else ''


def _combine(pages: list[list[object]]) -> list[dict[str, object]]:
    """Merge pages into one ascending, deduped list keyed by ``ts``.

    Slack pages a conversation newest-first and walks its cursor backward, so a
    fetch-order concatenation would be reversed and, at a page seam, could repeat
    a message; keying by ``ts`` dedupes and sorting the keys puts the result in
    chronological order so the driver can advance its cursor monotonically. The
    key is the raw ``ts`` string, whose fixed width makes lexicographic order
    equal chronological order and identity exact -- where ``float(ts)`` would
    round-trip through a double.
    """
    by_ts: dict[str, dict[str, object]] = {}
    for page in pages:
        for message in page:
            if isinstance(message, dict):
                ts = message.get('ts')
                if isinstance(ts, str):
                    by_ts[ts] = message
    return [by_ts[ts] for ts in sorted(by_ts)]


class _ApiClient(Protocol):
    """The slice of the Slack API the driver depends on, so a test can supply a
    fake without a network."""

    async def history_since(
        self, channel: str, oldest: str
    ) -> list[dict[str, object]]: ...

    async def replies_since(
        self, channel: str, parent: str, oldest: str
    ) -> list[dict[str, object]]: ...

    async def aclose(self) -> None: ...


class _Api:
    """A minimal async Slack Web API client over ``httpx``.

    Isolates the network so the driver's polling and event-building stay pure and
    testable. Only a rate-limit rejection is retried -- a 429 means Slack
    throttled the request without running it, so replaying is safe -- with capped
    exponential backoff; every other ``{"ok": false}`` is terminal.
    """

    def __init__(
        self, token: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=_API_ROOT,
            headers={'Authorization': f'Bearer {token}'},
            timeout=httpx.Timeout(75.0, connect=20.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def history_since(self, channel: str, oldest: str) -> list[dict[str, object]]:
        """Every top-level message strictly after ``oldest``, oldest-first."""
        return await self._paginate(
            'conversations.history', channel=channel, oldest=oldest
        )

    async def replies_since(
        self, channel: str, parent: str, oldest: str
    ) -> list[dict[str, object]]:
        """Every reply in the thread rooted at ``parent`` strictly after
        ``oldest``, oldest-first, excluding the parent itself.

        ``conversations.replies`` returns the thread's parent at the top of every
        page regardless of ``oldest``, so it is dropped here -- the caller has
        already emitted it as a top-level message.
        """
        replies = await self._paginate(
            'conversations.replies', channel=channel, ts=parent, oldest=oldest
        )
        return [reply for reply in replies if reply.get('ts') != parent]

    async def _paginate(self, method: str, **params: str) -> list[dict[str, object]]:
        """Follow ``response_metadata.next_cursor`` to the end of the window,
        then :func:`_combine` the pages into a single read's order and identity."""
        pages: list[list[object]] = []
        cursor = ''
        while True:
            page = await self._call(
                method, cursor=cursor, limit=str(_PAGE_LIMIT), **params
            )
            messages = page.get('messages')
            pages.append(messages if isinstance(messages, list) else [])
            cursor = _next_cursor(page)
            if not cursor:
                break
        return _combine(pages)

    async def _call(self, method: str, **params: str) -> dict[str, object]:
        """One Slack GET with rate-limit retry; the parsed body, or raises."""
        query = {key: value for key, value in params.items() if value}
        backoff = _BACKOFF_START
        result: dict[str, object] | None = None
        error = 'ratelimited'
        for _attempt in range(_MAX_ATTEMPTS):
            body = await self._get(method, query)
            if body.get('ok'):
                result = body
                break
            error = str(body.get('error') or 'unknown')
            if error != 'ratelimited':
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_CAP)
        if result is None:
            raise SlackError(f'Slack {method} failed: {error}')
        return result

    async def _get(self, method: str, query: dict[str, str]) -> dict[str, object]:
        """Issue the GET and parse a JSON object, mapping a transport or
        malformed-response failure to :class:`SlackError`."""
        try:
            response = await self._http.get(f'/{method}', params=query)
            body = response.json()
        except httpx.HTTPError as exc:
            raise SlackError(f'Slack {method} request failed: {exc}') from exc
        if not isinstance(body, dict):
            raise SlackError(f'Slack {method}: response was not a JSON object')
        return body


def _default_api() -> _ApiClient:
    """Build the real Slack client, resolving the bot token now -- called from
    inside :meth:`_Driver.watch`, so a missing token is a channel failure."""
    return _Api(_resolve_token())


class _Options(pydantic.BaseModel):
    """A Slack channel's own ``options`` block, validated when the driver runs."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    channel: str
    """The Slack conversation ID to watch -- a ``C…`` channel, ``D…`` DM, or
    ``G…`` group -- which the bot must already be a member of."""

    poll_interval: _config.Duration = dt.timedelta(seconds=1)
    """How often to re-poll for new messages within a run. Best kept at or below
    the boarding window, so a burst is collected in one run not split across
    two."""


class _Driver:
    """Watches one Slack conversation: its top level, and the threads it follows.

    Keeps a top-level ``cursor`` and a ``threads`` map (thread-parent ``ts`` ->
    that thread's reply cursor) as its opaque state, and each poll asks Slack for
    everything after each cursor. It never ends on its own: like any live source
    it stays open until the runtime closes it, and every cursor is marked before
    its yield (obligation 3) -- so on close the state is exactly what was emitted,
    never ahead of it.
    """

    def __init__(
        self,
        config: _config.ChannelConfig,
        cursor: str,
        threads: dict[str, str] | None = None,
        api_factory: Callable[[], _ApiClient] | None = None,
    ) -> None:
        self._config = config
        self._cursor = cursor
        self._threads = dict(threads or {})
        self._api_factory = api_factory

    def get_state(self) -> dict[str, object]:
        return {'cursor': self._cursor, 'threads': dict(self._threads)}

    async def watch(self) -> AsyncGenerator[Event, None]:
        options = self._options()
        api = (self._api_factory or _default_api)()
        try:
            while True:
                for message in await api.history_since(options.channel, self._cursor):
                    ts = message['ts']
                    assert isinstance(ts, str)  # _combine keeps only str-ts messages
                    self._cursor = ts  # obligation 3: mark before the yield
                    self._follow_thread(message, ts)
                    yield self._event(message, ts, 'message')
                for parent in list(self._threads):
                    replies = await api.replies_since(
                        options.channel, parent, self._threads[parent]
                    )
                    for reply in replies:
                        rts = reply['ts']
                        assert isinstance(rts, str)
                        self._threads[parent] = rts  # obligation 3: before the yield
                        yield self._event(reply, rts, 'reply')
                await asyncio.sleep(options.poll_interval.total_seconds())
        finally:
            await api.aclose()

    def _follow_thread(self, message: dict[str, object], parent: str) -> None:
        """Start following a top-level message's thread if it already has replies.

        A newly followed thread's reply cursor starts at the parent's own ``ts``,
        so the next poll fetches every reply after it; a thread already followed
        keeps its advanced cursor.
        """
        reply_count = message.get('reply_count')
        if isinstance(reply_count, int) and reply_count > 0:
            self._threads.setdefault(parent, parent)

    def _options(self) -> _Options:
        try:
            options = _Options.model_validate(self._config.options)
        except pydantic.ValidationError as exc:
            raise SlackError(f'invalid slack options: {exc}') from exc
        return options

    def _event(self, message: dict[str, object], ts: str, kind: str) -> Event:
        return Event(
            channel=self._config.key,
            type=self._config.type,
            kind=kind,
            id=ts,
            ts=_to_datetime(ts),
            meta=dict(self._config.meta),
            payload=dict(message),
        )


def _initial_cursor(prev_state: dict[str, object] | None, now: dt.datetime) -> str:
    """The top-level cursor to start from: the recorded one, or the run's clock
    for a channel that has never been positioned."""
    recorded = prev_state.get('cursor') if prev_state else None
    return recorded if isinstance(recorded, str) else _slack_ts(now)


def _initial_threads(prev_state: dict[str, object] | None) -> dict[str, str]:
    """The per-thread reply cursors to resume, keyed by thread-parent ``ts``;
    empty for a channel that has never followed a thread."""
    recorded = prev_state.get('threads') if prev_state else None
    result: dict[str, str] = {}
    if isinstance(recorded, dict):
        for parent, cursor in recorded.items():
            if isinstance(parent, str) and isinstance(cursor, str):
                result[parent] = cursor
    return result


def build(
    _key: str,
    config: _config.ChannelConfig,
    _prev_config: _config.ChannelConfig | None,
    prev_state: dict[str, object] | None,
    context: _driver.RunContext,
) -> _driver.ChannelDriver:
    """Construct the Slack driver for a channel (a :data:`_driver.DriverFactory`).

    Resumes from the recorded cursors when there are any. Does no IO and cannot
    fail: the token, the options, and the network are all touched inside
    :meth:`_Driver.watch`.
    """
    return _Driver(
        config,
        _initial_cursor(prev_state, context.now),
        _initial_threads(prev_state),
    )
