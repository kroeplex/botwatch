import botwatch
import contextlib
import pydantic
import pytest
from botwatch import slack
from botwatch.slack import _ts

_MESSAGE = slack.MessagePosted(
    id='1785600001.000000',
    time=_ts.ts_to_datetime('1785600001.000000'),
    conversation='C0123456789',
    ts='1785600001.000000',
    thread_ts=None,
    user='U0BOT',
    bot_id='B0BOT',
    username='Helper',
    subtype=None,
    text='[op 1] all done',
)


async def test_exclude_events() -> None:
    closed: list[bool] = []

    async def source() -> botwatch.Source[int, slack.MessagePosted]:
        try:
            yield botwatch.Update(events=(), state=0)
            yield botwatch.Update(
                events=(_MESSAGE, _MESSAGE.model_copy(update={'text': 'kept'})),
                state=1,
            )
            yield botwatch.Update(events=(_MESSAGE,), state=2)
        finally:
            closed.append(True)

    updates = botwatch.exclude_events(
        source(),
        lambda message: message.text.startswith('[op 1]'),
    )
    async with contextlib.aclosing(updates):
        received = [
            ([event.text for event in update.events], update.state)
            async for update in updates
        ]

    assert received == [([], 0), (['kept'], 1), ([], 2)]
    assert closed == [True]


@pytest.mark.parametrize(
    ('rule', 'matches'),
    [
        ({'user': 'U0BOT'}, True),
        ({'user': 'U0SOMEONE'}, False),
        ({'bot_id': 'B0BOT', 'username': 'Helper'}, True),
        ({'bot_id': 'B0BOT', 'username': 'Other'}, False),
        ({'text': r'^\[op 1\]'}, True),
        ({'text': r'^\[op 2\]'}, False),
        ({'user': 'U0BOT', 'text': 'done$'}, True),
    ],
)
def test_ExcludeRule__matches(
    rule: dict[str, str],
    matches: bool,
) -> None:
    assert slack.ExcludeRule.model_validate(rule).matches(_MESSAGE) is matches


@pytest.mark.parametrize('rule', [{}, {'user': None}, {'text': '('}])
def test_ExcludeRule__rejects_invalid(rule: dict[str, str | None]) -> None:
    with pytest.raises(pydantic.ValidationError):
        slack.ExcludeRule.model_validate(rule)
