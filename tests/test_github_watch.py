# pylint: disable=protected-access

import contextlib
import httpx
import pytest
from . import _fake_github
from botwatch import github
from botwatch.github import _api
from botwatch.github import _watch
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta

_REPO = _fake_github.REPO

_PULL = _fake_github.PULL

_NO_WAIT = timedelta(0)

_HEAD = _fake_github.HEAD

_NEW_HEAD = 'b' * 40

at = _fake_github.at


def _bodies(events: Sequence[github.GitHubEvent]) -> list[str]:
    return [
        event.body
        for event in events
        if isinstance(event, github.CommentPosted | github.ReviewSubmitted)
    ]


def _comment(event_id: str, time: datetime) -> github.CommentPosted:
    return github.CommentPosted(
        id=event_id,
        time=time,
        repo=_REPO,
        pull=_PULL,
        user='octocat',
        body='hello',
        url='https://github.com/',
    )


class Test_apply_poll:
    def test__reports_new_events_oldest_first(self) -> None:
        state = github.GitHubState(after=at(1), seen={'comment-1': at(1)})
        events = [
            _comment('comment-3', at(3)),
            _comment('comment-1', at(1)),
            _comment('comment-2', at(1)),
        ]

        update = _watch._apply_poll(
            state,
            events,
            settled_before=at(10),
            head=_HEAD,
        )

        assert [event.id for event in update.events] == ['comment-2', 'comment-3']
        assert update.state == github.GitHubState(
            head=_HEAD,
            after=at(3),
            seen={'comment-3': at(3)},
        )

    def test__trails_events_that_may_not_have_settled(self) -> None:
        state = github.GitHubState(after=at(1))
        events = [_comment('comment-1', at(1)), _comment('comment-3', at(3))]

        update = _watch._apply_poll(
            state,
            events,
            settled_before=at(2),
            head=_HEAD,
        )

        assert [event.id for event in update.events] == ['comment-1', 'comment-3']
        assert update.state == github.GitHubState(
            head=_HEAD,
            after=at(2),
            seen={'comment-3': at(3)},
        )

    def test__remembers_reported_events_a_listing_misses(self) -> None:
        state = github.GitHubState(
            after=at(10),
            seen={'comment-1': at(16), 'comment-2': at(15)},
        )
        listed = [_comment('comment-2', at(15)), _comment('comment-3', at(20))]

        first = _watch._apply_poll(
            state,
            listed,
            settled_before=at(14),
            head=_HEAD,
        )
        second = _watch._apply_poll(
            first.state,
            [_comment('comment-1', at(16)), *listed],
            settled_before=at(14),
            head=_HEAD,
        )

        assert [event.id for event in first.events] == ['comment-3']
        assert first.state == github.GitHubState(
            head=_HEAD,
            after=at(14),
            seen={'comment-1': at(16), 'comment-2': at(15), 'comment-3': at(20)},
        )
        assert not second.events

    def test__without_new_events_keeps_the_state(self) -> None:
        state = github.GitHubState(after=at(1), seen={'comment-1': at(1)})

        update = _watch._apply_poll(
            state,
            [_comment('comment-1', at(1))],
            settled_before=at(10),
            head=_HEAD,
        )

        assert not update.events
        assert update.state == state.model_copy(update={'head': _HEAD})


def _run(name: str, conclusion: str | None = 'success') -> _api.CheckRun:
    return _api.CheckRun(
        name=name,
        status='completed' if conclusion is not None else 'in_progress',
        conclusion=conclusion,
    )


def _suite(status: str, runs: int, *, age: timedelta = _NO_WAIT) -> _api.CheckSuite:
    return _api.CheckSuite(
        status=status,
        latest_check_runs_count=runs,
        created_at=at(100) - age,
    )


@pytest.mark.parametrize(
    ('runs', 'suites', 'statuses', 'verdict'),
    [
        ([], [], [], None),
        (
            [_run('test'), _run('lint')],
            [_suite('completed', 2)],
            [],
            ('success', (), ()),
        ),
        (
            [_run('test', 'failure'), _run('lint', None)],
            [_suite('in_progress', 2)],
            [],
            ('failure', ('test',), ('lint',)),
        ),
        (
            [_run('test', 'cancelled'), _run('lint')],
            [_suite('completed', 2)],
            [],
            ('cancelled', ('test',), ()),
        ),
        ([_run('test')], [_suite('in_progress', 1)], [], None),  # jobs to come
        ([_run('test')], [_suite('queued', 0, age=timedelta(seconds=10))], [], None),
        (
            [_run('test')],
            [_suite('queued', 0, age=timedelta(hours=1))],
            [],
            ('success', (), ()),
        ),
        (
            [],
            [],
            [_api.Status(context='ci/legacy', state='error')],
            ('failure', ('ci/legacy',), ()),
        ),
        ([_run('test')], [], [_api.Status(context='ci/legacy', state='pending')], None),
    ],
)
def test_conclude(
    runs: list[_api.CheckRun],
    suites: list[_api.CheckSuite],
    statuses: list[_api.Status],
    verdict: tuple[str, tuple[str, ...], tuple[str, ...]] | None,
) -> None:
    result = _watch._conclude(runs, suites, statuses, now=at(100))

    assert (
        (result.conclusion, result.failed, result.pending)
        if result is not None
        else None
    ) == verdict


