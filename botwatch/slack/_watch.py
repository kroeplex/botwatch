"""The Slack source: a conversation's top level, or one thread."""

import asyncio
import pydantic
from .. import _source
from . import _api
from . import _ts
from collections.abc import Sequence
from datetime import timedelta
from typing import Annotated
from typing import Literal

DEFAULT_POLL_INTERVAL = timedelta(seconds=30)

type ConversationId = Annotated[
    str,
    pydantic.StringConstraints(pattern=r'^[A-Z0-9]+$'),
]
"""A conversation's ID, such as ``C0123456789``: a channel, private channel, DM
or group DM."""


class SlackState(pydantic.BaseModel):
    """How far a Slack source has got."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    type: Literal['slack'] = 'slack'

    after: _ts.SlackTs
    """The source reports only messages after this ``ts``."""


class MessagePosted(_source.Event):
    """A message was posted to a conversation's top level, or to a thread."""

    type: Literal['slack.message_posted'] = 'slack.message_posted'

    conversation: ConversationId

    ts: _ts.SlackTs
    """The message's ``ts``, its ID within the conversation."""

    thread_ts: _ts.SlackTs | None
    """The thread's parent ``ts``, on a reply or on a parent with replies."""

    user: str | None
    """The author's user ID. An app's bot user has one too."""

    bot_id: str | None
    """The app integration that posted it, if an app did."""

    username: str | None
    """The display name an app set when posting with ``chat:write.customize``."""

    subtype: str | None
    """Slack's message subtype, such as ``thread_broadcast``. ``None`` on
    ordinary messages."""

    text: str


def _message_to_event(
    message: _api.Message,
    conversation: str,
) -> MessagePosted:
    return MessagePosted(
        id=message.ts,
        time=_ts.ts_to_datetime(message.ts),
        conversation=conversation,
        ts=message.ts,
        thread_ts=message.thread_ts,
        user=message.user,
        bot_id=message.bot_id,
        username=message.username,
        subtype=message.subtype,
        text=message.text,
    )


def _apply_poll(
    state: SlackState,
    messages: Sequence[_api.Message],
    *,
    conversation: str,
) -> _source.Update[SlackState, MessagePosted]:
    """Folds one poll's messages into the state: an event for each message after
    ``state.after``, oldest first, and the state after them."""
    new_messages = sorted(
        (message for message in messages if message.ts > state.after),
        key=lambda message: message.ts,
    )
    after = new_messages[-1].ts if new_messages else state.after
    return _source.Update(
        events=tuple(
            _message_to_event(message, conversation) for message in new_messages
        ),
        state=SlackState(after=after),
    )


def _find_latest_ts(
    recent: _api.RecentHistory,
    thread_ts: _ts.SlackTs | None,
) -> _ts.SlackTs | None:
    """The newest ``ts`` at the top level, or in a thread whose parent is in
    ``recent``, or ``None`` when the parent isn't there.

    The newest top-level message is always in ``recent``, so an empty one means
    an empty conversation, which starts at the beginning.
    """
    if thread_ts is None:
        latest: _ts.SlackTs | None = (
            recent.messages[-1].ts if recent.messages else _ts.EPOCH
        )
    else:
        parent = recent.find(thread_ts)
        latest = (parent.latest_reply or parent.ts) if parent is not None else None
    return latest


async def _fetch_start_state(
    client: _api.SlackClient,
    conversation: str,
    *,
    thread_ts: _ts.SlackTs | None,
    max_age: timedelta,
) -> SlackState:
    """The state of a source that starts now: just after the newest message
    already there.

    Taking the position from Slack rather than from the local clock means clock
    skew can neither skip a message posted just after the start nor report one
    posted just before it.
    """
    recent = await client.fetch_recent_history(conversation, max_age=max_age)
    latest = _find_latest_ts(recent, thread_ts)
    if latest is None:
        assert thread_ts is not None  # the top level always has an answer
        latest = await client.fetch_latest_reply_ts(conversation, thread_ts)
    return SlackState(after=latest)


async def _fetch_new_messages(
    client: _api.SlackClient,
    conversation: str,
    *,
    thread_ts: _ts.SlackTs | None,
    after: _ts.SlackTs,
    max_age: timedelta,
) -> list[_api.Message]:
    """The messages after ``after``, at the top level or in one thread.

    The conversation's recent history, shared with every other source on it,
    answers whenever it can. A thread whose parent there shows no reply after
    ``after`` has nothing new, and the top level is read straight from it when
    it reaches back far enough. Anything else takes a call of its own.
    """
    recent = await client.fetch_recent_history(conversation, max_age=max_age)
    if thread_ts is None:
        if recent.covers(after):
            messages = [message for message in recent.messages if message.ts > after]
        else:
            messages = await client.fetch_history(conversation, oldest=after)
    else:
        parent = recent.find(thread_ts)
        if parent is not None and (parent.latest_reply or parent.ts) <= after:
            messages = []
        else:
            messages = await client.fetch_replies(conversation, thread_ts, oldest=after)
    return messages


async def watch(
    client: _api.SlackClient,
    conversation: str,
    *,
    state: SlackState | None,
    thread_ts: _ts.SlackTs | None = None,
    poll_interval: timedelta = DEFAULT_POLL_INTERVAL,
) -> _source.Source[SlackState, MessagePosted]:
    """Watches a conversation's top level, or with ``thread_ts`` one thread,
    forever.

    Starts from ``state``, or when it's ``None`` from just after the newest
    message already there, and first yields that starting state as a
    checkpoint. Then it polls every ``poll_interval`` and yields an update for
    each poll that finds new messages.

    Example::

        async with slack.SlackClient(token) as client:
            source = slack.watch(client, 'C0123456789', state=None)
            async with contextlib.aclosing(source):
                async for update in source:
                    for event in update.events:
                        print(event.text)
    """
    # Sources on one conversation poll in near step, so half an interval is
    # enough for them to share each history call.
    max_age = poll_interval / 2
    current = (
        state
        if state is not None
        else await _fetch_start_state(
            client,
            conversation,
            thread_ts=thread_ts,
            max_age=max_age,
        )
    )
    yield _source.Update(events=(), state=current)

    while True:
        messages = await _fetch_new_messages(
            client,
            conversation,
            thread_ts=thread_ts,
            after=current.after,
            max_age=max_age,
        )

        update = _apply_poll(current, messages, conversation=conversation)
        if update.events:
            yield update
        current = update.state

        await asyncio.sleep(poll_interval.total_seconds())
