"""The ``botwatch`` command."""

import argparse
import asyncio
import contextlib
import fcntl
import io
import json
import logging
import os
import pydantic
import re
import sys
import yaml
from . import __version__
from . import _config
from . import _run
from . import _state
from . import github
from . import slack
from collections.abc import Iterator
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

_TOKEN_VARIABLES = {'slack': 'SLACK_TOKEN', 'github': 'GITHUB_TOKEN'}
"""The environment variable holding each source type's token."""

_TOKEN_EPILOG = (
    'Slack sources read their token from $SLACK_TOKEN, and GitHub sources from'
    ' $GITHUB_TOKEN.'
)

_PULL_REQUEST_PATTERN = re.compile(
    r'(?:https://github\.com/)?(?P<repo>[\w.-]+/[\w.-]+)(?:#|/pull/)(?P<pull>\d+)'
    r'(?:[/?#].*)?',
)
"""A pull request, as ``OWNER/REPO#NUMBER`` or its URL."""

_SOURCE_CONFIG: pydantic.TypeAdapter[_config.SourceConfig] = pydantic.TypeAdapter(
    _config.SourceConfig,
)

_USAGE_ERROR = 2
"""The exit status for a bad command line, config or environment."""


def _fail(message: str) -> NoReturn:
    print(f'botwatch: {message}', file=sys.stderr)
    raise SystemExit(_USAGE_ERROR)


def _add_config_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        '--config',
        type=Path,
        required=True,
        help='a YAML (or JSON) file naming the sources to watch',
    )