async def _poll_once(
    fake: _fake_github.FakeGitHub,
    state: github.GitHubState | None,
) -> list[github.GitHubEvent]:
    """The events of the source's first poll that finds any."""
    async with fake.create_client() as client:
        source = github.watch(
            client,
            _REPO,
            _PULL,
            state=state,
            poll_interval=_NO_WAIT,
        )
        async with contextlib.aclosing(source):
            await anext(source)
            update = await anext(source)
    return list(update.events)


class Test_watch:
    async def test__starts_from_now_by_githubs_clock(self) -> None:
        fake = _fake_github.FakeGitHub(now=at(5))
        fake.comment(4)

        async with fake.create_client() as client:
            source = github.watch(client, _REPO, _PULL, state=None)
            async with contextlib.aclosing(source):
                checkpoint = await anext(source)

        assert not checkpoint.events
        assert checkpoint.state == github.GitHubState(after=at(5), head=_HEAD)

    async def test__reports_comments_and_reviews(self) -> None:
        fake = _fake_github.FakeGitHub()
        fake.comment(1, body='before')
        fake.comment(3, body='please look')
        review = fake.start_review()
        fake.add_review_comment(review, 'typo here')
        fake.add_review_comment(review, 'and here')
        fake.submit(review, 4, state='CHANGES_REQUESTED', body='a few things')
        fake.start_review()  # pending, so not yet visible to others

        events = await _poll_once(fake, github.GitHubState(after=at(2)))

        comment, submitted = events
        assert isinstance(comment, github.CommentPosted)
        assert comment.body == 'please look'
        assert isinstance(submitted, github.ReviewSubmitted)
        assert submitted.state == 'CHANGES_REQUESTED'
        assert submitted.body == 'a few things'
        assert [(comment.body, comment.line) for comment in submitted.comments] == [
            ('typo here', 1),
            ('and here', 1),
        ]

    async def test__places_a_review_by_when_it_was_submitted(self) -> None:
        fake = _fake_github.FakeGitHub()
        review = fake.start_review()  # takes the older ID
        comment = fake.comment(3, body='meanwhile')
        state = github.GitHubState(
            after=at(3),
            seen={f'comment-{comment.id}': at(3)},
        )
        fake.submit(review, 5, body='done')

        events = await _poll_once(fake, state)

        assert _bodies(events) == ['done']

    async def test__reports_an_event_listed_late(self) -> None:
        fake = _fake_github.FakeGitHub(now=at(5))

        def post_between_listings(request: httpx.Request) -> None:
            if request.url.path.endswith('/comments') and not fake.comments:
                fake.comment(4, body='after the comments were listed')
                fake.review(5, body='before the reviews were listed')

        fake.after_each = post_between_listings

        async with fake.create_client() as client:
            source = github.watch(
                client,
                _REPO,
                _PULL,
                state=github.GitHubState(after=at(2)),
                poll_interval=_NO_WAIT,
            )
            async with contextlib.aclosing(source):
                await anext(source)
                first = await anext(source)
                second = await anext(source)

        assert _bodies(first.events) == [
            'before the reviews were listed',
        ]
        assert _bodies(second.events) == [
            'after the comments were listed',
        ]

    async def test__reports_nothing_twice(self) -> None:
        fake = _fake_github.FakeGitHub()
        fake.comment(3, body='first')

        async with fake.create_client() as client:
            source = github.watch(
                client,
                _REPO,
                _PULL,
                state=github.GitHubState(after=at(2)),
                poll_interval=_NO_WAIT,
            )
            async with contextlib.aclosing(source):
                await anext(source)
                first = await anext(source)
                fake.comment(3, body='same second')
                second = await anext(source)

        assert _bodies(first.events) == ['first']
        assert _bodies(second.events) == ['same second']

    async def test__reports_a_push(self) -> None:
        fake = _fake_github.FakeGitHub(now=at(3))
        fake.push(_NEW_HEAD, user='octocat', message='Fix the typo')

        events = await _poll_once(fake, github.GitHubState(after=at(2), head=_HEAD))

        assert events == [
            github.HeadChanged(
                id=f'head-1-{_NEW_HEAD}',
                time=at(3),
                repo=_REPO,
                pull=_PULL,
                user='octocat',
                before=_HEAD,
                after=_NEW_HEAD,
                message='Fix the typo',
                url=f'https://github.com/{_REPO}/compare/{_HEAD}...{_NEW_HEAD}',
            ),
        ]

    async def test__numbers_pushes_that_return_to_a_head(self) -> None:
        fake = _fake_github.FakeGitHub()
        ids: list[str] = []

        async with fake.create_client() as client:
            source = github.watch(
                client,
                _REPO,
                _PULL,
                state=github.GitHubState(after=at(2), head=_HEAD),
                poll_interval=_NO_WAIT,
            )
            async with contextlib.aclosing(source):
                await anext(source)
                for head in [_NEW_HEAD, _HEAD, _NEW_HEAD]:
                    fake.push(head)
                    ids.extend(event.id for event in (await anext(source)).events)

        assert ids == [f'head-1-{_NEW_HEAD}', f'head-2-{_HEAD}', f'head-3-{_NEW_HEAD}']

    async def test__an_idle_poll_costs_only_304s(self) -> None:
        fake = _fake_github.FakeGitHub()
        fake.review(3)
        clock_reads = 0

        def comment_on_the_third_poll(request: httpx.Request) -> None:
            nonlocal clock_reads
            if request.url.path == '/rate_limit':
                clock_reads += 1
                if clock_reads == 4:  # the start, then the third poll
                    fake.comment(4)

        fake.after_each = comment_on_the_third_poll
        async with fake.create_client() as client:
            source = github.watch(
                client,
                _REPO,
                _PULL,
                state=github.GitHubState(after=at(2), head=_HEAD),
                poll_interval=_NO_WAIT,
            )
            async with contextlib.aclosing(source):
                await anext(source)
                await anext(source)  # the review, on the first poll
                await anext(source)  # the comment, on the third

        clock_indexes = [
            index
            for index, request in enumerate(fake.requests)
            if request.url.path == '/rate_limit'
        ]
        # The head, comments, reviews, issue events, check runs, check suites
        # and statuses.
        assert fake.statuses[clock_indexes[2] + 1 : clock_indexes[3]] == [304] * 7

    async def test__records_a_missing_head_as_it_starts(self) -> None:
        fake = _fake_github.FakeGitHub()
        fake.push(_NEW_HEAD)

        async with fake.create_client() as client:
            source = github.watch(
                client,
                _REPO,
                _PULL,
                state=github.GitHubState(after=at(2)),
            )
            async with contextlib.aclosing(source):
                checkpoint = await anext(source)

        assert checkpoint.state.head == _NEW_HEAD

    async def test__reports_state_changes(self) -> None:
        fake = _fake_github.FakeGitHub()
        fake.act(3, 'labeled')
        fake.act(4, 'merged')
        fake.act(4, 'closed')  # comes with the merge
        fake.act(5, 'reopened', user='octocat')
        fake.act(6, 'convert_to_draft')
        fake.act(7, 'ready_for_review')
        fake.act(8, 'closed')

        events = await _poll_once(fake, github.GitHubState(after=at(2), head=_HEAD))

        assert [
            (event.state, event.user)
            for event in events
            if isinstance(event, github.StateChanged)
        ] == [
            ('merged', 'maintainer'),
            ('reopened', 'octocat'),
            ('converted_to_draft', 'maintainer'),
            ('ready_for_review', 'maintainer'),
            ('closed', 'maintainer'),
        ]

    async def test__records_the_conclusion_as_it_starts(self) -> None:
        fake = _fake_github.FakeGitHub()
        fake.check('test')

        async with fake.create_client() as client:
            source = github.watch(client, _REPO, _PULL, state=None)
            async with contextlib.aclosing(source):
                checkpoint = await anext(source)

        assert checkpoint.state.checks == github.CheckResult(
            head=_HEAD,
            conclusion='success',
        )

    async def test__reports_a_conclusion_per_push_and_rerun(self) -> None:
        fake = _fake_github.FakeGitHub(now=at(5))
        fake.check('test', None)
        conclusions: list[tuple[str, str, tuple[str, ...]]] = []

        async with fake.create_client() as client:
            source = github.watch(
                client,
                _REPO,
                _PULL,
                state=github.GitHubState(after=at(2), head=_HEAD),
                poll_interval=_NO_WAIT,
            )
            async with contextlib.aclosing(source):
                await anext(source)
                for head, conclusion in [
                    (_HEAD, 'failure'),
                    (_HEAD, 'success'),  # re-run
                    (_NEW_HEAD, 'success'),  # pushed
                ]:
                    if head != fake.head:
                        fake.push(head)
                    fake.check('test', conclusion)
                    conclusions.extend(
                        (event.head, event.conclusion, event.failed)
                        for event in (await anext(source)).events
                        if isinstance(event, github.ChecksConcluded)
                    )

        assert conclusions == [
            (_HEAD, 'failure', ('test',)),
            (_HEAD, 'success', ()),
            (_NEW_HEAD, 'success', ()),
        ]
