"""The GitHub source: one pull request's comments and reviews."""

import asyncio
import pydantic
import re
from .. import _source
from .. import _utils
from . import _api
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
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


def _reject_number(value: object) -> object:
    """Keeps a number, which pydantic would read as a Unix time, from passing
    for a time."""
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and re.fullmatch(r'[\d.]+', value)
    ):
        raise ValueError(f'not a time such as 2026-08-01T16:00:00Z: {value}')
    return value


type Time = Annotated[
    pydantic.AwareDatetime,
    pydantic.BeforeValidator(_reject_number),
    pydantic.AfterValidator(lambda time: time.astimezone(timezone.utc)),
    pydantic.PlainSerializer(_utils.datetime.to_rfc3339, when_used='json'),
]
"""A time by GitHub's clock, in UTC, written in RFC 3339."""


type Conclusion = Literal['success', 'failure', 'cancelled']


class CheckResult(pydantic.BaseModel):
    """A head commit's checks conclusion, as last seen."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    head: str

    conclusion: Conclusion | None
    """``None`` while the checks have no conclusion yet."""


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

    head: str | None = None
    """The pull request's head commit when last polled. ``None`` until the
    source records it as it starts, which reports no head change."""

    head_changes: int = 0
    """How many head changes the source has reported, which numbers each one
    so that no ID repeats, even when the head returns to an earlier commit."""

    checks: CheckResult | None = None
    """The checks' conclusion when last polled, or ``None`` until the source
    records it as it starts, which reports nothing."""

    checks_reports: int = 0
    """How many checks conclusions the source has reported, which numbers each
    one so that no ID repeats."""

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


class HeadChanged(_source.Event):
    """The pull request's head moved to another commit: commits were pushed,
    or force-pushed."""

    type: Literal['github.head_changed'] = 'github.head_changed'

    repo: Repo

    pull: int

    user: str | None
    """The login of the new head commit's committer: whoever made it, or
    rebased or amended it, and usually whoever pushed it. ``web-flow`` for a
    commit GitHub made; ``None`` when GitHub can't match the commit to an
    account."""

    before: str
    """The head commit's SHA before."""

    after: str
    """The head commit's SHA now."""

    message: str
    """The new head commit's message."""

    url: str
    """The changes between the two on github.com."""


type StateName = Literal[
    'merged',
    'closed',
    'reopened',
    'ready_for_review',
    'converted_to_draft',
]

_STATE_NAMES: dict[str, StateName] = {
    'merged': 'merged',
    'closed': 'closed',
    'reopened': 'reopened',
    'ready_for_review': 'ready_for_review',
    'convert_to_draft': 'converted_to_draft',
}
"""The issue events that change a pull request's state, and what each is
reported as."""


class StateChanged(_source.Event):
    """The pull request was merged, closed, reopened, marked ready for review,
    or converted to a draft. A merge also closes it, and that ``closed`` is
    left out."""

    type: Literal['github.state_changed'] = 'github.state_changed'

    repo: Repo

    pull: int

    user: str | None
    """Who did it. ``None`` for a deleted account."""

    state: StateName

    url: str
    """The pull request on github.com."""


class ChecksConcluded(_source.Event):
    """The head commit's checks reached a conclusion other than the last one
    seen for that head: one failed, or all finished. A re-run that turns a
    failure into a success is reported too."""

    type: Literal['github.checks_concluded'] = 'github.checks_concluded'

    repo: Repo

    pull: int

    head: str
    """The SHA of the head commit the checks ran on."""

    conclusion: Conclusion
    """``failure`` as soon as one check fails; otherwise, once all have
    finished, ``cancelled`` if one was cancelled, else ``success``."""

    failed: tuple[str, ...]
    """The names of the checks and statuses that failed or were cancelled."""

    pending: tuple[str, ...]
    """The names of the checks and statuses still running, when a failure is
    reported before they finish."""

    url: str
    """The pull request's checks on github.com."""


type GitHubEvent = (
    CommentPosted | ReviewSubmitted | HeadChanged | StateChanged | ChecksConcluded
)

_FAILED_CONCLUSIONS = frozenset({'failure', 'timed_out', 'action_required'})

_FAILED_SUITE_CONCLUSIONS = frozenset({'failure', 'timed_out', 'startup_failure'})
"""The conclusions of a check suite that failed before running any checks, such
as a workflow whose file is broken."""

