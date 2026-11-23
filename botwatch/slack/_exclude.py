"""Rules for messages a Slack source should skip."""

import pydantic
import re
from . import _watch
from typing import Self


class ExcludeRule(pydantic.BaseModel):
    """Matches a message when every field set here matches it, such as the
    caller's own posts, recognised by their author or a text prefix."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    user: str | None = None
    """The author's user ID."""

    bot_id: str | None = None
    """The ID of the app integration that posted it."""

    username: str | None = None
    """The display name an app set when posting with ``chat:write.customize``."""

    text: re.Pattern[str] | None = None
    """A regular expression to search the text for."""

    @pydantic.model_validator(mode='after')
    def _check_some_field_is_set(self) -> Self:
        fields = (self.user, self.bot_id, self.username, self.text)
        if all(field is None for field in fields):
            raise ValueError('an exclude rule needs at least one field')
        return self

    def matches(self, message: _watch.MessagePosted) -> bool:
        return (
            (self.user is None or message.user == self.user)
            and (self.bot_id is None or message.bot_id == self.bot_id)
            and (self.username is None or message.username == self.username)
            and (self.text is None or self.text.search(message.text) is not None)
        )
