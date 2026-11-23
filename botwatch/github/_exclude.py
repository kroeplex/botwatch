"""Rules for comments and reviews a GitHub source should skip."""

import pydantic
import re
from . import _watch
from typing import Self
from typing import assert_never


class ExcludeRule(pydantic.BaseModel):
    """Matches a comment or review when every field set here matches it, such
    as the caller's own, recognised by its author or a text prefix."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    user: str | None = None
    """The author's login, in any case."""

    text: re.Pattern[str] | None = None
    """A regular expression, searched for anywhere in the comment's or review's
    Markdown body. A review with comments but no body, such as a lone reply on
    the diff, matches when every one of its comments does. A head or state
    change has no text, so it never matches. Anchor it with ``^``; set
    flags inline, such as ``(?i)``."""

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
            case _watch.HeadChanged() | _watch.StateChanged():
                bodies = []
            case _:
                assert_never(event)
        return self.text is None or (
            bool(bodies) and all(self.text.search(body) is not None for body in bodies)
        )

    def matches(self, event: _watch.GitHubEvent) -> bool:
        return (
            self.user is None
            or (
                event.user is not None and event.user.casefold() == self.user.casefold()
            )
        ) and self._matches_text(event)
