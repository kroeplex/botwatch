"""The config: the sources to watch, and the run's two clocks."""

import json
import pydantic
import re
import yaml
from . import _exclude
from . import _source
from . import _utils
from . import github
from . import slack
from collections.abc import Collection
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Annotated
from typing import Literal
from typing import NewType
from typing import Self
from typing import assert_never

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
    if duration % timedelta(milliseconds=1):
        raise ValueError(f'not a whole number of milliseconds: {value}')
    return duration


def format_duration(duration: timedelta) -> str:
    """Writes a duration in the largest unit that keeps it whole, so it reads
    back unchanged."""
    milliseconds = duration // timedelta(milliseconds=1)
    unit, size = next(
        (unit, size)
        for unit, size in [('h', 3_600_000), ('m', 60_000), ('s', 1000), ('ms', 1)]
        if milliseconds % size == 0
    )
    return f'{milliseconds // size}{unit}' if milliseconds else '0s'


type Duration = Annotated[
    timedelta,
    pydantic.BeforeValidator(_parse_duration),
    pydantic.PlainSerializer(format_duration, when_used='json'),
]
"""A timedelta, written in a config as ``500ms``, ``5s``, ``55m`` or ``1h``."""


type SourceState = Annotated[
    slack.SlackState | github.GitHubState,
    pydantic.Field(discriminator='type'),
]
"""Any source's state."""


@dataclass(frozen=True)
class Clients:
    """One API client per source type, shared by every source of that type."""

    slack: slack.SlackClient

    github: github.GitHubClient


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
        clients: Clients,
        *,
        state: SourceState | None,
    ) -> _source.Source[slack.SlackState, slack.MessagePosted]:
        """Its source, from ``state``; a state of another type, which its key
        rules out, counts as none."""
        return _exclude.exclude_events(
            slack.watch(
                clients.slack,
                self.conversation,
                state=state if isinstance(state, slack.SlackState) else None,
                thread_ts=self.thread_ts,
                poll_interval=self.poll_interval,
            ),
            lambda message: any(rule.matches(message) for rule in self.exclude),
        )


class GitHubSourceConfig(pydantic.BaseModel):
    """Watches a pull request's conversation comments and reviews."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    type: Literal['github']

    repo: github.Repo

    pull: int = pydantic.Field(gt=0)
    """The pull request's number."""

    poll_interval: Duration = pydantic.Field(
        default=github.DEFAULT_POLL_INTERVAL,
        gt=timedelta(0),
    )

    exclude: tuple[github.ExcludeRule, ...] = ()
    """Comments and reviews to skip: those matching any of these rules, such as
    the caller's own. Skipped ones still advance the source."""

    @property
    def key(self) -> SourceKey:
        return SourceKey(f'github:{self.repo}#{self.pull}')

    def watch(
        self,
        clients: Clients,
        *,
        state: SourceState | None,
    ) -> _source.Source[github.GitHubState, github.GitHubEvent]:
        """Its source, from ``state``; a state of another type, which its key
        rules out, counts as none."""
        return _exclude.exclude_events(
            github.watch(
                clients.github,
                self.repo,
                self.pull,
                state=state if isinstance(state, github.GitHubState) else None,
                poll_interval=self.poll_interval,
            ),
            lambda event: any(rule.matches(event) for rule in self.exclude),
        )


type SourceConfig = Annotated[
    SlackSourceConfig | GitHubSourceConfig,
    pydantic.Field(discriminator='type'),
]


class TypeDefaults[RuleT: pydantic.BaseModel](pydantic.BaseModel):
    """Settings for every source of one type."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    poll_interval: Duration | None = pydantic.Field(default=None, gt=timedelta(0))
    """The poll interval of each source that doesn't set its own."""

    exclude: tuple[RuleT, ...] = ()
    """Rules every source applies ahead of its own, such as one skipping the
    caller's own posts."""


class SlackDefaults(TypeDefaults[slack.ExcludeRule]):
    """Settings for every Slack source."""


class GitHubDefaults(TypeDefaults[github.ExcludeRule]):
    """Settings for every GitHub source."""


class Defaults(pydantic.BaseModel):
    """Settings for every source of each type."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    slack: SlackDefaults = SlackDefaults()

    github: GitHubDefaults = GitHubDefaults()


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

    defaults: Defaults = Defaults()

    sources: tuple[SourceConfig, ...] = ()

    @pydantic.field_validator('sources')
    @classmethod
    def _check_unique_keys(
        cls,
        sources: Sequence[SourceConfig],
    ) -> Sequence[SourceConfig]:
        keys = [source.key for source in sources]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            listed = ', '.join(duplicates)
            raise ValueError(f'sources watched twice: {listed}')
        return sources

    def resolve(self, source: SourceConfig) -> SourceConfig:
        """``source``, as configured, as it runs: with its type's defaults
        applied, their exclude rules ahead of its own and their poll interval
        unless it sets one."""
        defaults: SlackDefaults | GitHubDefaults
        match source:
            case SlackSourceConfig():
                defaults = self.defaults.slack
            case GitHubSourceConfig():
                defaults = self.defaults.github
            case _:
                assert_never(source)
        if 'poll_interval' in source.model_fields_set or defaults.poll_interval is None:
            poll_interval = source.poll_interval
        else:
            poll_interval = defaults.poll_interval
        return source.model_copy(
            update={
                'poll_interval': poll_interval,
                'exclude': (*defaults.exclude, *source.exclude),
            },
        )

    def with_defaults(self, defaults: Defaults) -> Self:
        return self.model_copy(update={'defaults': defaults})

    def find(self, key: SourceKey) -> SourceConfig | None:
        return next((source for source in self.sources if source.key == key), None)

    def with_source(self, source: SourceConfig) -> Self:
        """This config with ``source`` added, or replacing the one with its key
        in place."""
        sources = [
            source if existing.key == source.key else existing
            for existing in self.sources
        ]
        if self.find(source.key) is None:
            sources.append(source)
        return self.model_copy(update={'sources': tuple(sources)})

    def without(self, keys: Collection[SourceKey]) -> Self:
        """This config without the sources whose keys are in ``keys``."""
        return self.model_copy(
            update={
                'sources': tuple(
                    source for source in self.sources if source.key not in keys
                ),
            },
        )

    def to_text(self, *, is_json: bool = False) -> str:
        """The config as YAML, or JSON, with just the settings that were
        given."""
        document = self.model_dump(mode='json', exclude_unset=True)
        return (
            json.dumps(document, indent=2) + '\n'
            if is_json
            else yaml.safe_dump(document, sort_keys=False)
        )

    @classmethod
    def load(cls, path: Path) -> Self:
        """Reads a config file: JSON when ``path`` ends in ``.json``, as
        :meth:`save` writes it, and YAML otherwise."""
        text = path.read_text(encoding='utf-8')
        return (
            cls.model_validate_json(text)
            if path.suffix == '.json'
            else cls.model_validate(yaml.safe_load(text))
        )

    def save(self, path: Path) -> None:
        """Writes the config atomically, as JSON when ``path`` ends in ``.json``
        and as YAML otherwise."""
        _utils.files.write_atomically(
            path, self.to_text(is_json=path.suffix == '.json')
        )
