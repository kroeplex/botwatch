"""Load and validate the run config: which channels to watch, and the timing.

The loader owns three things per channel — ``key``, ``type``, and ``meta`` — and
passes everything else through untouched as the driver's own ``options``, which
the core never interprets. Structurally invalid config is fatal (botwatch cannot
construct a coherent run); a driver rejecting *its own* options is not, and is
handled later where the driver runs.
"""

from __future__ import annotations

import datetime as dt
import pydantic
import re
import yaml
from pathlib import Path
from typing import Annotated

# Caller-chosen, identifier-shaped, and filesystem-safe: `[A-Za-z0-9._-]{1,64}`
# with no leading dot. fs-safety is not needed today (state is one file) but is
# one-directional insurance — a future per-key state *directory* would need it,
# and tightening the charset later would break every config already written.
_KEY_RE = re.compile(r'[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}\Z')

_DURATION_RE = re.compile(r'(?P<n>\d+)(?P<unit>ms|s|m|h|d)\Z')
_DURATION_UNIT = {
    'ms': dt.timedelta(milliseconds=1),
    's': dt.timedelta(seconds=1),
    'm': dt.timedelta(minutes=1),
    'h': dt.timedelta(hours=1),
    'd': dt.timedelta(days=1),
}


class ConfigError(Exception):
    """A config that botwatch cannot construct a coherent run from — fatal."""


def _parse_duration(value: object) -> dt.timedelta:
    """Parse a single-term duration like ``20m`` / ``2s`` / ``500ms``.

    A bare integer is already-seconds only if a caller passed one; the config
    format is the string form, so anything else is rejected here rather than
    guessed at.
    """
    if isinstance(value, dt.timedelta):
        result = value
    elif isinstance(value, str) and (match := _DURATION_RE.fullmatch(value.strip())):
        result = int(match['n']) * _DURATION_UNIT[match['unit']]
    else:
        raise ValueError(
            f'not a duration (expected e.g. "20m", "2s", "500ms"): {value!r}'
        )
    return result


Duration = Annotated[dt.timedelta, pydantic.BeforeValidator(_parse_duration)]


class Defaults(pydantic.BaseModel):
    """Run-level timing (issue 0006's two clocks)."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    quiet_timeout: Duration = dt.timedelta(minutes=20)
    """How long a run waits when nothing at all is happening before it exits."""

    boarding_window: Duration = dt.timedelta(seconds=2)
    """Once the first event arrives, how much longer to keep collecting."""


class ChannelConfig(pydantic.BaseModel):
    """One watched source.

    ``key``/``type``/``meta`` are the core's; ``options`` is everything else the
    caller wrote under the channel, held verbatim for the driver to validate and
    use. The core never looks inside ``options``.
    """

    model_config = pydantic.ConfigDict(frozen=True)

    key: str
    """Caller-chosen, unique, ``[A-Za-z0-9._-]{1,64}`` with no leading dot."""

    type: str
    """Which driver watches this channel, e.g. ``slack``."""

    meta: dict[str, object] = pydantic.Field(default_factory=dict)
    """Opaque caller payload, preserved verbatim and echoed onto every event."""

    options: dict[str, object] = pydantic.Field(default_factory=dict)
    """Everything the caller wrote under the channel but ``key``/``type``/``meta``
    -- the driver's own config, which the core never interprets."""

    @pydantic.field_validator('key')
    @classmethod
    def _key_is_well_formed(cls, value: str) -> str:
        if not _KEY_RE.fullmatch(value):
            raise ValueError(
                f'key must match [A-Za-z0-9._-]{{1,64}} with no leading dot: {value!r}'
            )
        return value

    @pydantic.model_validator(mode='before')
    @classmethod
    def _separate_driver_options(cls, data: object) -> object:
        """Move every key but key/type/meta into ``options``, verbatim.

        Round-trip-safe: an already-separated form (an explicit ``options``
        block, as produced by dumping a ChannelConfig) is preserved, with any
        stray top-level driver fields folded in -- so parsing a dumped config
        back yields the same options rather than an empty dict.
        """
        if isinstance(data, dict):
            reserved = {'key', 'type', 'meta'}
            kept = {k: v for k, v in data.items() if k in reserved}
            options = dict(data.get('options') or {})
            options.update(
                {k: v for k, v in data.items() if k not in reserved and k != 'options'}
            )
            kept['options'] = options
            data = kept
        return data


class Config(pydantic.BaseModel):
    """A whole run's configuration."""

    model_config = pydantic.ConfigDict(extra='forbid', frozen=True)

    version: int = 1
    """Carried but unenforced -- nothing reads it, so a mismatch is undefined. It
    exists so the first real bump need not guess what an absent field meant."""

    defaults: Defaults = pydantic.Field(default_factory=Defaults)
    """Run-level timing, overridable per channel where a driver can honour it."""

    channels: tuple[ChannelConfig, ...] = ()
    """The sources to watch this run."""

    @pydantic.model_validator(mode='after')
    def _keys_are_unique(self) -> Config:
        seen: set[str] = set()
        dupes: set[str] = set()
        for channel in self.channels:
            if channel.key in seen:
                dupes.add(channel.key)
            seen.add(channel.key)
        if dupes:
            raise ValueError(
                f'duplicate channel key(s), which is a hard error: {sorted(dupes)}'
            )
        return self


def load_config(path: Path | str) -> Config:
    """Read and validate a YAML (or JSON — JSON is YAML) config file.

    Any structural problem — unreadable file, non-mapping document, a malformed
    key, a duplicate key, a bad duration — is raised as `ConfigError`, since none
    of them leave a run botwatch can coherently start.
    """
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding='utf-8'))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f'cannot read config {str(path)!r}: {exc}') from exc
    if not isinstance(raw, dict):
        raise ConfigError(
            f'config {str(path)!r} must be a mapping, got {type(raw).__name__}'
        )
    try:
        config = Config.model_validate(raw)
    except pydantic.ValidationError as exc:
        raise ConfigError(f'invalid config {str(path)!r}:\n{exc}') from exc
    return config
