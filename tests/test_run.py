import asyncio
import contextlib
import io
import json
import pytest
from . import _fake_github
from . import _fake_slack
from botwatch import _config
from botwatch import _run
from botwatch import _state
from botwatch import github
from botwatch import slack
from datetime import timedelta

_PARENT = '1785600000.000000'

_POLL = timedelta(seconds=0.005)

_TOP = _config.SlackSourceConfig(
    type='slack',
    conversation=_fake_slack.CONVERSATION,
    poll_interval=_POLL,
)

_THREAD = _config.SlackSourceConfig(
    type='slack',
    conversation=_fake_slack.CONVERSATION,
    thread_ts=_PARENT,
    poll_interval=_POLL,
)


_FAILING_KEY = _config.SourceKey('slack:C0NO')

_FAILING_STATE = slack.SlackState(after=_PARENT)
"""The saved state of a source that fails, which it must keep."""


def _clients(
    slack_client: slack.SlackClient,
    github_client: github.GitHubClient | None = None,
) -> _config.Clients:
    return _config.Clients(
        slack=slack_client,
        github=github_client or _fake_github.FakeGitHub().create_client(),
    )


def _create_ts(number: int) -> str:
    return f'{1785600000 + number}.000000'


def _read_records(out: io.StringIO) -> list[tuple[str, str, str]]:
    """Each record's source, event type and event id."""
    records = [json.loads(line) for line in out.getvalue().splitlines()]
    return [
        (record['source'], record['event']['type'], record['event']['id'])
        for record in records
    ]


async def test_run_once() -> None:
    fake = _fake_slack.FakeSlack()
    fake.post(_create_ts(1), text='before the run')
    config = _config.Config(
        quiet_timeout=timedelta(seconds=2),
        linger=timedelta(seconds=0.1),
        sources=(_TOP, _config.SlackSourceConfig(type='slack', conversation='C0NO')),
    )
    out = io.StringIO()

    async def post_later() -> None:
        await asyncio.sleep(0.1)
        fake.post(_create_ts(2), text='during the run')

    async with fake.create_client() as client:
        poster = asyncio.create_task(post_later())
        state = await _run.run_once(
            config,
            _state.StateFile(sources={_FAILING_KEY: _FAILING_STATE}),
            clients=_clients(client),
            out=out,
        )
        await poster

    assert [record[:2] for record in _read_records(out)] == [
        ('slack:C0NO', 'botwatch.source_failed'),
        ('slack:C0123456789', 'slack.message_posted'),
    ]
    record = json.loads(out.getvalue().splitlines()[1])['event']
    assert record == {
        'type': 'slack.message_posted',
        'id': _create_ts(2),
        'time': '2026-08-01T16:00:02.000Z',
        'conversation': _fake_slack.CONVERSATION,
        'ts': _create_ts(2),
        'thread_ts': None,
        'user': 'U0456ABC',
        'bot_id': None,
        'username': None,
        'subtype': None,
        'text': 'during the run',
    }
    assert state == _state.StateFile(
        sources={
            _FAILING_KEY: _FAILING_STATE,
            _TOP.key: slack.SlackState(after=_create_ts(2)),
        },
    )


async def test_run_once__resumes_each_source_from_its_state() -> None:
    fake = _fake_slack.FakeSlack()
    for number in [1, 2, 3]:
        fake.post(_create_ts(number))
    config = _config.Config(
        quiet_timeout=timedelta(seconds=1),
        linger=timedelta(0),
        sources=(_TOP,),
    )
    previous = _state.StateFile(
        sources={
            _TOP.key: slack.SlackState(after=_create_ts(1)),
            _config.SourceKey('slack:C0GONE'): slack.SlackState(after=_create_ts(1)),
        },
    )
    out = io.StringIO()

    async with fake.create_client() as client:
        state = await _run.run_once(
            config,
            previous,
            clients=_clients(client),
            out=out,
        )

    assert [record[2] for record in _read_records(out)] == [
        _create_ts(2),
        _create_ts(3),
    ]
    assert state == _state.StateFile(
        sources={_TOP.key: slack.SlackState(after=_create_ts(3))},
    )


async def _post_over_time(
    fake: _fake_slack.FakeSlack,
    count: int,
) -> None:
    """Posts ``count`` messages 20ms apart, alternating between the thread and
    the top level."""
    for number in range(1, count + 1):
        await asyncio.sleep(0.02)
        fake.post(
            _create_ts(number),
            thread_ts=_PARENT if number % 2 else None,
        )


