"""The ``botwatch`` command."""

import argparse
import asyncio
import io
import json
import logging
import os
import sys
import yaml
from . import _config
from . import _run
from . import _state
from . import slack
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

_TOKEN_VARIABLE = 'SLACK_TOKEN'

_USAGE_ERROR = 2
"""The exit status for a bad command line, config or environment."""

_COMMANDS = ('run', 'add', 'remove', 'show')


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
        epilog=f'Slack sources read their token from ${_TOKEN_VARIABLE}.',
    )
    commands = parser.add_subparsers(dest='command', required=True)

    run = commands.add_parser(
        'run',
        help='wait for events and write them as JSON lines (the default)',
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
            'Watch a Slack conversation, or one of its threads. A source already'
            ' watched is replaced, keeping its place in the state file.'
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
    is_run = not arguments or (
        arguments[0] not in _COMMANDS and arguments[0] not in ('-h', '--help')
    )
    return _build_parser().parse_args(['run', *arguments] if is_run else arguments)


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


def _parse_source(args: argparse.Namespace) -> _config.SlackSourceConfig:
    """The source ``add``'s arguments describe."""
    try:
        document: dict[str, object] = {
            'type': args.type,
            'conversation': args.conversation,
            'exclude': [yaml.safe_load(rule) for rule in args.exclude],
        }
        if args.thread is not None:
            document['thread_ts'] = args.thread
        if args.poll_interval is not None:
            document['poll_interval'] = args.poll_interval
        source = _config.SlackSourceConfig.model_validate(document)
    except (ValueError, yaml.YAMLError) as error:
        _fail(f'bad source: {error}')
    return source


def _add_command(args: argparse.Namespace) -> None:
    config = _load_config(args.config) if args.config.exists() else _config.Config()
    source = _parse_source(args)
    existing = config.find(source.key)
    if existing == source:
        status = 'unchanged'
    else:
        status = 'updated' if existing is not None else 'added'
        _save_config(config.with_source(source), args.config)
    print(f'{status} {source.key}')


def _remove_command(args: argparse.Namespace) -> None:
    config = _load_config(args.config)
    keys = [_config.SourceKey(key) for key in args.keys]
    for key in keys:
        if config.find(key) is None:
            print(f'botwatch: not watched: {key}', file=sys.stderr)
        else:
            print(f'removed {key}')
    if any(config.find(key) is not None for key in keys):
        _save_config(config.without(keys), args.config)


def _describe_position(state: slack.SlackState | None) -> str:
    return state.describe() if state is not None else 'starts from now on its next run'


def _print_json(
    config: _config.Config,
    stored: Mapping[_config.SourceKey, _state.SourceState],
    unconfigured: Sequence[_config.SourceKey],
) -> None:
    document = config.model_dump(mode='json')
    sources = [
        {
            'key': source.key,
            'config': source_document,
            'state': (
                stored[source.key].model_dump(mode='json')
                if source.key in stored
                else None
            ),
        }
        for source, source_document in zip(
            config.sources,
            document['sources'],
            strict=True,
        )
    ]
    print(
        json.dumps(
            {
                'quiet_timeout': document['quiet_timeout'],
                'linger': document['linger'],
                'sources': sources,
                'unconfigured_state': unconfigured,
            },
            indent=2,
        ),
    )


def _print_table(
    config: _config.Config,
    state: _state.StateFile | None,
    unconfigured: Sequence[_config.SourceKey],
    state_path: Path | None,
) -> None:
    document = config.model_dump(mode='json')
    print(f"quiet_timeout {document['quiet_timeout']}, linger {document['linger']}")
    width = max((len(source.key) for source in config.sources), default=0)
    for source, source_document in zip(
        config.sources,
        document['sources'],
        strict=True,
    ):
        position = (
            f'  {_describe_position(state.sources.get(source.key))}'
            if state is not None
            else ''
        )
        interval = source_document['poll_interval']
        print(f'  {source.key:<{width}}  every {interval}{position}')
    if not config.sources:
        print('  no sources')
    if unconfigured:
        print(f'In {state_path} but no longer configured, dropped on the next run:')
        for key in unconfigured:
            print(f'  {key}')


def _show_command(args: argparse.Namespace) -> None:
    config = _load_config(args.config)
    state = _read_state(args.state) if args.state is not None else None
    stored = state.sources if state is not None else {}
    keys = {source.key for source in config.sources}
    unconfigured = sorted(key for key in stored if key not in keys)
    if args.json:
        _print_json(config, stored, unconfigured)
    else:
        _print_table(config, state, unconfigured, args.state)


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
