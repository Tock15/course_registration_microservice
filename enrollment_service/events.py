"""Fire-and-forget domain event publishing (Observer subject)."""
import asyncio
import logging
from abc import ABC, abstractmethod

import httpx

from common.events import BaseEvent, WaitlistJoinedEvent

logger = logging.getLogger(__name__)

__all__ = [
    "BaseEvent",
    "EventPublisher",
    "HttpEventPublisher",
    "WaitlistJoinedEvent",
]


class EventPublisher(ABC):
    @abstractmethod
    def publish(self, event: BaseEvent) -> None:
        """Schedule delivery of an event. Must never raise."""

    async def aclose(self) -> None:
        """Wait for in-flight deliveries (called on shutdown)."""


class HttpEventPublisher(EventPublisher):
    """POSTs events to the Notification Service's /events endpoint."""

    def __init__(self, http: httpx.AsyncClient, base_url: str) -> None:
        self._http = http
        self._url = f"{base_url.rstrip('/')}/events"
        self._in_flight: set[asyncio.Task] = set()

    def publish(self, event: BaseEvent) -> None:
        task = asyncio.create_task(self._send(event))
        self._in_flight.add(task)
        task.add_done_callback(self._in_flight.discard)

    async def _send(self, event: BaseEvent) -> None:
        try:
            res = await self._http.post(self._url, json=event.model_dump(mode="json"))
            if res.status_code >= 300:
                logger.warning(
                    "Notification rejected %s (%s): HTTP %s",
                    event.event_type,
                    event.event_id,
                    res.status_code,
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "Notification delivery failed for %s (%s): %s",
                event.event_type,
                event.event_id,
                exc,
            )

    async def aclose(self) -> None:
        if self._in_flight:
            await asyncio.gather(*self._in_flight, return_exceptions=True)
