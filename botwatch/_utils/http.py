"""HTTP calls that retry transient failures without limit, for API clients."""

import asyncio
import httpx
import logging
from collections.abc import Callable

INITIAL_BACKOFF = 1.0
"""Seconds before the first retry of a transient failure; doubles per retry."""

MAX_BACKOFF = 60.0

type Outcome = httpx.Response | httpx.TransportError
"""What one attempt at a call produced: a response, or a network error."""


def _describe(outcome: Outcome) -> str:
    return (
        f'HTTP {outcome.status_code}'
        if isinstance(outcome, httpx.Response)
        else repr(outcome)
    )


async def _try_send(
    client: httpx.AsyncClient,
    request: httpx.Request,
) -> Outcome:
    try:
        outcome: Outcome = await client.send(request)
    except httpx.TransportError as error:
        outcome = error
    return outcome


async def send_until_settled(
    client: httpx.AsyncClient,
    request: httpx.Request,
    *,
    is_transient: Callable[[Outcome], bool],
    get_retry_delay: Callable[[Outcome, float], float],
    logger: logging.Logger,
) -> httpx.Response:
    """Sends ``request`` until it doesn't fail transiently, logging each retry.

    Between attempts it waits ``get_retry_delay(outcome, backoff)`` seconds,
    where ``backoff`` starts at :data:`INITIAL_BACKOFF` and doubles up to
    :data:`MAX_BACKOFF`, for the service to override with what it asked for.
    """
    backoff = INITIAL_BACKOFF
    outcome = await _try_send(client, request)
    while is_transient(outcome):
        delay = get_retry_delay(outcome, backoff)
        logger.warning(
            '%s %s: %s; retrying in %.1fs',
            request.url.host,
            request.url.path,
            _describe(outcome),
            delay,
        )
        await asyncio.sleep(delay)
        backoff = min(backoff * 2, MAX_BACKOFF)
        outcome = await _try_send(client, request)
    assert isinstance(outcome, httpx.Response)  # errors are transient
    return outcome
