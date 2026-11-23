"""The config: the sources to watch, and the run's two clocks."""

import pydantic
import re
import yaml
from . import _exclude
from . import _source
from . import slack
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Annotated
from typing import Literal
from typing import NewType
from typing import Self

SourceKey = NewType('SourceKey', str)
"""A source's identity across runs, derived from what it watches, such as
``slack:C0123456789`` or ``slack:C0123456789/1785600000.000200``."""

_DURATION_PATTERN = re.compile(r'(\d+(?:\.\d+)?)(ms|s|m|h)')

_DURATION_UNITS = {
    'ms': timedelta(milliseconds=1),
    's': timedelta(seconds=1),
    'm': timedelta(minutes=1),
    'h': timedelta(hours=1),
}


def _parse_duration(value: object) -> timedelta:
    """Parses text such as ``'500ms'``, ``'5s'``, ``'55m'`` or ``'1h'``. A
    timedelta passes through."""
    if isinstance(value, timedelta):
        duration = value
    elif isinstance(value, str) and (match := _DURATION_PATTERN.fullmatch(value)):
        try:
            duration = float(match[1]) * _DURATION_UNITS[match[2]]
        except OverflowError as error:
            raise ValueError(f'duration too long: {value}') from error
    else:
        raise ValueError(f'not a duration such as 5s or 55m: {value!r}')
    return duration


type Duration = Annotated[timedelta, pydantic.BeforeValidator(_parse_duration)]
"""A timedelta, written in a config as ``500ms``, ``5s``, ``55m`` or ``1h``."""


class SlackSourceConfig(pydantic.BaseModel):
    """Watches a Slack conversation's top level, or with ``thread_ts`` one
    thread."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    type: Literal['slack']

    conversation: slack.ConversationId

    thread_ts: slack.SlackTs | None = None
    """The thread's parent ``ts``, to watch that thread."""

    poll_interval: Duration = pydantic.Field(
        default=slack.DEFAULT_POLL_INTERVAL,
        gt=timedelta(0),
    )

    exclude: tuple[slack.ExcludeRule, ...] = ()
    """Messages to skip: those matching any of these rules, such as the
    caller's own posts. Skipped messages still advance the source."""

    @property
    def key(self) -> SourceKey:
        thread = f'/{self.thread_ts}' if self.thread_ts is not None else ''
        return SourceKey(f'slack:{self.conversation}{thread}')

    def watch(
        self,
        client: slack.SlackClient,
        *,
        state: slack.SlackState | None,
    ) -> _source.Source[slack.SlackState, slack.MessagePosted]:
        return _exclude.exclude_events(
            slack.watch(
                client,
                self.conversation,
                state=state,
                thread_ts=self.thread_ts,
                poll_interval=self.poll_interval,
            ),
            lambda message: any(rule.matches(message) for rule in self.exclude),
        )


class Config(pydantic.BaseModel):
    """What one run watches, and how long it waits."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    quiet_timeout: Duration = pydantic.Field(
        default=timedelta(minutes=20),
        gt=timedelta(0),
    )
    """How long a run waits for its first event."""

    linger: Duration = pydantic.Field(
        default=timedelta(seconds=5),
        ge=timedelta(0),
    )
    """How much longer a run collects events after its first."""

    sources: tuple[SlackSourceConfig, ...] = ()

    @pydantic.field_validator('sources')
    @classmethod
    def _check_unique_keys(
        cls,
        sources: Sequence[SlackSourceConfig],
    ) -> Sequence[SlackSourceConfig]:
        keys = [source.key for source in sources]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            listed = ', '.join(duplicates)
            raise ValueError(f'sources watched twice: {listed}')
        return sources

    @classmethod
    def load(cls, path: Path) -> Self:
        """Reads a YAML config file. JSON is YAML, so JSON works too."""
        return cls.model_validate(yaml.safe_load(path.read_text(encoding='utf-8')))