_FAILED_STATES = frozenset({'failure', 'error'})

_EMPTY_SUITE_GRACE = timedelta(minutes=1)
"""How long a queued check suite with no checks may still be about to start
some. An app can create a suite that never runs any, and it stays queued for
good."""


@dataclass(frozen=True)
class _Verdict:
    conclusion: Conclusion
    failed: tuple[str, ...]
    pending: tuple[str, ...]


def _is_running(suite: _api.CheckSuite, now: datetime) -> bool:
    """Whether a check suite may yet add checks or results: any suite not
    completed, unless it has sat queued with no checks past its grace, and a
    completed one with no checks awaiting approval to run."""
    if suite.status == 'completed':
        running = (
            suite.conclusion == 'action_required' and suite.latest_check_runs_count == 0
        )
    elif suite.status == 'queued' and suite.latest_check_runs_count == 0:
        running = now - suite.created_at < _EMPTY_SUITE_GRACE
    else:
        running = True
    return running


def _conclude(
    runs: Sequence[_api.CheckRun],
    suites: Sequence[_api.CheckSuite],
    statuses: Sequence[_api.Status],
    *,
    now: datetime,
) -> _Verdict | None:
    """The checks' conclusion, or ``None`` while it isn't known yet: while
    they are still running without a failure, or when there are none.

    Only each check's newest run counts, as a workflow can run again for the
    same commit. A check suite still running means more checks may come, such
    as jobs that wait for others, so the checks haven't finished even when
    every check created so far has.
    """
    latest = {run.name: run for run in sorted(runs, key=lambda run: run.id)}
    failures = (
        {
            run.name
            for run in latest.values()
            if run.status == 'completed' and run.conclusion in _FAILED_CONCLUSIONS
        }
        | {status.context for status in statuses if status.state in _FAILED_STATES}
        | {
            suite.app.name if suite.app is not None else 'check suite'
            for suite in suites
            if suite.latest_check_runs_count == 0
            and suite.conclusion in _FAILED_SUITE_CONCLUSIONS
        }
    )
    cancellations = {
        run.name for run in latest.values() if run.conclusion == 'cancelled'
    }
    pending = tuple(
        sorted(
            {run.name for run in latest.values() if run.status != 'completed'}
            | {status.context for status in statuses if status.state == 'pending'},
        ),
    )
    running = bool(pending) or any(_is_running(suite, now) for suite in suites)
    failed = tuple(sorted(failures | cancellations))
    verdict: _Verdict | None
    if failures:
        verdict = _Verdict('failure', failed, pending)
    elif running or not (runs or statuses):
        verdict = None
    elif cancellations:
        verdict = _Verdict('cancelled', failed, ())
    else:
        verdict = _Verdict('success', (), ())
    return verdict


