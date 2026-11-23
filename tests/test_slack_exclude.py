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

_USERLESS = _MESSAGE.model_copy(update={'user': None})
"""Like a webhook's or a legacy bot's post."""


@pytest.mark.parametrize(
    ('rule', 'message', 'matches'),
    [
        ({'user': 'U0BOT'}, _MESSAGE, True),
        ({'user': 'U0SOMEONE'}, _MESSAGE, False),
        ({'bot_id': 'B0BOT', 'username': 'Helper'}, _MESSAGE, True),
        ({'bot_id': 'B0BOT', 'username': 'Other'}, _MESSAGE, False),
        ({'text': r'^\[op 1\]'}, _MESSAGE, True),
        ({'text': r'^\[op 2\]'}, _MESSAGE, False),
        ({'text': '(?i)ALL DONE'}, _MESSAGE, True),
        ({'user': 'U0BOT', 'text': 'done$'}, _MESSAGE, True),
        ({'user': 'U0BOT'}, _USERLESS, False),
        ({'bot_id': 'B0BOT'}, _USERLESS, True),
    ],
)
def test_ExcludeRule__matches(
    rule: dict[str, str],
    message: slack.MessagePosted,
    matches: bool,
) -> None:
    assert slack.ExcludeRule.model_validate(rule).matches(message) is matches


@pytest.mark.parametrize(
    'rule',
    [{}, {'user': None}, {'user': ''}, {'text': ''}, {'text': '('}],
)
def test_ExcludeRule__rejects_invalid(rule: dict[str, str | None]) -> None:
    with pytest.raises(pydantic.ValidationError):
        slack.ExcludeRule.model_validate(rule)
