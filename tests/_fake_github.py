"""An in-memory GitHub pull request, served through :class:`httpx.MockTransport`."""

import email.utils
import hashlib
import httpx
import pydantic
from botwatch import github
from botwatch.github import _api
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from datetime import timezone

REPO = 'octo-org/octo-repo'

PULL = 26

HEAD = 'a' * 40
"""The pull request's head commit until something is pushed."""

_PREFIX = f'/repos/{REPO}'


def at(second: int) -> datetime:
    """A time a few seconds into the pull request's life."""
    return datetime(2026, 8, 1, 16, 0, tzinfo=timezone.utc) + timedelta(
        seconds=second,
    )


@dataclass
class FakeGitHub:
    """One pull request's comments, reviews, head and issue events, answering
    the calls botwatch makes the way GitHub does: lists oldest first, in pages
    of ``per_page``, with ``ETag``\\ s that turn a repeated call into a 304,
    which carries no ``Link`` header."""

    now: datetime = field(default_factory=lambda: at(0))
    """GitHub's clock, as the ``Date`` header gives it."""

    comments: list[_api.IssueComment] = field(default_factory=list)

    reviews: list[_api.Review] = field(default_factory=list)

    review_comments: dict[int, list[_api.ReviewComment]] = field(
        default_factory=dict,
    )
    """Each review's comments, by the review's ID."""

    head: str = HEAD
    """The head commit's SHA."""

    commits: dict[str, _api.Commit] = field(default_factory=dict)
    """Each pushed commit, by SHA."""

    issue_events: list[_api.IssueEvent] = field(default_factory=list)

    requests: list[httpx.Request] = field(default_factory=list)

    statuses: list[int] = field(default_factory=list)
    """The status of each response, in order."""

    after_each: Callable[[httpx.Request], None] | None = None
    """Called after each request is answered, to change things mid-poll."""

    _next_id: int = 1

    def _take_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def comment(
        self,
        second: int,
        *,
        user: str = 'octocat',
        body: str = 'hello',
    ) -> _api.IssueComment:
        comment = _api.IssueComment(
            id=self._take_id(),
            user=_api.User(login=user),
            body=body,
            created_at=at(second),
            html_url=f'https://github.com/{REPO}/pull/{PULL}#issuecomment',
        )
        self.comments.append(comment)
        return comment

    def start_review(self, *, user: str = 'reviewer') -> _api.Review:
        """A pending review, which takes its ID now."""
        review = _api.Review(
            id=self._take_id(),
            user=_api.User(login=user),
            body='',
            state='PENDING',
            html_url=f'https://github.com/{REPO}/pull/{PULL}#pullrequestreview',
        )
        self.reviews.append(review)
        self.review_comments[review.id] = []
        return review

    def add_review_comment(self, review: _api.Review, body: str) -> None:
        self.review_comments[review.id].append(
            _api.ReviewComment(
                id=self._take_id(),
                pull_request_review_id=review.id,
                user=review.user,
                body=body,
                path='README.md',
                line=1,
                html_url=f'https://github.com/{REPO}/pull/{PULL}#discussion',
            ),
        )

    def submit(
        self,
        review: _api.Review,
        second: int,
        *,
        state: str = 'COMMENTED',
        body: str = '',
    ) -> None:
        index = self.reviews.index(review)
        self.reviews[index] = review.model_copy(
            update={'state': state, 'body': body, 'submitted_at': at(second)},
        )

    def review(
        self,
        second: int,
        *,
        user: str = 'reviewer',
        body: str = 'looks good',
        state: str = 'APPROVED',
    ) -> None:
        self.submit(self.start_review(user=user), second, state=state, body=body)

    def push(
        self,
        sha: str,
        *,
        user: str | None = 'octocat',
        message: str = 'Fix it',
    ) -> None:
        """Moves the head to ``sha``, committed by ``user``."""
        self.head = sha
        self.commits[sha] = _api.Commit.model_validate(
            {
                'commit': {'message': message},
                'committer': {'login': user} if user is not None else None,
            },
        )

    def act(self, second: int, event: str, *, user: str = 'maintainer') -> None:
        """Records an issue event, such as ``merged`` or ``labeled``."""
        self.issue_events.append(
            _api.IssueEvent(
                id=self._take_id(),
                event=event,
                actor=_api.User(login=user),
                created_at=at(second),
            ),
        )

    def _serve_head(self, request: httpx.Request) -> httpx.Response:
        """The head's SHA, which is also its ``ETag``, as GitHub serves it."""
        etag = f'"{self.head}"'
        if request.headers.get('If-None-Match') == etag:
            response = httpx.Response(304, headers={'ETag': etag})
        else:
            response = httpx.Response(200, text=self.head, headers={'ETag': etag})
        return response

    def _serve_commit(self, request: httpx.Request) -> httpx.Response:
        commit = self.commits[request.url.params['sha']]
        return httpx.Response(
            200,
            content=pydantic.TypeAdapter(list[_api.Commit]).dump_json([commit]),
        )

    def _list(self, request: httpx.Request) -> list[pydantic.BaseModel] | None:
        path = request.url.path
        items: list[pydantic.BaseModel] | None
        if path == f'{_PREFIX}/issues/{PULL}/comments':
            since = datetime.fromisoformat(request.url.params['since'])
            items = [
                comment for comment in self.comments if comment.created_at >= since
            ]
        elif path == f'{_PREFIX}/pulls/{PULL}/reviews':
            items = list(self.reviews)
        elif path == f'{_PREFIX}/issues/{PULL}/events':
            items = list(self.issue_events)
        elif path == f'{_PREFIX}/pulls/{PULL}/comments':
            submitted = {
                review.id for review in self.reviews if review.submitted_at is not None
            }
            review_comments: list[_api.ReviewComment] = [
                comment
                for review_id, comments in self.review_comments.items()
                if review_id in submitted
                for comment in comments
            ]
            review_comments.sort(key=lambda comment: comment.id)
            items = list(review_comments)
        else:
            items = None
        return items

    def _serve_list(
        self,
        request: httpx.Request,
        items: list[pydantic.BaseModel],
    ) -> httpx.Response:
        page = int(request.url.params.get('page', '1'))
        size = int(request.url.params['per_page'])
        content = pydantic.TypeAdapter(list[pydantic.BaseModel]).dump_json(
            items[(page - 1) * size : page * size],
            serialize_as_any=True,
        )
        headers = {'ETag': f'"{hashlib.sha1(content).hexdigest()}"'}
        if page * size < len(items):
            next_url = request.url.copy_merge_params({'page': str(page + 1)})
            headers['Link'] = f'<{next_url}>; rel="next"'
        return (
            httpx.Response(304, headers={'ETag': headers['ETag']})
            if request.headers.get('If-None-Match') == headers['ETag']
            else httpx.Response(200, content=content, headers=headers)
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        items = self._list(request)
        if request.url.path == f'{_PREFIX}/commits/refs/pull/{PULL}/head':
            response = self._serve_head(request)
        elif request.url.path == f'{_PREFIX}/commits':
            response = self._serve_commit(request)
        elif request.url.path == '/rate_limit':
            response = httpx.Response(
                200,
                json={},
                headers={'Date': email.utils.format_datetime(self.now, usegmt=True)},
            )
        elif items is None:
            response = httpx.Response(404, json={'message': 'Not Found'})
        else:
            response = self._serve_list(request, items)
        self.statuses.append(response.status_code)
        if self.after_each is not None:
            self.after_each(request)
        return response

    def create_client(self) -> github.GitHubClient:
        return github.GitHubClient(
            'ghp-test',
            transport=httpx.MockTransport(self._handle),
        )
