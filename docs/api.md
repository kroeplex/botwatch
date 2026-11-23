# Python API

botwatch's parts work as a library. The [design](design.md) explains how they
fit together; the docstrings have the details.

## `botwatch`

| Name | What it is |
| --- | --- |
| `Source[StateT, EventT]` | A source: an async generator of `Update`s that yields forever. Its first update is a checkpoint, and its consumer closes it. |
| `Update[StateT, EventT]` | Zero or more `events`, and the source's full `state` after them. |
| `Event` | The base of every event model: `type`, `id` and `time`. |
| `select(sources, *, quiet_timeout, linger)` | Waits on a mapping of sources at once, yielding `(key, task)` pairs as [`turbopipes.aselect`](https://github.com/kkroening/turbopipes) does, until `linger` after the first update with events, or `quiet_timeout` if none comes. |
| `exclude_events(source, is_excluded)` | A stage that drops matching events but keeps each update's state. |

## `botwatch.slack`

| Name | What it is |
| --- | --- |
| `SlackClient(token)` | An async Web API client for one token, used as an async context manager. It retries transient failures. |
| `watch(client, conversation, *, state, thread_ts=None, poll_interval=DEFAULT_POLL_INTERVAL)` | A source for a conversation's top level, or with `thread_ts` one thread. |
| `SlackState` | A Slack source's state: `after`; the source reports only messages after this `ts`. |
| `MessagePosted` | The `slack.message_posted` event. |
| `ExcludeRule` | A rule over `user`, `bot_id`, `username` and a `text` regex. |
| `SlackError` | Raised when Slack answers `"ok": false`. |
| `SlackTs`, `ConversationId` | Validated string types. |

## Example

```python
import asyncio
import botwatch
import contextlib
import os
from botwatch import slack
from datetime import timedelta


async def main(token: str) -> None:
    async with slack.SlackClient(token) as client:
        sources = {
            'top': slack.watch(client, 'C0123456789', state=None),
            'thread': slack.watch(
                client,
                'C0123456789',
                state=None,
                thread_ts='1785600000.000200',
            ),
        }
        updates = botwatch.select(
            sources,
            quiet_timeout=timedelta(minutes=55),
            linger=timedelta(seconds=5),
        )
        async with contextlib.aclosing(updates):
            async for key, task in updates:
                update = await task  # raises what the source raised
                for event in update.events:
                    print(key, event.text)


asyncio.run(main(os.environ['SLACK_TOKEN']))
```
