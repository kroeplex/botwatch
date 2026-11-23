# pylint: disable=protected-access

import asyncio
import botwatch
import contextlib
from . import _fake_slack
from botwatch import slack
from botwatch.slack import _api
from botwatch.slack import _ts
from botwatch.slack import _watch
from datetime import timedelta

_CONVERSATION = _fake_slack.CONVERSATION

_NO_WAIT = timedelta(0)


class Test_apply_poll:
    def test__reports_new_messages_oldest_first(self) -> None:
        state = slack.SlackState(after='1785600001.000000')
        messages = [
            _api.Message(ts='1785600003.000000', user='U1', text='third'),
            _api.Message(ts='1785600001.000000', text='already reported'),
            _api.Message(ts='1785600002.000000', bot_id='B1', username='Bot'),
        ]

        update = _watch._apply_poll(state, messages, conversation=_CONVERSATION)

        assert update.events == (
            slack.MessagePosted(
                id='1785600002.000000',
                time=_ts.ts_to_datetime('1785600002.000000'),
                conversation=_CONVERSATION,
                ts='1785600002.000000',
                thread_ts=None,
                user=None,
                bot_id='B1',
                username='Bot',
                subtype=None,
                text='',
            ),
            slack.MessagePosted(
                id='1785600003.000000',
                time=_ts.ts_to_datetime('1785600003.000000'),
                conversation=_CONVERSATION,
                ts='1785600003.000000',
                thread_ts=None,
                user='U1',
                bot_id=None,
                username=None,
                subtype=None,
                text='third',
            ),
        )
        assert update.state == slack.SlackState(after='1785600003.000000')

    def test__without_new_messages_keeps_the_state(self) -> None:
        state = slack.SlackState(after='1785600001.000000')

        update = _watch._apply_poll(state, [], conversation=_CONVERSATION)

        assert not update.events
        assert update.state == state


class Test_watch:
    async def test__starts_after_the_newest_message(self) -> None:
        fake = _fake_slack.FakeSlack()
        fake.post('1785600001.000000')
        fake.post('1785600002.000000')

        async with fake.create_client() as client:
            source = slack.watch(client, _CONVERSATION, state=None)
            async with contextlib.aclosing(source):
                checkpoint = await anext(source)

        assert not checkpoint.events
        assert checkpoint.state == slack.SlackState(after='1785600002.000000')

    async def test__starts_after_the_newest_reply(self) -> None:
        fake = _fake_slack.FakeSlack()
        fake.post('1785600001.000000')
        fake.post('1785600002.000000', thread_ts='1785600001.000000')
        fake.post('1785600003.000000')

        async with fake.create_client() as client:
            source = slack.watch(
                client,
                _CONVERSATION,
                state=None,
                thread_ts='1785600001.000000',
            )
            async with contextlib.aclosing(source):
                checkpoint = await anext(source)

        assert checkpoint.state == slack.SlackState(after='1785600002.000000')

    async def test__starts_a_thread_without_replies_after_its_parent(self) -> None:
        fake = _fake_slack.FakeSlack()
        fake.post('1785600001.000000')
        fake.post('1785600002.000000')

        async with fake.create_client() as client:
            source = slack.watch(
                client,
                _CONVERSATION,
                state=None,
                thread_ts='1785600001.000000',
            )
            async with contextlib.aclosing(source):
                checkpoint = await anext(source)

        assert checkpoint.state == slack.SlackState(after='1785600001.000000')

    async def test__starts_an_empty_conversation_at_the_beginning(self) -> None:
        fake = _fake_slack.FakeSlack()

        async with fake.create_client() as client:
            source = slack.watch(client, _CONVERSATION, state=None)
            async with contextlib.aclosing(source):
                checkpoint = await anext(source)

        assert checkpoint.state == slack.SlackState(after=_ts.EPOCH)

    async def test__resumes_from_the_state(self) -> None:
        fake = _fake_slack.FakeSlack()
        for ts in ['1785600001.000000', '1785600002.000000', '1785600003.000000']:
            fake.post(ts)
        state = slack.SlackState(after='1785600001.000000')

        async with fake.create_client() as client:
            source = slack.watch(
                client,
                _CONVERSATION,
                state=state,
                poll_interval=_NO_WAIT,
            )
            async with contextlib.aclosing(source):
                checkpoint = await anext(source)
                update = await anext(source)

        assert checkpoint.state == state
        assert [event.ts for event in update.events] == [
            '1785600002.000000',
            '1785600003.000000',
        ]
        assert update.state.after == '1785600003.000000'

    async def test__yields_only_polls_that_find_messages(self) -> None:
        fake = _fake_slack.FakeSlack()
        state = slack.SlackState(after='1785600001.000000')

        async with fake.create_client() as client:
            source = slack.watch(
                client,
                _CONVERSATION,
                state=state,
                poll_interval=_NO_WAIT,
            )
            async with contextlib.aclosing(source):
                await anext(source)
                pending = asyncio.ensure_future(anext(source))
                while len(fake.requests) < 3:
                    await asyncio.sleep(0)
                fake.post('1785600002.000000', text='late')
                update = await pending

        assert [event.text for event in update.events] == ['late']

    async def test__watches_one_thread_across_pages(self) -> None:
        fake = _fake_slack.FakeSlack()
        fake.post('1785600001.000000', text='parent')
        for second, text in [(2, 'old'), (3, 'new 1'), (4, 'new 2'), (5, 'new 3')]:
            fake.post(
                f'178560000{second}.000000',
                thread_ts='1785600001.000000',
                text=text,
            )
        fake.post('1785600006.000000', text='elsewhere')
        state = slack.SlackState(after='1785600002.000000')

        async with fake.create_client() as client:
            source = slack.watch(
                client,
                _CONVERSATION,
                state=state,
                thread_ts='1785600001.000000',
                poll_interval=_NO_WAIT,
            )
            async with contextlib.aclosing(source):
                await anext(source)
                update = await anext(source)

        assert [(event.text, event.thread_ts) for event in update.events] == [
            ('new 1', '1785600001.000000'),
            ('new 2', '1785600001.000000'),
            ('new 3', '1785600001.000000'),
        ]
        assert sum('cursor' in request.url.params for request in fake.requests) == 1


