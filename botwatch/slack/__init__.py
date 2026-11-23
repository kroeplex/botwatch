"""Slack sources: watch a conversation's top level, or one thread.

See :func:`watch`.
"""

from ._api import SlackClient
from ._api import SlackError
from ._exclude import ExcludeRule
from ._ts import SlackTs
from ._watch import DEFAULT_POLL_INTERVAL
from ._watch import ConversationId
from ._watch import MessagePosted
from ._watch import SlackState
from ._watch import watch

__all__ = [
    'ConversationId',
    'DEFAULT_POLL_INTERVAL',
    'ExcludeRule',
    'MessagePosted',
    'SlackClient',
    'SlackError',
    'SlackState',
    'SlackTs',
    'watch',
]
