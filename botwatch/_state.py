"""The state file: how far each source has got, kept between runs."""

import os
import pydantic
import shutil
import tempfile
import yaml
from . import _config
from . import slack
from pathlib import Path
from typing import Self

type SourceState = slack.SlackState
"""Any source's state. Becomes a union discriminated on ``type`` as source types
are added."""


def _write_atomically(
    path: Path,
    text: str,
) -> None:
    """Writes ``text`` to a temporary file beside ``path``, then renames it over
    ``path``, so a reader sees the old file or the new one, never a torn one.

    The temporary file is removed if anything fails, and an existing file's
    permissions carry over.
    """
    with tempfile.NamedTemporaryFile(
        'w',
        encoding='utf-8',
        dir=path.parent,
        prefix=f'.{path.name}.',
        delete_on_close=False,
    ) as temporary:
        temporary.write(text)
        temporary.flush()
        os.fsync(temporary.fileno())
        if path.exists():
            shutil.copymode(path, temporary.name)
        os.replace(temporary.name, path)


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
        _write_atomically(path, self.to_yaml())
