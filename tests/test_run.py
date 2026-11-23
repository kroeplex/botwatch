import asyncio
import io
import json
from . import _fake_slack
from botwatch import _config
from botwatch import _run
from datetime import timedelta


async def test_run_once() -> None:
    fake = _fake_slack.FakeSlack()
    fake.post('1785600001.000000', text='before the run')
    config = _config.Config(
        quiet_timeout=timedelta(seconds=2),
        linger=timedelta(seconds=0.1),
        sources=(
            _config.SlackSourceConfig(
                type='slack',
                conversation=_fake_slack.CONVERSATION,
                poll_interval=timedelta(seconds=0.01),
            ),
            _config.SlackSourceConfig(type='slack', conversation='C_MISSING'),
        ),
    )
    out = io.StringIO()

    async def post_later() -> None:
        await asyncio.sleep(0.1)
        fake.post('1785600002.000000', text='during the run')

    async with fake.create_client() as client:
        poster = asyncio.create_task(post_later())
        await _run.run_once(config, slack_client=client, out=out)
        await poster

    records = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [(record['source'], record['event']['type']) for record in records] == [
        ('slack:C_MISSING', 'botwatch.source_failed'),
        ('slack:C0123456789', 'slack.message_posted'),
    ]
    assert 'channel_not_found' in records[0]['event']['error']
    assert records[1]['event'] | {'time': None} == {
        'id': '1785600002.000000',
        'time': None,
        'type': 'slack.message_posted',
        'conversation': _fake_slack.CONVERSATION,
        'ts': '1785600002.000000',
        'thread_ts': None,
        'user': 'U0456ABC',
        'bot_id': None,
        'username': None,
        'subtype': None,
        'text': 'during the run',
    }
    assert records[1]['event']['time'] == '2026-08-01T16:00:02.000Z'
