# pylint: disable=protected-access

import asyncio
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
            source = slack.watch(client, _CONVERSATION, None)
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
                None,
                thread_ts='1785600001.000000',
            )
            async with contextlib.aclosing(source):
                checkpoint = await anext(source)

        assert checkpoint.state == slack.SlackState(after='1785600002.000000')

    async def test__starts_an_empty_conversation_at_the_beginning(self) -> None:
        fake = _fake_slack.FakeSlack()

        async with fake.create_client() as client:
            source = slack.watch(client, _CONVERSATION, None)
            async with contextlib.aclosing(source):
                checkpoint = await anext(source)

        assert checkpoint.state == slack.SlackState(after=_ts.EPOCH)

    async def test__resumes_from_the_state(self) -> None:
        fake = _fake_slack.FakeSlack()
        for ts in ['1785600001.000000', '1785600002.000000', '1785600003.000000']:
            fake.post(ts)
        state = slack.SlackState(after='1785600001.000000')

        async with fake.create_client() as client:
            source = slack.watch(client, _CONVERSATION, state, poll_interval=_NO_WAIT)
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
            source = slack.watch(client, _CONVERSATION, state, poll_interval=_NO_WAIT)
            async with contextlib.aclosing(source):
                await anext(source)
                pending = asyncio.ensure_future(anext(source))
                while len(fake.requests) < 3:
                    await asyncio.sleep(0)
                fake.post('1785600002.000000', text='late')
                update = await pending

        assert [event.text for event in update.events] == ['late']

    async def test__watches_one_thread(self) -> None:
        fake = _fake_slack.FakeSlack()
        fake.post('1785600001.000000', text='parent')
        fake.post('1785600002.000000', thread_ts='1785600001.000000', text='old')
        fake.post('1785600003.000000', thread_ts='1785600001.000000', text='new')
        fake.post('1785600004.000000', text='elsewhere')
        state = slack.SlackState(after='1785600002.000000')

        async with fake.create_client() as client:
            source = slack.watch(
                client,
                _CONVERSATION,
                state,
                thread_ts='1785600001.000000',
                poll_interval=_NO_WAIT,
            )
            async with contextlib.aclosing(source):
                await anext(source)
                update = await anext(source)

        assert [(event.text, event.thread_ts) for event in update.events] == [
            ('new', '1785600001.000000'),
        ]
