"""A one-shot ``select`` over many event sources, for agents.

One invocation reads a config naming the sources to watch and a state file
recording how far it got last time, blocks until something happens, writes any
new events to stdout, writes the advanced state back, and exits.
"""

from ._config import ChannelConfig
from ._config import Config
from ._config import ConfigError
from ._config import Defaults
from ._config import load_config
from ._event import Event
from ._state import ChannelState
from ._state import State

__version__ = '0.1.0'

__all__ = [
    'ChannelConfig',
    'ChannelState',
    'Config',
    'ConfigError',
    'Defaults',
    'Event',
    'State',
    'load_config',
]
