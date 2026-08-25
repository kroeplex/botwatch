"""Command-line entry point: one run, then exit."""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from . import _config
from . import _registry
from . import _runtime
from . import _state
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Load config and state, do one run to stdout, save state, and exit.

    Exit code 2 for a config that cannot be loaded (a fatal, structural
    problem); 0 otherwise, including a genuinely quiet run that emits nothing.
    """
    parser = argparse.ArgumentParser(
        prog='botwatch',
        description='A one-shot select over many event sources, for agents.',
    )
    parser.add_argument('--config', required=True, help='path to the config file')
    parser.add_argument('--state', required=True, help='path to the state file')
    args = parser.parse_args(argv)

    now = dt.datetime.now(dt.timezone.utc)
    try:
        config = _config.load_config(args.config)
    except _config.ConfigError as exc:
        print(str(exc), file=sys.stderr)
        code = 2
    else:
        state = _state.State.load(args.state, now=now)
        asyncio.run(
            _runtime.run_once(
                config, state, _registry.build_registry(), out=sys.stdout, now=now
            )
        )
        state.save(args.state, now=now)
        code = 0
    return code
