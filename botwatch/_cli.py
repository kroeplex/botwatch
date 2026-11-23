"""The ``botwatch`` command."""

import argparse
import asyncio
import io
import logging
import os
import sys
import yaml
from . import _config
from . import _run
from . import _state
from . import slack
from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

_TOKEN_VARIABLE = 'SLACK_TOKEN'

_USAGE_ERROR = 2
"""The exit status for a bad command line, config or environment."""


def _fail(message: str) -> NoReturn:
    print(f'botwatch: {message}', file=sys.stderr)
    raise SystemExit(_USAGE_ERROR)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog='botwatch',
        description=(
            'Block until something happens at a configured source, then write'
            ' the events to stdout as JSON lines.'
        ),
        epilog=f'Slack sources read their token from ${_TOKEN_VARIABLE}.',
    )
    parser.add_argument(
        '--config',
        type=Path,
        required=True,
        help='a YAML (or JSON) file naming the sources to watch',
    )
    parser.add_argument(
        '--state',
        type=Path,
        help=(
            'where to keep how far each source got, between runs; without it,'
            ' every run starts from now'
        ),
    )
    return parser.parse_args(argv)


def _load_config(path: Path) -> _config.Config:
    try:
        config = _config.Config.load(path)
    except (OSError, ValueError, yaml.YAMLError) as error:
        _fail(f'bad config {path}: {error}')
    return config


def _load_state(path: Path | None) -> _state.StateFile:
    if path is not None and not os.access(path.parent, os.W_OK):
        _fail(f'cannot write a state file in {path.parent}')
    try:
        state = _state.StateFile.load(path) if path is not None else _state.StateFile()
    except (OSError, ValueError, yaml.YAMLError) as error:
        _fail(f'bad state file {path}: {error}')
    return state


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


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(
        format='botwatch: %(levelname)s: %(message)s',
        level=logging.WARNING,
    )
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding='utf-8')  # JSON Lines is UTF-8
    args = _parse_args(argv)
    config = _load_config(args.config)
    state = _load_state(args.state)

    token = os.environ.get(_TOKEN_VARIABLE, '')
    if config.sources and not token:
        _fail(f'set {_TOKEN_VARIABLE} to a Slack token')

    new_state = asyncio.run(_watch(config, state, token))
    if args.state is not None:
        _save_state(new_state, args.state)