async def _poll_for(
    sources: dict[str, botwatch.Source[slack.SlackState, slack.MessagePosted]],
    seconds: float,
) -> list[slack.MessagePosted]:
    """Runs ``sources`` together for ``seconds`` and returns their events."""
    events: list[slack.MessagePosted] = []
    updates = botwatch.select(
        sources,
        quiet_timeout=timedelta(seconds=seconds),
        linger=timedelta(seconds=seconds),
    )
    async with contextlib.aclosing(updates):
        async for _key, task in updates:
            events.extend((await task).events)
    return events


def _count_calls(fake: _fake_slack.FakeSlack, method: str) -> int:
    return sum(request.url.path.endswith(method) for request in fake.requests)


class Test_watch__sharing:
    async def test__idle_threads_make_no_calls_of_their_own(self) -> None:
        fake = _fake_slack.FakeSlack(page_size=50)
        parents = ['1785600001.000000', '1785600002.000000', '1785600003.000000']
        for number, parent in enumerate(parents):
            fake.post(parent)
            fake.post(f'178560001{number}.000000', thread_ts=parent)

        async with fake.create_client() as client:
            sources = {
                parent: slack.watch(
                    client,
                    _CONVERSATION,
                    state=slack.SlackState(after=f'178560001{number}.000000'),
                    thread_ts=parent,
                    poll_interval=timedelta(seconds=0.05),
                )
                for number, parent in enumerate(parents)
            }
            events = await _poll_for(sources, 0.3)

        assert not events
        assert _count_calls(fake, 'conversations.replies') == 0
        # About one shared call per polling round, not one per source per round.
        assert _count_calls(fake, 'conversations.history') <= 9

    async def test__a_thread_with_a_new_reply_fetches_it(self) -> None:
        fake = _fake_slack.FakeSlack(page_size=50)
        fake.post('1785600001.000000')
        fake.post('1785600002.000000', thread_ts='1785600001.000000', text='new')

        async with fake.create_client() as client:
            source = slack.watch(
                client,
                _CONVERSATION,
                state=slack.SlackState(after='1785600001.000000'),
                thread_ts='1785600001.000000',
                poll_interval=_NO_WAIT,
            )
            async with contextlib.aclosing(source):
                await anext(source)
                update = await anext(source)

        assert [event.text for event in update.events] == ['new']

    async def test__a_thread_out_of_view_polls_on_its_own(self) -> None:
        fake = _fake_slack.FakeSlack(page_size=2)
        fake.post('1785600001.000000')  # the parent, older than the page
        fake.post('1785600002.000000', thread_ts='1785600001.000000', text='new')
        for second in [3, 4, 5]:
            fake.post(f'178560000{second}.000000')

        async with fake.create_client() as client:
            source = slack.watch(
                client,
                _CONVERSATION,
                state=slack.SlackState(after='1785600001.000000'),
                thread_ts='1785600001.000000',
                poll_interval=_NO_WAIT,
            )
            async with contextlib.aclosing(source):
                await anext(source)
                update = await anext(source)

        assert [event.text for event in update.events] == ['new']

    async def test__the_top_level_reads_the_shared_history(self) -> None:
        fake = _fake_slack.FakeSlack(page_size=50)
        for second in [1, 2, 3]:
            fake.post(f'178560000{second}.000000')

        async with fake.create_client() as client:
            source = slack.watch(
                client,
                _CONVERSATION,
                state=slack.SlackState(after='1785600001.000000'),
                poll_interval=_NO_WAIT,
            )
            async with contextlib.aclosing(source):
                await anext(source)
                update = await anext(source)

        assert [event.ts for event in update.events] == [
            '1785600002.000000',
            '1785600003.000000',
        ]
        assert not any('oldest' in request.url.params for request in fake.requests)

    async def test__the_top_level_reads_further_back_when_it_must(self) -> None:
        fake = _fake_slack.FakeSlack(page_size=2)
        for second in [1, 2, 3, 4]:
            fake.post(f'178560000{second}.000000')

        async with fake.create_client() as client:
            source = slack.watch(
                client,
                _CONVERSATION,
                state=slack.SlackState(after='1785600001.000000'),
                poll_interval=_NO_WAIT,
            )
            async with contextlib.aclosing(source):
                await anext(source)
                update = await anext(source)

        assert [event.ts for event in update.events] == [
            '1785600002.000000',
            '1785600003.000000',
            '1785600004.000000',
        ]
