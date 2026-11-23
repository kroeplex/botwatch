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

_SETTLE_TIME = timedelta(seconds=10)
"""How long after its time an event may still be becoming visible. GitHub's
timestamps don't follow the order in which events become visible, and a poll
lists them in several calls."""


type Time = Annotated[
    pydantic.AwareDatetime,
    pydantic.PlainSerializer(_utils.datetime.to_rfc3339, when_used='json'),
]
"""A time by GitHub's clock, written in RFC 3339."""


class GitHubState(pydantic.BaseModel):
    """How far a GitHub source has got: a time, and the events from then on
    already reported.

    The time trails the newest event reported by up to :data:`_SETTLE_TIME`,
    so an event that becomes visible late is still listed by the next poll.
    """

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    type: Literal['github'] = 'github'

    after: Time
    """The source reports only events at or after this time, by GitHub's
    clock."""

    seen: dict[str, Time] = pydantic.Field(default_factory=dict)
    """The events at or after ``after`` already reported: each one's ID, and
    its time."""

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


def _apply_poll(
    state: GitHubState,
    events: Sequence[GitHubEvent],
    *,
    settled_before: datetime,
) -> _source.Update[GitHubState, GitHubEvent]:
    """Folds one poll's events into the state: those not yet reported, oldest
    first, and the state after them.

    The state moves on to the newest event reported, but no further than
    ``settled_before``, before which every event is visible. It remembers
    every event reported from there on.
    """
    new_events = sorted(
        (
            event
            for event in events
            if event.time >= state.after and event.id not in state.seen
        ),
        key=lambda event: (event.time, event.id),
    )
    after = (
        max(state.after, min(new_events[-1].time, settled_before))
        if new_events
        else state.after
    )
    reported = {**state.seen, **{event.id: event.time for event in new_events}}
    return _source.Update(
        events=tuple(new_events),
        state=GitHubState(
            after=after,
            seen={id_: time for id_, time in reported.items() if time >= after},
        ),
    )


def _review_event_id(review: _api.Review) -> str:
    return f'review-{review.id}'


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
        id=_review_event_id(review),
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
    """The comments and submitted reviews at or after ``state.after``. The
    diff comments are listed only when a review there is new.

    A review is placed by when it was submitted, not by its ID, which it gets
    while still pending.
    """
    comments = await client.fetch_issue_comments(repo, pull, since=state.after)
    submitted = [
        (review, submitted_at)
        for review in await client.fetch_reviews(repo, pull)
        if (submitted_at := review.submitted_at) is not None
        and submitted_at >= state.after
    ]
    review_comments: dict[int, list[_api.ReviewComment]] = {}
    if any(_review_event_id(review) not in state.seen for review, _ in submitted):
        for comment in await client.fetch_review_comments(repo, pull):
            review_comments.setdefault(comment.pull_request_review_id, []).append(
                comment,
            )
    return [
        *(_comment_to_event(comment, repo, pull) for comment in comments),
        *(
            _review_to_event(
                review,
                submitted_at,
                review_comments.get(review.id, []),
                repo,
                pull,
            )
            for review, submitted_at in submitted
        ),
    ]


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
        started_at = await client.fetch_time()
        events = await _fetch_events(client, repo, pull, current)
        update = _apply_poll(
            current,
            events,
            settled_before=started_at - _SETTLE_TIME,
        )
        if update.events:
            yield update
        current = update.state

        await asyncio.sleep(poll_interval.total_seconds())
