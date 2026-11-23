"""Rules for events a GitHub source should skip."""

import pydantic
import re
from . import _watch
from typing import Self
from typing import assert_never


class ExcludeRule(pydantic.BaseModel):
    """Matches an event when every field set here matches it, such as the
    caller's own, recognised by its login or a text prefix."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    user: str | None = None
    """The login of whoever posted the comment or review, committed the new
    head, or changed the state, in any case. A checks conclusion has no login,
    so it never matches."""

    text: re.Pattern[str] | None = None
    """A regular expression, searched for anywhere in a comment's or review's
    Markdown body, or a new head commit's message. A review with comments but
    no body, such as a lone reply on the diff, matches when every one of its
    comments does. A state change or checks conclusion has no text, so it never
    matches. Anchor it with ``^``; set flags inline, such as ``(?i)``."""

    @pydantic.model_validator(mode='after')
    def _check_some_field_is_set(self) -> Self:
        pattern = self.text.pattern if self.text is not None else None
        if not any((self.user, pattern)):
            raise ValueError('an exclude rule needs at least one field')
        return self

    def _matches_text(self, event: _watch.GitHubEvent) -> bool:
        bodies: list[str]
        match event:
            case _watch.ReviewSubmitted() if not event.body and event.comments:
                bodies = [comment.body for comment in event.comments]
            case _watch.CommentPosted() | _watch.ReviewSubmitted():
                bodies = [event.body]
            case _watch.HeadChanged():
                bodies = [event.message]
            case _watch.StateChanged() | _watch.ChecksConcluded():
                bodies = []
            case _:
                assert_never(event)
        return self.text is None or (
            bool(bodies) and all(self.text.search(body) is not None for body in bodies)
        )

    def _matches_user(self, event: _watch.GitHubEvent) -> bool:
        login = event.user if not isinstance(event, _watch.ChecksConcluded) else None
        return self.user is None or (
            login is not None and login.casefold() == self.user.casefold()
        )

    def matches(self, event: _watch.GitHubEvent) -> bool:
        return self._matches_user(event) and self._matches_text(event)