def _add_setting_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        '--poll-interval',
        metavar='DURATION',
        help='how often to poll, e.g. 15s (built in: 30s for slack, 1m for github)',
    )
    parser.add_argument(
        '--exclude',
        action='append',
        default=[],
        metavar='RULE',
        help=(
            'skip matching events: a YAML mapping of user and text (a regex),'
            ' plus bot_id and username for slack, e.g. "{user: U0123BOT}";'
            ' repeatable'
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='botwatch',
        description=(
            'Block until something happens at a configured source, then write'
            ' the events to stdout as JSON lines. `run` is the default command;'
            ' `botwatch COMMAND -h` describes each.'
        ),
        epilog=_TOKEN_EPILOG,
        suggest_on_error=True,
    )
    parser.add_argument(
        '--version',
        action='version',
        version=f'%(prog)s {__version__}',
    )
    commands = parser.add_subparsers(dest='command', required=True)

    run = commands.add_parser(
        'run',
        help='wait for events and write them as JSON lines (the default)',
        description=(
            'Wait until something happens at a configured source, write the'
            ' events to stdout as JSON lines, save the state, and exit. The'
            ' config is read once, as the run starts.'
        ),
        epilog=_TOKEN_EPILOG,
    )
    _add_config_argument(run)
    run.add_argument(
        '--state',
        type=Path,
        help=(
            'where to keep how far each source got, between runs; without it,'
            ' every run starts each source from its start_after, or else from'
            ' now'
        ),
    )

    add = commands.add_parser(
        'add',
        help='watch a source, creating the config if needed',
        description=(
            'Watch a Slack conversation or one of its threads, or a GitHub pull'
            " request's comments and reviews. For a source already watched, the"
            ' options given replace its settings, and the rest are kept;'
            ' `--exclude` replaces the whole list. Settings a source leaves'
            " unset fall back to its type's defaults (see `botwatch defaults`)."
        ),
    )
    _add_config_argument(add)
    add.add_argument('type', choices=['slack', 'github'], help='the kind of source')
    add.add_argument(
        'target',
        help=(
            'for slack, the conversation ID, e.g. C0123456789; for github, the'
            ' pull request as OWNER/REPO#NUMBER or its URL'
        ),
    )
    add.add_argument(
        '--thread',
        metavar='TS',
        help="slack only: a thread's first message `ts`, to watch just that thread",
    )
    add.add_argument(
        '--start-after',
        metavar='POSITION',
        help=(
            'where it starts until it has saved state: the newest `ts` (slack)'
            ' or time such as 2026-08-01T16:00:00Z (github) already read there,'
            ' not that of your own reply (default: the run that first watches'
            ' it)'
        ),
    )
    _add_setting_arguments(add)

    defaults = commands.add_parser(
        'defaults',
        help='set the settings every source of a type falls back on',
        description=(
            'Set the defaults for every source of a type: its poll interval,'
            ' unless a source sets its own, and exclude rules applied ahead of'
            " each source's own. The options given replace those settings, and"
            ' the rest are kept; `--exclude` replaces the whole list.'
        ),
    )
    _add_config_argument(defaults)
    defaults.add_argument(
        'type', choices=['slack', 'github'], help='the kind of source'
    )
    defaults.add_argument(
        '--reset',
        action='store_true',
        help="drop the type's defaults before applying the options given",
    )
    _add_setting_arguments(defaults)

    remove = commands.add_parser(
        'remove',
        help='stop watching sources',
        description=(
            'Stop watching sources, by key. Their state is dropped on the next run.'
        ),
    )
    _add_config_argument(remove)
    remove.add_argument('keys', nargs='+', metavar='KEY', help='e.g. slack:C0123456789')

    show = commands.add_parser(
        'show',
        help='check and list the sources, and where each is up to',
        description=(
            'Validate the config, and the state file if given, and list each'
            ' source with its position.'
        ),
    )
    _add_config_argument(show)
    show.add_argument('--state', type=Path, help='the state file to read')
    show.add_argument('--json', action='store_true', help='print JSON instead')

    return parser


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # `run` takes only options, so a leading option of its own means `run`.
    is_run = bool(arguments) and (
        arguments[0].startswith('-')
        and arguments[0] not in ('-h', '--help', '--version')
    )
    return _build_parser().parse_args(['run', *arguments] if is_run else arguments)


@contextlib.contextmanager
def _lock_config(path: Path) -> Iterator[None]:
    """Holds an exclusive lock on the config's directory, which leaves no file
    behind, so concurrent edits don't lose each other's changes."""
    try:
        descriptor = os.open(path.resolve().parent, os.O_RDONLY)
    except OSError as error:
        _fail(f'cannot lock the config {path}: {error}')
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except OSError as error:
        os.close(descriptor)
        _fail(f'cannot lock the config {path}: {error}')
    try:
        yield
    finally:
        os.close(descriptor)


def _load_config(path: Path) -> _config.Config:
    try:
        config = _config.Config.load(path)
    except (OSError, ValueError, yaml.YAMLError) as error:
        _fail(f'bad config {path}: {error}')
    return config


def _read_state(path: Path) -> _state.StateFile:
    try:
        state = _state.StateFile.load(path)
    except (OSError, ValueError, yaml.YAMLError) as error:
        _fail(f'bad state file {path}: {error}')
    return state


def _save_config(
    config: _config.Config,
    path: Path,
) -> None:
    try:
        config.save(path)
    except OSError as error:
        _fail(f'cannot save the config {path}: {error}')


def _save_state(
    state: _state.StateFile,
    path: Path,
) -> None:
    try:
        state.save(path)
    except OSError as error:
        _fail(f'cannot save the state file {path}: {error}')


async def _watch(
    config: _config.Config,
    state: _state.StateFile,
    tokens: Mapping[str, str],
) -> _state.StateFile:
    async with (
        slack.SlackClient(tokens['slack']) as slack_client,
        github.GitHubClient(tokens['github']) as github_client,
    ):
        new_state = await _run.run_once(
            config,
            state,
            clients=_config.Clients(slack=slack_client, github=github_client),
            out=sys.stdout,
        )
    return new_state


def _read_tokens(config: _config.Config) -> dict[str, str]:
    """Each source type's token, failing if a configured type has none."""
    tokens = {
        kind: os.environ.get(variable, '')
        for kind, variable in _TOKEN_VARIABLES.items()
    }
    missing = sorted(
        {
            _TOKEN_VARIABLES[source.type]
            for source in config.sources
            if not tokens[source.type]
        },
    )
    if missing:
        _fail('set ' + ' and '.join(missing))
    return tokens


def _run_command(args: argparse.Namespace) -> None:
    config = _load_config(args.config)
    if args.state is not None and not os.access(args.state.parent, os.W_OK):
        _fail(f'cannot write a state file in {args.state.parent}')
    state = _read_state(args.state) if args.state is not None else _state.StateFile()

    tokens = _read_tokens(config)
    new_state = asyncio.run(_watch(config, state, tokens))
    if args.state is not None:
        _save_state(new_state, args.state)


def _describe_target(args: argparse.Namespace) -> dict[str, object]:
    """The thing ``add``'s arguments name, as the start of a source config."""
    target: dict[str, object]
    if args.type == 'slack':
        target = {'type': 'slack', 'conversation': args.target}
        if args.thread is not None:
            target['thread_ts'] = args.thread
    elif args.thread is not None:
        _fail('--thread is only for slack sources')
    elif (match := _PULL_REQUEST_PATTERN.fullmatch(args.target)) is None:
        _fail(f'not a pull request such as octo-org/octo-repo#26: {args.target}')
    else:
        target = {'type': 'github', 'repo': match['repo'], 'pull': match['pull']}
    return target


def _written(model: pydantic.BaseModel) -> dict[str, object]:
    """What a config writes for ``model``: the settings that were given, even
    those equal to a default."""
    return model.model_dump(mode='json', exclude_unset=True)


def _parse_settings(args: argparse.Namespace) -> dict[str, object]:
    """The settings ``--poll-interval`` and ``--exclude`` give. Raises
    :class:`yaml.YAMLError` for a rule that isn't YAML."""
    settings: dict[str, object] = {}
    if args.poll_interval is not None:
        settings['poll_interval'] = args.poll_interval
    if args.exclude:
        settings['exclude'] = [yaml.safe_load(rule) for rule in args.exclude]
    return settings


def _parse_source(
    args: argparse.Namespace,
    config: _config.Config,
) -> _config.SourceConfig:
    """The source ``add``'s arguments describe. Settings they don't give keep
    the values of the source already watched, if there is one."""
    try:
        given = {**_describe_target(args), **_parse_settings(args)}
        if args.start_after is not None:
            given['start_after'] = args.start_after
        existing = config.find(_SOURCE_CONFIG.validate_python(given).key)
        kept = (
            existing.model_dump(mode='json', exclude_unset=True)
            if existing is not None
            else {}
        )
        source = _SOURCE_CONFIG.validate_python({**kept, **given})
    except (ValueError, yaml.YAMLError) as error:
        _fail(f'bad source: {error}')
    return source


def _add_command(args: argparse.Namespace) -> None:
    with _lock_config(args.config):
        config = _load_config(args.config) if args.config.exists() else _config.Config()
        source = _parse_source(args, config)
        existing = config.find(source.key)
        if existing is not None and _written(existing) == _written(source):
            status = 'unchanged'
        else:
            status = 'updated' if existing is not None else 'added'
            _save_config(config.with_source(source), args.config)
    print(f'{status} {source.key}')


def _defaults_command(args: argparse.Namespace) -> None:
    with _lock_config(args.config):
        config = _load_config(args.config) if args.config.exists() else _config.Config()
        existing = (
            config.defaults.slack if args.type == 'slack' else config.defaults.github
        )
        try:
            updated = type(existing).model_validate(
                {
                    **({} if args.reset else _written(existing)),
                    **_parse_settings(args),
                },
            )
        except (ValueError, yaml.YAMLError) as error:
            _fail(f'bad defaults: {error}')
        if _written(updated) == _written(existing):
            status = 'unchanged'
        else:
            status = 'updated'
            _save_config(
                config.with_defaults(
                    config.defaults.model_copy(update={args.type: updated}),
                ),
                args.config,
            )
    print(f'{status} defaults for {args.type}')


def _remove_command(args: argparse.Namespace) -> None:
    keys = [_config.SourceKey(key) for key in args.keys]
    with _lock_config(args.config):
        config = _load_config(args.config)
        removed = [key for key in keys if config.find(key) is not None]
        if removed:
            _save_config(config.without(removed), args.config)
    for key in keys:
        if key in removed:
            print(f'removed {key}')
        else:
            print(f'botwatch: not watched: {key}', file=sys.stderr)


def _describe_position(
    state: _config.SourceState | None,
    start: _config.SourceState | None,
) -> str:
    """Where a source is up to, or else where its next run starts it."""
    start_text = start.describe() if start is not None else 'from now'
    return (
        state.describe()
        if state is not None
        else f'starts {start_text} on its next run'
    )


class _ShownSource(pydantic.BaseModel):
    key: _config.SourceKey

    config: _config.SourceConfig
    """Its settings as it runs, with its type's defaults applied."""

    state: _config.SourceState | None
    """``None`` when it has no saved state."""


class _Shown(pydantic.BaseModel):
    """What ``show --json`` prints."""

    quiet_timeout: _config.Duration

    linger: _config.Duration

    defaults: _config.Defaults

    sources: tuple[_ShownSource, ...]

    unconfigured_state: tuple[_config.SourceKey, ...]
    """Keys in the state file that are no longer configured."""


def _print_table(shown: _Shown, state_path: Path | None) -> None:
    quiet_timeout = _config.format_duration(shown.quiet_timeout)
    linger = _config.format_duration(shown.linger)
    print(f'quiet_timeout {quiet_timeout}, linger {linger}')
    type_defaults: list[tuple[str, _config.SlackDefaults | _config.GitHubDefaults]] = [
        ('slack', shown.defaults.slack),
        ('github', shown.defaults.github),
    ]
    for kind, defaults in type_defaults:
        if _written(defaults):
            print(f'defaults for {kind}: {json.dumps(_written(defaults))}')
    width = max((len(source.key) for source in shown.sources), default=0)
    for source in shown.sources:
        interval = _config.format_duration(source.config.poll_interval)
        position = (
            f'  {_describe_position(source.state, source.config.start_state)}'
            if state_path is not None
            else ''
        )
        print(f'  {source.key:<{width}}  every {interval}{position}')
    if not shown.sources:
        print('  no sources')
    if shown.unconfigured_state:
        print(f'In {state_path} but no longer configured, dropped on the next run:')
        for key in shown.unconfigured_state:
            print(f'  {key}')


def _show_command(args: argparse.Namespace) -> None:
    config = _load_config(args.config)
    if args.state is not None and not args.state.exists():
        print(f'botwatch: no state file at {args.state} yet', file=sys.stderr)
    stored = _read_state(args.state).sources if args.state is not None else {}
    keys = {source.key for source in config.sources}
    shown = _Shown(
        quiet_timeout=config.quiet_timeout,
        linger=config.linger,
        defaults=config.defaults,
        sources=tuple(
            _ShownSource(
                key=source.key,
                config=config.resolve(source),
                state=stored.get(source.key),
            )
            for source in config.sources
        ),
        unconfigured_state=tuple(sorted(key for key in stored if key not in keys)),
    )
    if args.json:
        print(shown.model_dump_json(indent=2))
    else:
        _print_table(shown, args.state)


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(
        format='botwatch: %(levelname)s: %(message)s',
        level=logging.WARNING,
    )
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding='utf-8')  # JSON Lines is UTF-8
    args = _parse_args(argv)
    {
        'run': _run_command,
        'add': _add_command,
        'defaults': _defaults_command,
        'remove': _remove_command,
        'show': _show_command,
    }[args.command](args)
