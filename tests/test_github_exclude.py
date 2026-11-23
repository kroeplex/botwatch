import pytest
import re
from . import _fake_github
from botwatch import github


def _review(body: str, comments: list[str]) -> github.ReviewSubmitted:
    return github.ReviewSubmitted(
        id='review-1',
        time=_fake_github.at(1),
        repo=_fake_github.REPO,
        pull=_fake_github.PULL,
        user='Octo-Bot',
        state='COMMENTED',
        body=body,
        url='https://github.com/',
        comments=tuple(
            github.ReviewComment(
                id=number,
                path='README.md',
                line=1,
                in_reply_to=None,
                body=comment,
                url='https://github.com/',
            )
            for number, comment in enumerate(comments)
        ),
    )


@pytest.mark.parametrize(
    ('rule', 'review', 'matches'),
    [
        (github.ExcludeRule(user='octo-bot'), _review('', []), True),
        (github.ExcludeRule(user='octocat'), _review('', []), False),
        (github.ExcludeRule(text=re.compile('^🤖')), _review('🤖 done', []), True),
        (
            github.ExcludeRule(text=re.compile('^🤖')),
            _review('', ['🤖 a', '🤖 b']),
            True,
        ),
        (github.ExcludeRule(text=re.compile('^🤖')), _review('', ['🤖 a', 'b']), False),
        (github.ExcludeRule(text=re.compile('^🤖')), _review('', []), False),
    ],
)
def test_ExcludeRule__matches(
    rule: github.ExcludeRule,
    review: github.ReviewSubmitted,
    matches: bool,
) -> None:
    assert rule.matches(review) is matches


_PUSH = github.HeadChanged(
    id='head-a-b',
    time=_fake_github.at(1),
    repo=_fake_github.REPO,
    pull=_fake_github.PULL,
    user='Octo-Bot',
    before='a',
    after='b',
    message='[bot] Fix it',
    url='https://github.com/',
)


@pytest.mark.parametrize(
    ('rule', 'matches'),
    [
        (github.ExcludeRule(user='octo-bot'), True),
        (github.ExcludeRule(text=re.compile(r'^\[bot\]')), True),
        (github.ExcludeRule(user='octo-bot', text=re.compile('^human')), False),
    ],
)
def test_ExcludeRule__matches_a_push_by_committer_and_message(
    rule: github.ExcludeRule,
    matches: bool,
) -> None:
    assert rule.matches(_PUSH) is matches


def test_ExcludeRule__never_matches_a_checks_conclusion() -> None:
    concluded = github.ChecksConcluded(
        id='checks-1-a',
        time=_fake_github.at(1),
        repo=_fake_github.REPO,
        pull=_fake_github.PULL,
        head='a',
        conclusion='success',
        failed=(),
        pending=(),
        url='https://github.com/',
    )

    assert not github.ExcludeRule(user='octo-bot').matches(concluded)
    assert not github.ExcludeRule(text=re.compile('.*')).matches(concluded)
