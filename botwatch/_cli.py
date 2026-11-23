"""The ``botwatch`` command."""

import argparse
import asyncio
import logging
import os
import pydantic
import sys
import yaml
from . import _config
from . import _run
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
    return parser.parse_args(argv)


def _load_config(path: Path) -> _config.Config:
    try:
        config = _config.Config.load(path)
    except (OSError, yaml.YAMLError, pydantic.ValidationError) as error:
        _fail(f'bad config {path}: {error}')
    return config


async def _watch(config: _config.Config, token: str) -> None:
    async with slack.SlackClient(token) as client:
        await _run.run_once(config, slack_client=client, out=sys.stdout)


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(
        format='botwatch: %(levelname)s: %(message)s',
        level=logging.WARNING,
    )
    args = _parse_args(argv)
    config = _load_config(args.config)

    token = os.environ.get(_TOKEN_VARIABLE, '')
    if config.sources and not token:
        _fail(f'set {_TOKEN_VARIABLE} to a Slack token')

    asyncio.run(_watch(config, token))