def _apply_poll(
    state: GitHubState,
    events: Sequence[GitHubEvent],
    *,
    settled_before: datetime,
    head: str,
    checks: CheckResult | None = None,
    found: Sequence[HeadChanged | ChecksConcluded] = (),
) -> _source.Update[GitHubState, GitHubEvent]:
    """Folds one poll's events into the state: those not yet reported, and
    ``found``, oldest first, and the state after them.

    ``found`` are events found by comparing with ``state`` rather than placed
    by time, such as a change of head, and are always reported.

    The state moves on to the newest event reported, but no further than
    ``settled_before``, before which every event is visible. It remembers
    every event reported from there on, ``head``, and ``checks`` unless it's
    ``None``.
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
        events=tuple(
            sorted(
                [*new_events, *found],
                key=lambda event: (event.time, event.id),
            ),
        ),
        state=GitHubState(
            after=after,
            seen={id_: time for id_, time in reported.items() if time >= after},
            head=head,
            head_changes=state.head_changes
            + sum(isinstance(event, HeadChanged) for event in found),
            checks=checks if checks is not None else state.checks,
            checks_reports=state.checks_reports
            + sum(isinstance(event, ChecksConcluded) for event in found),
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


def _to_state_changes(
    issue_events: Sequence[_api.IssueEvent],
    repo: str,
    pull: int,
    url: str,
) -> list[StateChanged]:
    """The issue events that change the pull request's state, without the
    ``closed`` that comes with a merge."""
    merged_at = {event.created_at for event in issue_events if event.event == 'merged'}
    return [
        StateChanged(
            id=f'event-{event.id}',
            time=event.created_at,
            repo=repo,
            pull=pull,
            user=event.actor.login if event.actor is not None else None,
            state=_STATE_NAMES[event.event],
            url=url,
        )
        for event in issue_events
        if event.event in _STATE_NAMES
        and not (event.event == 'closed' and event.created_at in merged_at)
    ]


async def _fetch_head_change(
    client: _api.GitHubClient,
    repo: str,
    pull: int,
    *,
    number: int,
    before: str,
    after: str,
    time: datetime,
) -> HeadChanged:
    """The ``number``\\ th head change. A push has no time of its own, so it
    takes ``time``, when the poll saw it."""
    commit = await client.fetch_commit(repo, after)
    return HeadChanged(
        id=f'head-{number}-{after}',
        time=time,
        repo=repo,
        pull=pull,
        user=commit.committer.login if commit.committer is not None else None,
        before=before,
        after=after,
        message=commit.message,
        url=f'https://github.com/{repo}/compare/{before}...{after}',
    )


async def _fetch_events(
    client: _api.GitHubClient,
    repo: str,
    pull: int,
    state: GitHubState,
    url: str,
) -> list[GitHubEvent]:
    """The comments, submitted reviews and state changes at or after
    ``state.after``. The diff comments are listed only when a review there is
    new.

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
    state_changes = _to_state_changes(
        await client.fetch_issue_events(repo, pull),
        repo,
        pull,
        url,
    )
    return [
        *(_comment_to_event(comment, repo, pull) for comment in comments),
        *state_changes,
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


async def _fetch_checks(
    client: _api.GitHubClient,
    repo: str,
    head: str,
    now: datetime,
) -> tuple[_Verdict | None, CheckResult]:
    """The head's checks verdict, and the result to record for it."""
    verdict = _conclude(
        await client.fetch_check_runs(repo, head),
        await client.fetch_check_suites(repo, head),
        await client.fetch_statuses(repo, head),
        now=now,
    )
    return verdict, CheckResult(
        head=head,
        conclusion=verdict.conclusion if verdict is not None else None,
    )


async def watch(
    client: _api.GitHubClient,
    repo: str,
    pull: int,
    *,
    state: GitHubState | None,
    poll_interval: timedelta = DEFAULT_POLL_INTERVAL,
) -> _source.Source[GitHubState, GitHubEvent]:
    """Watches a pull request's comments, reviews, head, state and checks,
    forever.

    Starts from ``state``, or when it's ``None`` from now by GitHub's clock,
    taking the pull request's current head and checks conclusion when the
    state has none, and first yields that starting state as a checkpoint.
    Then it polls every ``poll_interval`` and yields an update for each poll
    that finds new events.
    """
    current = (
        state if state is not None else GitHubState(after=await client.fetch_time())
    )
    head = (
        current.head
        if current.head is not None
        else await client.fetch_head(repo, pull)
    )
    if current.checks is None:
        _, checks = await _fetch_checks(client, repo, head, await client.fetch_time())
        current = current.model_copy(update={'checks': checks})
    current = current.model_copy(update={'head': head})
    yield _source.Update(events=(), state=current)

    url = f'https://github.com/{repo}/pull/{pull}'
    while True:
        started_at = await client.fetch_time()
        head = await client.fetch_head(repo, pull)
        events = await _fetch_events(client, repo, pull, current, url)
        head_changed = (
            await _fetch_head_change(
                client,
                repo,
                pull,
                number=current.head_changes + 1,
                before=current.head,
                after=head,
                time=started_at,
            )
            if current.head is not None and current.head != head
            else None
        )
        verdict, checks = await _fetch_checks(client, repo, head, started_at)
        checks_concluded = (
            ChecksConcluded(
                id=(
                    f'checks-{current.checks_reports + 1}-{head}'
                    f'-{verdict.conclusion}'
                ),
                time=started_at,
                repo=repo,
                pull=pull,
                head=head,
                conclusion=verdict.conclusion,
                failed=verdict.failed,
                pending=verdict.pending,
                url=f'{url}/checks',
            )
            if verdict is not None and checks != current.checks
            else None
        )
        update = _apply_poll(
            current,
            events,
            settled_before=started_at - _SETTLE_TIME,
            head=head,
            checks=checks,
            found=[
                event for event in (head_changed, checks_concluded) if event is not None
            ],
        )
        if update.events:
            yield update
        current = update.state

        await asyncio.sleep(poll_interval.total_seconds())
