"""The state file: how far each source has got, kept between runs."""

import pydantic
import yaml
from . import _config
from . import _utils
from . import slack
from pathlib import Path
from typing import Self

type SourceState = slack.SlackState
"""Any source's state. Becomes a union discriminated on ``type`` as source types
are added."""


class StateFile(pydantic.BaseModel):
    """What a run hands the next: each configured source's latest state."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    sources: dict[_config.SourceKey, SourceState] = pydantic.Field(
        default_factory=dict,
    )

    @classmethod
    def from_yaml(cls, text: str) -> Self:
        return cls.model_validate(yaml.safe_load(text) or {})

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.model_dump(mode='json'), sort_keys=False)

    @classmethod
    def load(cls, path: Path) -> Self:
        """Reads a state file. A missing one is an empty state."""
        try:
            text = path.read_text(encoding='utf-8')
        except FileNotFoundError:
            text = ''
        return cls.from_yaml(text)

    def save(self, path: Path) -> None:
        _utils.files.write_atomically(path, self.to_yaml())
