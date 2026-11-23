"""The GitHub source: one pull request's comments and reviews."""

import asyncio
import pydantic
from .. import _source
from .. import _utils
from . import _api
from collections.abc import Sequence
from datetime import datetime
from datetime import timedelta
from typing import Annotated
from typing import Literal

DEFAULT_POLL_INTERVAL = timedelta(minutes=1)

type Repo = Annotated[
    str,
    pydantic.StringConstraints(pattern=r'^[\w.-]+/[\w.-]+$'),
]
"""A repository's full name, such as ``octo-org/octo-repo``."""

_SINCE_MARGIN = timedelta(seconds=1)
"""Asks for comments from a little before the cursor, as GitHub's ``since``
doesn't say whether it includes its own second."""


class GitHubState(pydantic.BaseModel):
    """How far a GitHub source has got.

    GitHub's times are whole seconds, so ``after`` alone can't tell apart two
    events in the same second; ``seen`` lists those already reported.
    """

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    type: Literal['github'] = 'github'

    after: datetime
    """The source reports only events at or after this time, by GitHub's
    clock."""

    seen: tuple[str, ...] = ()
    """The IDs of the events already reported at exactly ``after``."""

    @pydantic.field_serializer('after', when_used='json')
    def _serialize_after(self, after: datetime) -> str:
        return _utils.datetime.to_rfc3339(after)

    def describe(self) -> str:
        """Where the source is up to, for people."""
        return f'after {self.after:%Y-%m-%d %H:%M:%S} UTC'


class CommentPosted(_source.Event):
    """A comment was posted to a pull request's conversation."""

    type: Literal['github.comment_posted'] = 'github.comment_posted'

    repo: Repo

    pull: int

    user: str | None
    """The author's login. ``None`` for a deleted account."""

    body: str

    url: str
    """The comment on github.com."""


class ReviewComment(pydantic.BaseModel):
    """One comment on the diff, in a review."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    id: int

    path: str
    """The file it's on."""

    line: int | None
    """The line it's on, or ``None`` when the diff has moved on."""

    in_reply_to: int | None
    """The comment it replies to, in the same thread."""

    body: str

    url: str


class ReviewSubmitted(_source.Event):
    """A review was submitted. A lone reply on the diff is a review too, with
    one comment and no body."""

    type: Literal['github.review_submitted'] = 'github.review_submitted'

    repo: Repo

    pull: int

    user: str | None
    """The reviewer's login. ``None`` for a deleted account."""

    state: str
    """``APPROVED``, ``CHANGES_REQUESTED``, ``COMMENTED``, or ``DISMISSED`` for
    one dismissed before it was reported."""

    body: str

    url: str

    comments: tuple[ReviewComment, ...]
    """Its comments on the diff, oldest first."""


type GitHubEvent = CommentPosted | ReviewSubmitted


def _is_new(state: GitHubState, time: datetime, event_id: str) -> bool:
    return time > state.after or (time == state.after and event_id not in state.seen)


def _apply_poll(
    state: GitHubState,
    events: Sequence[GitHubEvent],
) -> _source.Update[GitHubState, GitHubEvent]:
    """Folds one poll's events into the state: those not yet reported, oldest
    first, and the state after them."""
    new_events = sorted(
        (event for event in events if _is_new(state, event.time, event.id)),
        key=lambda event: (event.time, event.id),
    )
    after = new_events[-1].time if new_events else state.after
    seen = (state.seen if after == state.after else ()) + tuple(
        event.id for event in new_events if event.time == after
    )
    return _source.Update(
        events=tuple(new_events),
        state=GitHubState(after=after, seen=seen),
    )


def _comment_to_event(
    comment: _api.IssueComment,
    repo: str,
    pull: int,
) -> CommentPosted:
    return CommentPosted(
        id=f'comment-{comment.id}',
        time=comment.created_at,
        repo=repo,
        pull=pull,
        user=comment.user.login if comment.user is not None else None,
        body=comment.body or '',
        url=comment.html_url,
    )


def _review_to_event(
    review: _api.Review,
    submitted_at: datetime,
    comments: Sequence[_api.ReviewComment],
    repo: str,
    pull: int,
) -> ReviewSubmitted:
    return ReviewSubmitted(
        id=f'review-{review.id}',
        time=submitted_at,
        repo=repo,
        pull=pull,
        user=review.user.login if review.user is not None else None,
        state=review.state,
        body=review.body or '',
        url=review.html_url,
        comments=tuple(
            ReviewComment(
                id=comment.id,
                path=comment.path,
                line=comment.line,
                in_reply_to=comment.in_reply_to_id,
                body=comment.body,
                url=comment.html_url,
            )
            for comment in sorted(comments, key=lambda comment: comment.id)
        ),
    )


async def _fetch_events(
    client: _api.GitHubClient,
    repo: str,
    pull: int,
    state: GitHubState,
) -> list[GitHubEvent]:
    """The comments and submitted reviews at or after ``state.after``, without
    those already reported.

    A review is placed by when it was submitted, not by its ID, which it gets
    while still pending.
    """
    comments = await client.fetch_issue_comments(
        repo,
        pull,
        since=state.after - _SINCE_MARGIN,
    )
    submitted = [
        (review, submitted_at)
        for review in await client.fetch_reviews(repo, pull)
        if (submitted_at := review.submitted_at) is not None
        and _is_new(state, submitted_at, f'review-{review.id}')
    ]
    events: list[GitHubEvent] = [
        _comment_to_event(comment, repo, pull) for comment in comments
    ]
    for review, submitted_at in submitted:
        review_comments = await client.fetch_review_comments(repo, pull, review.id)
        events.append(
            _review_to_event(review, submitted_at, review_comments, repo, pull),
        )
    return events


async def watch(
    client: _api.GitHubClient,
    repo: str,
    pull: int,
    *,
    state: GitHubState | None,
    poll_interval: timedelta = DEFAULT_POLL_INTERVAL,
) -> _source.Source[GitHubState, GitHubEvent]:
    """Watches a pull request's conversation comments and reviews, forever.

    Starts from ``state``, or when it's ``None`` from now by GitHub's clock,
    and first yields that starting state as a checkpoint. Then it polls every
    ``poll_interval`` and yields an update for each poll that finds new events.
    """
    current = (
        state if state is not None else GitHubState(after=await client.fetch_time())
    )
    yield _source.Update(events=(), state=current)

    while True:
        events = await _fetch_events(client, repo, pull, current)
        update = _apply_poll(current, events)
        if update.events:
            yield update
        current = update.state

        await asyncio.sleep(poll_interval.total_seconds())