async def _watch_in_runs(
    linger: timedelta,
    crash_after: timedelta | None,
) -> list[tuple[str, str, str]]:
    """Runs botwatch again and again while messages arrive, carrying the state
    from run to run, and returns every record written.

    With ``crash_after``, every other run is killed that long after it starts,
    before it can return its state.
    """
    fake = _fake_slack.FakeSlack()
    fake.post(_PARENT)
    config = _config.Config(
        quiet_timeout=timedelta(seconds=0.1),
        linger=linger,
        sources=(_TOP, _THREAD),
    )
    state = _state.StateFile(
        sources={
            _TOP.key: slack.SlackState(after=_PARENT),
            _THREAD.key: slack.SlackState(after=_PARENT),
        },
    )
    out = io.StringIO()

    async with fake.create_client() as client:
        poster = asyncio.create_task(_post_over_time(fake, 12))
        crashes = crash_after is not None
        while not poster.done():
            run = _run.run_once(
                config,
                state,
                clients=_clients(client),
                out=out,
            )
            if crash_after is not None and crashes:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(run, crash_after.total_seconds())
            else:
                state = await run
            crashes = not crashes
        state = await _run.run_once(
            config,
            state,
            clients=_clients(client),
            out=out,
        )

    return _read_records(out)


@pytest.mark.parametrize(
    ('linger', 'crash_after'),
    [
        (timedelta(0), None),
        (timedelta(seconds=0.03), None),
        (timedelta(seconds=0.3), None),
        (timedelta(seconds=0.03), timedelta(seconds=0.05)),
    ],
)
async def test_run_once__where_a_run_ends_never_changes_what_is_emitted(
    linger: timedelta,
    crash_after: timedelta | None,
) -> None:
    """The design spec "Where a run ends never changes what is emitted", in
    ``docs/design.md``."""
    records = await _watch_in_runs(linger, crash_after)

    first_seen = list(dict.fromkeys(records))
    assert [id_ for source, _, id_ in first_seen if source == _TOP.key] == [
        _create_ts(number) for number in range(2, 13, 2)
    ]
    assert [id_ for source, _, id_ in first_seen if source == _THREAD.key] == [
        _create_ts(number) for number in range(1, 13, 2)
    ]
    if crash_after is None:
        assert records == first_seen
    else:
        assert len(records) > len(first_seen)  # the crashed runs did replay


async def test_run_once__skips_excluded_messages_but_moves_past_them() -> None:
    fake = _fake_slack.FakeSlack()
    fake.post(_create_ts(1), user='U0ME', text='mine')
    fake.post(_create_ts(2), user='U0YOU', text='yours')
    fake.post(_create_ts(3), user='U0ME', text='mine again')
    excluding = _TOP.model_copy(update={'exclude': (slack.ExcludeRule(user='U0ME'),)})
    config = _config.Config(
        quiet_timeout=timedelta(seconds=0.2),
        linger=timedelta(0),
        sources=(excluding,),
    )
    previous = _state.StateFile(sources={_TOP.key: slack.SlackState(after=_PARENT)})
    out = io.StringIO()

    async with fake.create_client() as client:
        state = await _run.run_once(
            config,
            previous,
            clients=_clients(client),
            out=out,
        )

    assert [record[2] for record in _read_records(out)] == [_create_ts(2)]
    assert state.sources[_TOP.key] == slack.SlackState(after=_create_ts(3))


async def test_run_once__writes_github_events() -> None:
    fake_github = _fake_github.FakeGitHub()
    fake_github.comment(3, body='please look')
    fake_github.review(4, body='approved')
    source = _config.GitHubSourceConfig(
        type='github',
        repo=_fake_github.REPO,
        pull=_fake_github.PULL,
        poll_interval=_POLL,
    )
    config = _config.Config(
        quiet_timeout=timedelta(seconds=5),
        linger=timedelta(seconds=0.05),
        sources=(source,),
    )
    out = io.StringIO()

    async with (
        _fake_slack.FakeSlack().create_client() as slack_client,
        fake_github.create_client() as github_client,
    ):
        state = await _run.run_once(
            config,
            _state.StateFile(
                sources={source.key: github.GitHubState(after=_fake_github.at(2))},
            ),
            clients=_clients(slack_client, github_client),
            out=out,
        )

    records = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [record['source'] for record in records] == [source.key] * 2
    assert [record['event']['type'] for record in records] == [
        'github.comment_posted',
        'github.review_submitted',
    ]
    assert state.sources[source.key] == github.GitHubState(
        after=_fake_github.at(4),
        seen=(records[1]['event']['id'],),
    )
