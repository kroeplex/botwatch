"""Rules for comments and reviews a GitHub source should skip."""

import pydantic
import re
from . import _watch
from typing import Self


class ExcludeRule(pydantic.BaseModel):
    """Matches a comment or review when every field set here matches it, such
    as the caller's own, recognised by its author or a text prefix."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    user: str | None = None
    """The author's login."""

    text: re.Pattern[str] | None = None
    """A regular expression, searched for anywhere in the comment's or review's
    Markdown body. Anchor it with ``^``; set flags inline, such as ``(?i)``."""

    @pydantic.model_validator(mode='after')
    def _check_some_field_is_set(self) -> Self:
        pattern = self.text.pattern if self.text is not None else None
        if not any((self.user, pattern)):
            raise ValueError('an exclude rule needs at least one field')
        return self

    def matches(self, event: _watch.GitHubEvent) -> bool:
        return (self.user is None or event.user == self.user) and (
            self.text is None or self.text.search(event.body) is not None
        )
