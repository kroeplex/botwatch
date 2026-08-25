"""Assemble the driver registry.

Kept a function rather than import-time state so importing ``botwatch`` has no
side effects (PBP 5.3): a driver type is added here, and nothing runs until the
CLI asks for the registry.
"""

from __future__ import annotations

from . import _driver
from . import _slack


def build_registry() -> _driver.Registry:
    """Return the map of channel ``type`` to driver factory."""
    return {'slack': _slack.build}
