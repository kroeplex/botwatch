"""A one-shot ``select`` over many event sources, for agents.

One invocation reads a config naming the sources to watch and a state file
recording how far it got last time, blocks until something happens, writes any
new events to stdout, writes the advanced state back, and exits.
"""

from ._select import select
from ._source import Event
from ._source import Source
from ._source import Update

__all__ = [
    'Event',
    'Source',
    'Update',
    'select',
]

__version__ = '0.1.0'
