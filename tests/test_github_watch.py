# pylint: disable=protected-access

import contextlib
from . import _fake_github
from botwatch import github
from botwatch.github import _watch
from datetime import datetime
from datetime import timedelta

_REPO = _fake_github.REPO

_PULL = _fake_github.PULL

_NO_WAIT = timedelta(0)

at = _fake_github.at


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
        state = github.GitHubState(after=at(1), seen=('comment-1',))
        events = [
            _comment('comment-3', at(3)),
            _comment('comment-1', at(1)),
            _comment('comment-2', at(1)),
        ]

        update = _watch._apply_poll(state, events)

        assert [event.id for event in update.events] == ['comment-2', 'comment-3']
        assert update.state == github.GitHubState(after=at(3), seen=('comment-3',))

    def test__remembers_every_event_in_the_last_second(self) -> None:
        state = github.GitHubState(after=at(1), seen=('comment-1',))
        events = [_comment('comment-1', at(1)), _comment('comment-2', at(1))]

        update = _watch._apply_poll(state, events)

        assert [event.id for event in update.events] == ['comment-2']
        assert update.state == github.GitHubState(
            after=at(1),
            seen=('comment-1', 'comment-2'),
        )

    def test__without_new_events_keeps_the_state(self) -> None:
        state = github.GitHubState(after=at(1), seen=('comment-1',))

        update = _watch._apply_poll(state, [_comment('comment-1', at(1))])

        assert not update.events
        assert update.state == state


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
        assert checkpoint.state == github.GitHubState(after=at(5))

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
        assert [comment.body for comment in submitted.comments] == [
            'typo here',
            'and here',
        ]

    async def test__places_a_review_by_when_it_was_submitted(self) -> None:
        fake = _fake_github.FakeGitHub()
        review = fake.start_review()  # takes the older ID
        comment = fake.comment(3, body='meanwhile')
        state = github.GitHubState(after=at(3), seen=(f'comment-{comment.id}',))
        fake.submit(review, 5, body='done')

        events = await _poll_once(fake, state)

        assert [event.body for event in events] == ['done']

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

        assert [event.body for event in first.events] == ['first']
        assert [event.body for event in second.events] == ['same second']
