"""The ``botwatch`` command."""

import argparse
import asyncio
import contextlib
import fcntl
import io
import logging
import os
import pydantic
import sys
import yaml
from . import _config
from . import _run
from . import _state
from . import slack
from collections.abc import Iterator
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

_TOKEN_VARIABLE = 'SLACK_TOKEN'

_TOKEN_EPILOG = f'Slack sources read their token from ${_TOKEN_VARIABLE}.'

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
            ' every run starts from now'
        ),
    )

    add = commands.add_parser(
        'add',
        help='watch a source, creating the config if needed',
        description=(
            'Watch a Slack conversation, or one of its threads. For a source'
            ' already watched, the options given replace its settings, and the'
            ' rest are kept; `--exclude` replaces the whole list.'
        ),
    )
    _add_config_argument(add)
    add.add_argument('type', choices=['slack'], help='the kind of source')
    add.add_argument('conversation', help='the conversation ID, e.g. C0123456789')
    add.add_argument(
        '--thread',
        metavar='TS',
        help="a thread's first message `ts`, to watch just that thread",
    )
    add.add_argument(
        '--poll-interval',
        metavar='DURATION',
        help='how often to poll, e.g. 15s (default 30s)',
    )
    add.add_argument(
        '--exclude',
        action='append',
        default=[],
        metavar='RULE',
        help=(
            'skip matching messages: a YAML mapping of user, bot_id, username'
            ' and text (a regex), e.g. "{user: U0123BOT}"; repeatable'
        ),
    )

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
    # `run` takes only options, so a leading option other than help means `run`.
    is_run = bool(arguments) and (
        arguments[0].startswith('-') and arguments[0] not in ('-h', '--help')
    )
    return _build_parser().parse_args(['run', *arguments] if is_run else arguments)


@contextlib.contextmanager
def _lock_config(path: Path) -> Iterator[None]:
    """Holds an exclusive lock on the config, through a file beside it, so
    concurrent edits don't lose each other's changes."""
    target = path.resolve()
    with contextlib.ExitStack() as stack:
        try:
            lock = stack.enter_context(
                target.with_name(f'.{target.name}.lock').open('a', encoding='utf-8'),
            )
        except OSError as error:
            _fail(f'cannot lock the config {path}: {error}')
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


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
    token: str,
) -> _state.StateFile:
    async with slack.SlackClient(token) as client:
        new_state = await _run.run_once(
            config,
            state,
            slack_client=client,
            out=sys.stdout,
        )
    return new_state


def _run_command(args: argparse.Namespace) -> None:
    config = _load_config(args.config)
    if args.state is not None and not os.access(args.state.parent, os.W_OK):
        _fail(f'cannot write a state file in {args.state.parent}')
    state = _read_state(args.state) if args.state is not None else _state.StateFile()

    token = os.environ.get(_TOKEN_VARIABLE, '')
    if config.sources and not token:
        _fail(f'set {_TOKEN_VARIABLE} to a Slack token')

    new_state = asyncio.run(_watch(config, state, token))
    if args.state is not None:
        _save_state(new_state, args.state)


def _parse_source(
    args: argparse.Namespace,
    config: _config.Config,
) -> _config.SlackSourceConfig:
    """The source ``add``'s arguments describe. Settings they don't give keep
    the values of the source already watched, if there is one."""
    given: dict[str, object] = {'type': args.type, 'conversation': args.conversation}
    if args.thread is not None:
        given['thread_ts'] = args.thread
    if args.poll_interval is not None:
        given['poll_interval'] = args.poll_interval
    try:
        if args.exclude:
            given['exclude'] = [yaml.safe_load(rule) for rule in args.exclude]
        existing = config.find(_config.SlackSourceConfig.model_validate(given).key)
        kept = (
            existing.model_dump(mode='json', exclude_unset=True)
            if existing is not None
            else {}
        )
        source = _config.SlackSourceConfig.model_validate({**kept, **given})
    except (ValueError, yaml.YAMLError) as error:
        _fail(f'bad source: {error}')
    return source


def _add_command(args: argparse.Namespace) -> None:
    with _lock_config(args.config):
        config = _load_config(args.config) if args.config.exists() else _config.Config()
        source = _parse_source(args, config)
        existing = config.find(source.key)
        if existing == source:
            status = 'unchanged'
        else:
            status = 'updated' if existing is not None else 'added'
            _save_config(config.with_source(source), args.config)
    print(f'{status} {source.key}')


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


def _describe_position(state: slack.SlackState | None) -> str:
    return state.describe() if state is not None else 'starts from now on its next run'


class _ShownSource(pydantic.BaseModel):
    key: _config.SourceKey

    config: _config.SlackSourceConfig

    state: _state.SourceState | None
    """``None`` when it has no saved state."""


class _Shown(pydantic.BaseModel):
    """What ``show --json`` prints."""

    quiet_timeout: _config.Duration

    linger: _config.Duration

    sources: tuple[_ShownSource, ...]

    unconfigured_state: tuple[_config.SourceKey, ...]
    """Keys in the state file that are no longer configured."""


def _print_table(shown: _Shown, state_path: Path | None) -> None:
    quiet_timeout = _config.format_duration(shown.quiet_timeout)
    linger = _config.format_duration(shown.linger)
    print(f'quiet_timeout {quiet_timeout}, linger {linger}')
    width = max((len(source.key) for source in shown.sources), default=0)
    for source in shown.sources:
        interval = _config.format_duration(source.config.poll_interval)
        position = (
            f'  {_describe_position(source.state)}' if state_path is not None else ''
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
        sources=tuple(
            _ShownSource(key=source.key, config=source, state=stored.get(source.key))
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
        'remove': _remove_command,
        'show': _show_command,
    }[args.command](args)
